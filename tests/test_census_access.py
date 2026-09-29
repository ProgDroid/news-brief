"""`census` one-time links, login sessions and the labeller role's grants.

Plan Task 5 (docs/superpowers/plans/2026-09-28-event-census.md), spec 6.1 and
6.2. Every token function takes `now`, so each test pins time to `cf.NOW` and
offsets from it (F13). Roles are cluster-global and survive
`DROP SCHEMA public CASCADE`, so the fixture re-applies the grants after each
reset. Every labeller-role connection is closed in `finally` (F21).
"""

from datetime import timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict

import census
import census_fixtures as cf
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)

NOW = cf.NOW
PASSWORD = "labeller-test-password"


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        census.apply_labeller_grants(c, PASSWORD)
        yield c


def _labeller():
    params = conninfo_to_dict(db.conninfo())
    params.update(user=census.LABELLER_ROLE, password=PASSWORD)
    return psycopg.connect(**params)


def _rows(conn) -> list[tuple]:
    rows = conn.execute("SELECT * FROM census_sessions ORDER BY id").fetchall()
    conn.commit()
    return rows


def test_link_is_single_use(kb):
    link = census.mint_link(kb, NOW)
    assert census.open_session(kb, link, NOW) is not None
    assert census.open_session(kb, link, NOW) is None


def test_expired_link_is_refused(kb):
    link = census.mint_link(kb, NOW)
    assert census.open_session(kb, link, NOW + timedelta(minutes=11)) is None


def test_link_is_valid_just_inside_ten_minutes(kb):
    link = census.mint_link(kb, NOW)
    assert census.open_session(kb, link, NOW + timedelta(minutes=9)) is not None


def test_revoked_link_is_refused(kb):
    link = census.mint_link(kb, NOW)
    assert census.revoke_all(kb, NOW) == 1
    assert census.open_session(kb, link, NOW) is None


def test_link_token_is_not_a_session(kb):
    link = census.mint_link(kb, NOW)
    assert census.session_valid(kb, link, NOW) is False
    census.open_session(kb, link, NOW)
    assert census.session_valid(kb, link, NOW) is False


def test_session_token_cannot_open_a_session(kb):
    session = census.open_session(kb, census.mint_link(kb, NOW), NOW)
    assert census.open_session(kb, session, NOW) is None
    assert len(_rows(kb)) == 2


def test_opened_session_is_valid_and_unknown_token_is_not(kb):
    session = census.open_session(kb, census.mint_link(kb, NOW), NOW)
    assert census.session_valid(kb, session, NOW) is True
    assert census.session_valid(kb, "not-a-token", NOW) is False


def test_revoked_sessions_are_invalid(kb):
    session = census.open_session(kb, census.mint_link(kb, NOW), NOW)
    assert census.revoke_all(kb, NOW) == 1
    assert census.session_valid(kb, session, NOW) is False


def test_session_expires_after_thirty_days(kb):
    session = census.open_session(kb, census.mint_link(kb, NOW), NOW)
    assert census.session_valid(kb, session, NOW + timedelta(days=29)) is True
    assert census.session_valid(kb, session, NOW + timedelta(days=31)) is False


def test_no_plaintext_token_is_stored(kb):
    link = census.mint_link(kb, NOW)
    session = census.open_session(kb, link, NOW)
    text = kb.execute("SELECT t::text FROM census_sessions t").fetchall()
    kb.commit()
    assert len(text) == 2
    for (row,) in text:
        assert link not in row
        assert session not in row


def test_token_functions_leave_no_transaction_open(kb):
    idle = psycopg.pq.TransactionStatus.IDLE
    link = census.mint_link(kb, NOW)
    assert kb.info.transaction_status == idle
    session = census.open_session(kb, link, NOW)
    assert kb.info.transaction_status == idle
    census.session_valid(kb, session, NOW)
    assert kb.info.transaction_status == idle
    census.revoke_all(kb, NOW)
    assert kb.info.transaction_status == idle


def test_grants_are_idempotent_and_complete(kb):
    census.apply_labeller_grants(kb, PASSWORD)
    census.apply_labeller_grants(kb, PASSWORD)
    conn = _labeller()
    try:
        assert census.missing_privileges(conn) == []
    finally:
        conn.close()


def test_labeller_can_run_the_session_code(kb):
    """The grant list is what the code needs, not what the spec listed: run
    the real functions as the role (FOR UPDATE on census_windows needs UPDATE)."""
    cf.prepared(kb)
    link = census.mint_link(kb, NOW)
    w1 = cf.window_id(kb, 1)
    conn = _labeller()
    try:
        session = census.open_session(conn, link, NOW)
        assert census.session_valid(conn, session, NOW) is True
        assert census.current_task(conn, NOW).kind == "blind"
        census.window_items(conn, w1)
        assert census.serve_page(conn, NOW).window_id == w1
        census.record_event(conn, w1, "heartbeat", NOW)
        group = census.create_group(conn, w1, NOW)
        [pair] = cf.cross_outlet_groups(kb, w1, 1)
        census.save_assignments(
            conn, w1, "c", 1, [(i, group, False) for i in pair], NOW
        )
        census.finish_blind(conn, w1, NOW)
        # D1: every write goes to the served window, so window 1's one-pair
        # sample is answered (serving window 2) before window 2 is abandoned.
        [(a, b)] = census.precision_pairs(conn, w1)
        census.save_precision(conn, w1, a, b, "same", NOW)
        census.go_status(conn)
        census.abandon(conn, cf.window_id(kb, 2), "test", NOW)
        assert census.missing_privileges(conn) == []
    finally:
        conn.close()


def _revoke_then_missing(kb, statement: str) -> list[str]:
    kb.execute(statement)
    kb.commit()
    conn = _labeller()
    try:
        return census.missing_privileges(conn)
    finally:
        conn.close()


def test_missing_privileges_names_a_revoked_grant(kb):
    missing = _revoke_then_missing(
        kb, "REVOKE INSERT ON public.census_events FROM census_labeller"
    )
    assert "INSERT on census_events" in missing


def test_missing_privileges_names_a_revoked_column_and_sequence(kb):
    missing = _revoke_then_missing(
        kb,
        "REVOKE UPDATE (consumed_at) ON public.census_sessions FROM census_labeller",
    )
    assert "UPDATE(consumed_at) on census_sessions" in missing
    missing = _revoke_then_missing(
        kb, "REVOKE USAGE ON SEQUENCE public.census_groups_id_seq FROM census_labeller"
    )
    assert "USAGE on sequence census_groups_id_seq" in missing


def test_missing_schema_usage_is_named(kb):
    missing = _revoke_then_missing(
        kb, "REVOKE USAGE ON SCHEMA public FROM census_labeller"
    )
    assert "USAGE on schema public" in missing


def test_labeller_role_cannot_read_settings_or_write_items(kb):
    conn = _labeller()
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM settings")
        conn.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE items SET title = 'x'")
        conn.rollback()
    finally:
        conn.close()


def test_grant_list_starts_with_schema_usage():
    first = census.LABELLER_GRANTS[0]
    assert (first.kind, first.obj, first.privilege) == ("schema", "public", "USAGE")


_ACTUAL_ACL = """
SELECT 'schema', n.nspname, NULL, a.privilege_type
  FROM pg_namespace n, aclexplode(n.nspacl) a
 WHERE n.nspname = 'public' AND a.grantee = %(role)s
UNION ALL
SELECT CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'table' END,
       c.relname, NULL, a.privilege_type
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
       aclexplode(c.relacl) a
 WHERE n.nspname = 'public' AND a.grantee = %(role)s
UNION ALL
SELECT 'column', c.relname, t.attname, a.privilege_type
  FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace,
       aclexplode(t.attacl) a
 WHERE n.nspname = 'public' AND a.grantee = %(role)s
"""


def _actual_grants(kb) -> set[tuple]:
    role = kb.execute(
        "SELECT oid FROM pg_roles WHERE rolname = %s", (census.LABELLER_ROLE,)
    ).fetchone()[0]
    rows = kb.execute(_ACTUAL_ACL, {"role": role}).fetchall()
    kb.commit()
    return {tuple(r) for r in rows}


def _expected_grants(kb) -> set[tuple]:
    out = set()
    for g in census.LABELLER_GRANTS:
        if g.kind == "sequence":
            _, seq = census._sequence_of(kb, g.obj)
            out.add(("sequence", seq, None, g.privilege))
        else:
            out.add((g.kind, g.obj, g.column, g.privilege))
    kb.commit()
    return out


def test_labeller_holds_exactly_the_expected_privileges(kb):
    assert _actual_grants(kb) == _expected_grants(kb)


def test_reapplying_the_grants_clears_every_stale_privilege(kb):
    """apply_labeller_grants revokes everything the role holds in schema
    public first, so the result is exactly LABELLER_GRANTS: a stale table,
    column, sequence and schema grant are all gone after one re-apply."""
    stale = (
        "GRANT DELETE ON public.census_events TO census_labeller",
        "GRANT UPDATE ON public.census_windows TO census_labeller",
        "GRANT SELECT ON public.settings TO census_labeller",
        "GRANT UPDATE (revoked_at) ON public.census_sessions TO census_labeller",
        "GRANT INSERT (title) ON public.items TO census_labeller",
        "GRANT UPDATE ON SEQUENCE public.census_groups_id_seq TO census_labeller",
        "GRANT USAGE ON SEQUENCE public.items_id_seq TO census_labeller",
        "GRANT CREATE ON SCHEMA public TO census_labeller",
    )
    for statement in stale:
        kb.execute(statement)
    kb.commit()
    before = _actual_grants(kb) - _expected_grants(kb)
    assert before == {  # control: each stale grant is really there
        ("table", "census_events", None, "DELETE"),
        ("table", "census_windows", None, "UPDATE"),
        ("table", "settings", None, "SELECT"),
        ("column", "census_sessions", "revoked_at", "UPDATE"),
        ("column", "items", "title", "INSERT"),
        ("sequence", "census_groups_id_seq", None, "UPDATE"),
        ("sequence", "items_id_seq", None, "USAGE"),
        ("schema", "public", None, "CREATE"),
    }

    census.apply_labeller_grants(kb, PASSWORD)
    assert _actual_grants(kb) == _expected_grants(kb)


def test_password_is_stored_as_a_scram_verifier(kb):
    stored = kb.execute(
        "SELECT rolpassword FROM pg_authid WHERE rolname = %s",
        (census.LABELLER_ROLE,),
    ).fetchone()[0]
    kb.commit()
    assert stored.startswith("SCRAM-SHA-256$")
    assert PASSWORD not in stored


def test_missing_sequence_is_named_when_the_lookup_would_fail(kb):
    kb.execute("ALTER SEQUENCE public.census_groups_id_seq OWNED BY NONE")
    kb.commit()
    conn = _labeller()
    try:
        missing = census.missing_privileges(conn)
    finally:
        conn.close()
    assert "USAGE on sequence of census_groups (not found)" in missing


def test_labeller_role_can_render_the_readout_read_only(kb):
    """The runbook runs the readout from the pinned image AS the labeller role
    (final review I1): every SELECT `census_report.render` issues must be
    covered by LABELLER_GRANTS, inside READ ONLY, with the same text as the
    owner's connection produces."""
    from scripts import census_report

    cf.prepared(kb)
    cf.pass_gate(kb, NOW)
    expected = census_report.render(kb, NOW)
    kb.commit()
    assert "Go/no-go" in expected  # the control: the readout has content
    conn = _labeller()
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        assert census_report.render(conn, NOW) == expected
    finally:
        conn.rollback()
        conn.close()


# ── Surplus privileges (the labeller refuses to start over-privileged) ──────


def _surplus_as_labeller() -> list[str]:
    conn = _labeller()
    try:
        return census.privilege_surplus(conn)
    finally:
        conn.close()


def test_no_surplus_after_the_grants(kb):
    assert _surplus_as_labeller() == []


def test_privilege_surplus_names_each_stale_grant(kb):
    for statement in (
        "GRANT DELETE ON public.census_events TO census_labeller",
        "GRANT SELECT ON public.settings TO census_labeller",
        "GRANT UPDATE (revoked_at) ON public.census_sessions TO census_labeller",
        "GRANT UPDATE ON SEQUENCE public.census_groups_id_seq TO census_labeller",
        "GRANT CREATE ON SCHEMA public TO census_labeller",
    ):
        kb.execute(statement)
    kb.commit()
    assert _surplus_as_labeller() == [
        "CREATE on schema public",
        "DELETE on census_events",
        "SELECT on settings",
        "UPDATE on sequence census_groups_id_seq",
        "UPDATE(revoked_at) on census_sessions",
    ]
    # ...and the grants script clears exactly what was named.
    census.apply_labeller_grants(kb, PASSWORD)
    assert _surplus_as_labeller() == []


def test_privilege_surplus_sees_every_privilege_type_the_server_has(kb):
    """No hard-coded privilege list: MAINTAIN (PG 17+) is seen when the
    server has it. The control asks the server which types it knows."""
    known = {
        p
        for (p,) in kb.execute(
            "SELECT privilege_type FROM aclexplode(acldefault('r', 10::oid))"
        ).fetchall()
    }
    kb.commit()
    assert "MAINTAIN" in known  # the test database is PostgreSQL 17 or later
    kb.execute("GRANT MAINTAIN ON public.census_events TO census_labeller")
    kb.commit()
    assert _surplus_as_labeller() == ["MAINTAIN on census_events"]


def test_privilege_surplus_names_a_role_attribute(kb):
    """Attributes are cluster-global: restored in `finally`, or every later
    test's labeller would start over-privileged."""
    kb.execute("ALTER ROLE census_labeller CREATEDB")
    kb.commit()
    try:
        assert _surplus_as_labeller() == ["role attribute CREATEDB"]
    finally:
        kb.execute("ALTER ROLE census_labeller NOCREATEDB")
        kb.commit()
    assert _surplus_as_labeller() == []


def test_privilege_surplus_names_replication(kb):
    """REPLICATION allows replication connections, which read all data."""
    kb.execute("ALTER ROLE census_labeller REPLICATION")
    kb.commit()
    try:
        assert _surplus_as_labeller() == ["role attribute REPLICATION"]
    finally:
        kb.execute("ALTER ROLE census_labeller NOREPLICATION")
        kb.commit()
    assert _surplus_as_labeller() == []


def test_privilege_surplus_names_a_role_membership(kb):
    """Review I1: a membership (here pg_read_all_data) grants what no ACL on
    public shows. Only the labeller's OWN memberships count: a grant in the
    other direction (the labeller role granted to another role, as PG 16+
    does automatically for a CREATEROLE creator) is not flagged. Memberships
    are cluster-global, so both are revoked in `finally`."""
    main = kb.execute("SELECT current_user").fetchone()[0]
    kb.execute("GRANT pg_read_all_data TO census_labeller")
    kb.execute(f'GRANT census_labeller TO "{main}"')
    kb.commit()
    try:
        assert _surplus_as_labeller() == ["member of role pg_read_all_data"]
    finally:
        kb.execute("REVOKE pg_read_all_data FROM census_labeller")
        kb.execute(f'REVOKE census_labeller FROM "{main}"')
        kb.commit()
    assert _surplus_as_labeller() == []


def test_census_tables_constant_matches_the_catalog(kb):
    """`_CENSUS_TABLES` is hand-written on purpose: deriving it from the
    catalog would silently widen the labeller's SELECT to every future
    `census_*` table. So a new census table fails here until someone
    decides its grant."""
    rows = kb.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND c.relname LIKE %s",
        ("census\\_%",),
    ).fetchall()
    kb.commit()
    in_catalog = {name for (name,) in rows}
    assert "census_windows" in in_catalog  # control: the pattern matches
    assert len(set(census._CENSUS_TABLES)) == len(census._CENSUS_TABLES)
    assert set(census._CENSUS_TABLES) == in_catalog


# ── Links and the /label reset race ─────────────────────────────────────────


def test_revoke_all_leaves_an_expired_link_alone(kb):
    """An expired, unconsumed link is neither counted nor updated."""
    expired = census.mint_link(kb, NOW - census.LINK_TTL - timedelta(minutes=1))
    live = census.mint_link(kb, NOW)
    assert census.revoke_all(kb, NOW) == 1
    revoked = dict(
        kb.execute("SELECT token_sha256, revoked_at FROM census_sessions").fetchall()
    )
    kb.commit()
    assert revoked == {census._sha256(expired): None, census._sha256(live): NOW}


def test_open_session_and_revoke_all_serialise_on_one_lock(kb):
    """/label reset must not interleave with a link being opened: under READ
    COMMITTED, revoke_all's UPDATE would miss a session row committed after
    its snapshot. Both take one transaction-scoped advisory lock; while
    another connection holds it, each waits."""
    link = census.mint_link(kb, NOW)
    kb.execute("SET lock_timeout = '500ms'")
    kb.commit()
    try:
        with db.connect() as other:
            other.execute(
                "SELECT pg_advisory_xact_lock(%s)", (census._SESSIONS_LOCK_KEY,)
            )
            try:
                with pytest.raises(errors.LockNotAvailable):
                    census.open_session(kb, link, NOW)
                assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
                with pytest.raises(errors.LockNotAvailable):
                    census.revoke_all(kb, NOW)
            finally:
                other.rollback()
    finally:
        kb.execute("RESET lock_timeout")
        kb.commit()
    # Released: nothing was consumed or revoked while it waited.
    assert census.open_session(kb, link, NOW) is not None


def test_open_session_compares_the_digest_with_compare_digest(kb, monkeypatch):
    """Spec 6.2: a constant-time comparison gates the consumption."""
    link = census.mint_link(kb, NOW)
    calls = []
    monkeypatch.setattr(
        census.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or False
    )
    assert census.open_session(kb, link, NOW) is None
    digest = census._sha256(link)
    assert calls == [(digest, digest)]
    # A refused comparison consumed nothing: the link still opens.
    monkeypatch.undo()
    assert census.open_session(kb, link, NOW) is not None


# ── scripts/census_grants.py ────────────────────────────────────────────────


def test_census_grants_reports_a_missing_password_on_stderr(monkeypatch, capsys):
    from scripts import census_grants

    monkeypatch.delenv(census_grants.PASSWORD_VAR, raising=False)
    assert census_grants.main() == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert census_grants.PASSWORD_VAR in err


def test_census_grants_docstring_keeps_the_password_off_the_command_line():
    from scripts import census_grants

    doc = census_grants.__doc__
    assert "CENSUS_LABELLER_PASSWORD=..." not in doc
    assert "grep '^CENSUS_LABELLER_PASSWORD=' .env" in doc
    assert ". ./.env" not in doc

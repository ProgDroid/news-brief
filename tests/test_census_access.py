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
        items = census.window_items(conn, w1)
        assert census.serve_page(conn, NOW).window_id == w1
        group = census.create_group(conn, w1, NOW)
        census.save_assignments(
            conn, w1, "c", 1, [(i["id"], group, False) for i in items[:2]], NOW
        )
        census.finish_blind(conn, w1, NOW)
        census.precision_pairs(conn, w1)
        census.go_status(conn)
        census.abandon(conn, cf.window_id(kb, 3), "test", NOW)
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


def test_exact_grant_comparison_catches_a_surplus_grant(kb):
    for surplus in (
        "GRANT DELETE ON public.census_events TO census_labeller",
        "GRANT UPDATE ON public.census_windows TO census_labeller",
        "GRANT UPDATE (revoked_at) ON public.census_sessions TO census_labeller",
    ):
        kb.execute(surplus)
        kb.commit()
        assert _actual_grants(kb) != _expected_grants(kb), surplus
        census.apply_labeller_grants(kb, PASSWORD)  # grants only add
        kb.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM census_labeller")
        kb.execute(
            "REVOKE ALL (revoked_at) ON public.census_sessions FROM census_labeller"
        )
        kb.commit()
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

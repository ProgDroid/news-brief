"""Constraint tests for the knowledge base schema (migration 0006).

Every CHECK, unique key and trigger in 0006 gets a test that tries to violate
it. A constraint nothing attempts to break is a comment: spec 12.2's argument
is that a field can look correct and carry no information, and the same is
true of a constraint nothing exercises.
"""

import contextlib

import psycopg
import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing.

    Module-local, matching tests/test_db.py and tests/test_config.py. Shared in
    conftest it would be reachable from every test module by parameter name
    alone, and it runs DROP SCHEMA public CASCADE.
    """
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    """A fully migrated database, including 0006."""
    db.run_migrations(conn)
    conn.commit()
    return conn


@contextlib.contextmanager
def rejects(conn, exc):
    """Assert the block raises `exc` AND leave the transaction usable.

    db.connect sets autocommit=False (db.py:110), so a constraint violation
    aborts the whole transaction and every later statement raises
    InFailedSqlTransaction -- a sibling of the error you expected, not a
    subclass, so the test fails on the wrong line with the wrong message.
    conn.transaction() opens a SAVEPOINT when already inside a transaction and
    rolls back to it, so the next assertion in the same test still works.

    Use this for EVERY expected violation, even where a bare pytest.raises
    would happen to work today -- the next author to add a second assertion to
    the test will not know to change it.
    """
    with pytest.raises(exc):
        with conn.transaction():
            yield


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _outlet(conn, name="Reuters"):
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _entity(conn, name="Apple", type_="company"):
    return conn.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id",
        (name, type_),
    ).fetchone()[0]


def test_outlet_kind_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, kind) VALUES ('X', 'blog')")


def test_perspective_null_is_allowed_and_a_bad_value_is_not(kb):
    """NULL means "no vantage claim made", NOT "neutral" -- calling a source
    neutral is a positive editorial claim, as contestable as picking a side."""
    kb.execute("INSERT INTO outlets (name, perspective) VALUES ('A', NULL)")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, perspective) VALUES ('B', 'NEUTRAL')")


def test_entity_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO entities (name, type) VALUES ('X', 'thing')")


def test_the_same_text_from_two_outlets_is_two_items(kb):
    """Syndicated wire copy must not collapse. Spec 3.4.

    A global UNIQUE (content_hash) would attribute a Reuters story carried by
    five outlets to whichever was ingested first, capping corroboration at one
    -- the opposite of why outlets was split out of sources.
    """
    for outlet_id in (_outlet(kb, "Reuters"), _outlet(kb, "Guardian")):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u', 't', 'HASH')",
            (outlet_id,),
        )
    assert kb.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_the_same_text_twice_from_one_outlet_is_rejected(kb):
    outlet_id = _outlet(kb)
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'HASH')",
        (outlet_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u2', 't2', 'HASH')",
            (outlet_id,),
        )


def test_two_instrument_mappings_with_no_market_collide(kb):
    """NULLS NOT DISTINCT, spec 3.4. Under default Postgres semantics NULLs
    compare distinct, so both rows would insert and one symbol would map to the
    same entity twice."""
    entity_id = _entity(kb, "Apple")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (entity_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
            "VALUES (%s, 'AAPL', 'equity')",
            (entity_id,),
        )


def test_asset_class_admits_index_not_only_equity_and_crypto(kb):
    """The commodity-signals-are-index-class bug: "no instrument for BRENT" was
    a wrong asset CLASS, not a missing symbol."""
    entity_id = _entity(kb, "Brent", "instrument")
    for cls in ("equity", "index", "crypto", "commodity", "fx"):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, market, asset_class) "
            "VALUES (%s, %s, 'X', %s)",
            (entity_id, f"S{cls}", cls),
        )
    assert kb.execute("SELECT count(*) FROM entity_instruments").fetchone()[0] == 5


def _event(conn, type_="action", commitment="in_force"):
    return conn.execute(
        "INSERT INTO events (summary, type, commitment_state) "
        "VALUES ('s', %s, %s) RETURNING id",
        (type_, commitment),
    ).fetchone()[0]


def _item(conn, outlet_id, content_hash="H"):
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', %s) RETURNING id",
        (outlet_id, content_hash),
    ).fetchone()[0]


def test_type_and_commitment_state_vary_independently(kb):
    """Spec 3.2: three orthogonal fields, because geopolitical reporting mixes
    things that happened, things people said, and things people claim
    happened, and one field cannot carry that."""
    for type_ in ("action", "statement", "disclosure"):
        for commitment in ("in_force", "committed", "intended", "proposed"):
            _event(kb, type_, commitment)
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 12


def test_event_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "rumour")


def test_commitment_state_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "action", "mooted")


def test_an_event_can_supersede_another(kb):
    """Rule 1 marks the contradicted event superseded. Presence of the FK IS
    the state -- no status enum, so a single writer cannot make it degenerate."""
    old, new = _event(kb), _event(kb)
    kb.execute("UPDATE events SET superseded_by = %s WHERE id = %s", (new, old))
    assert (
        kb.execute("SELECT superseded_by FROM events WHERE id = %s", (old,)).fetchone()[
            0
        ]
        == new
    )


def test_standing_rejects_an_unknown_value(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'rumoured')",
            (item_id, event_id),
        )


def test_source_relationship_is_nullable_with_no_default(kb):
    """Spec 2.1: the one extracted enum with no anchor gets NO default -- a
    NOT NULL DEFAULT is the fastest route to the degenerate outcome 12.2 rates
    worse than a missing field. Absent means "not labelled", never
    "independent"."""
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    assert (
        kb.execute("SELECT source_relationship FROM assertions").fetchone()[0] is None
    )


def test_one_item_asserts_one_event_once(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'official')",
            (item_id, event_id),
        )


def _observation(conn, entity_id, metric="price", value=100, window=None):
    return conn.execute(
        "INSERT INTO observations (entity_id, symbol, metric, value, return_window, "
        "observed_at, provider) VALUES (%s, 'S', %s, %s, %s, now(), 'yahoo') RETURNING id",
        (entity_id, metric, value, window),
    ).fetchone()[0]


def test_a_return_without_a_window_is_rejected(kb):
    """Spec 2.2: a return without a period is not a number."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "return", 0.13, None)


def test_a_price_with_a_window_is_rejected(kb):
    """The biconditional runs both ways: a level has no window."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "price", 100, "1d")


def test_a_return_and_a_price_are_two_separate_rows(kb):
    """The replay's "SK Hynix +13%" is a return row; the level it moved from is
    a separate price row. Conflating them is how a one-day bounce became
    confirmation of a multi-quarter thesis."""
    entity_id = _entity(kb, "SK Hynix")
    _observation(kb, entity_id, "return", 0.13, "1d")
    _observation(kb, entity_id, "price", 100)
    assert kb.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_metric_rejects_an_unknown_value(kb):
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "sentiment")


def test_observations_carry_provider_not_extractor_model(kb):
    """Spec 3.5: observations are FETCHED, not extracted. A column named
    extractor_model holding 'yahoo' would be a lie in the one field that exists
    to make silent model drift detectable."""
    cols = {
        r[0]
        for r in kb.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'observations'"
        ).fetchall()
    }
    assert "provider" in cols
    assert "extractor_model" not in cols


def _claim(conn, text="a claim", status="standing", resolved_on=None, **kw):
    cols = ["claim", "status", "first_seen", "resolved_on"]
    vals = [text, status, "2026-09-01", resolved_on]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO claims ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        vals,
    ).fetchone()[0]


def test_status_admits_all_six_states(kb):
    """Spec 2.1: one lifecycle, one column. resolved_outcome does not exist --
    two enums with 'broken' in both permitted status='standing' alongside
    resolved_outcome='broken', and left rule 4's 'stale' homeless."""
    for status in (
        "standing",
        "challenged",
        "broken",
        "confirmed",
        "expired",
        "withdrawn",
    ):
        resolved = None if status == "standing" else "2026-09-02"
        extra = {"broken_by_note": "n"} if status == "broken" else {}
        _claim(kb, f"c {status}", status, resolved, **extra)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 6


def test_status_rejects_stale_which_is_spelled_expired_here(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "stale", "2026-09-02")


def test_a_non_standing_claim_must_record_when_it_left_standing(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "expired", None)


def test_horizon_elapsed_requires_a_resolution(kb):
    """Elapsed days mean nothing without the resolution they are measured to."""
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "standing", None, horizon_elapsed=2)


def test_a_broken_claim_must_say_what_broke_it(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "broken", "2026-09-02")


def test_a_broken_claim_accepts_either_a_note_or_an_event(kb):
    """The incumbent writes free text ('unmarked rewrite: ...'); rule 1 wants
    the contradicting event. A single BIGINT FK could not hold the existing
    values, which is why there are two columns."""
    event_id = _event(kb)
    _claim(kb, "c1", "broken", "2026-09-02", broken_by_note="unmarked rewrite: x")
    _claim(kb, "c2", "broken", "2026-09-02", broken_by_event_id=event_id)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 2


def test_horizon_days_rejects_zero_negatives_and_over_ten_years(kb):
    """Matches the 1..3650 range _coerce_horizon_days already enforces
    (_MAX_HORIZON_DAYS). Without it, resolution_date lands on or before
    first_seen and rule 4 fires the moment the claim is created.

    Three assertions in one test: this is exactly why `rejects` uses a
    savepoint. A bare pytest.raises would leave the transaction aborted and the
    second iteration would raise InFailedSqlTransaction instead.
    """
    for bad in (0, -1, 3651):
        with rejects(kb, psycopg.errors.CheckViolation):
            _claim(kb, f"c{bad}", horizon_days=bad)


def test_horizon_days_null_is_allowed_and_means_unknown(kb):
    """Spec 2.2: absence is not a short horizon. Never defaulted -- a default
    would manufacture calibration data."""
    claim_id = _claim(kb, "c", horizon_days=None)
    assert (
        kb.execute(
            "SELECT horizon_days FROM claims WHERE id = %s", (claim_id,)
        ).fetchone()[0]
        is None
    )


def test_claims_without_a_ledger_ancestor_coexist(kb):
    """ledger_id is UNIQUE but nullable, and NULLs are DISTINCT by default --
    deliberately, so KB-native claims need no synthetic id."""
    _claim(kb, "c1")
    _claim(kb, "c2")
    assert (
        kb.execute("SELECT count(*) FROM claims WHERE ledger_id IS NULL").fetchone()[0]
        == 2
    )


def test_a_ledger_id_cannot_be_reused(kb):
    """merge_ledger treats an echoed id as authoritative, so two rows claiming
    c-0001 would make the model's citation ambiguous."""
    _claim(kb, "c1", ledger_id="c-0001")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _claim(kb, "c2", ledger_id="c-0001")

"""Constraint tests for the event census schema (migration 0017).

Every CHECK, unique key and trigger 0017 adds gets a test that tries to break
it, following tests/test_kb_schema.py's convention. The retention hold is the
load-bearing piece here (spec 2026-09-28-gold-set-labelling-design.md sec 7):
a `BEFORE DELETE` row trigger on `items` plus a `BEFORE TRUNCATE` statement
trigger, both raising while a `census_block` row names items still held.
"""

import contextlib

import psycopg
import pytest

import conftest
import db

TARGET = "0017_census"

CENSUS_TABLES = {
    "census_block",
    "census_window_order",
    "census_skipped_windows",
    "census_windows",
    "census_window_items",
    "census_groups",
    "census_assignments",
    "census_adjudications",
    "census_sessions",
    "census_events",
}

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


@contextlib.contextmanager
def rejects(conn, exc):
    """Assert the block raises `exc` and leave the transaction usable.

    Matches tests/test_kb_schema.py's helper of the same name: db.connect sets
    autocommit=False, so a constraint violation aborts the whole transaction
    unless the block runs inside its own SAVEPOINT via conn.transaction().
    """
    with pytest.raises(exc):
        with conn.transaction():
            yield


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _functions(conn) -> set[str]:
    rows = conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _outlet(conn):
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES ('O' || gen_random_uuid(), 'wire') "
        "RETURNING id"
    ).fetchone()[0]


def _item(conn, created_at):
    outlet_id = _outlet(conn)
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at) "
        "VALUES (%s, 'u', 't', gen_random_uuid()::text, %s) RETURNING id",
        (outlet_id, created_at),
    ).fetchone()[0]


def _event(conn):
    return conn.execute(
        "INSERT INTO events (summary, type, commitment_state) "
        "VALUES ('e', 'statement', 'intended') RETURNING id"
    ).fetchone()[0]


def _assertion(conn, item_id, event_id):
    conn.execute(
        "INSERT INTO assertions (item_id, event_id, standing) VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )


def _triage(conn, item_id):
    conn.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', 'topical', 1)",
        (item_id,),
    )


def _child_counts(conn, item_id):
    assertions = conn.execute(
        "SELECT count(*) FROM assertions WHERE item_id = %s", (item_id,)
    ).fetchone()[0]
    triage = conn.execute(
        "SELECT count(*) FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone()[0]
    return assertions, triage


def _block(conn, block_start, block_end):
    conn.execute(
        "INSERT INTO census_block (block_start, block_end, prepared_at, "
        "c439ade_deployed_at, gap_split_share, gap_pairs, gap_windows, "
        "gap_deciles, gap_band) "
        "VALUES (%s, %s, now(), now(), 0.0, 0, 0, ARRAY[]::double precision[], 'proceed')",
        (block_start, block_end),
    )


def _window(
    conn, order_no=1, window_start="2026-09-01T00:00:00+00:00", pass_=1, status="open"
):
    return conn.execute(
        "INSERT INTO census_windows (order_no, window_start, pass, status) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (order_no, window_start, pass_, status),
    ).fetchone()[0]


def test_all_ten_census_tables_exist(kb):
    assert CENSUS_TABLES <= _tables(kb)
    assert len(CENSUS_TABLES) == 10


def test_second_census_block_row_is_rejected(kb):
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T00:00:00+00:00")

    with rejects(kb, psycopg.errors.UniqueViolation):
        _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-29T00:00:00+00:00")

    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO census_block (id, block_start, block_end, prepared_at, "
            "c439ade_deployed_at, gap_split_share, gap_pairs, gap_windows, "
            "gap_deciles, gap_band) "
            "VALUES (false, now(), now(), now(), now(), 0.0, 0, 0, "
            "ARRAY[]::double precision[], 'proceed')"
        )


def test_held_item_delete_raises_and_children_survive(kb):
    item_id = _item(kb, "2026-09-20T12:00:00+00:00")
    event_id = _event(kb)
    _assertion(kb, item_id, event_id)
    _triage(kb, item_id)
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T12:00:00+00:00")
    kb.commit()

    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with kb.transaction():
            kb.execute("DELETE FROM items WHERE id = %s", (item_id,))

    message = str(excinfo.value)
    assert "held by the event census" in message
    assert "2026-09-28" in message

    assertions, triage = _child_counts(kb, item_id)
    assert assertions == 1
    assert triage == 1


def test_cascade_control_without_a_census(kb):
    """F3: no block row, so the naive delete succeeds and its children --
    linked ON DELETE CASCADE -- go with it. Proves the survival above comes
    from the trigger refusing the delete, not from the children being
    orphan-proof on their own."""
    item_id = _item(kb, "2026-09-20T12:00:00+00:00")
    event_id = _event(kb)
    _assertion(kb, item_id, event_id)
    _triage(kb, item_id)
    kb.commit()

    kb.execute("DELETE FROM items WHERE id = %s", (item_id,))

    assertions, triage = _child_counts(kb, item_id)
    assert assertions == 0
    assert triage == 0


def test_truncate_items_is_refused_while_census_exists(kb):
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T12:00:00+00:00")
    kb.commit()

    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute("TRUNCATE items CASCADE")


def test_post_block_item_delete_succeeds(kb):
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T00:00:00+00:00")
    item_id = _item(kb, "2026-09-29T00:00:00+00:00")
    kb.commit()

    cur = kb.execute("DELETE FROM items WHERE id = %s", (item_id,))

    assert cur.rowcount == 1
    assert (
        kb.execute("SELECT 1 FROM items WHERE id = %s", (item_id,)).fetchone() is None
    )


def test_item_at_block_end_is_not_held(kb):
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T00:00:00+00:00")
    item_id = _item(kb, "2026-09-28T00:00:00+00:00")
    kb.commit()

    cur = kb.execute("DELETE FROM items WHERE id = %s", (item_id,))

    assert cur.rowcount == 1


def test_releasing_the_block_lifts_the_hold_except_pinned_items(kb):
    _block(kb, "2026-01-01T00:00:00+00:00", "2026-09-28T00:00:00+00:00")
    unpinned_id = _item(kb, "2026-09-20T00:00:00+00:00")
    pinned_id = _item(kb, "2026-09-20T00:00:00+00:00")
    window_id = _window(kb)
    kb.execute(
        "INSERT INTO census_window_items (window_id, item_id) VALUES (%s, %s)",
        (window_id, pinned_id),
    )
    kb.commit()

    kb.execute("DELETE FROM census_block")
    kb.commit()

    cur = kb.execute("DELETE FROM items WHERE id = %s", (unpinned_id,))
    assert cur.rowcount == 1

    # RestrictViolation, not ForeignKeyViolation: SQLSTATE 23001 vs 23503, and
    # this codebase's own RESTRICT case (tests/test_kb_schema.py:703) already
    # asserts the same class for the same reason -- see task-1-report.md.
    with rejects(kb, psycopg.errors.RestrictViolation):
        kb.execute("DELETE FROM items WHERE id = %s", (pinned_id,))


def test_down_refuses_with_labels_and_runs_when_empty(kb):
    window_id = _window(kb)
    item_id = _item(kb, "2026-09-20T00:00:00+00:00")
    kb.execute(
        "INSERT INTO census_assignments "
        "(window_id, item_id, unsure, tab_id, client_seq, created_at) "
        "VALUES (%s, %s, false, 't1', 1, '2026-09-20T00:00:00+00:00')",
        (window_id, item_id),
    )
    kb.commit()

    steps = conftest.steps_back_through(kb, TARGET)

    with pytest.raises(psycopg.errors.RaiseException):
        db.run_migrations(kb, direction="down", steps=steps)

    assert "census_block" in _tables(kb), (
        "a refused down migration must not have partially dropped the schema"
    )

    kb.execute("DELETE FROM census_assignments")
    kb.commit()

    reverted = db.run_migrations(kb, direction="down", steps=steps)
    kb.commit()

    assert reverted[-1] == TARGET
    assert not (CENSUS_TABLES & _tables(kb))
    assert not any(name.startswith("census_hold_") for name in _functions(kb)), (
        "DROP TABLE does not drop a function; the down migration must drop it"
    )


def test_adjudication_pair_order_is_checked(kb):
    window_id = _window(kb)

    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO census_adjudications "
            "(window_id, kind, item_a, item_b, decision, created_at) "
            "VALUES (%s, 'precision', 5, 3, 'same', '2026-09-20T00:00:00+00:00')",
            (window_id,),
        )


def test_backlog_excluded_and_null_published_are_integer_counts(kb):
    """Important #1: spec 4.1 stores the NUMBER excluded/kept per window, and
    plan Task 3's WindowStat writes both as int -- a BOOLEAN column would
    reject that insert outright."""
    window_id = kb.execute(
        "INSERT INTO census_windows "
        "(order_no, window_start, pass, status, backlog_excluded, null_published) "
        "VALUES (1, '2026-09-01T00:00:00+00:00', 1, 'open', 3, 5) RETURNING id"
    ).fetchone()[0]

    row = kb.execute(
        "SELECT backlog_excluded, null_published FROM census_windows WHERE id = %s",
        (window_id,),
    ).fetchone()
    assert row == (3, 5)

    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO census_windows "
            "(order_no, window_start, pass, status, backlog_excluded) "
            "VALUES (2, '2026-09-02T00:00:00+00:00', 1, 'open', -1)"
        )


def test_stratum_is_a_checked_smallint(kb):
    """Minor #1: stratum is `start.hour // 6`, an int 0..3 (plan Task 3); a
    TEXT column would accept an out-of-range or non-numeric value and only
    fail later, silently, when something compares it to an int."""
    kb.execute(
        "INSERT INTO census_window_order (order_no, window_start, stratum) "
        "VALUES (1, '2026-09-01T00:00:00+00:00', 3)"
    )

    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO census_window_order (order_no, window_start, stratum) "
            "VALUES (2, '2026-09-01T06:00:00+00:00', 4)"
        )


def test_assignment_created_at_has_no_default_and_is_required(kb):
    """Minor #2, representative table: every plan Task 4 writer injects its
    own clock rather than relying on SQL now(), so a writer that forgets the
    timestamp must fail NOT NULL rather than silently stamp wall-clock time."""
    window_id = _window(kb)
    item_id = _item(kb, "2026-09-20T00:00:00+00:00")
    kb.commit()

    with rejects(kb, psycopg.errors.NotNullViolation):
        kb.execute(
            "INSERT INTO census_assignments (window_id, item_id, unsure, tab_id, client_seq) "
            "VALUES (%s, %s, false, 't1', 1)",
            (window_id, item_id),
        )

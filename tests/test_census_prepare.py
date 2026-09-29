"""`census.prepare` and its building blocks, against a real database.

Uses the `tests/test_comprehend_integration.py:17-25` fixture shape: a fresh
schema per test, migrated all the way up (through 0017).
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

import census
import census_fixtures as cf
import db

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


def _outlet(conn, label: str) -> int:
    return conn.execute(
        "INSERT INTO outlets (name) VALUES (%s) RETURNING id",
        (f"{label} {uuid.uuid4().hex[:8]}",),
    ).fetchone()[0]


def _item(conn, outlet_id, created_at, published_at="unset", title=None) -> int:
    if published_at == "unset":
        published_at = created_at
    return conn.execute(
        "INSERT INTO items "
        "(outlet_id, url, title, published_at, content_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outlet_id,
            f"https://example.test/item/{uuid.uuid4().hex}",
            title or f"Item {uuid.uuid4().hex[:8]}",
            published_at,
            f"hash-{uuid.uuid4().hex}",
            created_at,
        ),
    ).fetchone()[0]


def _census_row_counts(conn) -> dict[str, int]:
    """Row counts of every table `prepare` writes to, by literal name (no
    string-built SQL: each table gets its own static query)."""
    return {
        "census_block": conn.execute("SELECT count(*) FROM census_block").fetchone()[0],
        "census_windows": conn.execute(
            "SELECT count(*) FROM census_windows"
        ).fetchone()[0],
        "census_window_order": conn.execute(
            "SELECT count(*) FROM census_window_order"
        ).fetchone()[0],
        "census_window_items": conn.execute(
            "SELECT count(*) FROM census_window_items"
        ).fetchone()[0],
        "census_skipped_windows": conn.execute(
            "SELECT count(*) FROM census_skipped_windows"
        ).fetchone()[0],
    }


def test_prepare_freezes_16_windows_and_the_repeat(kb):
    cf.prepared(kb)

    rows = kb.execute(
        "SELECT id, order_no, pass, repeat_of FROM census_windows ORDER BY pass, order_no"
    ).fetchall()
    assert len(rows) == 17

    pass1_by_order = {order_no: wid for wid, order_no, pass_, _ in rows if pass_ == 1}
    pass2_rows = [r for r in rows if r[2] == 2]
    assert len(pass2_rows) == 1
    pass2_id, pass2_order_no, _pass, repeat_of = pass2_rows[0]
    assert pass2_order_no == census.REPEAT_ORDER_NO
    assert repeat_of == pass1_by_order[census.REPEAT_ORDER_NO]

    def _items(window_id):
        return {
            r[0]
            for r in kb.execute(
                "SELECT item_id FROM census_window_items WHERE window_id = %s",
                (window_id,),
            ).fetchall()
        }

    order2_items = _items(pass1_by_order[census.REPEAT_ORDER_NO])
    assert order2_items
    assert _items(pass2_id) == order2_items

    block_row = kb.execute("SELECT block_start, block_end FROM census_block").fetchone()
    block = census.Block(start=block_row[0], end=block_row[1])
    expected_windows, _skipped = census.window_stats(kb, block)
    expected_by_start = {w.start: set(w.item_ids) for w in expected_windows}

    for order_no, window_id in pass1_by_order.items():
        w_start = kb.execute(
            "SELECT window_start FROM census_windows WHERE id = %s", (window_id,)
        ).fetchone()[0]
        assert _items(window_id) == expected_by_start[w_start], order_no


def test_prepare_is_a_no_op_the_second_time(kb):
    cf.prepared(kb)
    before = _census_row_counts(kb)

    result = census.prepare(kb, date(2026, 10, 1), cf.DEPLOYED_AT, cf.NOW)

    assert result.startswith("already prepared")
    assert _census_row_counts(kb) == before


def test_forty_item_rule_counts_eligible_items(kb):
    outlet = _outlet(kb, "T")
    w0 = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    w1 = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

    for i in range(39):
        _item(kb, outlet, w0 + timedelta(minutes=i))
    # A 40th item in the same window, but backlog-excluded: eligible count stays 39.
    _item(kb, outlet, w0 + timedelta(minutes=40), published_at=w0 - timedelta(hours=30))

    for i in range(40):
        _item(kb, outlet, w1 + timedelta(minutes=i))

    kb.commit()
    block = census.Block(start=w0, end=w1 + timedelta(hours=6))
    windows, skipped = census.window_stats(kb, block)

    skip_map = dict(skipped)
    assert skip_map[w0] == "fewer than 40 eligible items (39)"
    matched = next(w for w in windows if w.start == w1)
    assert len(matched.item_ids) == 40


def test_window_stats_refuses_a_block_that_is_not_a_multiple_of_the_window(kb):
    w0 = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    block = census.Block(start=w0, end=w0 + timedelta(hours=7))

    with pytest.raises(census.CensusRefusal) as excinfo:
        census.window_stats(kb, block)

    message = str(excinfo.value)
    assert str(census.WINDOW_HOURS) in message
    assert "7" in message


def test_backlog_excluded_and_null_published_kept(kb):
    outlet = _outlet(kb, "T2")
    w0 = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    for i in range(38):
        _item(kb, outlet, w0 + timedelta(minutes=i))
    for i in range(2):
        _item(kb, outlet, w0 + timedelta(minutes=39 + i), published_at=None)
    for i in range(3):
        _item(
            kb,
            outlet,
            w0 + timedelta(minutes=41 + i),
            published_at=w0 - timedelta(hours=48),
        )
    kb.commit()

    block = census.Block(start=w0, end=w0 + timedelta(hours=6))
    windows, skipped = census.window_stats(kb, block)

    assert not skipped
    w = windows[0]
    assert len(w.item_ids) == 40
    assert w.null_published == 2
    assert w.backlog_excluded == 3


def test_quote_pages_and_empty_titles_are_excluded_from_window_stats(kb):
    """I1a: `window_stats` must apply `exclusion_reason`'s quote-page and
    empty-title branches, not just compute them -- this is the exclusion that
    matters most in production (the 2026-09-15 Reuters quote-page flood).
    Deleting `if reason is not None: continue` (census.py) makes both items
    below eligible, which would change `len(w.item_ids)` and put their ids in
    it."""
    outlet = _outlet(kb, "T3")
    w0 = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)

    for i in range(40):
        _item(kb, outlet, w0 + timedelta(minutes=i))
    quote_id = _item(kb, outlet, w0 + timedelta(minutes=41), title="LCO - Reuters")
    blank_id = _item(kb, outlet, w0 + timedelta(minutes=42), title="   ")
    kb.commit()

    block = census.Block(start=w0, end=w0 + timedelta(hours=6))
    windows, skipped = census.window_stats(kb, block)

    assert not skipped
    w = windows[0]
    assert len(w.item_ids) == 40
    assert quote_id not in w.item_ids
    assert blank_id not in w.item_ids


def test_empty_window_is_recorded_as_skipped_with_zero(kb):
    """M1: a 6-hour slot with NO captured items at all must still appear in
    `skipped` (a capture outage), not just one whose items were all
    excluded. `window_stats` must enumerate every slot in the block, not only
    the ones a row landed in."""
    outlet = _outlet(kb, "T4")
    w0 = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    w1 = w0 + timedelta(hours=6)  # left completely empty
    w2 = w0 + timedelta(hours=12)

    for i in range(40):
        _item(kb, outlet, w0 + timedelta(minutes=i))
    for i in range(40):
        _item(kb, outlet, w2 + timedelta(minutes=i))
    kb.commit()

    block = census.Block(start=w0, end=w0 + timedelta(hours=18))
    windows, skipped = census.window_stats(kb, block)

    skip_map = dict(skipped)
    assert skip_map[w1] == "fewer than 40 eligible items (0)"
    assert {w.start for w in windows} == {w0, w2}
    # Every 6h slot in the block is accounted for as eligible or skipped.
    assert len(windows) + len(skipped) == 3


def test_windows_bucket_in_utc(kb):
    kb.execute("SET TIME ZONE 'Asia/Tokyo'")
    cf.prepared(kb)

    rows = kb.execute("SELECT window_start FROM census_window_order").fetchall()
    assert rows
    for (window_start,) in rows:
        assert window_start.astimezone(timezone.utc).hour in {0, 6, 12, 18}


def test_gap_pairs_are_distinct_items(kb):
    """F9, plus I1b and M4.

    The brief's own wording ("an item with two assertions on one event") is
    impossible: `assertions_item_event` is `UNIQUE (item_id, event_id)`
    (migration 0006:127), so one item cannot hold two assertions on the SAME
    event. This test instead gives item_a and item_b assertions on TWO
    events (E1 and E2) they both share, which still exercises the
    `SELECT DISTINCT` dedup the rule protects: a naive join would produce two
    rows (one per shared event) for the one (item_a, item_b) pair.

    `item_c` shares outlet_a with item_a and asserts a THIRD event (E3) only
    item_a also asserts -- never one item_b asserts too, so item_c has no
    event in common with item_b at all, and the only way it can appear in a
    pair is through item_a (I1b): deleting the `i1.outlet_id <> i2.outlet_id`
    filter would then count the (item_a, item_c) pair too, since every OTHER
    seeded pair in this suite is already cross-outlet and cannot catch that
    mutation.
    """
    outlet_a = _outlet(kb, "GA")
    outlet_b = _outlet(kb, "GB")
    t0 = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    item_a = _item(kb, outlet_a, t0)
    item_b = _item(kb, outlet_b, t0 + timedelta(minutes=10))
    item_c = _item(kb, outlet_a, t0 + timedelta(minutes=20))

    events = []
    for label in ("E1", "E2", "E3"):
        events.append(
            kb.execute(
                "INSERT INTO events (summary, type, commitment_state, occurred_at) "
                "VALUES (%s, 'action', 'in_force', %s) RETURNING id",
                (label, t0),
            ).fetchone()[0]
        )
    e1, e2, e3 = events

    for event_id in (e1, e2):
        for item_id in (item_a, item_b):
            kb.execute(
                "INSERT INTO assertions (item_id, event_id, standing, created_at) "
                "VALUES (%s, %s, 'reported', %s)",
                (item_id, event_id, t0),
            )
    # item_c: same outlet as item_a, shares E3 with item_a only -- item_b
    # never asserts E3, so item_c cannot form a legitimate cross-outlet pair
    # through item_b either.
    for item_id in (item_a, item_c):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing, created_at) "
            "VALUES (%s, %s, 'reported', %s)",
            (item_id, e3, t0),
        )
    kb.commit()

    result = census.gap_check(kb)
    assert result.pairs == 1
    # M4: item_a and item_b both fall in the same 00:00-06:00 UTC window.
    assert result.windows == 1


def test_prepare_refuses_when_gap_share_exceeds_half(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    cf.seed_gap_pairs(kb, block.start, n=5, gap_minutes=12 * 60)
    kb.commit()

    with pytest.raises(census.CensusRefusal, match="50%"):
        census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)

    assert all(n == 0 for n in _census_row_counts(kb).values())


def test_prepare_records_within_6h_band(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    days = (block.end - block.start).days
    cf.seed_corpus(kb, block.start, days=days, per_window=45, outlets=3)
    # g = 2.7h -> split_share = min(2.7/6, 1) = 0.45.
    cf.seed_gap_pairs(kb, block.start, n=5, gap_minutes=162)
    kb.commit()

    result = census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)
    assert "prepared" in result

    row = kb.execute("SELECT gap_band, gap_deciles FROM census_block").fetchone()
    assert row[0] == "within_6h_only"
    assert len(row[1]) == 9


def test_prepare_arms_the_hold(kb):
    cf.prepared(kb)
    row = kb.execute(
        "SELECT prepared_at, c439ade_deployed_at FROM census_block"
    ).fetchone()
    assert row is not None
    prepared_at, deployed_at = row
    assert prepared_at == cf.NOW
    assert deployed_at == cf.DEPLOYED_AT

    # M7: the row's existence is not the hold -- prove a pre-block item
    # delete actually raises.
    outlet = _outlet(kb, "Held")
    held_item = _item(kb, outlet, cf.NOW - timedelta(days=20))
    kb.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="held by the event census"):
        kb.execute("DELETE FROM items WHERE id = %s", (held_item,))
    kb.rollback()


def test_prepare_refuses_with_no_merged_pairs(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    days = (block.end - block.start).days
    cf.seed_corpus(kb, block.start, days=days, per_window=45, outlets=3)
    kb.commit()

    with pytest.raises(census.CensusRefusal, match="no merged cross-outlet pairs"):
        census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)

    assert all(n == 0 for n in _census_row_counts(kb).values())


def test_prepare_leaves_no_open_transaction_when_already_prepared(kb):
    cf.prepared(kb)
    census.prepare(kb, date(2026, 10, 1), cf.DEPLOYED_AT, cf.NOW)
    assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_prepare_leaves_no_open_transaction_on_gap_stop_refusal(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    cf.seed_gap_pairs(kb, block.start, n=5, gap_minutes=12 * 60)
    kb.commit()

    with pytest.raises(census.CensusRefusal):
        census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)

    assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_prepare_leaves_no_open_transaction_on_no_merged_pairs_refusal(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    days = (block.end - block.start).days
    cf.seed_corpus(kb, block.start, days=days, per_window=45, outlets=3)
    kb.commit()

    with pytest.raises(census.CensusRefusal):
        census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)

    assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_prepare_leaves_no_open_transaction_on_block_refusal(kb):
    today = date(2026, 9, 30)  # too early: compute_block itself refuses
    cf.seed_gap_pairs(
        kb, datetime(2026, 9, 19, tzinfo=timezone.utc), n=5, gap_minutes=20
    )
    kb.commit()

    with pytest.raises(census.CensusRefusal):
        census.prepare(kb, today, cf.DEPLOYED_AT, cf.NOW)

    assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_prepare_refuses_over_the_rows_of_a_released_census(kb):
    """Runbook step 11 deletes census_block; the old order/window rows remain.
    A second prepare must refuse up front (a CensusRefusal, which the mode
    turns into exit 2), not die on a unique violation, and write nothing."""
    cf.prepared(kb)
    kb.execute("DELETE FROM census_block")
    kb.commit()
    before = _census_row_counts(kb)
    assert before["census_windows"] == 17  # control: the old rows are there

    with pytest.raises(census.CensusRefusal, match="released census"):
        census.prepare(kb, date(2026, 10, 1), cf.DEPLOYED_AT, cf.NOW)

    assert kb.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert _census_row_counts(kb) == before

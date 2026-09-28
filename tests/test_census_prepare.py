"""`census.prepare` and its building blocks, against a real database.

Uses the `tests/test_comprehend_integration.py:17-25` fixture shape: a fresh
schema per test, migrated all the way up (through 0017).
"""

import uuid
from datetime import date, datetime, timedelta, timezone

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

    result = census.prepare(
        kb, date(2026, 10, 1), cf.DEPLOYED_AT, datetime.now(timezone.utc)
    )

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


def test_windows_bucket_in_utc(kb):
    kb.execute("SET TIME ZONE 'Asia/Tokyo'")
    cf.prepared(kb)

    rows = kb.execute("SELECT window_start FROM census_window_order").fetchall()
    assert rows
    for (window_start,) in rows:
        assert window_start.astimezone(timezone.utc).hour in {0, 6, 12, 18}


def test_gap_pairs_are_distinct_items(kb):
    outlet_a = _outlet(kb, "GA")
    outlet_b = _outlet(kb, "GB")
    t0 = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    item_a = _item(kb, outlet_a, t0)
    item_b = _item(kb, outlet_b, t0 + timedelta(minutes=10))

    events = []
    for label in ("E1", "E2"):
        events.append(
            kb.execute(
                "INSERT INTO events (summary, type, commitment_state, occurred_at) "
                "VALUES (%s, 'action', 'in_force', %s) RETURNING id",
                (label, t0),
            ).fetchone()[0]
        )

    for event_id in events:
        for item_id in (item_a, item_b):
            kb.execute(
                "INSERT INTO assertions (item_id, event_id, standing, created_at) "
                "VALUES (%s, %s, 'reported', %s)",
                (item_id, event_id, t0),
            )
    kb.commit()

    result = census.gap_check(kb)
    assert result.pairs == 1


def test_prepare_refuses_when_gap_share_exceeds_half(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    cf.seed_gap_pairs(kb, block.start, n=5, gap_minutes=12 * 60)
    kb.commit()

    with pytest.raises(census.CensusRefusal, match="50%"):
        census.prepare(kb, today, cf.DEPLOYED_AT, datetime.now(timezone.utc))

    assert all(n == 0 for n in _census_row_counts(kb).values())


def test_prepare_records_within_6h_band(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    days = (block.end - block.start).days
    cf.seed_corpus(kb, block.start, days=days, per_window=45, outlets=3)
    # g = 2.7h -> split_share = min(2.7/6, 1) = 0.45.
    cf.seed_gap_pairs(kb, block.start, n=5, gap_minutes=162)
    kb.commit()

    result = census.prepare(kb, today, cf.DEPLOYED_AT, datetime.now(timezone.utc))
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
    assert prepared_at is not None
    assert deployed_at == cf.DEPLOYED_AT


def test_prepare_refuses_with_no_merged_pairs(kb):
    today = date(2026, 10, 1)
    block = census.compute_block(today)
    days = (block.end - block.start).days
    cf.seed_corpus(kb, block.start, days=days, per_window=45, outlets=3)
    kb.commit()

    with pytest.raises(census.CensusRefusal, match="no merged cross-outlet pairs"):
        census.prepare(kb, today, cf.DEPLOYED_AT, datetime.now(timezone.utc))

    assert all(n == 0 for n in _census_row_counts(kb).values())

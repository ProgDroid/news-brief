"""Shared seeding helpers for the event-census test suite.

`label` and `answer_precision` drive Task 4's session-state functions (plan
ruling R1), so a test that needs a later window can reach it through the
same public API the labeller uses rather than by writing status columns.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import census

DEPLOYED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

# Pinned (plan Global Constraint F13, "Tests pin `now`"): every census test
# passes this instead of `datetime.now(timezone.utc)`, so `prepared_at` is an
# assertable, exact value rather than "not None".
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _outlet(conn, label: str) -> int:
    name = f"{label} {uuid.uuid4().hex[:8]}"
    return conn.execute(
        "INSERT INTO outlets (name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()[0]


def seed_corpus(
    conn, start: datetime, days: int, per_window: int, outlets: int = 3
) -> None:
    """Insert `outlets` outlets and, for every 6-hour window across `days`
    days from `start`, `per_window` distinctly-titled, eligible items.

    Every item gets a non-empty, non-quote-page title and a `published_at`
    close to `created_at`, so every seeded item is eligible unless a test
    overrides that afterward.
    """
    outlet_ids = [_outlet(conn, f"Outlet{i}") for i in range(outlets)]
    windows = days * 24 // census.WINDOW_HOURS
    seq = 0
    for w in range(windows):
        window_start = start + timedelta(hours=w * census.WINDOW_HOURS)
        step = max((census.WINDOW_HOURS * 3600) // max(per_window, 1), 1)
        for j in range(per_window):
            seq += 1
            created_at = window_start + timedelta(seconds=j * step)
            outlet_id = outlet_ids[seq % outlets]
            conn.execute(
                "INSERT INTO items "
                "(outlet_id, url, title, published_at, content_hash, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    outlet_id,
                    f"https://example.test/item/{seq}",
                    f"Seeded item {seq}",
                    created_at,
                    f"seed-hash-{seq}-{uuid.uuid4().hex[:8]}",
                    created_at,
                ),
            )


def seed_gap_pairs(
    conn, block_start: datetime, n: int = 5, gap_minutes: int = 20
) -> None:
    """A handful of short-gap (< 1h) cross-outlet assertion pairs, so
    `census.gap_check` has evidence to evaluate (ruling R3).
    """
    outlet_a = _outlet(conn, "GapA")
    outlet_b = _outlet(conn, "GapB")
    for i in range(n):
        t0 = block_start + timedelta(days=1, hours=i)
        t1 = t0 + timedelta(minutes=gap_minutes)
        item_a = conn.execute(
            "INSERT INTO items "
            "(outlet_id, url, title, published_at, content_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                outlet_a,
                f"https://example.test/gap-a/{i}",
                f"Gap item A{i}",
                t0,
                f"gap-a-{i}-{uuid.uuid4().hex[:8]}",
                t0,
            ),
        ).fetchone()[0]
        item_b = conn.execute(
            "INSERT INTO items "
            "(outlet_id, url, title, published_at, content_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                outlet_b,
                f"https://example.test/gap-b/{i}",
                f"Gap item B{i}",
                t1,
                f"gap-b-{i}-{uuid.uuid4().hex[:8]}",
                t1,
            ),
        ).fetchone()[0]
        event_id = conn.execute(
            "INSERT INTO events (summary, type, commitment_state, occurred_at) "
            "VALUES (%s, 'action', 'in_force', %s) RETURNING id",
            (f"Gap event {i}", t0),
        ).fetchone()[0]
        for item_id in (item_a, item_b):
            conn.execute(
                "INSERT INTO assertions (item_id, event_id, standing, created_at) "
                "VALUES (%s, %s, 'reported', %s)",
                (item_id, event_id, t0),
            )


def prepared(conn, today: date = date(2026, 10, 1), now: datetime = NOW) -> None:
    """Seed a corpus large enough to fill every stratum, plus short-gap
    merged pairs, and run `census.prepare` -- leaving the census in the
    state most Task 4+ tests want to start from.
    """
    block = census.compute_block(today)
    days = (block.end - block.start).days
    seed_corpus(conn, block.start, days=days, per_window=45, outlets=3)
    seed_gap_pairs(conn, block.start)
    conn.commit()
    census.prepare(conn, today, DEPLOYED_AT, now)


def window_id(conn, order_no: int, pass_: int = 1) -> int:
    """The `census_windows.id` of `order_no` in pass `pass_`."""
    row = conn.execute(
        "SELECT id FROM census_windows WHERE order_no = %s AND pass = %s",
        (order_no, pass_),
    ).fetchone()
    conn.commit()
    return row[0]


def cross_outlet_groups(conn, window_id: int, n: int) -> list[list[int]]:
    """`n` disjoint two-item groups whose items come from different outlets,
    taken greedily in capture order from the window's own items.

    Raises if the window cannot supply `n` of them, so a test that asked for
    eight multi-outlet groups (rule F14) never silently gets fewer.
    """
    items = census.window_items(conn, window_id)
    groups: list[list[int]] = []
    pending: dict | None = None
    for item in items:
        if len(groups) == n:
            break
        if pending is None:
            pending = item
        elif item["outlet"] != pending["outlet"]:
            groups.append([pending["id"], item["id"]])
            pending = None
    if len(groups) < n:
        raise AssertionError(
            f"window {window_id} supplies only {len(groups)} cross-outlet "
            f"groups; the test asked for {n}"
        )
    return groups


def label(conn, window_id: int, groups: list[list[int]], now: datetime) -> None:
    """Assign every listed group (one new `census_groups` row each) and
    finish the blind pass at `now`. Items not listed stay untouched, which
    is a singleton. One write per group, with ascending `client_seq`, so
    arrival order and sequence order agree here.
    """
    for seq, members in enumerate(groups, start=1):
        group_id = census.create_group(conn, window_id, now)
        census.save_assignments(
            conn,
            window_id,
            "fixture",
            seq,
            [(item_id, group_id, False) for item_id in members],
            now,
        )
    census.finish_blind(conn, window_id, now)


def answer_precision(conn, window_id: int, now: datetime) -> None:
    """Answer every precision pair currently offered for the window `same`."""
    for item_a, item_b in census.precision_pairs(conn, window_id):
        census.save_precision(conn, window_id, item_a, item_b, "same", now)


def pass_gate(conn, now: datetime, groups: int = 8) -> None:
    """Rule F14: label windows 1 and 2 (pass 1) with `groups` multi-outlet
    groups each, and answer window 1's precision. Window 2's precision is
    held for the repeat, so it is not answerable here.
    """
    w1 = window_id(conn, 1)
    label(conn, w1, cross_outlet_groups(conn, w1, groups), now)
    answer_precision(conn, w1, now)
    w2 = window_id(conn, 2)
    label(conn, w2, cross_outlet_groups(conn, w2, groups), now)


def finish_empty(conn, order_nos, now: datetime) -> None:
    """Finish each pass-1 window in `order_nos` with nothing grouped: every
    item a singleton, so no precision sample and the window completes."""
    for order_no in order_nos:
        label(conn, window_id(conn, order_no), [], now)

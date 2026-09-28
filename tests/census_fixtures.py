"""Shared seeding helpers for the event-census test suite.

`label` and `answer_precision` are deliberately NOT here: they need Task 4's
session-state functions, which do not exist yet (plan ruling R1).
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import census

DEPLOYED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


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


def prepared(conn, today: date = date(2026, 10, 1)) -> None:
    """Seed a corpus large enough to fill every stratum, plus short-gap
    merged pairs, and run `census.prepare` -- leaving the census in the
    state most Task 4+ tests want to start from.
    """
    block = census.compute_block(today)
    days = (block.end - block.start).days
    seed_corpus(conn, block.start, days=days, per_window=45, outlets=3)
    seed_gap_pairs(conn, block.start)
    conn.commit()
    census.prepare(conn, today, DEPLOYED_AT, datetime.now(timezone.utc))

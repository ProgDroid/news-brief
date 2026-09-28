"""Event census: the block, the window order and `census_prepare`.

Spec: docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md sec 4
(windows), 4.3 (the gap check). Plan:
docs/superpowers/plans/2026-09-28-event-census.md, Task 3.

Imports only `db`, `common`, `census_metrics` and the standard library, so
both `census_prepare` (a `brief.py` mode) and the later `labeller.py`
(a separate, privilege-restricted process) can use it without pulling in
`brief`, `comprehend`, `config` or anything that imports `anthropic`.
"""

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import census_metrics
import common

# ── Pinned values (plan Global Constraints) ─────────────────────────────────
# CAPTURE_DAY_ONE, LOOKBACK_DAYS: capture began 2026-09-04, and the look-back
# equals comprehend.CANDIDATE_WINDOW_DAYS (14) -- duplicated here, not
# imported, because census.py must not import comprehend. Kept equal by
# tests/test_census_rules.py:test_lookback_matches_comprehend.
CAPTURE_DAY_ONE = date(2026, 9, 4)
LOOKBACK_DAYS = 14

BLOCK_DAYS = 14
BLOCK_LAG_DAYS = 3
MIN_BLOCK_DAYS = 10

WINDOW_HOURS = 6
MIN_WINDOW_ITEMS = 40
BACKLOG_HOURS = 24

WINDOWS_PER_STRATUM = 4
SEED = 20260928
REPEAT_ORDER_NO = 2


class CensusRefusal(Exception):
    """A pre-registered rule refused to prepare or continue the census.

    Always printed to the operator and never silently swallowed --
    `mode_census_prepare` (brief.py) turns this into `sys.exit(2)`.
    """


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class WindowStat:
    start: datetime
    stratum: int
    item_ids: tuple[int, ...]
    backlog_excluded: int
    null_published: int


@dataclass(frozen=True)
class GapCheck:
    split_share: float
    pairs: int
    windows: int
    deciles: tuple[float, ...]
    band: str


def _window_start(ts: datetime) -> datetime:
    """Floor `ts` to its 6-hour UTC bucket start (F8: always via UTC)."""
    ts_utc = ts.astimezone(timezone.utc)
    hour = (ts_utc.hour // WINDOW_HOURS) * WINDOW_HOURS
    return ts_utc.replace(hour=hour, minute=0, second=0, microsecond=0)


def compute_block(prepared_on: date) -> Block:
    """Spec sec 4.2 item 1: the 14 days ending 3 days before preparation,
    truncated so no window starts before CAPTURE_DAY_ONE + LOOKBACK_DAYS.

    Refuses below MIN_BLOCK_DAYS, naming the earliest date at which the
    truncated block would reach it.
    """
    end = datetime(
        prepared_on.year, prepared_on.month, prepared_on.day, tzinfo=timezone.utc
    ) - timedelta(days=BLOCK_LAG_DAYS)
    floor_date = CAPTURE_DAY_ONE + timedelta(days=LOOKBACK_DAYS)
    floor_dt = datetime(
        floor_date.year, floor_date.month, floor_date.day, tzinfo=timezone.utc
    )
    start = max(end - timedelta(days=BLOCK_DAYS), floor_dt)

    span_days = (end - start).days
    if span_days < MIN_BLOCK_DAYS:
        earliest = floor_date + timedelta(days=MIN_BLOCK_DAYS + BLOCK_LAG_DAYS)
        raise CensusRefusal(
            f"block would span only {span_days} days (minimum {MIN_BLOCK_DAYS}); "
            f"the earliest valid preparation date is {earliest.isoformat()}"
        )
    return Block(start=start, end=end)


def exclusion_reason(
    title: str | None, published_at: datetime | None, created_at: datetime
) -> str | None:
    """Spec sec 4.1's population filter, checked in this order:
    empty title, quote page (through `common.is_quote_page`, the module
    attribute -- never `from common import is_quote_page`, so a monkeypatch
    of the module attribute is honoured), then the 24h backlog rule.
    """
    if title is None or not title.strip():
        return "empty_title"
    if common.is_quote_page(title):
        return "quote_page"
    if published_at is not None and (created_at - published_at) > timedelta(
        hours=BACKLOG_HOURS
    ):
        return "backlog"
    return None


def window_stats(
    conn, block: Block
) -> tuple[list[WindowStat], list[tuple[datetime, str]]]:
    """One SQL pass over `[block.start, block.end)`, bucketed into 6-hour UTC
    windows. A window with fewer than MIN_WINDOW_ITEMS eligible items is
    skipped, with a reason naming the count actually seen (F7) -- including a
    window with NO captured items at all, which gets "(0)" (M1): every 6-hour
    slot in the block is enumerated up front, not only the ones a row landed
    in, so a capture outage is recorded rather than silently vanishing from
    both `windows` and `skipped`.
    """
    rows = conn.execute(
        "SELECT id, title, published_at, created_at FROM items "
        "WHERE created_at >= %s AND created_at < %s ORDER BY created_at",
        (block.start, block.end),
    ).fetchall()

    buckets: dict[datetime, dict] = {}
    for item_id, title, published_at, created_at in rows:
        w_start = _window_start(created_at)
        bucket = buckets.setdefault(
            w_start, {"eligible": [], "backlog_excluded": 0, "null_published": 0}
        )
        reason = exclusion_reason(title, published_at, created_at)
        if reason == "backlog":
            bucket["backlog_excluded"] += 1
            continue
        if reason is not None:
            # empty_title / quote_page: excluded, uncounted (spec sec 4.1
            # stores only backlog_excluded and null_published per window).
            continue
        bucket["eligible"].append(item_id)
        if published_at is None:
            bucket["null_published"] += 1

    total_slots = int(
        (block.end - block.start).total_seconds() // (WINDOW_HOURS * 3600)
    )
    all_starts = [
        block.start + timedelta(hours=i * WINDOW_HOURS) for i in range(total_slots)
    ]

    windows: list[WindowStat] = []
    skipped: list[tuple[datetime, str]] = []
    empty_bucket = {"eligible": [], "backlog_excluded": 0, "null_published": 0}
    for w_start in all_starts:
        bucket = buckets.get(w_start, empty_bucket)
        n = len(bucket["eligible"])
        if n < MIN_WINDOW_ITEMS:
            skipped.append(
                (w_start, f"fewer than {MIN_WINDOW_ITEMS} eligible items ({n})")
            )
            continue
        windows.append(
            WindowStat(
                start=w_start,
                stratum=w_start.hour // WINDOW_HOURS,
                item_ids=tuple(bucket["eligible"]),
                backlog_excluded=bucket["backlog_excluded"],
                null_published=bucket["null_published"],
            )
        )
    return windows, skipped


def draw_order(windows: list[WindowStat], seed: int = SEED) -> list[WindowStat]:
    """Spec sec 4.2 item 2: four windows per stratum, shuffled, interleaved.

    One `random.Random(seed)`. For each stratum 0..3, `rng.sample` draws
    WINDOWS_PER_STRATUM windows from that stratum's pool (sorted by start,
    for determinism), refusing by name if the pool is thin. Round r then
    takes the r-th pick of each stratum, visited in the order of a fresh
    `rng.sample([0, 1, 2, 3], 4)` per round.
    """
    rng = random.Random(seed)
    by_stratum: dict[int, list[WindowStat]] = {0: [], 1: [], 2: [], 3: []}
    for w in windows:
        by_stratum[w.stratum].append(w)

    picks: dict[int, list[WindowStat]] = {}
    for s in range(4):
        pool = sorted(by_stratum[s], key=lambda w: w.start)
        if len(pool) < WINDOWS_PER_STRATUM:
            raise CensusRefusal(
                f"stratum {s} has only {len(pool)} eligible windows "
                f"(needs {WINDOWS_PER_STRATUM})"
            )
        picks[s] = rng.sample(pool, WINDOWS_PER_STRATUM)

    order: list[WindowStat] = []
    for round_ in range(WINDOWS_PER_STRATUM):
        stratum_order = rng.sample([0, 1, 2, 3], 4)
        for s in stratum_order:
            order.append(picks[s][round_])
    return order


def gap_check(conn) -> GapCheck:
    """Spec sec 4.3: the expected split share under 6-hour windows, from
    every cross-outlet item pair production has merged into one event.

    Zero merged pairs is not a passing empty mean -- it is unanswerable, so
    it refuses (ruling R3).
    """
    rows = conn.execute(
        "SELECT DISTINCT a1.item_id, a2.item_id, i1.created_at, i2.created_at "
        "FROM assertions a1 "
        "JOIN assertions a2 "
        "  ON a1.event_id = a2.event_id AND a1.item_id < a2.item_id "
        "JOIN items i1 ON i1.id = a1.item_id "
        "JOIN items i2 ON i2.id = a2.item_id "
        "WHERE i1.outlet_id <> i2.outlet_id"
    ).fetchall()

    if not rows:
        raise CensusRefusal(
            "gap check has no merged cross-outlet pairs; cannot evaluate"
        )

    gaps_hours: list[float] = []
    windows_touched: set[datetime] = set()
    for _item_a, _item_b, created_a, created_b in rows:
        gaps_hours.append(abs((created_a - created_b).total_seconds()) / 3600)
        windows_touched.add(_window_start(created_a))
        windows_touched.add(_window_start(created_b))

    share = census_metrics.split_share(gaps_hours)
    return GapCheck(
        split_share=share,
        pairs=len(rows),
        windows=len(windows_touched),
        deciles=census_metrics.deciles(gaps_hours),
        band=census_metrics.gap_band(share),
    )


def prepare(conn, today: date, c439ade_deployed_at: datetime, now: datetime) -> str:
    """Freeze the block and the 16-window order (plus the pass-2 repeat), in
    one transaction. Idempotent: a second call is a no-op that reports the
    existing block (spec sec 6.1, "it does, in one run").

    Every early return or raise below rolls back first (M5/F21): the
    "already prepared" read and the gap check both leave the session in an
    open transaction, and `prepare` must not depend on its caller (a
    `db.advisory_lock`'s cleanup, or a test's own `conn.commit()`) to close
    it, or a future caller of `prepare` that does neither leaves one idle.
    """
    existing = conn.execute(
        "SELECT block_start, block_end FROM census_block"
    ).fetchone()
    if existing is not None:
        b_start, b_end = existing
        conn.rollback()
        return f"already prepared: block {b_start.date()} to {b_end.date()}"

    try:
        gap = gap_check(conn)
    except CensusRefusal:
        conn.rollback()
        raise

    if gap.band == "stop":
        conn.rollback()
        raise CensusRefusal(
            f"gap split share {gap.split_share:.1%} exceeds 50%; refusing to "
            f"prepare the census -- bring the window length back to the operator"
        )

    try:
        block = compute_block(today)
        windows, skipped = window_stats(conn, block)
        order = draw_order(windows, seed=SEED)
    except CensusRefusal:
        conn.rollback()
        raise

    try:
        window_ids: dict[int, int] = {}
        for order_no, w in enumerate(order, start=1):
            conn.execute(
                "INSERT INTO census_window_order (order_no, window_start, stratum) "
                "VALUES (%s, %s, %s)",
                (order_no, w.start, w.stratum),
            )
            window_id = conn.execute(
                "INSERT INTO census_windows "
                "(order_no, window_start, pass, status, backlog_excluded, "
                " null_published) "
                "VALUES (%s, %s, 1, 'prepared', %s, %s) RETURNING id",
                (order_no, w.start, w.backlog_excluded, w.null_published),
            ).fetchone()[0]
            window_ids[order_no] = window_id
            for item_id in w.item_ids:
                conn.execute(
                    "INSERT INTO census_window_items (window_id, item_id) "
                    "VALUES (%s, %s)",
                    (window_id, item_id),
                )

        for w_start, reason in skipped:
            conn.execute(
                "INSERT INTO census_skipped_windows (window_start, reason) "
                "VALUES (%s, %s)",
                (w_start, reason),
            )

        repeat_window = order[REPEAT_ORDER_NO - 1]
        repeat_of_id = window_ids[REPEAT_ORDER_NO]
        pass2_id = conn.execute(
            "INSERT INTO census_windows "
            "(order_no, window_start, pass, repeat_of, status, backlog_excluded, "
            " null_published) "
            "VALUES (%s, %s, 2, %s, 'prepared', %s, %s) RETURNING id",
            (
                REPEAT_ORDER_NO,
                repeat_window.start,
                repeat_of_id,
                repeat_window.backlog_excluded,
                repeat_window.null_published,
            ),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO census_window_items (window_id, item_id) "
            "SELECT %s, item_id FROM census_window_items WHERE window_id = %s",
            (pass2_id, repeat_of_id),
        )

        conn.execute(
            "INSERT INTO census_block "
            "(block_start, block_end, prepared_at, c439ade_deployed_at, "
            " gap_split_share, gap_pairs, gap_windows, gap_deciles, gap_band) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                block.start,
                block.end,
                now,
                c439ade_deployed_at,
                gap.split_share,
                gap.pairs,
                gap.windows,
                list(gap.deciles),
                gap.band,
            ),
        )
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return (
        f"prepared block {block.start.date()} to {block.end.date()}: "
        f"{len(order)} windows drawn plus the repeat, {len(skipped)} skipped, "
        f"gap band {gap.band} ({gap.pairs} pairs, {gap.windows} windows)"
    )

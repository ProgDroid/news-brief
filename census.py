"""Event census: the block, the window order, `census_prepare`, and the
session state the labeller drives.

Spec: docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md sec 4
(windows), 4.3 (the gap check), 4.5 (go/no-go), 4.6 (the repeat), 6.3 (the
blind pass) and 6.4 (the precision sample). Plan:
docs/superpowers/plans/2026-09-28-event-census.md, Tasks 3 and 4.

Imports only `db`, `common`, `census_metrics` and the standard library, so
both `census_prepare` (a `brief.py` mode) and the later `labeller.py`
(a separate, privilege-restricted process) can use it without pulling in
`brief`, `comprehend`, `config` or anything that imports `anthropic`.
"""

import functools
import hashlib
import hmac
import random
import secrets
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from psycopg import sql

import census_metrics
import common
from census_metrics import (  # noqa: F401 -- re-exported
    GO_MAX_MEDIAN_MINUTES,
    GO_MIN_MEAN_GROUPS,
    IDLE_CAP_MINUTES,
    PRECISION_PAIRS,
)

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

# The one ruling `prepare` accepts over a gap-check stop (spec sec 11,
# 2026-10-02): proceed with the headline narrowed to within-6h confirmation.
# `proceed` is deliberately not a ruling -- it would make a stop a clean pass.
GAP_RULINGS = ("within_6h_only",)

# Session state (plan Task 4). The four thresholds below are DEFINED in
# census_metrics (the functions take them as defaults) and re-exported here so
# `census.X` keeps working; never redefine them in this module.
REPEAT_AFTER_ORDER_NO = 8
REPEAT_MIN_DAYS = 7
TOTAL_SESSIONS = 17

# "Done" for sequencing (B4): the blind pass is over, whether it was saved
# (and possibly completed by its precision sample) or abandoned.
_DONE = ("blind_done", "complete", "abandoned")
_REPEAT_VOID_REASON = "repeat void: window 2 abandoned"


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

    block_seconds = (block.end - block.start).total_seconds()
    if block_seconds % (WINDOW_HOURS * 3600) != 0:
        raise CensusRefusal(
            f"block length {block_seconds / 3600:g}h is not a multiple of "
            f"WINDOW_HOURS ({WINDOW_HOURS}h)"
        )
    total_slots = int(block_seconds // (WINDOW_HOURS * 3600))
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


def prepare(
    conn,
    today: date,
    c439ade_deployed_at: datetime,
    now: datetime,
    gap_ruling: str | None = None,
) -> str:
    """Freeze the block and the 16-window order (plus the pass-2 repeat), in
    one transaction. Idempotent: a second call is a no-op that reports the
    existing block (spec sec 6.1, "it does, in one run").

    `gap_ruling` is the operator's ruling over a gap-check stop (spec sec 11):
    with `within_6h_only` a stop proceeds under that band, storing the
    measured share unchanged. A ruling the gap check did not need is refused,
    not ignored -- the operator is acting on a picture that is no longer true.

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

    # A released census (runbook step 11 deletes census_block only) leaves its
    # order and window rows behind; inserting over them dies on a unique
    # violation deep in the write. Refuse up front, before anything is written.
    leftover = conn.execute(
        "SELECT (SELECT count(*) FROM census_window_order), "
        "(SELECT count(*) FROM census_windows)"
    ).fetchone()
    if any(leftover):
        conn.rollback()
        raise CensusRefusal(
            "a released census's rows remain (census_window_order / "
            "census_windows are not empty while census_block is absent); "
            "prepare will not start a new census over them"
        )

    if gap_ruling is not None and gap_ruling not in GAP_RULINGS:
        conn.rollback()
        raise CensusRefusal(
            f"unknown gap ruling {gap_ruling!r}; the only ruling is "
            f"{', '.join(GAP_RULINGS)}"
        )

    try:
        gap = gap_check(conn)
    except CensusRefusal:
        conn.rollback()
        raise

    ruled = False
    if gap.band == "stop":
        if gap_ruling is None:
            conn.rollback()
            raise CensusRefusal(
                f"gap split share {gap.split_share:.1%} exceeds 50%; refusing to "
                f"prepare the census -- bring the window length back to the operator"
            )
        gap = replace(gap, band=gap_ruling)
        ruled = True
    elif gap_ruling is not None:
        conn.rollback()
        raise CensusRefusal(
            f"gap ruling {gap_ruling!r} given, but the gap check did not stop "
            f"(split share {gap.split_share:.1%}, band {gap.band}); unset it"
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
        f"gap band {gap.band}"
        + (" by operator ruling" if ruled else "")
        + f" ({gap.pairs} pairs, {gap.windows} windows)"
    )


# ── Session state (plan Task 4) ─────────────────────────────────────────────
#
# Every public function below owns its transaction through `_transaction`:
# commit on return, roll back on any exception (F21). The `_`-prefixed
# helpers never commit, so a public function built from several of them is
# still one transaction. The labeller calls these one connection per request.


class WindowClosed(Exception):
    """A write reached a window whose blind pass is over -- `blind_done`,
    `complete` or `abandoned`. The labeller answers 409 and the client stops
    retrying (plan Review Focus 4)."""


class BadWrite(ValueError):
    """A write named something outside its window (F18): an item not in
    `census_window_items`, a group from another window, a precision pair not
    currently offered, a precision decision other than same/different, an
    empty abandon reason, or a window that does not exist. The labeller
    answers 400."""


@dataclass(frozen=True)
class Task:
    """What the labeller should serve now.

    `kind` is one of 'blind', 'precision', 'gate_failed', 'waiting',
    'complete', 'not_prepared'. `window_id` is set only for 'blind' and
    'precision'. `session_no` (ruling R11) counts done windows, pass 1 and
    pass 2 together: the count + 1 for 'blind' (the session about to be
    labelled), the count itself for every other kind -- so window 1's
    precision is session 1, the gate after windows 1-2 is 2, a complete
    census is TOTAL_SESSIONS and an unprepared one is 0.
    """

    kind: str
    window_id: int | None
    session_no: int
    detail: str


@dataclass(frozen=True)
class _Window:
    id: int
    order_no: int
    pass_: int
    status: str
    blind_done_at: datetime | None
    # Read only by the readout (scripts/census_report.py), which reuses this
    # reader rather than keeping a second query of census_windows.
    window_start: datetime
    opened_at: datetime | None
    null_published: int
    abandon_reason: str | None

    @property
    def done(self) -> bool:
        return self.status in _DONE


# What callers may log through `record_event`: the blind pass's own events,
# refused once it is over. `finish` and `abandon` are written only by
# `finish_blind` and `abandon` (review m1): a stray abandon event would cap a
# live window's active minutes (`_window_active_minutes`). `open` is written
# only by `serve_page`, in the same transaction that decides what to serve.
_CALLER_EVENT_KINDS = ("action", "heartbeat")
_DECISIONS = ("same", "different")
_GATE_ORDER_NOS = (1, 2)


def _transaction(fn):
    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        try:
            result = fn(conn, *args, **kwargs)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return result

    return wrapper


def _windows(conn) -> list[_Window]:
    """Every census window, pass 1 before pass 2, each in order_no order.
    `_current_task`'s loops depend on this order; the readout sorts its own
    copy for display."""
    rows = conn.execute(
        "SELECT id, order_no, pass, status, blind_done_at, window_start, "
        "opened_at, null_published, abandon_reason FROM census_windows "
        "ORDER BY pass, order_no"
    ).fetchall()
    return [_Window(*row) for row in rows]


def _lock_window(conn, window_id: int) -> tuple[str, datetime | None, int, int]:
    """Lock the window row before any write to it (F17), so a Finish and a
    late assignment cannot interleave. Returns (status, blind_done_at,
    order_no, pass)."""
    row = conn.execute(
        "SELECT status, blind_done_at, order_no, pass FROM census_windows "
        "WHERE id = %s FOR UPDATE",
        (window_id,),
    ).fetchone()
    if row is None:
        raise BadWrite(f"no census window {window_id}")
    return row


def _raise_if_closed(window_id: int, status: str) -> None:
    if status in _DONE:
        raise WindowClosed(f"window {window_id} is {status}")


def _require_served(conn, window_id: int, kind: str, at: datetime) -> None:
    """D1, the write guard: a write is accepted only for the task the census
    serves at `at` -- `blind` writes (groups, assignments, events, Finish,
    Abandon) for the blind window, `precision` answers for the precision
    window. Anything else is BadWrite (400); label.js's text for a 400 says
    reload.

    Every writer calls this AFTER `_lock_window` and AFTER its closed-window
    check, so a write to a finished window is still WindowClosed (409). It
    calls the undecorated `_current_task`, never the public `current_task`:
    that one commits, which would release the F17 row lock mid-write. There
    is deliberately no way to switch it off.
    """
    task = _current_task(conn, at)
    if (task.kind, task.window_id) != (kind, window_id):
        served = (
            task.kind
            if task.window_id is None
            else (f"{task.kind} on window {task.window_id}")
        )
        raise BadWrite(
            f"window {window_id} is not served for {kind} (the census serves "
            f"{served}); reload"
        )


def _window_item_rows(conn, window_id: int) -> list[tuple]:
    """The window's frozen membership (F6), in capture order: only
    `census_window_items` decides who is in, never a re-run of the
    population filter over `items`."""
    return conn.execute(
        "SELECT i.id, i.title, i.url, o.name, i.created_at, i.outlet_id "
        "FROM census_window_items cwi "
        "JOIN items i ON i.id = cwi.item_id "
        "JOIN outlets o ON o.id = i.outlet_id "
        "WHERE cwi.window_id = %s "
        "ORDER BY i.created_at, i.id",
        (window_id,),
    ).fetchall()


def _latest_assignments(conn, window_id: int) -> dict[int, tuple[int | None, bool]]:
    """THE reader of the assignment log (B5): per item, the row with the
    highest `id`, which is arrival order -- not `client_seq`, which two tabs
    number independently. No other SQL in this module reads
    `census_assignments`."""
    rows = conn.execute(
        "SELECT DISTINCT ON (item_id) item_id, group_id, unsure "
        "FROM census_assignments WHERE window_id = %s "
        "ORDER BY item_id, id DESC",
        (window_id,),
    ).fetchall()
    return {item_id: (group_id, unsure) for item_id, group_id, unsure in rows}


def _window_assignments(conn, window_id: int) -> list[census_metrics.Assignment]:
    """Every member item with its latest state; an item never touched is a
    sure singleton (group None, not unsure)."""
    latest = _latest_assignments(conn, window_id)
    out = []
    for item_id, _title, _url, _outlet, _created_at, outlet_id in _window_item_rows(
        conn, window_id
    ):
        group_id, unsure = latest.get(item_id, (None, False))
        out.append(census_metrics.Assignment(item_id, outlet_id, group_id, unsure))
    return out


def _precision_pairs(conn, window_id: int) -> list[tuple[int, int]]:
    """The pairs offered now: the window's deterministic sample minus the
    pairs already answered. Empty unless the window is a pass-1 window whose
    blind pass is saved, and empty while order_no 2's sample is held for
    the repeat (sec 4.6: until pass 2 is done, blind_done or abandoned).
    Pass 2 has no sample: it measures consistency only, and its items are
    window 2's, whose pairs `UNIQUE (kind, item_a, item_b)` already owns.
    """
    row = conn.execute(
        "SELECT order_no, pass, status FROM census_windows WHERE id = %s",
        (window_id,),
    ).fetchone()
    if row is None:
        return []
    order_no, pass_, status = row
    if pass_ != 1 or status != "blind_done":
        return []
    if order_no == REPEAT_ORDER_NO:
        repeat = conn.execute(
            "SELECT status FROM census_windows WHERE repeat_of = %s", (window_id,)
        ).fetchone()
        if repeat is not None and repeat[0] not in _DONE:
            return []

    assignments = _window_assignments(conn, window_id)
    outlet_of = {a.item_id: a.outlet_id for a in assignments}
    sample = census_metrics.draw_precision_sample(
        census_metrics.multi_outlet_groups(assignments),
        outlet_of,
        random.Random(window_id),
        n=PRECISION_PAIRS,
    )
    answered = {
        (a, b)
        for a, b in conn.execute(
            "SELECT item_a, item_b FROM census_adjudications "
            "WHERE window_id = %s AND kind = 'precision'",
            (window_id,),
        ).fetchall()
    }
    return [pair for pair in sample if pair not in answered]


def _window_active_minutes(conn, window_id: int) -> float:
    """Active minutes over the window's events, heartbeats included, up to
    and including its blind pass's end (F19): `blind_done_at`, or for an
    abandoned window its first abandon event. A stale tab still
    heartbeating on the precision page, or after an abandon, adds nothing.
    """
    rows = conn.execute(
        "SELECT e.at FROM census_events e "
        "JOIN census_windows w ON w.id = e.window_id "
        "WHERE e.window_id = %s AND e.at <= COALESCE("
        "  w.blind_done_at,"
        "  (SELECT min(a.at) FROM census_events a "
        "   WHERE a.window_id = w.id AND a.kind = 'abandon'),"
        "  'infinity'::timestamptz)",
        (window_id,),
    ).fetchall()
    return census_metrics.active_minutes(
        [at for (at,) in rows], idle_cap_minutes=IDLE_CAP_MINUTES
    )


def _go_status(conn, windows: list[_Window]) -> census_metrics.GoNoGo | None:
    gate = sorted(
        (w for w in windows if w.pass_ == 1 and w.order_no in _GATE_ORDER_NOS),
        key=lambda w: w.order_no,
    )
    if len(gate) < len(_GATE_ORDER_NOS) or not all(w.done for w in gate):
        return None
    for w in gate:
        if w.status == "abandoned":
            return census_metrics.GoNoGo(
                ok=False,
                mean_m=float("nan"),
                median_minutes=float("nan"),
                values=(),
                reason=(f"window {w.order_no} abandoned; the gate cannot be evaluated"),
            )
    m_values = [
        len(census_metrics.multi_outlet_groups(_window_assignments(conn, w.id)))
        for w in gate
    ]
    minutes = [_window_active_minutes(conn, w.id) for w in gate]
    return census_metrics.go_no_go(m_values, minutes)


def _item_count(conn, window_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM census_window_items WHERE window_id = %s",
        (window_id,),
    ).fetchone()[0]


def _claimed_since_last_completion(conn, window_id: int) -> bool:
    """D1a (claim on serve): whether `window_id`'s page was served -- it has
    an `open` event -- later than the most recent completion of ANY window.

    A completion is any of: a `blind_done_at`, a `completed_at`, or an
    `abandon` event (abandon records no timestamp column; its event is its
    only time). So an open event left over from before the previous window
    finished claims nothing: only a page served in the current session
    boundary does.
    """
    return conn.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM census_events e"
        "  WHERE e.window_id = %s AND e.kind = 'open' AND e.at > ("
        "    SELECT COALESCE(max(t), '-infinity'::timestamptz) FROM ("
        "      SELECT blind_done_at AS t FROM census_windows"
        "      UNION ALL SELECT completed_at FROM census_windows"
        "      UNION ALL SELECT at FROM census_events WHERE kind = 'abandon'"
        "    ) done))",
        (window_id,),
    ).fetchone()[0]


def _current_task(conn, now: datetime) -> Task:
    """What to serve at `now` (plan Task 4, in priority order):

    1. no block -> not_prepared;
    2. the go/no-go failed and no override is recorded -> gate_failed;
    3. a window in progress (status `open`) -> blind on it (F16);
    4. a saved blind pass with precision pairs offered -> precision
       (order_no 2's are held until the repeat is done);
    5. the repeat, once order_no 8 is done and REPEAT_MIN_DAYS have passed
       since order_no 2's blind pass -> blind on it -- UNLESS the lowest
       pass-1 window not done was served (has an `open` event) since the most
       recent window completion (D1a, claim on serve): a page already loaded
       is never pre-empted, so the repeat takes over at a session boundary;
    6. the lowest pass-1 window not done -> blind;
    7. only the repeat remains, not yet eligible -> waiting, with the date;
    8. complete.

    Never commits: the writers call it as their guard while they hold the
    window's row lock (F17), and the public `current_task` wraps it.
    """
    block = conn.execute("SELECT go_override_reason FROM census_block").fetchone()
    if block is None:
        return Task("not_prepared", None, 0, "the census is not prepared")
    override = block[0]

    windows = _windows(conn)
    done = sum(1 for w in windows if w.done)

    def blind(w: _Window) -> Task:
        return Task("blind", w.id, done + 1, f"{_item_count(conn, w.id)} items")

    gate = _go_status(conn, windows)
    if gate is not None and not gate.ok and override is None:
        return Task("gate_failed", None, done, gate.reason)

    for w in windows:
        if w.status == "open":
            return blind(w)

    for w in windows:
        if w.status == "blind_done":
            pairs = _precision_pairs(conn, w.id)
            if pairs:
                return Task("precision", w.id, done, f"{len(pairs)} pairs")

    by_order = {w.order_no: w for w in windows if w.pass_ == 1}
    repeat = next((w for w in windows if w.pass_ == 2), None)
    anchor = by_order.get(REPEAT_ORDER_NO)
    eligible_at = None
    if (
        repeat is not None
        and not repeat.done
        and anchor is not None
        and anchor.blind_done_at is not None
    ):
        eligible_at = anchor.blind_done_at + timedelta(days=REPEAT_MIN_DAYS)
        after = by_order.get(REPEAT_AFTER_ORDER_NO)
        if after is not None and after.done and now >= eligible_at:
            nxt = next(
                (by_order[o] for o in sorted(by_order) if not by_order[o].done),
                None,
            )
            if nxt is None or not _claimed_since_last_completion(conn, nxt.id):
                return blind(repeat)

    for order_no in sorted(by_order):
        if not by_order[order_no].done:
            return blind(by_order[order_no])

    if eligible_at is not None:
        at_utc = eligible_at.astimezone(timezone.utc)
        return Task(
            "waiting",
            None,
            done,
            f"the repeat of window {REPEAT_ORDER_NO} opens {at_utc:%Y-%m-%d %H:%M} UTC",
        )
    return Task("complete", None, done, "the census is complete")


@_transaction
def current_task(conn, now: datetime) -> Task:
    """What to serve at `now`; see `_current_task`. Stamps nothing: `/label`
    and the morning nudge ask without claiming a window (D1a)."""
    return _current_task(conn, now)


@_transaction
def serve_page(conn, now: datetime) -> Task:
    """A page load: compute what to serve at `now` and, for a blind or
    precision task, stamp its `open` event -- in ONE transaction, so no state
    change can fall between the decision and the stamp. The stamp is what
    claims a pass-1 window against the repeat (D1a), and the only writer of
    `open` events."""
    task = _current_task(conn, now)
    if task.kind in ("blind", "precision"):
        _insert_event(conn, task.window_id, "open", now)
    return task


@_transaction
def window_items(conn, window_id: int) -> list[dict]:
    """The window's items in capture order, from `census_window_items` only:
    `id`, `title`, `url`, `outlet` (the outlet's name) and `created_at` (the
    capture time the page shows)."""
    return [
        {
            "id": item_id,
            "title": title,
            "url": url,
            "outlet": outlet,
            "created_at": created_at,
        }
        for item_id, title, url, outlet, created_at, _outlet_id in _window_item_rows(
            conn, window_id
        )
    ]


def _insert_event(conn, window_id: int, kind: str, at: datetime) -> None:
    """The one writer of `census_events`; callers have already validated."""
    conn.execute(
        "INSERT INTO census_events (window_id, kind, at) VALUES (%s, %s, %s)",
        (window_id, kind, at),
    )


@_transaction
def record_event(conn, window_id: int, kind: str, at: datetime) -> None:
    """Append one caller timing event: `action` or `heartbeat`.

    BadWrite (never a psycopg error, review m2) for any other kind --
    `finish` and `abandon` are written only by `finish_blind` and `abandon`
    (m1), `open` only by `serve_page` -- for a window that does not exist,
    and for an event on a window whose blind pass is over.
    """
    if kind not in _CALLER_EVENT_KINDS:
        raise BadWrite(f"event kind {kind!r} cannot be recorded by a caller")
    status, _blind_done_at, _order_no, _pass = _lock_window(conn, window_id)
    if status in _DONE:
        raise BadWrite(f"window {window_id} is {status}; no {kind} event")
    _require_served(conn, window_id, "blind", at)
    _insert_event(conn, window_id, kind, at)


@_transaction
def create_group(conn, window_id: int, at: datetime) -> int:
    """A new, empty group in `window_id`. Refused once the blind pass is over
    (WindowClosed) and for a window not served blind (BadWrite, D1)."""
    status, _blind_done_at, _order_no, _pass = _lock_window(conn, window_id)
    _raise_if_closed(window_id, status)
    _require_served(conn, window_id, "blind", at)
    return conn.execute(
        "INSERT INTO census_groups (window_id, created_at) VALUES (%s, %s) "
        "RETURNING id",
        (window_id, at),
    ).fetchone()[0]


@_transaction
def save_assignments(
    conn,
    window_id: int,
    tab_id: str,
    client_seq: int,
    rows: list[tuple[int, int | None, bool]],
    at: datetime,
) -> None:
    """Append one client action's rows `(item_id, group_id, unsure)` to the
    log and mark the window `open`.

    A retry of the same (tab, seq) is ignored by `ON CONFLICT ... DO
    NOTHING`, so arrival order stays the only order (B5). The whole write is
    refused -- nothing is logged -- if the window is closed (WindowClosed),
    or not served blind (D1), or if any item is not a member, any group
    belongs to another window, or one item appears twice in the same action
    (BadWrite, F18).
    """
    status, blind_done_at, _order_no, _pass = _lock_window(conn, window_id)
    _raise_if_closed(window_id, status)
    if blind_done_at is not None:
        raise WindowClosed(f"window {window_id} finished its blind pass")
    _require_served(conn, window_id, "blind", at)

    item_ids = [item_id for item_id, _group_id, _unsure in rows]
    if len(set(item_ids)) != len(item_ids):
        raise BadWrite("an item appears more than once in one write")
    if item_ids:
        members = {
            item_id
            for (item_id,) in conn.execute(
                "SELECT item_id FROM census_window_items "
                "WHERE window_id = %s AND item_id = ANY(%s)",
                (window_id, item_ids),
            ).fetchall()
        }
        foreign = sorted(set(item_ids) - members)
        if foreign:
            raise BadWrite(f"items {foreign} are not in window {window_id}")

    group_ids = sorted({g for _item_id, g, _unsure in rows if g is not None})
    if group_ids:
        own = {
            group_id
            for (group_id,) in conn.execute(
                "SELECT id FROM census_groups WHERE window_id = %s AND id = ANY(%s)",
                (window_id, group_ids),
            ).fetchall()
        }
        foreign = sorted(set(group_ids) - own)
        if foreign:
            raise BadWrite(f"groups {foreign} do not belong to window {window_id}")

    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO census_assignments "
                "(window_id, item_id, group_id, unsure, tab_id, client_seq, "
                " created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (window_id, tab_id, client_seq, item_id) DO NOTHING",
                [
                    (window_id, item_id, group_id, bool(unsure), tab_id, client_seq, at)
                    for item_id, group_id, unsure in rows
                ],
            )
    conn.execute(
        "UPDATE census_windows SET status = 'open', "
        "opened_at = COALESCE(opened_at, %s) WHERE id = %s",
        (at, window_id),
    )


@_transaction
def latest_assignments(conn, window_id: int) -> dict[int, tuple[int | None, bool]]:
    """Per touched item, `(group_id, unsure)` from its latest log row (B5)."""
    return _latest_assignments(conn, window_id)


@_transaction
def window_assignments(conn, window_id: int) -> list[census_metrics.Assignment]:
    """Every member item, in capture order, as a `census_metrics.Assignment`;
    built on the same reader as `latest_assignments`."""
    return _window_assignments(conn, window_id)


@_transaction
def finish_blind(conn, window_id: int, at: datetime) -> None:
    """Save the blind pass: log `finish`, set `blind_done` at `at`. A pass-1
    window with no multi-outlet group has no precision sample, so it
    completes here. Pass 2 stays `blind_done`: it has no sample (see
    `_precision_pairs`)."""
    status, _blind_done_at, _order_no, pass_ = _lock_window(conn, window_id)
    _raise_if_closed(window_id, status)
    _require_served(conn, window_id, "blind", at)
    _insert_event(conn, window_id, "finish", at)
    conn.execute(
        "UPDATE census_windows SET status = 'blind_done', blind_done_at = %s "
        "WHERE id = %s",
        (at, window_id),
    )
    if pass_ == 1 and not census_metrics.multi_outlet_groups(
        _window_assignments(conn, window_id)
    ):
        conn.execute(
            "UPDATE census_windows SET status = 'complete', completed_at = %s "
            "WHERE id = %s",
            (at, window_id),
        )


@_transaction
def abandon(conn, window_id: int, reason: str, at: datetime) -> None:
    """Abandon a window whose blind pass is not over, with a typed reason.

    B4: abandoning order_no 2's pass 1 voids the repeat as well, since there
    is nothing left to repeat. Abandoning the repeat itself releases order_no
    2's held precision sample (`_precision_pairs` treats abandoned as done).
    """
    if not reason or not reason.strip():
        raise BadWrite("abandon needs a reason")
    status, _blind_done_at, order_no, pass_ = _lock_window(conn, window_id)
    _raise_if_closed(window_id, status)
    _require_served(conn, window_id, "blind", at)
    _insert_event(conn, window_id, "abandon", at)
    conn.execute(
        "UPDATE census_windows SET status = 'abandoned', abandon_reason = %s "
        "WHERE id = %s",
        (reason.strip(), window_id),
    )
    if pass_ == 1 and order_no == REPEAT_ORDER_NO:
        conn.execute(
            "UPDATE census_windows SET status = 'abandoned', abandon_reason = %s "
            "WHERE repeat_of = %s "
            "AND status NOT IN ('blind_done', 'complete', 'abandoned')",
            (_REPEAT_VOID_REASON, window_id),
        )


@_transaction
def precision_pairs(conn, window_id: int) -> list[tuple[int, int]]:
    """The precision pairs offered now, each `(a, b)` with `a < b` (sec 6.4):
    `draw_precision_sample(..., random.Random(window_id))` over the saved
    blind pass, minus the pairs already answered."""
    return _precision_pairs(conn, window_id)


@_transaction
def save_precision(
    conn, window_id: int, item_a: int, item_b: int, decision: str, at: datetime
) -> None:
    """Record one precision decision for an offered pair, normalised to
    `a < b` (F18). The window completes when no pair remains. BadWrite for
    a window not served for precision (D1). It has no closed-window check of
    its own: a window whose sample is answered offers no pair."""
    if decision not in _DECISIONS:
        raise BadWrite(f"decision must be one of {_DECISIONS}, not {decision!r}")
    _lock_window(conn, window_id)
    _require_served(conn, window_id, "precision", at)
    pair = (min(item_a, item_b), max(item_a, item_b))
    offered = _precision_pairs(conn, window_id)
    if pair not in offered:
        raise BadWrite(f"pair {pair} is not offered in window {window_id}")
    conn.execute(
        "INSERT INTO census_adjudications "
        "(window_id, kind, item_a, item_b, decision, created_at) "
        "VALUES (%s, 'precision', %s, %s, %s, %s)",
        (window_id, pair[0], pair[1], decision, at),
    )
    if len(offered) == 1:
        conn.execute(
            "UPDATE census_windows SET status = 'complete', completed_at = %s "
            "WHERE id = %s",
            (at, window_id),
        )


@_transaction
def window_active_minutes(conn, window_id: int) -> float:
    """Active minutes of the window's blind pass (sec 6.3, F19)."""
    return _window_active_minutes(conn, window_id)


@_transaction
def go_status(conn) -> census_metrics.GoNoGo | None:
    """Sec 4.5's go/no-go over order_no 1 and 2 (pass 1): None until both
    are done; failed, naming the window, if either was abandoned (B4);
    otherwise `census_metrics.go_no_go` over their multi-outlet group counts
    and active minutes."""
    return _go_status(conn, _windows(conn))


# ── One-time links, login sessions and the labeller role (plan Task 5) ──────
#
# `/label` (the Telegram daemon, main role) mints a link; the labeller opens
# it and receives a 30-day session. Only sha256 hashes are stored (spec 6.2).
# Every function takes `now` and compares against it, never SQL now().

LINK_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)
LABELLER_ROLE = "census_labeller"


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# The /label reset race: `open_session` and `revoke_all` serialise on this one
# transaction-scoped advisory lock. Under READ COMMITTED, revoke_all's UPDATE
# works from a snapshot taken when it starts, so a session row committed by a
# concurrent open_session after that would survive the reset. Same key scheme
# as `db._lock_key`, computed here because census.py does not import db.
_SESSIONS_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"census_sessions").digest()[:8], "big", signed=True
)


def _lock_sessions(conn) -> None:
    """Take the sessions lock; released by the transaction's commit or
    rollback (`_transaction`), never held across requests."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SESSIONS_LOCK_KEY,))


@_transaction
def mint_link(conn, now: datetime) -> str:
    """A one-time link token, valid for `LINK_TTL`; returns the plaintext."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO census_sessions (kind, token_sha256, expires_at) "
        "VALUES ('link', %s, %s)",
        (_sha256(token), now + LINK_TTL),
    )
    return token


@_transaction
def open_session(conn, link_token: str, now: datetime) -> str | None:
    """Consume a link atomically (spec 6.2) and return a fresh session token,
    or None when the token is not an unconsumed, unrevoked, unexpired LINK
    (F12: a session token cannot open a session; F13: `now`, not SQL now()).

    Serialised with `revoke_all` on the sessions lock, and the stored hash is
    compared with `hmac.compare_digest` before the link is consumed (spec
    6.2, as `session_valid` does)."""
    _lock_sessions(conn)
    digest = _sha256(link_token)
    row = conn.execute(
        "SELECT id, token_sha256 FROM census_sessions "
        "WHERE token_sha256 = %(hash)s AND kind = 'link' "
        "AND consumed_at IS NULL AND revoked_at IS NULL "
        "AND expires_at > %(now)s FOR UPDATE",
        {"now": now, "hash": digest},
    ).fetchone()
    if row is None or not hmac.compare_digest(row[1], digest):
        return None
    conn.execute(
        "UPDATE census_sessions SET consumed_at = %s WHERE id = %s", (now, row[0])
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO census_sessions (kind, token_sha256, expires_at) "
        "VALUES ('session', %s, %s)",
        (_sha256(token), now + SESSION_TTL),
    )
    return token


@_transaction
def session_valid(conn, session_token: str, now: datetime) -> bool:
    """Whether the token is a live SESSION (not a link, F12)."""
    digest = _sha256(session_token)
    row = conn.execute(
        "SELECT token_sha256 FROM census_sessions "
        "WHERE token_sha256 = %s AND kind = 'session' "
        "AND revoked_at IS NULL AND expires_at > %s",
        (digest, now),
    ).fetchone()
    return row is not None and hmac.compare_digest(row[0], digest)


@_transaction
def revoke_all(conn, now: datetime) -> int:
    """`/label reset`: revoke every live session and every unspent link.
    Returns how many rows were revoked. Runs as the main role. Holds the
    sessions lock, so no link can be opened between its read and its update.
    An expired link or session is left alone and not counted."""
    _lock_sessions(conn)
    return conn.execute(
        "UPDATE census_sessions SET revoked_at = %s "
        "WHERE revoked_at IS NULL AND expires_at > %s "
        "AND NOT (kind = 'link' AND consumed_at IS NOT NULL)",
        (now, now),
    ).rowcount


@dataclass(frozen=True)
class Grant:
    kind: str  # 'schema' | 'table' | 'column' | 'sequence'
    obj: str
    privilege: str
    column: str | None = None


_CENSUS_TABLES = (
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
)
_INSERT_TABLES = (
    "census_groups",
    "census_assignments",
    "census_events",
    "census_adjudications",
    "census_sessions",
)
_WINDOW_UPDATE_COLUMNS = (
    "status",
    "abandon_reason",
    "opened_at",
    "blind_done_at",
    "completed_at",
)

# Derived from what census.py's labeller-side functions execute, not copied
# from spec 6.1: schema USAGE first (B1); UPDATE on census_windows columns
# doubles as the privilege `SELECT ... FOR UPDATE` needs; sessions need only
# consumed_at, since revoke_all runs as the main role.
LABELLER_GRANTS: tuple[Grant, ...] = (
    Grant("schema", "public", "USAGE"),
    *(Grant("table", t, "SELECT") for t in ("items", "outlets", *_CENSUS_TABLES)),
    *(Grant("table", t, "INSERT") for t in _INSERT_TABLES),
    *(Grant("column", "census_windows", "UPDATE", c) for c in _WINDOW_UPDATE_COLUMNS),
    Grant("column", "census_sessions", "UPDATE", "consumed_at"),
    *(Grant("sequence", t, "USAGE") for t in _INSERT_TABLES),
)

_SERIAL_SEQUENCE = (
    "SELECT s.oid, s.relname FROM pg_class s "
    "JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a' "
    "JOIN pg_class t ON t.oid = d.refobjid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE s.relkind = 'S' AND n.nspname = 'public' AND t.relname = %s"
)
_TABLE_OID = (
    "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = %s AND c.relkind = 'r'"
)


def _sequence_of(conn, table: str) -> tuple[int, str]:
    rows = conn.execute(_SERIAL_SEQUENCE, (table,)).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected one serial sequence for {table}: {rows}")
    return rows[0]


def _grant_statement(conn, g: Grant, role: sql.Identifier) -> sql.Composed:
    priv = sql.SQL(g.privilege)
    public = sql.Identifier("public")
    if g.kind == "schema":
        return sql.SQL("GRANT {p} ON SCHEMA {o} TO {r}").format(
            p=priv, o=sql.Identifier(g.obj), r=role
        )
    if g.kind == "table":
        return sql.SQL("GRANT {p} ON {s}.{o} TO {r}").format(
            p=priv, s=public, o=sql.Identifier(g.obj), r=role
        )
    if g.kind == "column":
        return sql.SQL("GRANT {p} ({c}) ON {s}.{o} TO {r}").format(
            p=priv,
            c=sql.Identifier(g.column),
            s=public,
            o=sql.Identifier(g.obj),
            r=role,
        )
    _, seq = _sequence_of(conn, g.obj)
    return sql.SQL("GRANT {p} ON SEQUENCE {s}.{o} TO {r}").format(
        p=priv, s=public, o=sql.Identifier(seq), r=role
    )


@_transaction
def apply_labeller_grants(conn, password: str) -> None:
    """Create the role if absent, set its password, then make its privileges
    in schema public EXACTLY LABELLER_GRANTS: first revoke everything it
    holds on every table (views included) and sequence there -- a table-level
    REVOKE ALL also clears column privileges, verified on PG 18.6 -- and on
    the schema itself (CREATE with it), then grant the list. One
    transaction, so the role is never seen half-granted. Idempotent: roles
    are cluster-global and outlive DROP SCHEMA.

    That revoke scope is the ACL part of what `privilege_surplus` checks,
    so a stale ACL entry it names is one a re-run clears -- provided the
    grant was made by the role running this (or by the owner, when a
    superuser or the owner runs it): REVOKE removes only grants whose
    grantor is the executing role. Role attributes and memberships are not
    touched: the main role may lack ADMIN on a granted role, and attributes
    need a superuser's ALTER ROLE. The labeller refuses to start while
    either is present, naming it; the operator removes it by hand."""
    role = sql.Identifier(LABELLER_ROLE)
    conn.execute(
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS "
            "(SELECT 1 FROM pg_roles WHERE rolname = {name}) THEN "
            "CREATE ROLE {role} LOGIN; END IF; END $$"
        ).format(name=sql.Literal(LABELLER_ROLE), role=role)
    )
    # A SCRAM verifier, computed client-side: the plaintext never appears in
    # SQL text, so a failing statement cannot put it in the server log.
    verifier = conn.pgconn.encrypt_password(
        password.encode(), LABELLER_ROLE.encode(), b"scram-sha-256"
    ).decode()
    conn.execute(
        sql.SQL("ALTER ROLE {role} LOGIN PASSWORD {pw}").format(
            role=role, pw=sql.Literal(verifier)
        )
    )
    public = sql.Identifier("public")
    for revoke in (
        "REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {r}",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {r}",
        "REVOKE ALL ON SCHEMA {s} FROM {r}",
    ):
        conn.execute(sql.SQL(revoke).format(s=public, r=role))
    for g in LABELLER_GRANTS:
        conn.execute(_grant_statement(conn, g, role))


@_transaction
def missing_privileges(conn) -> list[str]:
    """Every LABELLER_GRANTS entry the CURRENT role lacks, as readable
    strings. Objects are resolved through the catalogs by oid, so a missing
    schema USAGE is named alongside the rest instead of raising."""
    missing: list[str] = []
    schema_ok = conn.execute(
        "SELECT has_schema_privilege(current_user, 'public', 'USAGE')"
    ).fetchone()[0]
    if not schema_ok:
        missing.append("USAGE on schema public")
    for g in LABELLER_GRANTS:
        if g.kind == "schema":
            continue
        if g.kind == "sequence":
            try:
                oid, seq = _sequence_of(conn, g.obj)
            except RuntimeError:
                missing.append(f"{g.privilege} on sequence of {g.obj} (not found)")
                continue
            ok = conn.execute(
                "SELECT has_sequence_privilege(current_user, %s, %s)",
                (oid, g.privilege),
            ).fetchone()[0]
            name = f"{g.privilege} on sequence {seq}"
        else:
            row = conn.execute(_TABLE_OID, (g.obj,)).fetchone()
            if row is None:
                missing.append(f"{g.privilege} on {g.obj} (table not found)")
                continue
            if g.kind == "column":
                ok = conn.execute(
                    "SELECT has_column_privilege(current_user, %s, %s, %s)",
                    (row[0], g.column, g.privilege),
                ).fetchone()[0]
                name = f"{g.privilege}({g.column}) on {g.obj}"
            else:
                ok = conn.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (row[0], g.privilege),
                ).fetchone()[0]
                name = f"{g.privilege} on {g.obj}"
        if not ok:
            missing.append(name)
    return missing


# Everything `apply_labeller_grants` revokes and `privilege_surplus` reads:
# the ACL entries whose grantee is the role, on schema public itself and on
# every relation (tables, views, sequences, ...) and column in it.
_ROLE_ACL = """
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
_ROLE_ATTRIBUTES = ("SUPERUSER", "CREATEROLE", "CREATEDB", "BYPASSRLS", "REPLICATION")


def _describe(kind: str, obj: str, column: str | None, privilege: str) -> str:
    if kind == "schema":
        return f"{privilege} on schema {obj}"
    if kind == "sequence":
        return f"{privilege} on sequence {obj}"
    if kind == "column":
        return f"{privilege}({column}) on {obj}"
    return f"{privilege} on {obj}"


@_transaction
def privilege_surplus(conn) -> list[str]:
    """Everything the CURRENT role holds beyond LABELLER_GRANTS, as readable
    strings, sorted; empty when it holds exactly that set.

    Scope:
    - the role's own ACL entries on schema public itself (e.g. CREATE), and
      on every table, view, sequence and column in it -- exactly what
      `apply_labeller_grants` revokes, so a re-run clears these (subject to
      its grantor caveat). Every privilege type the server records is
      compared, read from the ACLs rather than from a list, so PG 17+'s
      MAINTAIN is included;
    - the role attributes SUPERUSER, CREATEROLE, CREATEDB, BYPASSRLS and
      REPLICATION (replication connections read all data);
    - every role the labeller is a MEMBER of (review I1): a membership such
      as pg_read_all_data, or the owner role, grants what no ACL on public
      shows. Filtered on `member`, so the reverse-direction grant PG 16+
      gives a CREATEROLE creator over the role it created is not flagged.
    The grants script clears neither attributes nor memberships; the
    operator's `ALTER ROLE` / `REVOKE <role> FROM census_labeller` does.

    Out of scope: functions (PUBLIC holds EXECUTE by default), privileges
    reached through PUBLIC, and objects outside schema public.
    """
    row = conn.execute(
        "SELECT oid, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, "
        "rolreplication FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    role_oid, *attributes = row
    held = {tuple(r) for r in conn.execute(_ROLE_ACL, {"role": role_oid}).fetchall()}
    expected = set()
    for g in LABELLER_GRANTS:
        if g.kind == "sequence":
            try:
                _oid, seq = _sequence_of(conn, g.obj)
            except RuntimeError:
                continue  # missing_privileges names it
            expected.add(("sequence", seq, None, g.privilege))
        else:
            expected.add((g.kind, g.obj, g.column, g.privilege))
    surplus = sorted(_describe(*entry) for entry in held - expected)
    surplus += [
        f"role attribute {name}"
        for name, has in zip(_ROLE_ATTRIBUTES, attributes, strict=True)
        if has
    ]
    surplus += [
        f"member of role {name}"
        for (name,) in conn.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = %s ORDER BY r.rolname",
            (role_oid,),
        ).fetchall()
    ]
    return surplus

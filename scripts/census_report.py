"""The event census readout (spec 4.3-4.6, 6.4, 8): read-only.

Prints where the census stands: the gap check, the block, every window, the
go/no-go, the achieved detectable difference at the current K, window 2's
consistency, blind precision and the by-half table. It writes nothing:
`main` runs `render` inside `SET TRANSACTION READ ONLY`.

`render` deliberately reads through `census`'s private, non-committing
helpers rather than its public `@_transaction` readers: those COMMIT, and a
commit ends the READ ONLY transaction, so every statement after the first
would silently run read-write.

Run:  py scripts/census_report.py
"""

import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import census  # noqa: E402  (path shim above must run first)
import census_metrics  # noqa: E402
import db  # noqa: E402

RHOS = (0.05, 0.10, 0.30)  # spec 4.4
BOOTSTRAP_REPS = 2000
_BLIND_DONE = ("blind_done", "complete")
_TOTAL_PASS1 = 16


def _read_block(conn) -> tuple | None:
    """The census_block row, or None when the census is not prepared."""
    return conn.execute(
        "SELECT block_start, block_end, prepared_at, c439ade_deployed_at, "
        "gap_split_share, gap_pairs, gap_windows, gap_deciles, gap_band, "
        "go_override_reason FROM census_block"
    ).fetchone()


def _read_windows(conn) -> list[census._Window]:
    """Every window in session order: pass 1 by order_no, with the pass-2
    repeat slotted after order_no REPEAT_AFTER_ORDER_NO.

    Read through `census._windows`, the one reader of census_windows, and
    sorted here: `_windows` keeps its own ORDER BY (pass, order_no), which
    `_current_task`'s loops depend on."""

    def key(w: census._Window):
        # the repeat runs after order_no REPEAT_AFTER_ORDER_NO
        if w.pass_ == 2:
            return (census.REPEAT_AFTER_ORDER_NO + 0.5, w.order_no)
        return (float(w.order_no), w.order_no)

    return sorted(census._windows(conn), key=key)


def _fmt(x: float, digits: int = 2) -> str:
    return "n/a" if x != x else f"{x:.{digits}f}"


def _pct(num: int, den: int) -> str:
    return "n/a" if den == 0 else f"{100 * num / den:.0f}%"


def _gap_section(block: tuple) -> list[str]:
    share, pairs, windows, deciles, band = block[4:9]
    return [
        "== Gap check (spec 4.3) ==",
        f"split share {100 * share:.1f}% over {pairs} pairs in {windows} windows; "
        f"band {band}",
        "deciles of gap (hours): " + ", ".join(f"{d:.2f}" for d in deciles),
        "biased toward passing (spec 4.3)",
    ]


def _block_section(block: tuple) -> list[str]:
    start, end, _prepared, deployed = block[0], block[1], block[2], block[3]
    straddles = start < deployed < end
    return [
        "== Block ==",
        f"start {start:%Y-%m-%d %H:%M} UTC, end {end:%Y-%m-%d %H:%M} UTC",
        f"c439ade deployed {deployed:%Y-%m-%d}; block straddles it: "
        f"{'yes' if straddles else 'no'}",
    ]


def _split_point(block: tuple) -> tuple[datetime, str]:
    """D3: the by-half table splits at the recorded c439ade deploy time when
    it falls inside the block, else at the block midpoint. Returns the point
    and the text naming which one was used."""
    start, end, deployed = block[0], block[1], block[3]
    if start < deployed < end:
        at = deployed.astimezone(timezone.utc)
        return deployed, f"the recorded c439ade deploy time, {at:%Y-%m-%d %H:%M} UTC"
    mid = start + (end - start) / 2
    at = mid.astimezone(timezone.utc)
    return mid, (
        f"the block midpoint, {at:%Y-%m-%d %H:%M} UTC "
        "(the c439ade deploy time falls outside the block)"
    )


def _stats(conn, window_id: int) -> dict:
    assignments = census._window_assignments(conn, window_id)
    sets = census_metrics.groups(assignments)
    return {
        "items": len(assignments),
        "groups": sum(1 for g in sets if len(g) >= 2),
        "singletons": sum(1 for g in sets if len(g) == 1),
        "unsure": sum(1 for a in assignments if a.unsure),
        "multi": len(census_metrics.multi_outlet_groups(assignments)),
    }


def _wall_start(conn, w: census._Window) -> datetime | None:
    """Where the window's wall-clock minutes start: the LAST `open` event at
    or before its first activity (its earliest `action` event or
    assignment), else `opened_at`.

    Not the first open: the page reloads itself after the previous window
    completes, so the window's first open can precede the sitting by hours
    or days. Not the last open overall: a reload mid-sitting stamps one too.
    """
    (at,) = conn.execute(
        "SELECT max(e.at) FROM census_events e "
        "WHERE e.window_id = %(w)s AND e.kind = 'open' AND e.at <= LEAST("
        "  (SELECT min(a.at) FROM census_events a "
        "   WHERE a.window_id = %(w)s AND a.kind = 'action'),"
        "  (SELECT min(s.created_at) FROM census_assignments s "
        "   WHERE s.window_id = %(w)s))",
        {"w": w.id},
    ).fetchone()
    return at if at is not None else w.opened_at


def _window_lines(
    conn, windows: list[census._Window], stats: dict[int, dict]
) -> list[str]:
    lines = [
        "== Windows (planned order) ==",
        "active min: the gaps of at most "
        f"{census_metrics.IDLE_CAP_MINUTES} min between the window's events, up "
        "to the end of its blind pass; wall min: from the last page open at or "
        "before the window's first action or assignment (its opened_at when "
        "there is none) to its blind_done_at",
    ]
    for w in windows:
        s = stats[w.id]
        head = f"#{w.order_no} pass {w.pass_} [{w.status}] items {s['items']}"
        if w.status == "prepared":
            lines.append(
                head + f", NULL-published {_pct(w.null_published, s['items'])}"
            )
            continue
        active = census._window_active_minutes(conn, w.id)
        start = _wall_start(conn, w)
        wall = (
            f"{(w.blind_done_at - start).total_seconds() / 60:.1f}"
            if start is not None and w.blind_done_at is not None
            else "n/a"
        )
        lines.append(
            head
            + f", groups {s['groups']}, singletons {s['singletons']}"
            + f", unsure {_pct(s['unsure'], s['items'])}"
            + f", NULL-published {_pct(w.null_published, s['items'])}"
            + f", multi-outlet groups {s['multi']}"
            + f", active/wall min {active:.1f}/{wall}"
            + (f" ({w.abandon_reason})" if w.abandon_reason else "")
        )
    return lines


def _go_lines(conn, windows: list[census._Window], override: str | None) -> list[str]:
    gate = census._go_status(conn, windows)
    if gate is None:
        return []
    return [
        "== Go/no-go (spec 4.5) ==",
        f"multi-outlet groups: windows 1 and 2 = {list(gate.values)}; "
        f"mean {_fmt(gate.mean_m)}",
        f"median active minutes {_fmt(gate.median_minutes, 1)}",
        f"verdict: {'GO' if gate.ok else 'NO-GO'} ({gate.reason})",
        f"override: {override if override else 'none'}",
    ]


def _mde_lines(windows: list[census._Window], stats: dict[int, dict]) -> list[str]:
    done = [w for w in windows if w.pass_ == 1 and w.status in _BLIND_DONE]
    k = len(done)
    if k < 2:
        return []
    m_values = [stats[w.id]["multi"] for w in done]
    head = "== Achieved detectable difference (spec 4.5) =="
    if statistics.mean(m_values) == 0:
        # Ruling R14: mde divides by the mean; a no-group state is a NO-GO
        # the operator must still see, not a crash.
        return [head, "detectable difference: undefined (no multi-outlet groups)"]
    label = " (final)" if k >= _TOTAL_PASS1 else ""
    m_bar = statistics.mean(m_values)
    cv = statistics.pstdev(m_values) / m_bar
    values = ", ".join(
        f"rho={rho:.2f}: {census_metrics.mde(m_values, rho):.1f} points" for rho in RHOS
    )
    return [
        head,
        f"at current K = {k}{label}, m-bar {m_bar:.2f}, CV {cv:.2f}: {values}",
    ]


def _labels(conn, window_id: int) -> dict[int, int | None]:
    """item -> group for the sure items of a window; None is a singleton."""
    latest = census._latest_assignments(conn, window_id)
    out: dict[int, int | None] = {}
    for row in census._window_item_rows(conn, window_id):
        group_id, unsure = latest.get(row[0], (None, False))
        if not unsure:
            out[row[0]] = group_id
    return out


_CONSISTENCY_NAMES = ("pairwise precision", "pairwise recall", "pairwise F1", "ARI")


def _consistency_stats(x: dict, y: dict) -> tuple[float, ...]:
    """Precision, recall, F1 (one `pairwise_agreement` call) and ARI."""
    return (
        *census_metrics.pairwise_agreement(x, y),
        census_metrics.adjusted_rand_index(x, y),
    )


def _consistency_rows(a: dict, b: dict, reps: int) -> list[tuple[str, float, str, int]]:
    """`(name, point value, interval text, n_dropped)` per statistic, each
    resample scored once for all four."""
    values = _consistency_stats(a, b)
    intervals = census_metrics.item_bootstrap_intervals(
        _consistency_stats, a, b, reps=reps
    )
    rows = []
    for name, value, (lo, hi, dropped) in zip(_CONSISTENCY_NAMES, values, intervals):
        span = (
            f"95% item-bootstrap {_fmt(lo)} to {_fmt(hi)}"
            if lo is not None
            else "interval unavailable"
        )
        rows.append((name, value, span, dropped))
    return rows


def _consistency_lines(conn, windows: list[census._Window]) -> list[str]:
    repeat = next((w for w in windows if w.pass_ == 2), None)
    if repeat is None:
        return []
    if repeat.status == "abandoned":
        return ["== Consistency (spec 4.6) ==", "consistency unavailable: repeat void"]
    if repeat.status not in _BLIND_DONE:
        return []
    first = next(
        (w for w in windows if w.pass_ == 1 and w.order_no == census.REPEAT_ORDER_NO),
        None,
    )
    if first is None:
        return []
    a = _labels(conn, first.id)
    b = _labels(conn, repeat.id)
    common = sorted(set(a) & set(b))
    a = {i: a[i] for i in common}
    b = {i: b[i] for i in common}

    lines = [
        "== Consistency: pass 2 vs pass 1 of window 2 (spec 4.6) ==",
        f"one window: thin; {len(common)} sure items in both passes",
    ]
    lines.extend(
        f"{name} {_fmt(value)} ({span}; {dropped} of {BOOTSTRAP_REPS} resamples undefined)"
        for name, value, span, dropped in _consistency_rows(a, b, BOOTSTRAP_REPS)
    )
    return lines


def _precision_counts(conn, window_id: int) -> tuple[int, int]:
    yes, asked = conn.execute(
        "SELECT count(*) FILTER (WHERE decision = 'same'), count(*) "
        "FROM census_adjudications WHERE window_id = %s AND kind = 'precision'",
        (window_id,),
    ).fetchone()
    return yes, asked


def _precision_lines(conn, windows: list[census._Window]) -> list[str]:
    per_window = [
        c
        for w in windows
        if w.pass_ == 1 and w.status == "complete"
        for c in [_precision_counts(conn, w.id)]
        if c[1] > 0
    ]
    lines = ["== Blind precision (spec 6.4) =="]
    if not per_window:
        lines.append("no complete windows with a precision sample yet")
        return lines
    p, lo, hi = census_metrics.precision_estimate(per_window)
    yes = sum(y for y, _ in per_window)
    asked = sum(n for _, n in per_window)
    interval = (
        f"95% window-clustered {_fmt(lo)} to {_fmt(hi)}"
        if lo is not None
        else "interval needs 2 windows"
    )
    lines.append(
        f"{_fmt(p)} = {yes}/{asked} pairs over {len(per_window)} complete windows; "
        f"{interval}"
    )
    return lines


def _half_lines(
    conn,
    windows: list[census._Window],
    stats: dict[int, dict],
    split: tuple[datetime, str],
) -> list[str]:
    point, named = split
    lines = [
        "== By block half (descriptive only) ==",
        f"split at {named}",
        "a window is classified by its window_start: one that starts before "
        "the split point counts as before, even if its "
        f"{census.WINDOW_HOURS} hours run past it; one that starts at it "
        "counts as after",
    ]
    for name, pick in (
        ("before the split", lambda t: t < point),
        ("after the split", lambda t: t >= point),
    ):
        done = [
            w
            for w in windows
            if w.pass_ == 1 and w.status == "complete" and pick(w.window_start)
        ]
        if not done:
            lines.append(f"{name}: no complete windows")
            continue
        mean_multi = statistics.mean(stats[w.id]["multi"] for w in done)
        mean_items = statistics.mean(stats[w.id]["items"] for w in done)
        counts = [_precision_counts(conn, w.id) for w in done]
        yes = sum(c[0] for c in counts)
        asked = sum(c[1] for c in counts)
        lines.append(
            f"{name}: {len(done)} windows, mean items {mean_items:.1f}, "
            f"mean multi-outlet groups {mean_multi:.2f}, "
            f"precision {yes}/{asked}"
        )
    return lines


def render(conn, now: datetime) -> str:
    """The whole readout as text. Issues only SELECTs and never commits."""
    block = _read_block(conn)
    if block is None:
        return "The census is not prepared."
    windows = _read_windows(conn)
    stats = {w.id: _stats(conn, w.id) for w in windows}

    sections = [
        [f"Event census readout as of {now:%Y-%m-%d %H:%M} UTC"],
        _gap_section(block),
        _block_section(block),
        _window_lines(conn, windows, stats),
        _go_lines(conn, windows, block[9]),
        _mde_lines(windows, stats),
        _consistency_lines(conn, windows),
        _precision_lines(conn, windows),
        _half_lines(conn, windows, stats, _split_point(block)),
    ]
    return "\n\n".join("\n".join(s) for s in sections if s) + "\n"


def main() -> int:
    if not db.is_configured():
        print("No database is configured: export DATABASE_URL.")
        return 2
    with db.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        try:
            if _read_block(conn) is None:
                print("The census is not prepared.")
                return 2
            print(render(conn, datetime.now(timezone.utc)))
        finally:
            conn.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())

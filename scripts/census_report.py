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


def _read_windows(conn) -> list[tuple]:
    """Every window in session order: pass 1 by order_no, with the pass-2
    repeat slotted after order_no REPEAT_AFTER_ORDER_NO."""
    rows = conn.execute(
        "SELECT id, order_no, pass, status, window_start, opened_at, "
        "blind_done_at, null_published, abandon_reason FROM census_windows"
    ).fetchall()

    def key(row):
        # the repeat runs after order_no REPEAT_AFTER_ORDER_NO
        if row[2] == 2:
            return (census.REPEAT_AFTER_ORDER_NO + 0.5, row[1])
        return (float(row[1]), row[1])

    return sorted(rows, key=key)


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


def _block_section(block: tuple) -> tuple[list[str], datetime]:
    start, end, _prepared, deployed = block[0], block[1], block[2], block[3]
    mid = start + (end - start) / 2
    straddles = start < deployed < end
    return (
        [
            "== Block ==",
            f"start {start:%Y-%m-%d %H:%M} UTC, end {end:%Y-%m-%d %H:%M} UTC",
            f"c439ade deployed {deployed:%Y-%m-%d}; block straddles it: "
            f"{'yes' if straddles else 'no'}",
            f"by-half split point (block midpoint): {mid:%Y-%m-%d %H:%M} UTC",
        ],
        mid,
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


def _window_lines(conn, windows: list[tuple], stats: dict[int, dict]) -> list[str]:
    lines = ["== Windows (planned order) =="]
    for (
        wid,
        order_no,
        pass_,
        status,
        _start,
        opened,
        done_at,
        null_pub,
        reason,
    ) in windows:
        s = stats[wid]
        head = f"#{order_no} pass {pass_} [{status}] items {s['items']}"
        if status == "prepared":
            lines.append(head + f", NULL-published {_pct(null_pub, s['items'])}")
            continue
        active = census._window_active_minutes(conn, wid)
        wall = (
            f"{(done_at - opened).total_seconds() / 60:.1f}"
            if opened is not None and done_at is not None
            else "n/a"
        )
        lines.append(
            head
            + f", groups {s['groups']}, singletons {s['singletons']}"
            + f", unsure {_pct(s['unsure'], s['items'])}"
            + f", NULL-published {_pct(null_pub, s['items'])}"
            + f", multi-outlet groups {s['multi']}"
            + f", active/wall min {active:.1f}/{wall}"
            + (f" ({reason})" if reason else "")
        )
    return lines


def _go_lines(conn, override: str | None) -> list[str]:
    gate = census._go_status(conn, census._windows(conn))
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


def _mde_lines(windows: list[tuple], stats: dict[int, dict]) -> list[str]:
    done = [w for w in windows if w[2] == 1 and w[3] in _BLIND_DONE]
    k = len(done)
    if k < 2:
        return []
    m_values = [stats[w[0]]["multi"] for w in done]
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


def _consistency_lines(conn, windows: list[tuple]) -> list[str]:
    repeat = next((w for w in windows if w[2] == 2), None)
    if repeat is None:
        return []
    if repeat[3] == "abandoned":
        return ["== Consistency (spec 4.6) ==", "consistency unavailable: repeat void"]
    if repeat[3] not in _BLIND_DONE:
        return []
    first = next(
        (w for w in windows if w[2] == 1 and w[1] == census.REPEAT_ORDER_NO), None
    )
    if first is None:
        return []
    a = _labels(conn, first[0])
    b = _labels(conn, repeat[0])
    common = sorted(set(a) & set(b))
    a = {i: a[i] for i in common}
    b = {i: b[i] for i in common}

    def component(index: int):
        return lambda x, y: census_metrics.pairwise_agreement(x, y)[index]

    stats = [
        ("pairwise precision", component(0)),
        ("pairwise recall", component(1)),
        ("pairwise F1", component(2)),
        ("ARI", census_metrics.adjusted_rand_index),
    ]
    lines = [
        "== Consistency: pass 2 vs pass 1 of window 2 (spec 4.6) ==",
        f"one window: thin; {len(common)} sure items in both passes",
    ]
    for name, fn in stats:
        value = fn(a, b)
        lo, hi, dropped = census_metrics.item_bootstrap_interval(
            fn, a, b, reps=BOOTSTRAP_REPS
        )
        span = (
            f"95% item-bootstrap {_fmt(lo)} to {_fmt(hi)}"
            if lo is not None
            else "interval unavailable"
        )
        lines.append(
            f"{name} {_fmt(value)} ({span}; "
            f"{dropped} of {BOOTSTRAP_REPS} resamples undefined)"
        )
    return lines


def _precision_counts(conn, window_id: int) -> tuple[int, int]:
    yes, asked = conn.execute(
        "SELECT count(*) FILTER (WHERE decision = 'same'), count(*) "
        "FROM census_adjudications WHERE window_id = %s AND kind = 'precision'",
        (window_id,),
    ).fetchone()
    return yes, asked


def _precision_lines(conn, windows: list[tuple]) -> list[str]:
    per_window = [
        c
        for w in windows
        if w[2] == 1 and w[3] == "complete"
        for c in [_precision_counts(conn, w[0])]
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
    conn, windows: list[tuple], stats: dict[int, dict], mid: datetime
) -> list[str]:
    lines = ["== By block half (descriptive only) =="]
    for name, pick in (
        ("first half", lambda t: t < mid),
        ("second half", lambda t: t >= mid),
    ):
        done = [w for w in windows if w[2] == 1 and w[3] == "complete" and pick(w[4])]
        if not done:
            lines.append(f"{name}: no complete windows")
            continue
        mean_multi = statistics.mean(stats[w[0]]["multi"] for w in done)
        mean_items = statistics.mean(stats[w[0]]["items"] for w in done)
        counts = [_precision_counts(conn, w[0]) for w in done]
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
    stats = {w[0]: _stats(conn, w[0]) for w in windows}
    block_lines, mid = _block_section(block)

    sections = [
        [f"Event census readout as of {now:%Y-%m-%d %H:%M} UTC"],
        _gap_section(block),
        block_lines,
        _window_lines(conn, windows, stats),
        _go_lines(conn, block[9]),
        _mde_lines(windows, stats),
        _consistency_lines(conn, windows),
        _precision_lines(conn, windows),
        _half_lines(conn, windows, stats, mid),
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

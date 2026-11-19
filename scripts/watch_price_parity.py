"""Pre-deploy parity check for watch price rows (news-brief-vap).

Runs the new price judge (watch.fetch_adj_closes + watch.evaluate_price) and
the portfolio repo's own watch_check.evaluate on the same open price rows, the
same day, and compares the verdicts. Hand-computed tests cannot see a yfinance
behaviour the fetcher fails to reproduce; this can.

Run it on the operator's PC, under the PORTFOLIO venv (it has yfinance; this
module imports only watch.py, which needs nothing beyond requests):

    <portfolio>/.venv/Scripts/python.exe scripts/watch_price_parity.py --portfolio <portfolio>

It prints counts and row ids only — never a row's name, reason or level: this
repository is public and so is anything pasted from it. Exit 0 when nothing
disagrees, 1 otherwise. An above_ma200 row within 0.5% of its MA may differ
for a day (the completed-bar rule) and is reported as tolerated.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import watch  # noqa: E402  (path shim above must run first)

STRADDLE = 0.005  # within 0.5% of the MA


def compare(ours: dict[str, str], theirs: dict[str, str], straddle: set[str]) -> dict:
    """Verdict maps (row id -> fired|not|unknown) -> the parity summary."""
    out = {"agree": 0, "disagree": [], "tolerated": [], "unknown": 0}
    for rid in sorted(set(ours) | set(theirs)):
        a, b = ours.get(rid), theirs.get(rid)
        if a is None or b is None:
            out["disagree"].append(rid)
        elif "unknown" in (a, b):
            out["unknown"] += 1
        elif a == b:
            out["agree"] += 1
        elif rid in straddle:
            out["tolerated"].append(rid)
        else:
            out["disagree"].append(rid)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--portfolio", required=True, type=Path)
    args = ap.parse_args(argv)

    sys.path.insert(0, str(args.portfolio))
    import watch_check  # the portfolio repo's own rule (needs yfinance)

    rows = [
        r
        for r in csv.DictReader(
            (args.portfolio / "data" / "watchlist.csv").open(encoding="utf-8")
        )
        if r.get("status") == "open" and r.get("condition") != "event"
    ]
    now = datetime.now(timezone.utc)
    bars: dict = {}
    ours: dict[str, str] = {}
    theirs: dict[str, str] = {}
    straddle: set[str] = set()
    for r in rows:
        rid = r["id"]
        level = float(r["level"]) if r.get("level") not in (None, "") else None
        row = watch.PriceRow(
            rid, "", r.get("action", ""), r["signal"], r["condition"], level
        )
        if row.signal not in bars:
            try:
                bars[row.signal] = watch.fetch_adj_closes(row.signal)
            except ValueError:
                bars[row.signal] = None
        v = watch.evaluate_price(row, bars[row.signal], now)
        ours[rid] = v.state
        if row.condition == "above_ma200" and bars[row.signal]:
            done = watch.completed_bars(bars[row.signal], now)
            if len(done) >= watch.MA_WINDOW:
                ma = sum(c for _, c in done[-watch.MA_WINDOW :]) / watch.MA_WINDOW
                if abs(done[-1][1] / ma - 1) <= STRADDLE:
                    straddle.add(rid)
        try:
            hit, _ = watch_check.evaluate(r)
        except Exception:
            hit = None
        theirs[rid] = "unknown" if hit is None else ("fired" if hit else "not")

    out = compare(ours, theirs, straddle)
    print(f"rows={len(rows)} symbols={len(bars)}")
    print(f"agree={out['agree']} unknown={out['unknown']}")
    print(f"tolerated={out['tolerated']}")
    print(f"disagree={out['disagree']}")
    return 1 if out["disagree"] else 0


if __name__ == "__main__":
    sys.exit(main())

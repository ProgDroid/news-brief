"""One-off retirement of the open paper prediction rows.

Context: news-brief-oh4. polygram.ink was dropped as a venue on 2026-09-11 (its
buy path charged ~$1 per share whatever the displayed price; see
docs/2026-09-11-live-buy-pricing.md and docs/2026-09-11-prediction-trading-retired.md).
The paper prediction sleeve took its market prices from the same API, so its open
rows can never be marked again.

Decided by the operator, 2026-09-11: each open row closes at the last mark the
weekly run recorded (reason `venue_retired`), is SCORED as if that mark were the
close, and is KEPT -- every prediction row is history. The scoring reproduces the
prediction branch of trading._stamp_close_metrics as it stood when it was removed,
because that code no longer exists to call:

    haircut = entry_spread (orderbook half-spread captured at open) else 0.02
              (HAIRCUT_BPS_PREDICTION's default, 200 bps); +0.02 for a momentum play
    net_return = max(realized_return - haircut, -1.0)   # a stake cannot lose more than itself
    benchmark_return = 0.0                               # the naive coin-flip baseline
    edge = net_return - 0.0

A row that was never marked closes with every score None: unknown, not zero.

Refuses if any LIVE row is still open -- that is real capital and this script
must never be the thing that "closes" it. Run after deploying the commit that
removed the prediction code (that code's weekly mark path now leaves these rows
alone and logs how many are waiting).

Usage (dry run prints the change and writes nothing):

    docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json
    docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

CLOSE_REASON = "venue_retired"
DEFAULT_HAIRCUT = 0.02  # HAIRCUT_BPS_PREDICTION default (200 bps) at retirement
NAIVE_BENCHMARK = 0.0


class Refused(Exception):
    """The book does not look the way this repair was reasoned about."""


def score(row: dict) -> dict:
    """The retired _stamp_close_metrics prediction branch, applied to the last mark."""
    mark = row.get("last_mark") or {}
    gross = mark.get("return")
    if gross is None:
        return {
            k: None
            for k in (
                "realized_return",
                "haircut",
                "net_return",
                "benchmark_return",
                "edge",
            )
        }
    haircut = row.get("entry_spread")
    if haircut is None:
        haircut = DEFAULT_HAIRCUT
    if row.get("play_type") == "momentum":
        haircut += DEFAULT_HAIRCUT
    net = max(gross - haircut, -1.0)
    return {
        "realized_return": gross,
        "haircut": haircut,
        "net_return": net,
        "benchmark_return": NAIVE_BENCHMARK,
        "edge": net - NAIVE_BENCHMARK,
    }


def plan(book: dict, *, today: str) -> list[tuple[dict, dict]]:
    """(row, changes) for every open paper prediction row. Pure. Raises Refused."""
    positions = book.get("positions", [])
    live_open = [
        p
        for p in positions
        if p.get("execution") == "live" and p.get("status") == "open"
    ]
    if live_open:
        raise Refused(
            f"{len(live_open)} live row(s) still open ({[p.get('id') for p in live_open]}); "
            "real capital is not retired by this script"
        )
    out = []
    for p in positions:
        if p.get("asset_class") != "prediction" or p.get("status") != "open":
            continue
        changes = {
            "status": "closed",
            "close_reason": CLOSE_REASON,
            "closed_date": today,
        }
        changes.update(score(p))
        out.append((p, changes))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book", type=Path, help="path to book.json")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args(argv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    book = json.loads(args.book.read_text(encoding="utf-8"))
    try:
        changes = plan(book, today=today)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    for row, ch in changes:
        print(f"row {row['id']}")
        for k, v in ch.items():
            print(f"  {k:<16} {row.get(k)!r} -> {v!r}")
    unmarked = sum(1 for _, ch in changes if ch["net_return"] is None)
    print(f"\n{len(changes)} row(s) to retire, {unmarked} never marked")

    if not args.apply:
        print("\ndry run; nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.book.with_suffix(f".{stamp}.bak")
    shutil.copy2(args.book, backup)
    for row, ch in changes:
        row.update(ch)
    args.book.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"\nwritten. backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

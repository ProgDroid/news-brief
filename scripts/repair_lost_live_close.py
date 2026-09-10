"""One-off repair of the live close that the venue completed and the book lost.

Context: news-brief-sb0.

On 2026-09-10 17:57 UTC a /close on 2026-09-01:prediction:3501950:NO:live sold
at the venue and succeeded. POST /trade/sell answered:

    {'success': True, 'sold': 2.022221, 'price': 0.9165,
     'proceeds': 1.8033655465, 'remainingShares': 0, 'balance': 49.593235}

sell_position was reading a nested {"sale": {...}} the venue has never sent, so
it returned None, close_live_position returned False, and the row was left open.
Nineteen seconds later a retry found the position gone -- it had been sold -- and
at 18:00 reconcile_live_book settled it as `close_reason="settled"` with
`realized_return=None`. The sale price and the proceeds were never recorded, so
the first completed live exit in this book's history reads as a settlement of
unknown value.

The parse bug is dead in the code. This script only repairs the one row it left
behind, from the numbers in that log line -- which is the whole record of the
sale that exists on our side.

Targeted by position id, never by a detector: a detector could only ever misfire
on some future row nobody has reasoned about, and this is live financial state.
Every pre-registered fact about the row is asserted before anything is written,
so a book that has moved on causes a REFUSAL rather than an edit of unknown size.

Usage (dry run prints the change and writes nothing):

    python scripts/repair_lost_live_close.py /app/logs/paper/book.json
    python scripts/repair_lost_live_close.py /app/logs/paper/book.json --apply

Stdlib only, and it takes the book path as an argument, so it runs against a
mounted volume without needing to be inside the image.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROW_ID = "2026-09-01:prediction:3501950:NO:live"

# MEASURED, from the venue's own reply in the 2026-09-10 17:57:01 log line.
SOLD_SHARES = 2.022221
SALE_PRICE = 0.9165
PROCEEDS = 1.8033655465

# What /close passes as its reason (brief.py). reconcile_live_book overwrote it
# with "settled", which says the venue disposed of the position on its own --
# the opposite of what happened.
TRUE_CLOSE_REASON = "manual"
WRONG_CLOSE_REASON = "settled"
CLOSED_DATE = "2026-09-10"

# The state the row must still be in. Anything else means the world moved and
# this script's assumptions expired.
EXPECT = {
    "execution": "live",
    "status": "closed",
    "close_reason": WRONG_CLOSE_REASON,
    "instrument": "3501950",
}


class Refused(Exception):
    """The book does not look the way this repair was reasoned about."""


def find_row(book: dict) -> dict:
    rows = [p for p in book.get("positions", []) if p.get("id") == ROW_ID]
    if len(rows) != 1:
        raise Refused(f"expected exactly 1 row with id {ROW_ID}, found {len(rows)}")
    return rows[0]


def check(row: dict) -> None:
    """Pre-registered facts. Every one of these was true on 2026-09-10."""
    for field, want in EXPECT.items():
        got = row.get(field)
        if got != want:
            raise Refused(f"{field} is {got!r}, expected {want!r}")
    if row.get("realized_return") is not None:
        raise Refused(
            f"realized_return is already {row['realized_return']!r} — something "
            f"has repaired this row (backfill_settled?); refusing to overwrite"
        )
    cost = row.get("cost_basis")
    if not isinstance(cost, (int, float)) or cost <= 0:
        raise Refused(f"cost_basis is {cost!r}; a return cannot be computed from it")
    # The venue sold what the book thought it held. If those disagree, the
    # proceeds above may not belong to this row at all.
    booked = row.get("shares")
    if isinstance(booked, (int, float)) and abs(booked - SOLD_SHARES) > 1e-6:
        raise Refused(
            f"book holds {booked} shares but the venue sold {SOLD_SHARES}; "
            f"the measured proceeds cannot be attributed to this row"
        )


def repair(row: dict) -> dict:
    """Return the fields this repair writes. Pure, so the test can read it."""
    return {
        "realized_return": PROCEEDS / row["cost_basis"] - 1.0,
        "close_reason": TRUE_CLOSE_REASON,
        "closed_date": CLOSED_DATE,
        "last_mark": {
            "date": CLOSED_DATE,
            "price": SALE_PRICE,
            "proceeds": PROCEEDS,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book", type=Path, help="path to book.json")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args(argv)

    book = json.loads(args.book.read_text(encoding="utf-8"))
    try:
        row = find_row(book)
        check(row)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    changes = repair(row)
    print(f"row {ROW_ID}")
    print(f"  cost_basis      {row['cost_basis']}")
    for k, v in changes.items():
        print(f"  {k:<15} {row.get(k)!r} -> {v!r}")

    if not args.apply:
        print("\ndry run; nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.book.with_suffix(f".{stamp}.bak")
    shutil.copy2(args.book, backup)
    row.update(changes)
    args.book.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"\nwritten. backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

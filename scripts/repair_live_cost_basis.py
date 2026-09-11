"""One-off re-basing of every live row: entry_price and cost_basis were not what was paid.

Context: news-brief-rhg. Measured 2026-09-11 from /trade/history (pgdiag) and the
operator's reading of polygram.ink's wallet history.

Until 2026-09-11 a live row recorded the venue's `fillPrice` as entry_price and
the requested `amount` as cost_basis. Both were the docs' story. The wallet shows
TWO debits per buy -- $2.00 for the buy and $0.06 for the fee, separately -- so the
cost of a $2 position is $2.06. And on all nine $2 buys ever placed,

    shares = 2 + 0.0205 / fillPrice

so a dollar bought ~1.011 shares whatever the displayed price (0.78 to 0.94): the
price PAID per share was amount/shares = 0.987-0.989, not the 0.78-0.94 the book
says. Every realized_return divided by 2.00 instead of 2.06 and so read ~2.5pp
better than the wallet.

This script derives the correction from each row's own fields -- no hard-coded
ids -- and asserts the nine-row world it was reasoned about before writing:

  * exactly 9 live rows, none carrying fill_price yet (a second run is refused)
  * every one cost_basis == 2.0 with fees.total_fee == 0.06 and 2.0 < shares < 2.03
  * where proceeds are recorded, they equal the venue's history to the cent

Per row it writes fill_price (the old entry_price), entry_price = cost_basis/shares,
cost_basis += fees.total_fee, above_band (paid > 0.92, true on all nine), and --
only where a realized_return already exists -- the return recomputed against the
true cost. A row still awaiting backfill_settled is left alone: backfill reads
cost_basis at run time and will divide by the corrected figure.

Usage (dry run prints the change and writes nothing):

    python scripts/repair_live_cost_basis.py /app/logs/paper/book.json
    python scripts/repair_live_cost_basis.py /app/logs/paper/book.json --apply

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

# The world this repair was reasoned about. Anything else is a REFUSAL.
EXPECTED_LIVE_ROWS = 9
STAKE = 2.0
FEE = 0.06
SHARES_BAND = (2.0, 2.03)  # 2 + 0.0205/fill for fill in (0.68, 1.0]
BAND_HI = 0.92  # PG_A_BAND_HI throughout the period

# Net proceeds of the three completed sales, from /trade/history on 2026-09-11
# (amount - totalFee; the wallet chain closes on these to the cent). A row that
# records a different figure is not the row these numbers describe.
MEASURED_PROCEEDS = {
    "3501950": 1.8033655465,
    "2243896": 1.7240742175,
    "2774057": 1.88307871875,
}


class Refused(Exception):
    """The book does not look the way this repair was reasoned about."""


def _check(row: dict) -> None:
    rid = row.get("id")
    if "fill_price" in row:
        raise Refused(f"{rid} already carries fill_price; this repair has run")
    if row.get("cost_basis") != STAKE:
        raise Refused(
            f"{rid} cost_basis is {row.get('cost_basis')!r}, expected {STAKE}"
        )
    fee = (row.get("fees") or {}).get("total_fee")
    if fee != FEE:
        raise Refused(f"{rid} fees.total_fee is {fee!r}, expected {FEE}")
    shares = row.get("shares")
    if not isinstance(shares, (int, float)) or not (
        SHARES_BAND[0] < shares < SHARES_BAND[1]
    ):
        raise Refused(f"{rid} shares is {shares!r}, outside {SHARES_BAND}")
    if not isinstance(row.get("entry_price"), (int, float)):
        raise Refused(f"{rid} entry_price is {row.get('entry_price')!r}")
    recorded = (row.get("last_mark") or {}).get("proceeds")
    want = MEASURED_PROCEEDS.get(str(row.get("instrument")))
    if recorded is not None and want is not None and abs(recorded - want) > 1e-6:
        raise Refused(
            f"{rid} records proceeds {recorded} but the venue's history says {want}"
        )


def _changes(row: dict) -> dict:
    cost = row["cost_basis"] + row["fees"]["total_fee"]
    ch = {
        "fill_price": row["entry_price"],
        "entry_price": row["cost_basis"] / row["shares"],
        "cost_basis": cost,
        "above_band": row["cost_basis"] / row["shares"] > BAND_HI,
    }
    old_rr = row.get("realized_return")
    if old_rr is None:
        return ch  # awaiting backfill, or resolved at the venue: nothing to redo
    proceeds = (row.get("last_mark") or {}).get("proceeds")
    if proceeds is None:
        # The pre-7f0a904 backfill stored the return and not the proceeds it
        # used. Invert exactly: it computed old_rr = proceeds/old_cost - 1.
        proceeds = (old_rr + 1.0) * row["cost_basis"]
        ch["last_mark"] = {
            **(row.get("last_mark") or {"date": None, "price": None}),
            "proceeds": proceeds,
        }
    ch["realized_return"] = proceeds / cost - 1.0
    return ch


def plan(book: dict) -> list[tuple[dict, dict]]:
    """(row, changes) for every live row. Pure. Raises Refused before any change."""
    live = [p for p in book.get("positions", []) if p.get("execution") == "live"]
    if len(live) != EXPECTED_LIVE_ROWS:
        raise Refused(f"expected {EXPECTED_LIVE_ROWS} live rows, found {len(live)}")
    for row in live:
        _check(row)
    return [(row, _changes(row)) for row in live]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book", type=Path, help="path to book.json")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args(argv)

    book = json.loads(args.book.read_text(encoding="utf-8"))
    try:
        changes = plan(book)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    for row, ch in changes:
        print(f"row {row['id']}")
        for k, v in ch.items():
            print(f"  {k:<16} {row.get(k)!r} -> {v!r}")
    redone = sum(1 for _, ch in changes if "realized_return" in ch)
    print(f"\n{len(changes)} row(s) re-based, {redone} return(s) recomputed")

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

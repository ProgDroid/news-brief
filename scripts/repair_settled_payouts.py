"""One-off valuation of the six live rows the venue resolved and paid out.

Context: news-brief-9tq. Run AFTER scripts/repair_live_cost_basis.py (news-brief-rhg),
which this script checks for and refuses without.

Six Sleeve A positions opened 2026-08-10..16 were never sold: the market resolved
in the held outcome's favour and the venue paid out shares x $1.00. The wallet
history shows six credits of ~$2.02, one per market, read by the operator on
2026-09-11. A resolution is not an order, so /trade/history holds no record of
them and backfill_settled correctly reports "no filled sell" -- the only source
for their value is that wallet reading, and it is the whole record on our side.

The number worth knowing: every one of these positions WON, and every one lost
money. shares x $1.00 (about 2.02) against the $2.06 the wallet debited is -1.6%
to -1.9%. That is the venue's buy rule (docs/2026-09-11-live-buy-pricing.md)
measured at resolution.

Targeted by the six market ids; the book must hold exactly these six as
settled-and-unvalued, each with cost_basis 2.06 (the rhg repair applied) and a
share count that pays about two dollars. Anything else is a REFUSAL.

Usage (dry run prints the change and writes nothing):

    python scripts/repair_settled_payouts.py /app/logs/paper/book.json
    python scripts/repair_settled_payouts.py /app/logs/paper/book.json --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# MEASURED: six payout credits in the wallet history, 2026-09-11, one per market.
RESOLVED_MARKETS = {"3324624", "2910437", "3348047", "3399428", "2910438", "3491476"}
PAYOUT_PER_SHARE = 1.0
COST_AFTER_RHG = 2.06
SHARES_BAND = (2.0, 2.03)


class Refused(Exception):
    """The book does not look the way this repair was reasoned about."""


def _settled_unvalued(book: dict) -> list[dict]:
    return [
        p
        for p in book.get("positions", [])
        if p.get("execution") == "live"
        and p.get("close_reason") == "settled"
        and p.get("realized_return") is None
    ]


def plan(book: dict) -> list[tuple[dict, dict]]:
    """(row, changes) for the six resolved rows. Pure. Raises Refused before any change."""
    live = [p for p in book.get("positions", []) if p.get("execution") == "live"]
    if any("fill_price" not in p for p in live):
        raise Refused(
            "a live row has no fill_price: run scripts/repair_live_cost_basis.py first"
        )
    rows = _settled_unvalued(book)
    found = {str(p.get("instrument")) for p in rows}
    if found != RESOLVED_MARKETS or len(rows) != 6:
        raise Refused(
            f"expected exactly the 6 resolved markets {sorted(RESOLVED_MARKETS)} as "
            f"settled-and-unvalued; found {len(rows)}: {sorted(found)}"
        )
    out = []
    for p in rows:
        rid = p.get("id")
        if abs((p.get("cost_basis") or 0.0) - COST_AFTER_RHG) > 1e-9:
            raise Refused(f"{rid} cost_basis is {p.get('cost_basis')!r}, expected 2.06")
        shares = p.get("shares")
        if not isinstance(shares, (int, float)) or not (
            SHARES_BAND[0] < shares < SHARES_BAND[1]
        ):
            raise Refused(f"{rid} shares is {shares!r}; a ~$2.02 payout needs ~2.02")
        payout = shares * PAYOUT_PER_SHARE
        out.append(
            (
                p,
                {
                    "realized_return": payout / p["cost_basis"] - 1.0,
                    "last_mark": {
                        "date": p.get("closed_date"),
                        "price": PAYOUT_PER_SHARE,
                        "proceeds": payout,
                    },
                },
            )
        )
    return out


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
    print(f"\n{len(changes)} row(s) valued from the measured payout")

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

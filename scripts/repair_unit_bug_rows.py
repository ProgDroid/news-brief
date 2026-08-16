"""One-off repair of the 2026-06 unit-bug rows in the paper book.

Context: docs/2026-08-16-trading-retrospective.md (B1 and B2).

B1 — four UK positions opened 2026-06-01..06-04 captured `entry_price` in pence
while every mark came back in pounds, booking ~-99% on each. The stored *prices*
are all correct; only `entry_price` is wrong, so every derived return is
recomputable. This turned the run's best equity call into a near-total loss.

B2 — one position captured `benchmark_entry` 10x low (733.33 where its neighbours
either side of the same week were ~7355), producing `edge = -903.9%` and dragging
the whole book's mean edge from -1.5% to -9.3%. The close-time index level is
recoverable as `entry * (1 + stored return)`, so this row is repairable too.

Both bug classes are already dead in the code (B1 died with the Stooq->Yahoo
cutover; B2 is now blocked by BENCHMARK_SANITY_RETURN in trading.py). This script
only cleans up the rows they left behind.

Repairs are targeted by position id, not by a detector. A detector could only ever
mis-fire on some future row nobody has reasoned about, and this is live financial
state. `strict=True` refuses to touch a row that does not still look exactly as it
did when it was analysed.

Usage (dry run prints the changes and writes nothing):

    python scripts/repair_unit_bug_rows.py /app/logs/paper/book.json
    python scripts/repair_unit_bug_rows.py /app/logs/paper/book.json --apply

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

# entry_price captured in GBX (pence) where the marks are GBP (pounds).
GBX_ROWS = {
    "2026-06-01:SGLNl_EQ:bullish",
    "2026-06-02:RRl_EQ:bullish",
    "2026-06-02:DXJGl_EQ:bearish",
    "2026-06-04:SPOLl_EQ:bullish",
}
GBX_DIVISOR = 100.0

# benchmark_entry captured a decimal place low.
BENCHMARK_ROWS = {"2026-06-26:equity:EXV1:bullish"}
BENCHMARK_MULTIPLIER = 10.0

# A repaired row must land inside this band, or the assumption was wrong.
PLAUSIBLE_EQUITY_RATIO = 3.0


def _ret(direction: str, entry: float, price: float) -> float:
    """Signed return, matching trading._signal_return."""
    raw = (price - entry) / entry
    return -raw if direction == "bearish" else raw


def _repair_gbx(p: dict, strict: bool) -> list[str]:
    entry = p["entry_price"]
    mark = (p.get("last_mark") or {}).get("price")
    if mark is None:
        raise ValueError(
            f"{p['id']}: unexpected state — no last_mark to verify against"
        )
    if entry / mark < GBX_DIVISOR / PLAUSIBLE_EQUITY_RATIO:
        # Already repaired, or never had the bug.
        if strict:
            raise ValueError(
                f"{p['id']}: unexpected state — entry={entry} mark={mark} is not the "
                "~100x mismatch this repair was written for"
            )
        return []

    # Identify which stored return the close actually used, BEFORE recomputing.
    old_realized = p.get("realized_return")
    source = None
    for label, node in list(p.get("checkpoints", {}).items()) + [
        ("last_mark", p.get("last_mark") or {})
    ]:
        if node.get("return") == old_realized:
            source = label
            break
    if old_realized is not None and source is None:
        raise ValueError(
            f"{p['id']}: unexpected state — realized_return {old_realized} matches "
            "no stored checkpoint or last_mark"
        )

    new_entry = entry / GBX_DIVISOR
    direction = p["direction"]
    p["entry_price"] = new_entry
    for cp in p.get("checkpoints", {}).values():
        cp["return"] = _ret(direction, new_entry, cp["price"])
    if p.get("last_mark"):
        p["last_mark"]["return"] = _ret(direction, new_entry, p["last_mark"]["price"])

    if source is not None:
        node = p["last_mark"] if source == "last_mark" else p["checkpoints"][source]
        p["realized_return"] = node["return"]
        if p.get("haircut") is not None:
            p["net_return"] = p["realized_return"] - p["haircut"]
            if p.get("benchmark_return") is not None:
                p["edge"] = p["net_return"] - p["benchmark_return"]

    return [
        f"{p['id']}: entry_price {entry} -> {new_entry:.4f}, "
        f"realized_return {old_realized:+.4%} -> {p.get('realized_return', 0):+.4%}"
    ]


def _repair_benchmark(p: dict, strict: bool) -> list[str]:
    entry = p.get("benchmark_entry")
    stored = p.get("benchmark_return")
    if entry is None or stored is None:
        if strict:
            raise ValueError(f"{p['id']}: unexpected state — no benchmark to repair")
        return []
    if abs(stored) < PLAUSIBLE_EQUITY_RATIO:
        if strict:
            raise ValueError(
                f"{p['id']}: unexpected state — benchmark_return {stored} is already "
                "plausible; this repair was written for the ~900% row"
            )
        return []

    level = entry * (1 + stored)  # recover the close-time index level
    new_entry = entry * BENCHMARK_MULTIPLIER
    new_return = (level - new_entry) / new_entry
    p["benchmark_entry"] = new_entry
    p["benchmark_return"] = new_return
    if p.get("net_return") is not None:
        p["edge"] = p["net_return"] - new_return
    return [
        f"{p['id']}: benchmark_entry {entry} -> {new_entry:.2f} "
        f"(recovered level {level:.2f}), edge {stored:+.4%} -> {new_return:+.4%}"
    ]


def repair_book(book: dict, strict: bool = False) -> list[str]:
    """Repair the known corrupt rows in place; returns a line per change made.

    Idempotent: a second call finds nothing left to repair and returns []. With
    strict=True, a targeted row that no longer looks the way it was analysed
    raises ValueError rather than being silently skipped.
    """
    changes: list[str] = []
    for p in book.get("positions", []):
        pid = p.get("id")
        if pid in GBX_ROWS:
            changes += _repair_gbx(p, strict)
        elif pid in BENCHMARK_ROWS:
            changes += _repair_benchmark(p, strict)
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book", type=Path, help="path to book.json")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the repaired book (default: dry run, prints changes only)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="fail if a targeted row no longer matches what was analysed",
    )
    args = ap.parse_args(argv)

    book = json.loads(args.book.read_text(encoding="utf-8"))
    changes = repair_book(book, strict=args.strict)
    if not changes:
        print("Nothing to repair — the book is already clean.")
        return 0
    for line in changes:
        print(f"  {line}")
    if not args.apply:
        print(f"\n{len(changes)} row(s) would change. Re-run with --apply to write.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.book.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(args.book, backup)
    args.book.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"\nWrote {len(changes)} repair(s). Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

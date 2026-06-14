#!/usr/bin/env python3
"""Phase-4 validation / performance layer: pure analysis over the trading book.

Reads closed positions from a book dict (book.json is the single source of truth;
benchmark/haircut are stamped onto each closed position by trading.py at close).
No network, no mutation. Builds the weekly performance report, the go-live readiness
gate, the daily prompt-feedback block, and the unified daily trade message.
"""

import statistics

from common import DATA_DIR

GATE_HISTORY_FILE = DATA_DIR / "paper" / "gate_history.json"
_DIMENSIONS = ("asset_class", "confidence", "play_type", "thesis_ref")
_ASSET_CLASSES = ("equity", "crypto", "prediction")


def _stats(positions: list) -> dict | None:
    """Net-return stats for a group of closed positions; None if none are scored.

    Positions without net_return (pre-Phase-4 closes) are excluded. mean_edge is
    over the subset carrying a non-null edge (legacy/benchmark-failed → excluded).
    """
    nets = [p["net_return"] for p in positions if p.get("net_return") is not None]
    if not nets:
        return None
    edges = [p["edge"] for p in positions if p.get("edge") is not None]
    return {
        "n": len(nets),
        "hit_rate": 100.0 * sum(1 for r in nets if r > 0) / len(nets),
        "mean_net": sum(nets) / len(nets),
        "median_net": statistics.median(nets),
        "mean_edge": (sum(edges) / len(edges)) if edges else None,
        "n_edge": len(edges),
    }


def aggregate_performance(book: dict) -> dict:
    """Overall + per-dimension net stats over the book's closed positions."""
    closed = [p for p in book.get("positions", []) if p.get("status") == "closed"]
    dims = {}
    for dim in _DIMENSIONS:
        groups: dict = {}
        for p in closed:
            key = p.get(dim)
            if key is None:
                continue
            groups.setdefault(key, []).append(p)
        dims[dim] = {k: s for k, v in groups.items() if (s := _stats(v))}
    return {"overall": _stats(closed), "dimensions": dims}

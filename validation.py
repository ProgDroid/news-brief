#!/usr/bin/env python3
"""Phase-4 validation / performance layer: pure analysis over the trading book.

Reads closed positions from a book dict (book.json is the single source of truth;
benchmark/haircut are stamped onto each closed position by trading.py at close).
No network, no mutation. Builds the weekly performance report, the go-live readiness
gate, the daily prompt-feedback block, and the unified daily trade message.
"""

import statistics

from common import (
    DATA_DIR,
    _write_json_atomic,
    _load_json_or,
    GATE_MIN_TRADES,
    GATE_MIN_HIT_RATE,
    GATE_SUSTAINED_EVALS,
)

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


def record_gate_history(book: dict) -> None:
    """Append this evaluation's per-asset mean edge to gate_history.json.

    Called once per weekly run BEFORE evaluate_gate, so the sustained-window check
    includes the current week. A null entry is recorded for asset classes with no
    scored trades this period.
    """
    per_asset = aggregate_performance(book)["dimensions"].get("asset_class", {})
    history = _load_json_or(GATE_HISTORY_FILE, {}) or {}
    for ac in _ASSET_CLASSES:
        s = per_asset.get(ac)
        history.setdefault(ac, []).append(s["mean_edge"] if s else None)
    _write_json_atomic(GATE_HISTORY_FILE, history)


def evaluate_gate(book: dict) -> dict:
    """Per-asset-class go-live readiness against the configured criteria.

    Each entry: {"ready": bool, "reason": str}. The gate is informational —
    nothing in the system auto-enables live trading.
    """
    per_asset = aggregate_performance(book)["dimensions"].get("asset_class", {})
    history = _load_json_or(GATE_HISTORY_FILE, {}) or {}
    out = {}
    for ac in _ASSET_CLASSES:
        s = per_asset.get(ac)
        if s is None:
            out[ac] = {"ready": False, "reason": "no closed trades yet"}
            continue
        recent = history.get(ac, [])[-GATE_SUSTAINED_EVALS:]
        sustained = len(recent) >= GATE_SUSTAINED_EVALS and all(
            e is not None and e > 0 for e in recent
        )
        if s["n"] < GATE_MIN_TRADES:
            reason = f"need {GATE_MIN_TRADES} closed trades, have {s['n']}"
        elif s["mean_edge"] is None or s["mean_edge"] <= 0:
            reason = "mean edge over benchmark not positive"
        elif s["hit_rate"] < GATE_MIN_HIT_RATE * 100:
            reason = (
                f"net hit-rate {s['hit_rate']:.0f}% < {GATE_MIN_HIT_RATE * 100:.0f}%"
            )
        elif not sustained:
            reason = f"edge not positive across last {GATE_SUSTAINED_EVALS} evals"
        else:
            out[ac] = {"ready": True, "reason": "all criteria met"}
            continue
        out[ac] = {"ready": False, "reason": reason}
    return out


def _fmt(s: dict) -> str:
    edge = f"{100 * s['mean_edge']:+.1f}%" if s["mean_edge"] is not None else "n/a"
    return (
        f"{s['hit_rate']:.0f}% hit · net {100 * s['mean_net']:+.1f}% "
        f"· edge {edge} (n={s['n']})"
    )


def performance_report(book: dict) -> str:
    """Telegram-HTML weekly performance report: overall + dimensions + go-live gate.

    Supersedes the old paper_scorecard. Pure — gate history is read, not written
    (record_gate_history is called separately in the weekly job).
    """
    agg = aggregate_performance(book)
    overall = agg["overall"]
    lines = ["<b>📊 PERFORMANCE REPORT</b>"]
    if overall is None:
        lines.append("No closed trades yet — nothing to score.")
        return "\n".join(lines)

    lines.append(f"• Overall: {_fmt(overall)}")
    for dim in _DIMENSIONS:
        groups = agg["dimensions"].get(dim, {})
        if not groups:
            continue
        lines.append(f"<b>by {dim}</b>")
        for key, s in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"  – {key}: {_fmt(s)}")

    # Chronically-wrong theses (negative mean net over a meaningful sample).
    bad = [
        (k, s)
        for k, s in agg["dimensions"].get("thesis_ref", {}).items()
        if s["n"] >= 3 and s["mean_net"] < 0
    ]
    if bad:
        lines.append("<b>⚠ chronically wrong</b> (consider /mute or /thesis):")
        for k, s in bad:
            lines.append(f"  – {k}: net {100 * s['mean_net']:+.1f}% (n={s['n']})")

    lines.append("<b>🚦 Go-live gate</b>")
    gate = evaluate_gate(book)
    for ac in _ASSET_CLASSES:
        g = gate[ac]
        mark = "✅ READY" if g["ready"] else "⛔ not ready"
        lines.append(f"  – {ac}: {mark} — {g['reason']}")
    return "\n".join(lines)

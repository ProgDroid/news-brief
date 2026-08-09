#!/usr/bin/env python3
"""Phase-4 validation / performance layer: pure analysis over the trading book.

Reads closed positions from a book dict (book.json is the single source of truth;
benchmark/haircut are stamped onto each closed position by trading.py at close).
No network, no mutation. Builds the weekly performance report, the go-live readiness
gate, the daily prompt-feedback block, and the unified daily trade message.
"""

import html
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
LEAKAGE_LOG_FILE = (
    DATA_DIR / "paper" / "leakage-log.json"
)  # written by trading._record_leakage
_DIMENSIONS = (
    "asset_class",
    "confidence",
    "play_type",
    "thesis_ref",
    "source_kind",
    "source_perspective",
)
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
    closed = [
        p
        for p in book.get("positions", [])
        if p.get("status") == "closed" and p.get("execution", "paper") != "live"
    ]
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


def live_performance(book: dict) -> dict:
    """Realized stats over closed live rows (real money), separate from the paper gate."""
    live = [
        p
        for p in book.get("positions", [])
        if p.get("status") == "closed"
        and p.get("execution") == "live"
        and p.get("realized_return") is not None
    ]
    if not live:
        return {"n": 0, "mean_return": 0.0, "by_sleeve": {}}
    rets = [p["realized_return"] for p in live]
    by_sleeve: dict = {}
    for p in live:
        by_sleeve.setdefault(p.get("sleeve", "?"), []).append(p["realized_return"])
    return {
        "n": len(live),
        "mean_return": sum(rets) / len(rets),
        "by_sleeve": {k: sum(v) / len(v) for k, v in by_sleeve.items()},
    }


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


_REPORT_MIN_N = 5  # below this, a report bucket is flagged thin (not yet meaningful)


def _fmt(s: dict) -> str:
    edge = f"{100 * s['mean_edge']:+.1f}%" if s["mean_edge"] is not None else "n/a"
    out = (
        f"{s['hit_rate']:.0f}% hit · net {100 * s['mean_net']:+.1f}% "
        f"· edge {edge} (n={s['n']})"
    )
    if s["n"] < _REPORT_MIN_N:
        out += " ⚠thin"
    return out


_CONF_ORDER = ("low", "medium", "high")


def _calibration_block(agg: dict) -> list[str]:
    """Confidence → realized performance, with an inversion flag.

    Lists low/medium/high (those present) via _fmt, then flags any adjacent pair
    where the higher confidence band realized LESS than the lower — the
    actionable miscalibration signal. Scored by mean_edge, falling back to
    mean_net when edge is unavailable. Returns [] when no confidence data exists.
    """
    conf = agg.get("dimensions", {}).get("confidence", {})
    present = [(c, conf[c]) for c in _CONF_ORDER if c in conf]
    if not present:
        return []
    lines = ["<b>🎯 Calibration (confidence → realized)</b>"]
    for c, s in present:
        lines.append(f"  – {c}: {_fmt(s)}")

    def _score(s: dict) -> float:
        return s["mean_edge"] if s["mean_edge"] is not None else s["mean_net"]

    inversions = [
        f"{hi_c}&lt;{lo_c}"
        for (lo_c, lo_s), (hi_c, hi_s) in zip(present, present[1:])
        if _score(hi_s) < _score(lo_s)
    ]
    if inversions:
        lines.append(
            "  ⚠ inverted: "
            + ", ".join(inversions)
            + " (higher confidence underperforming)"
        )
    return lines


def leakage_summary(window_days: int = 7) -> dict:
    """Sum directional-signal leakage counts over the most recent window_days.

    Reads the date-keyed log trading._record_leakage writes. Returns {} when the
    log is missing/empty. Non-integer values are skipped defensively.
    """
    data = _load_json_or(LEAKAGE_LOG_FILE, {}) or {}
    if not isinstance(data, dict):
        return {}
    totals: dict = {}
    for day in sorted(data.keys())[-window_days:]:
        for reason, n in (data.get(day) or {}).items():
            try:
                totals[reason] = totals.get(reason, 0) + int(n)
            except (TypeError, ValueError):
                continue
    return totals


def _leakage_block() -> list[str]:
    """One-line directional-signal leakage summary for the report ([] when empty)."""
    totals = leakage_summary()
    grand = sum(totals.values())
    if grand == 0:
        return []
    traded = totals.get("traded", 0)
    drops = {k: v for k, v in totals.items() if k != "traded" and v > 0}
    line = f"<b>🚰 Signal leakage (7d)</b>: {grand} directional → {traded} traded"
    if drops:
        parts = ", ".join(
            f"{v} {k}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])
        )
        line += f"; dropped: {parts}"
    return [line]


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

    lines.extend(_calibration_block(agg))

    lines.extend(_leakage_block())

    lines.append("<b>🚦 Go-live gate</b>")
    gate = evaluate_gate(book)
    for ac in _ASSET_CLASSES:
        g = gate[ac]
        mark = "✅ READY" if g["ready"] else "⛔ not ready"
        lines.append(f"  – {ac}: {mark} — {g['reason']}")

    lp = live_performance(book)
    if lp["n"]:
        lines.append(
            f"<b>💵 LIVE (real money)</b>: {lp['n']} closed, "
            f"mean net {lp['mean_return'] * 100:+.1f}%"
        )
    return "\n".join(lines)


_PROMPT_MIN_N = 5  # don't feed the model dimensions with tiny, noisy samples


def performance_prompt_block(book: dict) -> str:
    """Compact track-record block for the daily prompt (recalibration, not rules).

    Surfaces realized net hit-rate + edge by asset_class, confidence, and thesis_ref
    for dimensions with at least _PROMPT_MIN_N closed trades. Returns "" when nothing
    qualifies (so the caller adds nothing to the prompt).
    """
    agg = aggregate_performance(book)
    rows = []
    for dim in ("asset_class", "confidence", "thesis_ref"):
        for key, s in agg["dimensions"].get(dim, {}).items():
            if s["n"] < _PROMPT_MIN_N:
                continue
            edge = (
                f", edge {100 * s['mean_edge']:+.0f}%"
                if s["mean_edge"] is not None
                else ""
            )
            rows.append(
                f"  • {dim}={key}: {s['hit_rate']:.0f}% hit-rate, "
                f"net {100 * s['mean_net']:+.0f}%{edge} (n={s['n']})"
            )
    if not rows:
        return ""
    return (
        "## YOUR TRACK RECORD (paper, net of costs)\n"
        "Calibrate confidence against your realized performance below. This is "
        "context for self-correction, not a rule change.\n" + "\n".join(rows)
    )


_PRED_NAME_CAP = 72  # a market question can run 200+ chars; Telegram lines wrap badly

# Human labels for trading.open_sleeve_a_live's skip tally. Reasons in
# _SLEEVE_A_FAULTS mean something is BROKEN (unreadable venue data, a rejected
# order) as opposed to the sleeve correctly declining a market; they are flagged so
# a failing orderbook read cannot hide behind "nothing was in band".
_SKIP_LABELS = {
    "below_similarity": "weak match",
    "no_event_id": "no eventId on the market",
    "already_open": "already held",
    "unreadable": "market data unreadable",
    "market_closed": "market closed",
    "side_missing": "side data missing",
    "no_price": "no price",
    "out_of_band": "price outside band",
    "spread_too_wide": "spread too wide",
    "book_unreadable": "orderbook unreadable",
    "open_rejected": "order rejected (cap, kill-switch or non-fill)",
}
# "spread_too_wide" is NOT here: an illiquid market is the gate doing its job, and
# marking it alongside a failing venue read is what made a healthy run look broken.
_SLEEVE_A_FAULTS = frozenset(
    {"no_event_id", "unreadable", "book_unreadable", "open_rejected", "side_missing"}
)


def pred_title(p: dict, cap: int = _PRED_NAME_CAP) -> str:
    """A prediction row's PLAIN display title: the market question, collapsed+truncated.

    Prediction rows carry no ticker — trading.py stores the market id in `ticker`
    and the question in `topic`. Falling back to the id keeps rows opened before a
    question was available (or whose market fetch failed) renderable.

    Unescaped on purpose: inline-keyboard button labels are NOT HTML-parsed, so an
    escaped "&amp;" would show up literally on the button. HTML callers use
    pred_name; button callers use this with a tighter cap.
    """
    q = " ".join((p.get("topic") or "").split())
    if not q:
        return str(p.get("ticker") or p.get("instrument") or "?")
    return q if len(q) <= cap else q[: cap - 1].rstrip() + "…"


def pred_name(p: dict) -> str:
    """pred_title escaped for Telegram-HTML message bodies.

    Escaping is mandatory, not cosmetic: questions are venue-supplied free text and
    a bare "&" or "<" makes Telegram reject the whole message as a parse error.
    """
    return html.escape(pred_title(p))


def _pred_handle(p: dict) -> str:
    """The market id, as copy-pasteable <code> — the handle /close and /watch take.

    Kept alongside the question because the name is not a key: two markets in one
    event can share almost identical wording, and the command layer resolves by id.
    """
    return (
        f"<code>{html.escape(str(p.get('ticker') or p.get('instrument') or ''))}</code>"
    )


def _pred_lines(p: dict, *, live: bool) -> list[str]:
    """Two lines for one prediction row: name — outcome, then the metadata tail."""
    tail = ["live" if live else "paper"]
    if live and p.get("cost_basis") is not None:
        tail.append(f"${p['cost_basis']:g} @ {p.get('entry_price', 0):.2f}")
    elif p.get("play_type"):
        tail.append(str(p["play_type"]))
    tail.append(_pred_handle(p))
    return [
        f"  • {pred_name(p)} — {html.escape(str(p.get('outcome') or ''))}",
        f"    {' · '.join(tail)}",
    ]


def _sleeve_a_block(status: dict) -> list[str]:
    """Render why the real-money sleeve did or did not trade today. [] if no status.

    This exists because Sleeve A is fail-closed across eight independent gates, so
    "no live positions" is its ordinary output and was previously visible only in a
    container log. Rendered even when the sleeve is OFF: "are the flags actually set
    in the running container" is the first question a zero-trade day raises, and the
    deploy passes those flags through docker-compose, where they are easy to lose.
    """
    if not status:
        return []
    state = status.get("state")
    if state == "off":
        return [
            "<b>💵 Sleeve A (live)</b>",
            f"  OFF — PG_LIVE_ENABLED={int(bool(status.get('live_enabled')))}, "
            f"PG_A_ENABLED={int(bool(status.get('a_enabled')))}",
        ]
    if state == "no_creds":
        return [
            "<b>💵 Sleeve A (live)</b>",
            "  armed, but PolyGram credentials are missing",
        ]
    if state == "crashed":
        # mode_paper swallows live-path exceptions so the paper run still completes;
        # unreported, that is indistinguishable from the sleeve declining every market.
        return [
            "<b>💵 Sleeve A (live)</b>",
            f"  ❌ CRASHED — {html.escape(str(status.get('error') or 'unknown'))}",
        ]

    if state == "no_candidates":
        # The wallet is only read once a run has candidates, so don't imply it failed.
        return ["<b>💵 Sleeve A (live)</b>", "  armed · no candidate markets today"]

    wallet = status.get("wallet")
    wstr = f"wallet ${wallet:.2f}" if wallet is not None else "wallet UNREADABLE ⚠️"
    lines = [
        "<b>💵 Sleeve A (live)</b>",
        f"  {status.get('matches', 0)} match(es) → "
        f"{status.get('opened', 0)} opened · {wstr}",
    ]
    skips = status.get("skips") or {}
    if skips:
        parts = [
            f"{_SKIP_LABELS.get(r, r)} ×{n}" + (" ⚠️" if r in _SLEEVE_A_FAULTS else "")
            for r, n in sorted(skips.items(), key=lambda kv: -kv[1])
        ]
        lines.append(f"  skipped: {', '.join(parts)}")
    for b in status.get("blocked") or []:
        lines.append(
            f"  ◦ {html.escape(str(b.get('question') or '?'))[:_PRED_NAME_CAP]} "
            f"@ {b.get('price'):.2f} — {_SKIP_LABELS.get(b.get('why'), b.get('why'))}"
        )
    return lines


def daily_trade_message(book: dict, today: str, sleeve_a: dict | None = None) -> str:
    """Unified daily trade message (Telegram-HTML). Pure — uses last-known marks.

    Sections, each omitted when empty; returns "" when there is nothing to say.
    Marks are last-known (refreshed by the weekly mark-to-market), not re-priced
    here, to keep the collect path light.

    Paper and live prediction rows are rendered SEPARATELY. They are otherwise
    near-identical on the wire (same asset_class, same market id, both `bullish`),
    so a merged list makes it impossible to tell whether real money moved — which
    is exactly the question the live sleeve raises.

    `sleeve_a` is trading.open_sleeve_a_live's status dict; when supplied, the
    sleeve's reason for trading or not is rendered even if it opened nothing.
    """
    positions = book.get("positions", [])
    opened = [p for p in positions if p.get("opened") == today]
    open_now = [p for p in positions if p.get("status") == "open"]
    opened_dir = [p for p in opened if p.get("asset_class") != "prediction"]
    opened_pred = [p for p in opened if p.get("asset_class") == "prediction"]
    opened_paper = [p for p in opened_pred if p.get("execution") != "live"]
    opened_live = [p for p in opened_pred if p.get("execution") == "live"]
    sleeve_block = _sleeve_a_block(sleeve_a or {})
    if not (opened or open_now or sleeve_block):
        return ""

    lines = ["<b>📈 TRADE UPDATE</b>"]
    if opened_dir:
        lines.append("<b>Opened today</b>")
        for p in opened_dir:
            lines.append(
                f"  • {p['ticker']} ({p['asset_class']}) {p['direction']} "
                f"@ {p['entry_price']:g}"
            )
    if opened_paper:
        lines.append("<b>Prediction suggestions (paper)</b>")
        for p in opened_paper:
            lines.extend(_pred_lines(p, live=False))
    if opened_live:
        lines.append("<b>💵 Opened LIVE today (real money)</b>")
        for p in opened_live:
            lines.extend(_pred_lines(p, live=True))
    # Always rendered when a status is supplied — the counts and skip tally say
    # something the row list cannot (how many markets were considered, and why the
    # rest were declined), so it is not redundant with an "Opened LIVE" section.
    lines.extend(sleeve_block)
    if open_now:
        lines.append(f"<b>Open positions ({len(open_now)})</b>")
        for p in open_now:
            mark = p.get("last_mark")
            mstr = f"{100 * mark['return']:+.1f}%" if mark else "—"
            if p.get("asset_class") == "prediction":
                tag = "💵 live" if p.get("execution") == "live" else "paper"
                lines.append(
                    f"  • {pred_name(p)} — "
                    f"{html.escape(str(p.get('outcome') or ''))} [{tag}]: {mstr}"
                )
            else:
                lines.append(
                    f"  • {p['ticker']} ({p['asset_class']}) {p['direction']}: {mstr}"
                )
    return "\n".join(lines)

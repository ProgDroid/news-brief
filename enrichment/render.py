# enrichment/render.py
"""Serialise enrichment bundles into (a) a read-only prompt section and (b) a
descriptive signal annotation. Neither path may influence position sizing."""

from .models import EnrichmentBundles
from .universe import normalize_ticker

_CAVEAT = (
    "Bigdata.com sentiment is media tone about the company, NOT a price or "
    "direction forecast. Treat it as an orthogonal tone overlay and context for "
    "your own reading — never as a trade trigger. Source: https://bigdata.com"
)


def _fmt_sentiment(s) -> str:
    if s is None:
        return "n/a"
    trend = ""
    if s.trend_delta is not None and s.trend_mean is not None:
        trend = (
            f" trend={s.trend_delta:+.3f} (mean {s.trend_mean:.3f} over {s.n_points}d)"
        )
    return (
        f"sentiment={s.daily_sentiment} pressure={s.sentiment_pressure} "
        f"attention={s.abnormal_media_attention}{trend} as_of={s.as_of}"
    )


def render_prompt_block(bundles: EnrichmentBundles) -> str:
    if bundles.is_empty():
        return ""
    lines: list[str] = [
        "## BIGDATA.COM ENRICHMENT (read-only context — NEVER a trade trigger)",
        _CAVEAT,
        "",
    ]
    if bundles.symbols:
        lines.append("### Per-symbol sentiment & events")
        for s in bundles.symbols:
            if s.error:
                lines.append(f"- {s.ticker}: (unavailable: {s.error})")
                continue
            lines.append(
                f"- {s.ticker} [{s.rp_entity_id}]: {_fmt_sentiment(s.sentiment)}"
            )
            for e in s.events:
                lines.append(f"    • event [{e.category}] {e.date}: {e.title}")
            for d in s.evidence:
                lines.append(
                    f"    • evidence {d.date} {d.source}: {d.headline}"
                    + (f" (sent {d.sentiment})" if d.sentiment is not None else "")
                )
    if bundles.themes:
        lines.append("### Thematic coverage")
        for t in bundles.themes:
            if t.error:
                lines.append(f"- {t.theme}: (unavailable: {t.error})")
                continue
            lines.append(f"- {t.theme}:")
            for d in t.docs:
                lines.append(f"    • {d.date} {d.source}: {d.headline}")
    return "\n".join(lines)


def annotate_signals(signals: list[dict], bundles: EnrichmentBundles) -> list[dict]:
    """Attach a DESCRIPTIVE bigdata_sentiment dict to signals whose base ticker
    matches a symbol bundle. Read-only/informational — explicitly distinct from
    any sizing input. Returns new dicts; never mutates the inputs."""
    if bundles.is_empty():
        return [dict(sig) for sig in signals]
    by_ticker = {
        s.ticker: s for s in bundles.symbols if s.sentiment is not None and not s.error
    }
    out = []
    for sig in signals:
        tkr = sig.get("ticker")
        bundle = by_ticker.get(normalize_ticker(tkr)) if tkr else None
        if bundle is None:
            out.append(dict(sig))
            continue
        s = bundle.sentiment
        out.append(
            {
                **sig,
                "bigdata_sentiment": {
                    "daily_sentiment": s.daily_sentiment,
                    "sentiment_pressure": s.sentiment_pressure,
                    "abnormal_media_attention": s.abnormal_media_attention,
                    "trend_delta": s.trend_delta,
                    "rp_entity_id": bundle.rp_entity_id,
                },
            }
        )
    return out

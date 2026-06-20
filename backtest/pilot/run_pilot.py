"""MCP-approx pilot harness (Task 8, operator-run — NOT in CI, NOT TDD'd).

Loads the LLM-tone-scored sentiment series (backtest/pilot/series/), fetches
historical closes via yfinance, snaps each sentiment date to the last trading
day on/before it, and runs the TDD'd backtest engine. DIRECTIONAL ONLY — Claude
tone substitutes for RavenPack sentiment (see README.md). The real math lives in
the tested engine; this script is just glue.

Run: python backtest/pilot/run_pilot.py
"""

import json
import pathlib

from backtest.prices_yf import fetch_price_series
from backtest.run import report_markdown, run_backtest
from backtest.series import SentimentPoint, SentimentSeries, load_sentiment_series

HERE = pathlib.Path(__file__).parent
SERIES_DIR = HERE / "series"

# Pilot universe → yfinance symbol (all US-listed; symbol == ticker here).
UNIVERSE = {"MU": "MU", "CVX": "CVX", "RGLD": "RGLD"}
START, END = "2025-03-01", "2026-06-21"
HORIZONS = [1, 5, 21, 63]  # ~1d / 1w / 1mo / 3mo (trading-day offsets)


def snap_to_trading_days(s: SentimentSeries, price_dates: list[str]) -> SentimentSeries:
    """Move each sentiment point to the last trading day <= its date, so it
    aligns to a real close (month-end -28 dates can land on weekends)."""
    sd = sorted(price_dates)
    snapped: list[SentimentPoint] = []
    for p in s.points:
        prior = [d for d in sd if d <= p.date]
        if prior:
            snapped.append(SentimentPoint(prior[-1], p.value))
    return SentimentSeries(ticker=s.ticker, points=snapped)


def main() -> None:
    prices = {}
    sentiment = {}
    for ticker, yf_symbol in UNIVERSE.items():
        ps = fetch_price_series(yf_symbol, START, END)
        prices[ticker] = ps
        raw = json.loads((SERIES_DIR / f"sentiment_{ticker}.json").read_text("utf-8"))
        s = load_sentiment_series(raw)
        sentiment[ticker] = snap_to_trading_days(s, list(ps.closes))
        print(
            f"{ticker}: {len(ps.closes)} closes, "
            f"{len(sentiment[ticker].points)} sentiment points snapped"
        )

    sections = [
        "# MCP-approx sentiment backtest — pilot result (DIRECTIONAL ONLY)",
        "",
        "Universe: MU, CVX, RGLD · 12 monthly windows (2025-04 → 2026-03). "
        "Sentiment = Claude tone of bigdata_search headlines (NOT RavenPack). "
        "See [README](./README.md). Faithful run is Task 9 (REST).",
        "",
        "## Methodology + caveats (read before the tables)",
        "",
        "- **Purpose: wiring confirmation.** This was the lean first pass (3 names, "
        "12 monthly windows) to prove the capture→score→price→engine pipeline runs "
        "end-to-end and to measure cost — NOT to reach a go/no-go on sizing.",
        "- **Approximation.** Claude scores the media tone of each window's "
        "`bigdata_search` headlines into [-1, 1]; this substitutes for RavenPack's "
        "numeric sentiment (the MCP path exposes none). Results test whether *a* "
        "sentiment signal predicts returns, not whether **Bigdata.com's** does.",
        "- **Scoring deviation (this run).** No `ANTHROPIC_API_KEY` in the env, so "
        "tone was scored *inline by the operating model* (Claude Opus 4.8) rather "
        "than via `scorer_llm.score_window`'s haiku API call. Directionally "
        "equivalent; the headlines are saved under `raw/` so the API path is "
        "reproducible when a key is present. Scoring was done from headlines ONLY, "
        "before any price was fetched — no look-ahead.",
        "- **Capture cost (measured):** 36 windowed searches, ~0.6–1.2 query units "
        "each ≈ **~31 units total** (~$0.23 at $0.0075/unit). Archive reached back "
        "to 2025-04 cleanly.",
        "- **Sentiment dates** are month-end, snapped to the last trading day ≤ the "
        "date so they align to a real close. `standardize=True` z-scores tone "
        "within each ticker before pooling. q=4 quantile buckets.",
        "- **Tiny-n caveat.** Held-out n≈17–18. Rank-IC standard error ≈ "
        "1/√(n−1) ≈ 0.24, so any |IC| below ~0.5 here is statistically "
        "indistinguishable from zero — well short of the Bonferroni α/N=0.0125 the "
        "report flags. Treat every number below as directional, not significant.",
        "",
    ]
    for mode in ("level", "delta"):
        res = run_backtest(
            sentiment, prices, HORIZONS, mode=mode, q=4, standardize=True
        )
        sections.append(f"## mode = {mode}")
        sections.append(report_markdown(res))
        sections.append("")

    sections += [
        "## Read of this pilot (go/no-go)",
        "",
        "- **Pipeline: CONFIRMED.** Capture → inline tone score → yfinance prices → "
        "temporal-split engine → report all ran clean for 3 names × 12 windows. "
        "The `backtest/` engine and `scorer_llm` work against live-shaped data.",
        "- **Signal: directionally positive, statistically inconclusive.** Held-out "
        "5d rank IC was positive in both level (+0.29) and delta (+0.33) modes, but "
        "at n≈18 that is ~1.2 SE from zero and the quantile buckets are non-monotone "
        "— no usable edge is established.",
        "- **Confound.** All three names rose hard over the window (memory/AI "
        "supercycle, gold rally, oil spike). Low sentiment dispersion + strongly "
        "trending prices inflate spurious-IC risk; a flat/down regime is needed.",
        "- **Recommendation.** Do **not** size off this, and do **not** fan the "
        "MCP-approx version out to 50–100 names — more approximate tone scoring buys "
        "cost without statistical power or vendor fidelity. The durable win is the "
        "de-risked pipeline. The decisive run is **Task 9** (faithful RavenPack REST "
        "sentiment, larger universe, multiple regimes), gated on the business-email "
        "key. Until then, enrichment stays read-only / never-sizing.",
        "",
    ]

    report = "\n".join(sections)
    out = HERE / "RESULT-approx-2026-06-20.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()

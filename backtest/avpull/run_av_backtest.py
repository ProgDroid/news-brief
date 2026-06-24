# backtest/avpull/run_av_backtest.py
"""Operator-run: cached AV raw JSON -> two factor series -> existing engine ->
two held-out reports. yfinance for prices; NOT imported by the cron app / CI."""

import json
from pathlib import Path

from backtest.avpull.transforms import (
    news_series_from_pages,
    snap_series_to_calendar,
    transcript_series_from_calls,
)
from backtest.avpull.universe import UNIVERSE
from backtest.prices_yf import fetch_price_series
from backtest.run import report_markdown, run_backtest
from backtest.series import load_sentiment_series

HORIZONS = [1, 5, 10, 21, 63]
_AV = {"source_label": "Alpha Vantage", "source_url": "https://www.alphavantage.co"}


def _news_series(raw: Path, t: str) -> dict:
    p = raw / f"news_{t}.json"
    pages = json.loads(p.read_text()) if p.exists() else []
    return news_series_from_pages(t, pages)


def _transcript_series(raw: Path, t: str) -> dict:
    calls = []
    for p in sorted(raw.glob(f"tx_{t}_*.json")):
        resp = json.loads(p.read_text())
        if resp.get("transcript"):
            calls.append(resp)
    return transcript_series_from_calls(t, calls)


def main(
    cache_dir: str, out_dir: str, *, start: str = "2017-06-01", end: str = "2026-06-30"
) -> None:
    raw = Path(cache_dir) / "raw"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    prices, cals = {}, {}
    for t in UNIVERSE:
        try:
            ps = fetch_price_series(t, start, end)
        except Exception as e:  # one flaky yfinance symbol must not kill the run
            print(f"price fetch failed for {t}: {e}; skipping")
            continue
        prices[t] = ps
        cals[t] = set(ps.closes.keys())

    for factor, loader in (("news", _news_series), ("transcript", _transcript_series)):
        sentiment = {}
        for t in UNIVERSE:
            if t not in cals:  # no prices -> engine would skip it anyway
                continue
            snapped = snap_series_to_calendar(loader(raw, t), cals[t])
            sentiment[t] = load_sentiment_series(snapped)
        res = run_backtest(sentiment, prices, HORIZONS, mode="level", standardize=True)
        (out / f"RESULT-av-{factor}.md").write_text(report_markdown(res, **_AV))
        conf = res["confirmation"]
        print(f"{factor}: best={res['best_horizon']}d  held-out={conf}")

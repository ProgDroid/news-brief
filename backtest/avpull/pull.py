# backtest/avpull/pull.py
"""Resumable Alpha Vantage puller. Pure helpers (CI-tested) + live fetch/run
(operator-run, network — NOT in CI, like prices_yf / scorer_llm). The live
functions are added in a later task; this file currently holds only the pure
helpers."""

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path


def is_throttled(resp: dict) -> bool:
    return any(k in resp for k in ("Information", "Note", "Error Message"))


def quarters_2010_to(end_year: int, end_q: int) -> list[str]:
    out: list[str] = []
    for y in range(2010, end_year + 1):
        for q in range(1, 5):
            if y == end_year and q > end_q:
                break
            out.append(f"{y}Q{q}")
    return out


def pending_units(
    universe: list[str], quarters: list[str], done: set[tuple]
) -> list[tuple]:
    units: list[tuple] = [("news", t) for t in universe]
    for t in universe:
        for q in quarters:
            units.append(("transcript", t, q))
    return [u for u in units if u not in done]


_BASE = "https://www.alphavantage.co/query"


def _get(params: dict) -> dict:
    url = _BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode())


def fetch_news_pages(
    ticker: str,
    api_key: str,
    *,
    start_year: int = 2018,
    end_year: int,
    limit: int = 1000,
) -> list[dict]:
    """One bounded NEWS_SENTIMENT call per calendar year (time_from + time_to).
    AV rejects an open-ended time_from at limit=1000 with 'Invalid inputs'; a
    bounded year window is the proven shape. A year that hits the `limit` cap
    logs a warning — a few hyper-covered mega-cap years may drop the tail, which
    the weekly-mean factor tolerates. Raises on a non-data response so the
    caller's manifest does not mark the unit done."""
    pages: list[dict] = []
    for year in range(start_year, end_year + 1):
        resp = _get(
            {
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "time_from": f"{year}0101T0000",
                "time_to": f"{year}1231T2359",
                "sort": "EARLIEST",
                "limit": str(limit),
                "apikey": api_key,
            }
        )
        if is_throttled(resp):
            raise RuntimeError(f"AV error on news {ticker} {year}: {resp}")
        feed = resp.get("feed", [])
        if feed:
            pages.append(resp)
            if len(feed) >= limit:
                print(f"WARN news {ticker} {year}: hit {limit}-article cap")
        time.sleep(0.8)
    return pages


def fetch_transcript(symbol: str, quarter: str, api_key: str) -> dict:
    resp = _get(
        {
            "function": "EARNINGS_CALL_TRANSCRIPT",
            "symbol": symbol,
            "quarter": quarter,
            "apikey": api_key,
        }
    )
    if is_throttled(resp):
        raise RuntimeError(f"AV throttled on transcript {symbol} {quarter}: {resp}")
    return resp


def run(
    api_key: str, out_dir: str, *, end_year: int, end_q: int, per_min: int = 75
) -> None:
    from backtest.avpull.universe import UNIVERSE

    out = Path(out_dir)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    man = out / "manifest.json"
    done: set[tuple] = (
        {tuple(u) for u in json.loads(man.read_text())} if man.exists() else set()
    )
    quarters = quarters_2010_to(end_year, end_q)
    delay = 60.0 / per_min
    for unit in pending_units(UNIVERSE, quarters, done):
        if unit[0] == "news":
            _, t = unit
            (raw / f"news_{t}.json").write_text(
                json.dumps(fetch_news_pages(t, api_key, end_year=end_year))
            )
        else:
            _, t, q = unit
            (raw / f"tx_{t}_{q}.json").write_text(
                json.dumps(fetch_transcript(t, q, api_key))
            )
        done.add(unit)
        man.write_text(json.dumps([list(u) for u in done]))
        time.sleep(delay)
    print(f"pull complete: {len(done)} units in {out}")


if __name__ == "__main__":
    run(
        os.environ["ALPHAVANTAGE_API_KEY"],
        "./av_data",
        end_year=2026,
        end_q=1,
        per_min=75,
    )

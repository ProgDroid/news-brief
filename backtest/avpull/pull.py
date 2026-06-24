# backtest/avpull/pull.py
"""Resumable Alpha Vantage puller. Pure helpers (CI-tested) + live fetch/run
(operator-run, network — NOT in CI, like prices_yf / scorer_llm). The live
functions are added in a later task; this file currently holds only the pure
helpers."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
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


def _get(params: dict, *, retries: int = 4) -> dict:
    url = _BASE + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            wait = 2**attempt  # 1, 2, 4s
            print(f"network blip ({e}); retry {attempt + 1}/{retries - 1} in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")  # loop either returns or raises


def _news_query(
    ticker: str, api_key: str, t_from: datetime, t_to: datetime, limit: int
) -> dict:
    return _get(
        {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "time_from": t_from.strftime("%Y%m%dT%H%M"),
            "time_to": t_to.strftime("%Y%m%dT%H%M"),
            "sort": "EARLIEST",
            "limit": str(limit),
            "apikey": api_key,
        }
    )


def _fetch_news_window(
    ticker: str, api_key: str, t_from: datetime, t_to: datetime, limit: int
) -> list[dict]:
    """Fetch one [t_from, t_to] window; if it hits the `limit` cap (the window
    holds more articles than one call returns), split it in half and recurse so
    no part of a high-volume span is silently dropped. Stops splitting at a
    1-day window (accepts the cap there with a warning). Raises on a non-data
    response so the caller's manifest does not mark the unit done."""
    now = datetime.now()
    if t_to > now:  # AV rejects a future time_from; never query past 'now'
        t_to = now
    if t_from >= t_to:
        return []  # window is entirely in the future
    resp = _news_query(ticker, api_key, t_from, t_to, limit)
    if is_throttled(resp):
        raise RuntimeError(f"AV error on news {ticker} {t_from:%Y-%m-%d}: {resp}")
    feed = resp.get("feed", [])
    if not feed:
        return []
    if len(feed) >= limit and (t_to - t_from) > timedelta(days=1):
        mid = t_from + (t_to - t_from) / 2
        time.sleep(0.8)
        left = _fetch_news_window(ticker, api_key, t_from, mid, limit)
        time.sleep(0.8)
        right = _fetch_news_window(
            ticker, api_key, mid + timedelta(minutes=1), t_to, limit
        )
        return left + right
    if len(feed) >= limit:
        print(f"WARN news {ticker} {t_from:%Y-%m-%d}: 1000-cap on a <=1-day window")
    return [resp]


def fetch_news_pages(
    ticker: str,
    api_key: str,
    *,
    start_year: int = 2018,
    end_year: int,
    limit: int = 1000,
) -> list[dict]:
    """News pages for `ticker`, one bounded year window at a time (AV rejects an
    open-ended range), each recursively split on the per-call cap so high-volume
    name-years are fully covered rather than truncated to the earliest 1000."""
    pages: list[dict] = []
    for year in range(start_year, end_year + 1):
        pages.extend(
            _fetch_news_window(
                ticker,
                api_key,
                datetime(year, 1, 1, 0, 0),
                datetime(year, 12, 31, 23, 59),
                limit,
            )
        )
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

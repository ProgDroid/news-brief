# backtest/avpull/transforms.py
"""Pure transforms: raw Alpha Vantage JSON -> engine SentimentSeries dicts.
No network. AV numeric fields arrive as strings and are coerced to float."""

from datetime import date, timedelta
from statistics import mean


def _iso_week_friday(d: date) -> str:
    """Friday (ISO weekday 5) of d's ISO week, as YYYY-MM-DD. weekday(): Mon=0..
    Sun=6, so 4 - weekday lands on that week's Friday for every day Mon-Sun."""
    return (d + timedelta(days=4 - d.weekday())).isoformat()


def _news_date(time_published: str) -> date:
    # AV format: 'YYYYMMDDTHHMMSS'
    return date(
        int(time_published[0:4]), int(time_published[4:6]), int(time_published[6:8])
    )


def news_series_from_pages(
    ticker: str, pages: list[dict], *, start_date: str = "2018-01-01"
) -> dict:
    start = date.fromisoformat(start_date)
    tkr = ticker.upper()
    num: dict[str, float] = {}  # friday -> sum(rel*score)
    den: dict[str, float] = {}  # friday -> sum(rel)
    for page in pages:
        for item in page.get("feed", []):
            d = _news_date(item["time_published"])
            if d < start:
                continue
            for ts in item.get("ticker_sentiment", []):
                if (ts.get("ticker") or "").upper() != tkr:
                    continue
                rel = float(ts["relevance_score"])
                score = float(ts["ticker_sentiment_score"])
                wk = _iso_week_friday(d)
                num[wk] = num.get(wk, 0.0) + rel * score
                den[wk] = den.get(wk, 0.0) + rel
    points = [
        {"date": wk, "value": num[wk] / den[wk]} for wk in sorted(num) if den[wk] > 0
    ]
    return {"ticker": ticker, "points": points}


def _is_boilerplate(seg: dict) -> bool:
    speaker = (seg.get("speaker") or "").strip().lower()
    title = (seg.get("title") or "").strip().lower()
    return speaker == "operator" or "investor relations" in title


def quarter_to_anchor_date(quarter: str, *, offset_days: int = 50) -> str:
    year = int(quarter[:4])
    q = int(quarter[-1])
    end_month = q * 3
    if end_month == 12:
        qend = date(year, 12, 31)
    else:
        qend = date(year, end_month + 1, 1) - timedelta(days=1)
    return (qend + timedelta(days=offset_days)).isoformat()


def transcript_series_from_calls(ticker: str, calls: list[dict]) -> dict:
    points = []
    for call in calls:
        segs = [
            float(s["sentiment"])
            for s in call.get("transcript", [])
            if not _is_boilerplate(s)
        ]
        if not segs:
            continue
        points.append(
            {"date": quarter_to_anchor_date(call["quarter"]), "value": mean(segs)}
        )
    points.sort(key=lambda p: p["date"])
    return {"ticker": ticker, "points": points}

# backtest/gdelt/snap.py
"""Snap GDELT calendar-day records FORWARD onto the trading calendar and reduce
to a SentimentSeries. Forward (not backward) snap is causal: weekend/holiday
news becomes actionable at the NEXT trading session, so a trading day's signal
never contains information dated after that day's close (no look-ahead)."""

import bisect
from dataclasses import replace
from datetime import date

from backtest.gdelt.aggregate import GdeltDaily, merge_daily
from backtest.series import SentimentPoint, SentimentSeries


def snap_forward(
    dailies: dict[str, GdeltDaily], trading_days: set[str], *, max_gap: int = 4
) -> dict[str, GdeltDaily]:
    cal = sorted(trading_days)
    out: dict[str, GdeltDaily] = {}
    for d, rec in dailies.items():
        i = bisect.bisect_left(cal, d)  # first trading day >= d
        if i >= len(cal):
            continue
        tday = cal[i]
        if (date.fromisoformat(tday) - date.fromisoformat(d)).days > max_gap:
            continue
        out[tday] = (
            merge_daily(out[tday], rec, date_iso=tday)
            if tday in out
            else replace(rec, date=tday)
        )
    return out


def to_sentiment_series(
    snapped: dict[str, GdeltDaily], field: str, *, label: str = "GDELT"
) -> SentimentSeries:
    pts = tuple(
        SentimentPoint(d, rec.signal(field)) for d, rec in sorted(snapped.items())
    )
    return SentimentSeries(ticker=label, points=pts)

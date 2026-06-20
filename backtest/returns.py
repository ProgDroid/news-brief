# backtest/returns.py
"""Forward returns from a price series, by positional (trading-day) offset.
Pure stdlib — calendar-naive: horizon h means h positions ahead in the sorted
close index, which matches daily-bar data with weekends/holidays already removed."""

from backtest.series import PriceSeries


def forward_returns(
    prices: PriceSeries, horizons_days: list[int]
) -> dict[str, dict[int, float]]:
    dates = sorted(prices.closes)
    out: dict[str, dict[int, float]] = {}
    for i, d in enumerate(dates):
        base = prices.closes[d]
        if base == 0:
            continue
        per_h: dict[int, float] = {}
        for h in horizons_days:
            j = i + h
            if j < len(dates):
                per_h[h] = prices.closes[dates[j]] / base - 1.0
        if per_h:
            out[d] = per_h
    return out

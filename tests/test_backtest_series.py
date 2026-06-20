# tests/test_backtest_series.py
from backtest.series import (
    PriceSeries,
    SentimentPoint,
    SentimentSeries,
    load_price_series,
    load_sentiment_series,
)


def test_sentiment_series_sorts_and_maps():
    s = SentimentSeries(
        ticker="MU",
        points=[SentimentPoint("2025-02-01", 0.2), SentimentPoint("2025-01-01", -0.1)],
    )
    assert s.dates() == ["2025-01-01", "2025-02-01"]
    assert s.as_of_map() == {"2025-01-01": -0.1, "2025-02-01": 0.2}


def test_loaders_roundtrip():
    s = load_sentiment_series(
        {"ticker": "MU", "points": [{"date": "2025-01-01", "value": 0.3}]}
    )
    assert s.points[0].value == 0.3
    p = load_price_series({"ticker": "MU", "closes": {"2025-01-01": 100.0}})
    assert isinstance(p, PriceSeries) and p.closes["2025-01-01"] == 100.0

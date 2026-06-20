# tests/test_backtest_prices_yf.py
from backtest.prices_yf import to_price_series


def test_to_price_series_builds_close_map():
    ps = to_price_series("MU", [("2025-01-01", 100.0), ("2025-01-02", 101.5)])
    assert ps.ticker == "MU"
    assert ps.closes == {"2025-01-01": 100.0, "2025-01-02": 101.5}

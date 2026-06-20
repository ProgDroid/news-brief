# tests/test_backtest_prices_yf.py
import pytest

from backtest.prices_yf import _closes_from_frame, to_price_series


def test_to_price_series_builds_close_map():
    ps = to_price_series("MU", [("2025-01-01", 100.0), ("2025-01-02", 101.5)])
    assert ps.ticker == "MU"
    assert ps.closes == {"2025-01-01": 100.0, "2025-01-02": 101.5}


def test_closes_from_flat_columns():
    pd = pytest.importorskip("pandas")  # pandas is operator-only, not in CI
    idx = pd.to_datetime(["2025-01-01", "2025-01-02"])
    df = pd.DataFrame({"Close": [100.0, 101.5], "Open": [99.0, 100.0]}, index=idx)
    assert _closes_from_frame(df) == [("2025-01-01", 100.0), ("2025-01-02", 101.5)]


def test_closes_from_multiindex_single_symbol():
    # yfinance returns MultiIndex columns ('Close', <SYM>) even for one symbol;
    # the naive df['Close'] then yields a 1-col DataFrame, not a scalar Series.
    pd = pytest.importorskip("pandas")
    idx = pd.to_datetime(["2025-01-01", "2025-01-02"])
    cols = pd.MultiIndex.from_tuples([("Close", "MU"), ("Open", "MU")])
    df = pd.DataFrame([[100.0, 99.0], [101.5, 100.0]], index=idx, columns=cols)
    assert _closes_from_frame(df) == [("2025-01-01", 100.0), ("2025-01-02", 101.5)]

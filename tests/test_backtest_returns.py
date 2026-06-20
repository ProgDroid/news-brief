# tests/test_backtest_returns.py
from backtest.returns import forward_returns
from backtest.series import PriceSeries


def test_forward_returns_positional_offsets():
    p = PriceSeries(
        ticker="X",
        closes={
            "2025-01-01": 100.0,
            "2025-01-02": 110.0,
            "2025-01-03": 121.0,
            "2025-01-04": 121.0,
            "2025-01-05": 132.0,
        },
    )
    fr = forward_returns(p, [1, 2])
    assert round(fr["2025-01-01"][1], 6) == 0.10
    assert round(fr["2025-01-01"][2], 6) == 0.21
    assert "2025-01-05" not in fr or 1 not in fr["2025-01-05"]

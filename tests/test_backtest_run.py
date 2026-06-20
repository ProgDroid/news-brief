# tests/test_backtest_run.py
from backtest.run import report_markdown, run_backtest
from backtest.series import PriceSeries, SentimentPoint, SentimentSeries


def _ramp_prices(ticker, n):
    return PriceSeries(
        ticker, {f"2025-{m:02d}-01": 100.0 * (1.05**m) for m in range(1, n + 1)}
    )


def test_run_backtest_positive_ic_when_sentiment_leads_returns():
    s = SentimentSeries(
        "X", [SentimentPoint(f"2025-{m:02d}-01", float(8 - m)) for m in range(1, 8)]
    )
    res = run_backtest({"X": s}, {"X": _ramp_prices("X", 8)}, [1], mode="level")
    assert res["horizons"][1]["n"] >= 5
    assert res["horizons"][1]["ic"] > 0
    assert res["best_horizon"] == 1
    md = report_markdown(res)
    assert "Bigdata.com" in md and "IC" in md

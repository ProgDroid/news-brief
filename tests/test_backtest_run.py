# tests/test_backtest_run.py
from backtest.run import report_markdown, run_backtest
from backtest.series import PriceSeries, SentimentPoint, SentimentSeries


def _prices_with_step_returns(ticker, per_step_returns):
    """Build closes so the 1-day forward return from month m equals
    per_step_returns[m-1]. Dates are 2025-01-01 .. 2025-(N+1)-01."""
    closes = {"2025-01-01": 100.0}
    price = 100.0
    for i, r in enumerate(per_step_returns, start=1):
        price = price * (1 + r)
        closes[f"2025-{i + 1:02d}-01"] = price
    return PriceSeries(ticker, closes)


def test_run_backtest_positive_ic_when_sentiment_leads_returns():
    # sentiment m at month m; forward 1-day return from month m = m*0.01.
    # Perfect monotonic relationship -> rank IC ~ +1.
    n = 7
    s = SentimentSeries(
        "X", [SentimentPoint(f"2025-{m:02d}-01", float(m)) for m in range(1, n + 1)]
    )
    prices = _prices_with_step_returns("X", [m * 0.01 for m in range(1, n + 1)])
    res = run_backtest({"X": s}, {"X": prices}, [1], mode="level")
    assert res["horizons"][1]["n"] >= 5
    assert res["horizons"][1]["ic"] > 0.9  # genuine, not float noise
    assert res["best_horizon"] == 1
    md = report_markdown(res)
    assert "Bigdata.com" in md and "IC" in md


def test_report_marks_results_in_sample_and_selection_biased():
    # The runner selects best_horizon by max|IC| on the full sample and reports
    # in-sample IC; the report MUST flag that so an IC table is never read as a
    # sizing verdict before held-out confirmation.
    s = SentimentSeries(
        "X", [SentimentPoint(f"2025-{m:02d}-01", float(m)) for m in range(1, 8)]
    )
    prices = _prices_with_step_returns("X", [m * 0.01 for m in range(1, 8)])
    md = report_markdown(run_backtest({"X": s}, {"X": prices}, [1, 5], mode="level"))
    low = md.lower()
    assert "in-sample" in low
    assert "split_pairs" in low  # points the reader to held-out confirmation


def test_run_backtest_inverting_sentiment_flips_ic_negative():
    # Same returns, sentiment negated -> rank IC must go strongly negative.
    # Guards against the test passing for the wrong (sign-agnostic) reason.
    n = 7
    s = SentimentSeries(
        "X", [SentimentPoint(f"2025-{m:02d}-01", float(-m)) for m in range(1, n + 1)]
    )
    prices = _prices_with_step_returns("X", [m * 0.01 for m in range(1, n + 1)])
    res = run_backtest({"X": s}, {"X": prices}, [1], mode="level")
    assert res["horizons"][1]["ic"] < -0.9

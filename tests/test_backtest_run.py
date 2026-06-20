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


def _monotonic(n, sign=1.0):
    """A single ticker where 1d forward return rises monotonically with
    sentiment (rank IC ~ +1, or -1 when sign=-1)."""
    s = SentimentSeries(
        "X",
        [SentimentPoint(f"2025-{m:02d}-01", sign * float(m)) for m in range(1, n + 1)],
    )
    prices = _prices_with_step_returns("X", [m * 0.01 for m in range(1, n + 1)])
    return s, prices


def _ticker_window(ticker, start_month, sentiments, returns):
    """One ticker's series over consecutive months from start_month, with the
    1d forward return from month i equal to returns[i] (own price index)."""
    dates = [f"2025-{start_month + i:02d}-01" for i in range(len(sentiments))]
    s = SentimentSeries(
        ticker, [SentimentPoint(d, float(v)) for d, v in zip(dates, sentiments)]
    )
    closes = {dates[0]: 100.0}
    price = 100.0
    for i, r in enumerate(returns):
        price *= 1 + r
        closes[f"2025-{start_month + i + 1:02d}-01"] = price
    return s, PriceSeries(ticker, closes)


def test_run_backtest_positive_ic_in_discovery_and_holdout():
    s, prices = _monotonic(10)
    res = run_backtest({"X": s}, {"X": prices}, [1], mode="level")
    assert res["best_horizon"] == 1
    assert res["discovery_ic"][1] > 0.9  # selection set, genuine (not float noise)
    assert res["confirmation"]["ic"] > 0.9  # held-out confirmation
    assert res["confirmation"]["n"] >= 2
    md = report_markdown(res)
    assert "Bigdata.com" in md and "IC" in md


def test_run_backtest_inverting_sentiment_flips_ic_negative():
    # Same returns, sentiment negated -> rank IC must go strongly negative in
    # BOTH discovery and held-out. Guards the sign end-to-end.
    s, prices = _monotonic(10, sign=-1.0)
    res = run_backtest({"X": s}, {"X": prices}, [1], mode="level")
    assert res["discovery_ic"][1] < -0.9
    assert res["confirmation"]["ic"] < -0.9


def test_discovery_and_holdout_partition_pooled_pairs():
    # The held-out confirmation must use data NOT seen during horizon selection:
    # discovery + holdout disjoint + exhaustive, both non-empty.
    s, prices = _monotonic(10)
    res = run_backtest({"X": s}, {"X": prices}, [1, 5], mode="level", split_frac=0.5)
    c = res["counts"][1]
    assert c["n_discovery"] + c["n_holdout"] == c["n_total"]
    assert c["n_discovery"] > 0 and c["n_holdout"] > 0
    # confirmation == the held-out metric block at the discovery-selected horizon
    assert res["confirmation"] == res["holdout"][res["best_horizon"]]


def test_split_is_temporal_not_by_ticker_iteration():
    # Decisive temporal test: ticker A is LATE + positively correlated, ticker B
    # is EARLY + anti-correlated, and A is inserted first. A by-ticker-order split
    # would select on A (IC +1); a TEMPORAL split selects on the earlier B (IC -1)
    # and confirms on the later A (IC +1). The signs prove the split is by date.
    a_s, a_p = _ticker_window("A", 5, [1, 2, 3, 4], [0.01, 0.02, 0.03, 0.04])
    b_s, b_p = _ticker_window("B", 1, [1, 2, 3, 4], [0.04, 0.03, 0.02, 0.01])
    res = run_backtest(
        {"A": a_s, "B": b_s}, {"A": a_p, "B": b_p}, [1], mode="level", split_frac=0.5
    )
    assert res["discovery_ic"][1] < -0.9  # discovery = earlier window (B, anti)
    assert res["confirmation"]["ic"] > 0.9  # holdout = later window (A, positive)


def test_report_marks_discovery_in_sample_and_shows_holdout_confirmation():
    s, prices = _monotonic(10)
    md = report_markdown(run_backtest({"X": s}, {"X": prices}, [1, 5], mode="level"))
    low = md.lower()
    assert "in-sample" in low  # discovery sweep labeled
    assert "held-out" in low  # confirmation section present
    assert "selection-biased" in low  # selection caveat survives


def test_report_handles_empty_holdout_gracefully():
    # No overlapping dates -> no pooled pairs -> no confirmation; report renders.
    s = SentimentSeries("X", [SentimentPoint("2025-01-01", 1.0)])
    prices = PriceSeries("X", {"2025-06-01": 100.0, "2025-07-01": 101.0})
    md = report_markdown(run_backtest({"X": s}, {"X": prices}, [1], mode="level"))
    assert isinstance(md, str) and "Bigdata.com" in md

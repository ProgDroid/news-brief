# tests/test_backtest_align.py
from backtest.align import align, event_filtered, to_delta, to_zscore
from backtest.series import SentimentPoint, SentimentSeries


def test_to_zscore_centers_within_ticker():
    s = SentimentSeries(
        "X", [SentimentPoint("2025-01-01", 10.0), SentimentPoint("2025-02-01", 12.0)]
    )
    vals = [p.value for p in to_zscore(s).points]  # points follow sorted dates
    assert round(sum(vals), 9) == 0.0  # mean is recentred to zero
    assert vals[0] < 0 < vals[1]  # monotonic order preserved


def test_to_zscore_constant_series_is_all_zero():
    # std == 0 must not divide-by-zero; a flat series carries no signal.
    c = to_zscore(
        SentimentSeries("X", [SentimentPoint("d1", 5.0), SentimentPoint("d2", 5.0)])
    )
    assert [p.value for p in c.points] == [0.0, 0.0]


def test_align_intersects_dates():
    s = SentimentSeries("MU", [SentimentPoint("d1", 0.1), SentimentPoint("d2", 0.2)])
    fwd = {"d1": {5: 0.03}, "d3": {5: 0.09}}
    assert align(s, fwd, 5) == [(0.1, 0.03)]


def test_to_delta_first_difference():
    s = SentimentSeries("MU", [SentimentPoint("d1", 0.1), SentimentPoint("d2", 0.25)])
    d = to_delta(s)
    assert [round(p.value, 6) for p in d.points] == [0.15]
    assert d.points[0].date == "d2"


def test_event_filtered_keeps_only_event_window():
    rows = [("d1", 0.1, 0.01), ("d2", 0.2, 0.02), ("d3", 0.3, 0.03)]
    assert event_filtered(rows, {"d3"}, window=0) == [(0.3, 0.03)]


def test_to_delta_single_point_is_empty():
    s = SentimentSeries("MU", [SentimentPoint("d1", 0.1)])
    assert to_delta(s).points == ()


def test_event_filtered_missing_event_date_returns_empty():
    rows = [("d1", 0.1, 0.01), ("d2", 0.2, 0.02)]
    assert event_filtered(rows, {"dX"}, window=1) == []

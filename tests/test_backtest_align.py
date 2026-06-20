# tests/test_backtest_align.py
from backtest.align import align, event_filtered, to_delta
from backtest.series import SentimentPoint, SentimentSeries


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

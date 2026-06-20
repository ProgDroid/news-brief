# tests/test_backtest_sources.py
import json

from backtest.sources import FixtureSentimentSource


def test_fixture_source_reads_series(tmp_path):
    (tmp_path / "sentiment_MU.json").write_text(
        json.dumps({"ticker": "MU", "points": [{"date": "2025-01-01", "value": 0.2}]}),
        encoding="utf-8",
    )
    src = FixtureSentimentSource(str(tmp_path))
    s = src.series("MU")
    assert s.ticker == "MU" and s.points[0].value == 0.2


def test_fixture_source_missing_is_empty(tmp_path):
    src = FixtureSentimentSource(str(tmp_path))
    assert src.series("NOPE").points == ()

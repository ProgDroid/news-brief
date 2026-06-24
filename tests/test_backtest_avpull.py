# tests/test_backtest_avpull.py
from datetime import date

import pytest

from backtest.avpull.universe import UNIVERSE
from backtest.avpull.transforms import (
    _iso_week_friday,
    news_series_from_pages,
    quarter_to_anchor_date,
    transcript_series_from_calls,
)


def test_universe_is_deduped_and_covers_watchlist():
    assert len(UNIVERSE) == len(set(UNIVERSE))  # no dupes
    assert 30 <= len(UNIVERSE) <= 45  # diversified, not the n=18 pilot
    for w in ("CVX", "MU", "RGLD", "ESLT", "AVAV"):  # watchlist single-stocks
        assert w in UNIVERSE


def test_iso_week_friday_maps_any_weekday_into_same_iso_week():
    assert _iso_week_friday(date(2024, 1, 15)) == "2024-01-19"  # Mon -> that Fri
    assert _iso_week_friday(date(2024, 1, 19)) == "2024-01-19"  # Fri -> itself
    assert _iso_week_friday(date(2024, 1, 21)) == "2024-01-19"  # Sun -> that Fri


def _news_item(tp, ticker, rel, score):
    return {
        "time_published": tp,
        "ticker_sentiment": [
            {
                "ticker": ticker,
                "relevance_score": str(rel),
                "ticker_sentiment_score": str(score),
            }
        ],
    }


def test_news_series_relevance_weighted_weekly_mean():
    page = {
        "feed": [
            _news_item("20240115T120000", "MU", 0.2, 1.0),
            _news_item("20240117T120000", "MU", 0.8, 0.0),
        ]
    }
    out = news_series_from_pages("MU", [page])
    assert out["ticker"] == "MU"
    assert out["points"] == [{"date": "2024-01-19", "value": 0.2}]  # (0.2*1+0.8*0)/1.0


def test_news_series_drops_pre_2018_and_other_tickers():
    page = {
        "feed": [
            _news_item("20150101T120000", "MU", 1.0, 0.5),  # pre-2018 -> dropped
            _news_item("20240115T120000", "AAPL", 1.0, 0.9),  # wrong ticker -> ignored
            _news_item("20240115T120000", "MU", 1.0, 0.3),
        ]
    }
    out = news_series_from_pages("MU", [page])
    assert out["points"] == [{"date": "2024-01-19", "value": 0.3}]


def test_quarter_to_anchor_date_is_quarter_end_plus_offset():
    # 2024Q1 calendar end = 2024-03-31; +50d = 2024-05-20
    assert quarter_to_anchor_date("2024Q1") == "2024-05-20"
    # 2023Q4 end = 2023-12-31; +50d = 2024-02-19
    assert quarter_to_anchor_date("2023Q4") == "2024-02-19"


def test_transcript_series_means_non_boilerplate_segments():
    call = {
        "symbol": "MU",
        "quarter": "2024Q1",
        "transcript": [
            {"speaker": "Operator", "title": "", "sentiment": "0.0"},  # excluded
            {
                "speaker": "Satya Kumar",
                "title": "Investor Relations",
                "sentiment": "0.0",
            },  # excluded
            {
                "speaker": "Sanjay Mehrotra",
                "title": "President and CEO",
                "sentiment": "0.8",
            },
            {"speaker": "Mark Murphy", "title": "CFO", "sentiment": "0.4"},
        ],
    }
    out = transcript_series_from_calls("MU", [call])
    assert out["ticker"] == "MU"
    assert len(out["points"]) == 1
    assert out["points"][0]["date"] == "2024-05-20"
    assert out["points"][0]["value"] == pytest.approx(0.6)


def test_transcript_series_skips_calls_with_no_usable_segments():
    call = {
        "symbol": "MU",
        "quarter": "2024Q1",
        "transcript": [
            {"speaker": "Operator", "title": "", "sentiment": "0.0"},
        ],
    }
    assert transcript_series_from_calls("MU", [call])["points"] == []

"""Price rows judged by code (news-brief-vap): the same rules as the portfolio
repo's watch_check.py, on completed bars only. Rows are made up."""

from datetime import date, datetime, timedelta, timezone

from watch import (
    MA_WINDOW,
    STALE_DAYS,
    PriceRow,
    completed_bars,
    evaluate_price,
)

AS_OF = datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc)
YESTERDAY = date(2026, 11, 18)


def _row(condition="below", level=500.0, signal="SPY"):
    return PriceRow("p1", "Example dip", "add", signal, condition, level)


def _bars(values, last=YESTERDAY):
    """Daily bars ending on `last`, oldest first."""
    n = len(values)
    return [(last - timedelta(days=n - 1 - i), v) for i, v in enumerate(values)]


def test_below_fires_under_the_level_and_not_at_or_over_it():
    assert evaluate_price(_row("below", 500.0), _bars([499.0]), AS_OF).state == "fired"
    assert evaluate_price(_row("below", 500.0), _bars([500.0]), AS_OF).state == "not"
    assert evaluate_price(_row("below", 500.0), _bars([501.0]), AS_OF).state == "not"


def test_above_fires_over_the_level():
    assert evaluate_price(_row("above", 500.0), _bars([501.0]), AS_OF).state == "fired"
    assert evaluate_price(_row("above", 500.0), _bars([500.0]), AS_OF).state == "not"


def test_above_ma200_uses_the_mean_of_the_last_200_completed_bars():
    # 300 bars: the first 100 are huge, so a mean over more than the last 200
    # would put the MA far above the last close.
    values = [10_000.0] * 100 + [100.0] * 199 + [101.0]
    row = _row("above_ma200", None)
    v = evaluate_price(row, _bars(values), AS_OF)
    assert v.state == "fired"  # 101 > mean(199*100 + 101)/200 = 100.005
    assert MA_WINDOW == 200


def test_above_ma200_with_199_completed_bars_is_unknown():
    v = evaluate_price(_row("above_ma200", None), _bars([100.0] * 199), AS_OF)
    assert v.state == "unknown" and "199" in v.detail


def test_a_bar_dated_on_the_run_day_is_ignored():
    bars = _bars([510.0]) + [(AS_OF.date(), 490.0)]  # a forming bar would fire it
    assert evaluate_price(_row("below", 500.0), bars, AS_OF).state == "not"
    assert completed_bars(bars, AS_OF) == [(YESTERDAY, 510.0)]


def test_completed_bars_sorts_and_treats_naive_as_of_as_utc():
    bars = [(YESTERDAY, 2.0), (YESTERDAY - timedelta(days=1), 1.0)]
    assert completed_bars(bars, AS_OF.replace(tzinfo=None)) == sorted(bars)


def test_last_bar_five_days_old_is_not_stale_six_is():
    assert STALE_DAYS == 5
    five = _bars([499.0], last=AS_OF.date() - timedelta(days=5))
    six = _bars([499.0], last=AS_OF.date() - timedelta(days=6))
    assert evaluate_price(_row(), five, AS_OF).state == "fired"
    v = evaluate_price(_row(), six, AS_OF)
    assert v.state == "unknown" and "stale" in v.detail


def test_fetch_failure_is_unknown():
    v = evaluate_price(_row(), None, AS_OF)
    assert v.state == "unknown" and "fetch" in v.detail


def test_no_completed_bars_is_unknown():
    v = evaluate_price(_row(), [(AS_OF.date(), 1.0)], AS_OF)
    assert v.state == "unknown"


def test_gbp_values_compare_without_conversion():
    row = _row("below", 120.0, signal="VOD.L")
    assert evaluate_price(row, _bars([111.9]), AS_OF).state == "fired"


def test_detail_reads_like_watch_check():
    v = evaluate_price(_row("below", 500.0), _bars([512.30]), AS_OF)
    assert v.detail == "SPY 512.30 vs below 500 (+2.5%) @ 2026-11-18"
    assert v.row_id == "p1"


def test_ma_detail_reads_like_watch_check():
    v = evaluate_price(_row("above_ma200", None), _bars([100.0] * 200), AS_OF)
    assert v.detail == "SPY 100.00 vs 200dma 100.00 (+0.0%) @ 2026-11-18"
    assert v.state == "not"

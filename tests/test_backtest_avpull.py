# tests/test_backtest_avpull.py
from backtest.avpull.universe import UNIVERSE


def test_universe_is_deduped_and_covers_watchlist():
    assert len(UNIVERSE) == len(set(UNIVERSE))  # no dupes
    assert 30 <= len(UNIVERSE) <= 45  # diversified, not the n=18 pilot
    for w in ("CVX", "MU", "RGLD", "ESLT", "AVAV"):  # watchlist single-stocks
        assert w in UNIVERSE

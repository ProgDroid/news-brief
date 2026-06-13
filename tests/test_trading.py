"""trading.py: smoke + that the equity paper layer relocated intact."""

import pytest

import trading


def test_trading_exposes_equity_paper_layer():
    for name in (
        "resolve_stooq_symbol",
        "fetch_stooq_price",
        "_signal_return",
        "mode_paper",
        "mark_to_market",
        "paper_scorecard",
        "load_paper_book",
        "save_paper_book",
    ):
        assert hasattr(trading, name), name


def test_signal_return_directionality():
    assert trading._signal_return("bullish", 100.0, 110.0) == pytest.approx(0.10)
    assert trading._signal_return("bearish", 100.0, 110.0) == pytest.approx(-0.10)

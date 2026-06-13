"""trading.py: smoke + that the equity paper layer relocated intact."""

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


def test_paper_book_load_default_shape(tmp_path, monkeypatch):
    # load_paper_book returns the empty-book shape when the file is absent
    monkeypatch.setattr(trading, "PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    book = trading.load_paper_book()
    assert book == {"positions": []}


def test_paper_horizon_config():
    assert set(trading.PAPER_HORIZONS) == {"1w", "2w", "4w"}
    assert trading.PAPER_CLOSE_HORIZON in trading.PAPER_HORIZONS

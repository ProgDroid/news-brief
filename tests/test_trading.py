"""trading.py: smoke + that the equity paper layer relocated intact."""

import trading


def test_trading_exposes_equity_paper_layer():
    for name in (
        "resolve_stooq_symbol",
        "fetch_stooq_price",
        "fetch_price",
        "price_position",
        "_signal_return",
        "mode_paper",
        "mark_to_market",
        "paper_scorecard",
        "load_book",
        "save_book",
    ):
        assert hasattr(trading, name), name


def test_book_load_default_shape(tmp_path, monkeypatch):
    # load_book returns the empty-book shape when neither file exists
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    assert trading.load_book() == {"positions": []}


def test_legacy_book_migrates_in_place(tmp_path, monkeypatch):
    book_file = tmp_path / "book.json"
    legacy_file = tmp_path / "paper-book.json"
    monkeypatch.setattr(trading, "BOOK_FILE", book_file)
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", legacy_file)
    # A legacy equity position: old shape with stooq_symbol, no asset_class.
    legacy_file.write_text(
        '{"positions": [{"ticker": "AAPL", "stooq_symbol": "aapl.us", '
        '"direction": "bullish", "status": "open"}]}',
        encoding="utf-8",
    )
    book = trading.load_book()
    p = book["positions"][0]
    assert p["asset_class"] == "equity"
    assert p["venue"] == "t212"
    assert p["execution"] == "paper"
    assert p["instrument"] == "aapl.us"  # renamed from stooq_symbol
    assert p["play_type"] is None
    assert "stooq_symbol" not in p
    assert book_file.exists()  # migrated copy written
    assert legacy_file.exists()  # original kept as backup


def test_paper_horizon_config():
    assert set(trading.PAPER_HORIZONS) == {"1w", "2w", "4w"}
    assert trading.PAPER_CLOSE_HORIZON in trading.PAPER_HORIZONS

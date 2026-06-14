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

    # Second load reads book.json (already migrated) and leaves it intact.
    book2 = trading.load_book()
    assert book2["positions"][0]["instrument"] == "aapl.us"
    assert "stooq_symbol" not in book2["positions"][0]


def test_paper_horizon_config():
    assert set(trading.PAPER_HORIZONS) == {"1w", "2w", "4w"}
    assert trading.PAPER_CLOSE_HORIZON in trading.PAPER_HORIZONS


def test_stamp_open_benchmark_equity(monkeypatch):
    import trading

    monkeypatch.setattr(trading, "fetch_stooq_price", lambda s: 5000.0)
    p = {"asset_class": "equity"}
    trading._stamp_open_benchmark(p)
    assert p["benchmark_entry"] == 5000.0
    assert p["entry_spread"] is None


def test_stamp_open_benchmark_prediction(monkeypatch):
    import trading

    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.02)
    p = {"asset_class": "prediction", "token_id": "tok123"}
    trading._stamp_open_benchmark(p)
    assert p["benchmark_entry"] is None
    assert p["entry_spread"] == 0.02


def test_stamp_open_benchmark_best_effort(monkeypatch):
    import trading

    def _boom(_):
        raise RuntimeError("network down")

    monkeypatch.setattr(trading, "fetch_stooq_price", _boom)
    p = {"asset_class": "equity"}
    trading._stamp_open_benchmark(p)  # must not raise
    assert p["benchmark_entry"] is None


def test_fetch_pg_half_spread_parses_levels(monkeypatch):
    import trading

    monkeypatch.setattr(
        trading,
        "_polygram_get",
        lambda path: {"bids": [{"price": "0.40"}], "asks": [{"price": "0.50"}]},
    )
    # mid 0.45, half-spread 0.05 → 0.05/0.45
    assert abs(trading._fetch_pg_half_spread("tok") - (0.05 / 0.45)) < 1e-9


def test_fetch_pg_half_spread_none_on_garbage(monkeypatch):
    import trading

    monkeypatch.setattr(trading, "_polygram_get", lambda path: None)
    assert trading._fetch_pg_half_spread("tok") is None


def _closed_equity(**kw):
    p = {
        "ticker": "SHEL",
        "asset_class": "equity",
        "direction": "bullish",
        "realized_return": 0.10,
        "benchmark_entry": 100.0,
        "play_type": None,
    }
    p.update(kw)
    return p


def test_stamp_close_metrics_equity_with_benchmark(monkeypatch):
    import trading

    monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: 104.0)
    p = _closed_equity()
    trading._stamp_close_metrics(p, "2026-06-14")
    assert p["haircut"] == trading.HAIRCUT_BPS_EQUITY / 10_000
    assert abs(p["net_return"] - (0.10 - 0.0010)) < 1e-9
    assert abs(p["benchmark_return"] - 0.04) < 1e-9  # (104-100)/100
    assert abs(p["edge"] - (p["net_return"] - 0.04)) < 1e-9


def test_stamp_close_metrics_legacy_no_benchmark(monkeypatch):
    import trading

    monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: 104.0)
    p = _closed_equity(benchmark_entry=None)
    trading._stamp_close_metrics(p, "2026-06-14")
    assert p["net_return"] is not None
    assert p["benchmark_return"] is None
    assert p["edge"] is None


def test_stamp_close_metrics_benchmark_fetch_failure(monkeypatch):
    import trading

    def _boom(_):
        raise RuntimeError("down")

    monkeypatch.setattr(trading, "fetch_benchmark_level", _boom)
    p = _closed_equity()
    trading._stamp_close_metrics(p, "2026-06-14")  # must not raise
    assert p["net_return"] is not None
    assert p["benchmark_return"] is None
    assert p["edge"] is None


def test_stamp_close_metrics_prediction_resolution():
    import trading

    p = {
        "ticker": "mkt1",
        "asset_class": "prediction",
        "play_type": "resolution",
        "realized_return": 0.30,
        "entry_spread": 0.02,
        "benchmark_entry": None,
    }
    trading._stamp_close_metrics(p, "2026-06-14")
    # resolution settles at 1/0 — entry spread only, no exit leg
    assert p["haircut"] == 0.02
    assert abs(p["net_return"] - 0.28) < 1e-9
    assert p["benchmark_return"] == 0.0  # naive coin-flip baseline
    assert abs(p["edge"] - 0.28) < 1e-9


def test_stamp_close_metrics_prediction_momentum_fallback():
    import trading

    p = {
        "ticker": "mkt2",
        "asset_class": "prediction",
        "play_type": "momentum",
        "realized_return": 0.10,
        "entry_spread": None,
        "benchmark_entry": None,
    }
    trading._stamp_close_metrics(p, "2026-06-14")
    bps = trading.HAIRCUT_BPS_PREDICTION / 10_000
    assert abs(p["haircut"] - 2 * bps) < 1e-9  # entry fallback + exit bps
    assert abs(p["net_return"] - (0.10 - 2 * bps)) < 1e-9

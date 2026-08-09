"""trading.py: smoke + that the equity paper layer relocated intact."""

import pytest

import trading


def test_trading_exposes_equity_paper_layer():
    for name in (
        "resolve_symbol",
        "fetch_quote",
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

    monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: 5000.0)
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

    monkeypatch.setattr(trading, "fetch_benchmark_level", _boom)
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


def test_equity_daily_move_open_to_last(monkeypatch):
    # open 100, close 110 → +10%
    monkeypatch.setattr(
        trading,
        "fetch_quote",
        lambda s: trading.Quote(close=110.0, open_=100.0, volume=5000.0),
    )
    assert trading.fetch_daily_move("equity", "aapl.us") == 10.0


def test_kraken_daily_move(monkeypatch):
    # open 200, last 190 → -5%
    data = {"error": [], "result": {"XXBTZUSD": {"o": "200", "c": ["190", "0.1"]}}}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _R())
    assert trading.fetch_daily_move("crypto", "XBTUSD") == -5.0


def test_daily_move_none_on_failure(monkeypatch):
    monkeypatch.setattr(trading, "fetch_quote", lambda s: None)
    assert trading.fetch_daily_move("equity", "aapl.us") is None


def test_index_daily_move_uses_raw_yahoo_symbol(monkeypatch):
    # Market-pulse indices/commodities/FX price via _yahoo_fetch with the raw
    # symbol (^GSPC, GC=F, …), NOT through fetch_quote's base.market resolver.
    seen = []
    monkeypatch.setattr(
        trading,
        "_yahoo_fetch",
        lambda s: (
            seen.append(s) or trading.Quote(close=110.0, open_=100.0, volume=None)
        ),
    )

    def _boom(*a, **k):
        raise AssertionError("index must not route through fetch_quote")

    monkeypatch.setattr(trading, "fetch_quote", _boom)
    assert trading.fetch_daily_move("index", "^GSPC") == 10.0
    assert seen == ["^GSPC"]  # raw symbol passed straight through


def test_build_market_pulse_includes_spine_and_pinned(monkeypatch):
    monkeypatch.setattr(trading, "fetch_daily_move", lambda ac, inst: 1.5)
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(
        trading, "_load_json_or", lambda *a, **k: {}
    )  # empty vol history
    block = trading.build_market_pulse(["iran"])
    assert "MARKET PULSE" in block or "WHAT MOVED" in block
    assert "+1.5%" in block
    assert "S&P 500" in block  # spine label always present
    assert "Brent" in block  # pinned-iran instrument present


def test_build_market_pulse_skips_unresolvable(monkeypatch):
    monkeypatch.setattr(trading, "fetch_daily_move", lambda ac, inst: None)
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(trading, "_load_json_or", lambda *a, **k: {})
    block = trading.build_market_pulse([])
    assert "—" in block  # em-dash sentinel for unresolved moves; never raises


def test_build_market_pulse_filters_stale_anomalies(monkeypatch):
    from datetime import datetime, timezone, timedelta

    monkeypatch.setattr(trading, "fetch_daily_move", lambda ac, inst: None)
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": []})
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=6)).isoformat()
    stale = (now - timedelta(days=30)).isoformat()
    hist = {
        "equity:FRESHX": {"last_alert_ts": fresh},
        "equity:STALEX": {"last_alert_ts": stale},
    }
    monkeypatch.setattr(trading, "_load_json_or", lambda *a, **k: hist)
    block = trading.build_market_pulse([])
    assert "FRESHX" in block  # recent alert shown
    assert "STALEX" not in block  # 30-day-old alert filtered out


def test_paper_position_carries_source_tags(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "refresh_instruments_cache", lambda *a, **k: {})
    monkeypatch.setattr(trading, "resolve_symbol", lambda *a, **k: "shel.us")
    monkeypatch.setattr(trading, "fetch_price", lambda ac, sym: 60.0)
    # a snapshot signal that has ALREADY been annotated upstream with source tags
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": "SHEL", "asset_class": "equity", '
        '"direction": "bullish", "confidence": "high", "topic": "hormuz", '
        '"thesis_ref": null, "rationale": "x", "provenance": "Al Jazeera", '
        '"source_id": "Al Jazeera", "source_kind": "regional", '
        '"source_perspective": "ARAB"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    trading.mode_paper()
    pos = trading.load_book()["positions"][-1]
    assert pos["source_kind"] == "regional"
    assert pos["source_perspective"] == "ARAB"
    assert pos["source_id"] == "Al Jazeera"


def test_paper_opens_a_commodity_signal_as_index(monkeypatch, tmp_path):
    """A BRENT call opens against the raw Yahoo symbol instead of being skipped.

    Regression for "Paper skip: no instrument for BRENT (equity)" (2026-08-09): the
    pricing layer had routed "index" to Yahoo all along and MARKET_SPINE already
    priced Brent as BZ=F, but mode_paper had no branch, so every commodity signal
    was dropped on the floor.
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    monkeypatch.setattr(trading, "refresh_instruments_cache", lambda *a, **k: {})
    # The equity resolver must never be consulted for a commodity — that lookup
    # against the T212 equity universe is exactly what used to fail.
    monkeypatch.setattr(
        trading, "resolve_symbol", lambda *a, **k: pytest.fail("equity path used")
    )
    priced = []
    monkeypatch.setattr(
        trading, "fetch_price", lambda ac, sym: priced.append((ac, sym)) or 82.5
    )
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": "BRENT", "asset_class": "index", '
        '"direction": "bullish", "confidence": "high", "topic": "hormuz", '
        '"thesis_ref": null, "rationale": "x", "provenance": "Reuters"}]}',
        encoding="utf-8",
    )
    trading.mode_paper()
    pos = trading.load_book()["positions"][-1]
    assert pos["asset_class"] == "index"
    assert pos["instrument"] == "BZ=F"  # raw Yahoo, as MARKET_SPINE uses
    assert pos["entry_price"] == 82.5
    assert ("index", "BZ=F") in priced  # routed to the Yahoo pricer, not fetch_quote


def test_record_leakage_merges_by_date(monkeypatch, tmp_path):
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    trading._record_leakage("2026-06-25", {"traded": 2, "no_ticker": 3})
    trading._record_leakage("2026-06-26", {"traded": 1})
    data = trading._load_json_or(tmp_path / "leak.json", {})
    assert data["2026-06-25"]["no_ticker"] == 3
    assert data["2026-06-26"]["traded"] == 1


def test_paper_tallies_leakage(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "pb.json")
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    monkeypatch.setattr(trading, "refresh_instruments_cache", lambda *a, **k: {})
    sigs = {
        "signals": [
            {
                "topic": "a",
                "direction": "neutral",
                "confidence": "high",
                "ticker": "X",
                "asset_class": "equity",
            },
            {
                "topic": "b",
                "direction": "bullish",
                "confidence": "low",
                "ticker": "Y",
                "asset_class": "equity",
            },
            {
                "topic": "c",
                "direction": "bullish",
                "confidence": "high",
                "ticker": None,
                "asset_class": "equity",
            },
        ]
    }
    (signals_dir / f"signals-{today}.json").write_text(
        json.dumps(sigs), encoding="utf-8"
    )
    trading.mode_paper()
    data = trading._load_json_or(tmp_path / "leak.json", {})
    day = next(iter(data))
    assert data[day]["neutral"] == 1
    assert data[day]["low_confidence"] == 1
    assert data[day]["no_ticker"] == 1

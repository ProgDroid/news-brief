"""Crypto seam: Kraken pair resolution + pricing + asset-class dispatch."""

import trading


# ── resolve_kraken_pair ───────────────────────────────────────────────────────
def test_btc_maps_to_xbt_usd_pair():
    assert trading.resolve_kraken_pair("BTC", {}) == "XBTUSD"


def test_eth_maps_to_eth_usd_pair():
    assert trading.resolve_kraken_pair("ETH", {}) == "ETHUSD"


def test_resolve_kraken_is_case_insensitive():
    assert trading.resolve_kraken_pair("btc", {}) == "XBTUSD"


def test_crypto_override_is_authoritative():
    assert trading.resolve_kraken_pair("FOO", {"FOO": "FOOUSD"}) == "FOOUSD"


def test_unknown_coin_returns_none():
    assert trading.resolve_kraken_pair("NOTACOIN", {}) is None


def test_doge_maps_to_xdg_usd_pair():
    # Kraken's second non-obvious base rename (DOGE→XDG), mirrors the XBT quirk.
    assert trading.resolve_kraken_pair("DOGE", {}) == "XDGUSD"


def test_override_beats_known_map_entry():
    # An override must win even for a coin that IS in the static map (guards
    # against a future reorder that checks the map before the override).
    assert trading.resolve_kraken_pair("BTC", {"BTC": "XBTEUR"}) == "XBTEUR"


# ── fetch_kraken_price ────────────────────────────────────────────────────────
class _FakeKrakenResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_kraken(monkeypatch, payload):
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeKrakenResp(payload)
    )


def test_kraken_parses_last_trade_close(monkeypatch):
    # Kraken returns the canonical pair key (XXBTZUSD) regardless of the queried alias;
    # 'c' is [last_trade_price, lot_volume].
    _patch_kraken(
        monkeypatch, {"error": [], "result": {"XXBTZUSD": {"c": ["63000.5", "0.01"]}}}
    )
    assert trading.fetch_kraken_price("XBTUSD") == 63000.5


def test_kraken_error_array_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": ["EQuery:Unknown asset pair"], "result": {}})
    assert trading.fetch_kraken_price("NOPEUSD") is None


def test_kraken_empty_result_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": [], "result": {}})
    assert trading.fetch_kraken_price("XBTUSD") is None


def test_kraken_non_positive_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": [], "result": {"XXBTZUSD": {"c": ["0", "0"]}}})
    assert trading.fetch_kraken_price("XBTUSD") is None


# ── fetch_price / price_position dispatch ─────────────────────────────────────
def test_fetch_price_routes_by_asset_class(monkeypatch):
    monkeypatch.setattr(
        trading,
        "fetch_quote",
        lambda s: trading.Quote(close=42.0, open_=None, volume=None),
    )
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda s: ("kraken", s))
    assert trading.fetch_price("equity", "aapl.us") == 42.0
    assert trading.fetch_price("crypto", "XBTUSD") == ("kraken", "XBTUSD")


def test_price_position_dispatches_on_asset_class(monkeypatch):
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda s: 100.0)
    p = {"asset_class": "crypto", "instrument": "XBTUSD"}
    assert trading.price_position(p) == 100.0


# ── mode_paper opens a crypto position ────────────────────────────────────────
def test_mode_paper_opens_crypto_position(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    # No T212 call; crypto resolution doesn't use the equity cache anyway.
    monkeypatch.setattr(trading, "refresh_instruments_cache", lambda *a, **k: {})
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda pair: 60000.0)

    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": "BTC", "asset_class": "crypto", '
        '"direction": "bullish", "confidence": "high", "topic": "btc-etf-flows", '
        '"thesis_ref": null, "rationale": "Inflows accelerating.", "provenance": "web_search"}]}',
        encoding="utf-8",
    )

    trading.mode_paper()

    book = trading.load_book()
    assert len(book["positions"]) == 1
    p = book["positions"][0]
    assert p["asset_class"] == "crypto"
    assert p["venue"] == "kraken"
    assert p["execution"] == "paper"
    assert p["instrument"] == "XBTUSD"
    assert p["entry_price"] == 60000.0
    assert p["status"] == "open"

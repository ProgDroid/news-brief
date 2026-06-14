"""Phase 5: volume monitor (parsers, baseline, anomaly, cooldown, watchlist) + commands."""

import common


def test_vol_config_defaults_present():
    assert common.VOL_SPIKE_MULT == 2.5
    assert common.VOL_TRAILING_N == 20
    assert common.VOL_MIN_SAMPLES == 5
    assert common.VOL_ALERT_COOLDOWN_HRS == 12.0
    assert common.VOL_FLOOR_EQUITY == 0.0
    assert common.VOL_FLOOR_CRYPTO == 0.0
    assert common.VOL_FLOOR_PREDICTION == 0.0


import trading  # noqa: E402


class _FakeResp:
    def __init__(self, *, text=None, payload=None):
        self._text = text
        self._payload = payload

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_stooq_volume_parses_column_7(monkeypatch):
    csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-06-13,22:00:02,1,2,0.5,1.5,123456\n"
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeResp(text=csv)
    )
    assert trading.fetch_stooq_volume("aapl.us") == 123456.0


def test_stooq_volume_nd_returns_none(monkeypatch):
    csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\nFOO.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeResp(text=csv)
    )
    assert trading.fetch_stooq_volume("foo.us") is None


def test_kraken_volume_parses_24h(monkeypatch):
    payload = {"error": [], "result": {"XXBTZUSD": {"v": ["10.0", "250.5"]}}}
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeResp(payload=payload)
    )
    assert trading.fetch_kraken_volume("XBTUSD") == 250.5


def test_kraken_volume_error_returns_none(monkeypatch):
    payload = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeResp(payload=payload)
    )
    assert trading.fetch_kraken_volume("NOPEUSD") is None


def test_pg_volume_reads_market_field(monkeypatch):
    monkeypatch.setattr(
        trading, "polygram_market", lambda mid: {"volume24hr": "9999.5"}
    )
    assert trading.fetch_pg_volume("0xabc") == 9999.5


def test_pg_volume_missing_field_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading, "polygram_market", lambda mid: {"question": "no volume here"}
    )
    assert trading.fetch_pg_volume("0xabc") is None


def test_pg_volume_unfetchable_market_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert trading.fetch_pg_volume("0xabc") is None


def test_fetch_volume_dispatches_by_asset_class(monkeypatch):
    monkeypatch.setattr(trading, "fetch_stooq_volume", lambda s: 1.0)
    monkeypatch.setattr(trading, "fetch_kraken_volume", lambda p: 2.0)
    monkeypatch.setattr(trading, "fetch_pg_volume", lambda m: 3.0)
    assert trading.fetch_volume("equity", "aapl.us") == 1.0
    assert trading.fetch_volume("crypto", "XBTUSD") == 2.0
    assert trading.fetch_volume("prediction", "0xabc") == 3.0

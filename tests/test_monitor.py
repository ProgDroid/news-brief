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


from datetime import datetime, timezone, timedelta  # noqa: E402


def test_anomaly_fires_above_multiplier():
    prior = [100, 100, 100, 100, 100]  # mean 100, >= VOL_MIN_SAMPLES
    is_spike, ratio = trading._volume_anomaly(prior, 300.0, floor=0.0)
    assert is_spike is True
    assert ratio == 3.0


def test_anomaly_below_multiplier_is_quiet():
    prior = [100, 100, 100, 100, 100]
    is_spike, _ = trading._volume_anomaly(prior, 200.0, floor=0.0)  # 2.0 < 2.5
    assert is_spike is False


def test_anomaly_warmup_suppresses_when_too_few_samples():
    prior = [100, 100]  # < VOL_MIN_SAMPLES (5)
    is_spike, _ = trading._volume_anomaly(prior, 9999.0, floor=0.0)
    assert is_spike is False


def test_anomaly_floor_suppresses_thin_instrument():
    prior = [1, 1, 1, 1, 1]
    is_spike, _ = trading._volume_anomaly(prior, 5.0, floor=100.0)  # 5x but under floor
    assert is_spike is False


def test_append_sample_dedups_consecutive_duplicates():
    assert trading._append_sample([100, 200], 200.0) == [100, 200]
    assert trading._append_sample([100, 200], 300.0) == [100, 200, 300]


def test_append_sample_caps_at_trailing_n():
    big = list(range(common.VOL_TRAILING_N + 5))
    out = trading._append_sample(big, 999.0)
    assert len(out) == common.VOL_TRAILING_N
    assert out[-1] == 999.0


def test_cooldown_active_within_window():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    assert trading._in_cooldown(recent, now) is True


def test_cooldown_expired_after_window():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=common.VOL_ALERT_COOLDOWN_HRS + 1)).isoformat()
    assert trading._in_cooldown(old, now) is False


def test_cooldown_none_is_not_active():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    assert trading._in_cooldown(None, now) is False


def test_cooldown_handles_naive_timestamp():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    naive = "2026-06-14T11:00:00"  # no offset — must be treated as UTC, not crash
    assert trading._in_cooldown(naive, now) is True


def test_watchlist_roundtrip(tmp_path, monkeypatch):
    f = tmp_path / "watchlist.json"
    monkeypatch.setattr(trading, "WATCHLIST_FILE", f)
    assert trading.load_watchlist() == {"items": []}
    trading.save_watchlist(
        {"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]}
    )
    assert trading.load_watchlist()["items"][0]["instrument"] == "XBTUSD"


def test_resolve_watch_infers_crypto():
    entry = trading.resolve_watch_entry("BTC")
    assert entry == {"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}


def test_resolve_watch_infers_equity_when_not_crypto(monkeypatch):
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: "shel.uk")
    entry = trading.resolve_watch_entry("SHEL")
    assert entry == {"raw": "SHEL", "asset_class": "equity", "instrument": "shel.uk"}


def test_resolve_watch_explicit_equity_skips_crypto(monkeypatch):
    # 'BTC' is a known crypto symbol, but an explicit equity class must not infer crypto.
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: "btc.us")
    entry = trading.resolve_watch_entry("BTC", asset_class="equity")
    assert entry["asset_class"] == "equity"
    assert entry["instrument"] == "btc.us"


def test_resolve_watch_prediction_validates_market(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"question": "x"})
    entry = trading.resolve_watch_entry("0xabc", asset_class="prediction")
    assert entry == {"raw": "0xabc", "asset_class": "prediction", "instrument": "0xabc"}


def test_resolve_watch_prediction_bad_market_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert trading.resolve_watch_entry("0xbad", asset_class="prediction") is None


def test_resolve_watch_unresolvable_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: None)
    assert trading.resolve_watch_entry("NOTATHING") is None


def test_watched_instruments_unions_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    trading.save_watchlist(
        {"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]}
    )
    monkeypatch.setattr(
        trading,
        "load_book",
        lambda: {
            "positions": [
                {
                    "status": "open",
                    "asset_class": "crypto",
                    "instrument": "XBTUSD",
                },  # dup of watchlist
                {"status": "open", "asset_class": "equity", "instrument": "shel.uk"},
                {
                    "status": "closed",
                    "asset_class": "equity",
                    "instrument": "bp.uk",
                },  # ignored
            ]
        },
    )
    watched = trading._watched_instruments()
    assert ("crypto", "XBTUSD") in watched
    assert ("equity", "shel.uk") in watched
    assert ("equity", "bp.uk") not in watched
    assert len(watched) == 2  # XBTUSD deduped

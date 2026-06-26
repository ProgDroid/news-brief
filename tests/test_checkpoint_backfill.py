# tests/test_checkpoint_backfill.py
import trading
from datetime import datetime, timezone


class _FakeJsonResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _ts(date_str):
    return int(
        datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    )


def _yahoo_hist_payload(rows, currency="USD"):
    # rows: list of (date_str, close)
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency},
                    "timestamp": [_ts(d) for d, _ in rows],
                    "indicators": {"quote": [{"close": [c for _, c in rows]}]},
                }
            ],
            "error": None,
        }
    }


def test_snap_close_exact_date_hit():
    closes = {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0}
    assert trading._snap_close(closes, "2026-06-02") == 11.0


def test_snap_close_weekend_snaps_back_to_prior_close():
    # 2026-06-06 is a Saturday with no close; snap to Friday 2026-06-05.
    closes = {"2026-06-04": 10.0, "2026-06-05": 11.0, "2026-06-08": 12.0}
    assert trading._snap_close(closes, "2026-06-06") == 11.0


def test_snap_close_target_before_history_returns_none():
    closes = {"2026-06-10": 10.0}
    assert trading._snap_close(closes, "2026-06-01") is None


def test_snap_close_empty_map_returns_none():
    assert trading._snap_close({}, "2026-06-01") is None


def test_yahoo_closes_parses_series(monkeypatch):
    rows = [("2026-06-01", 10.0), ("2026-06-02", 11.0), ("2026-06-03", 12.0)]
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_yahoo_hist_payload(rows)),
    )
    out = trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03")
    assert out == {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0}


def test_yahoo_closes_converts_pence_to_pounds(monkeypatch):
    rows = [("2026-06-01", 2750.0)]
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_yahoo_hist_payload(rows, currency="GBp")),
    )
    out = trading._yahoo_closes("RR.L", "2026-06-01", "2026-06-01")
    assert out == {"2026-06-01": 27.5}


def test_yahoo_closes_skips_null_closes(monkeypatch):
    payload = _yahoo_hist_payload([("2026-06-01", 10.0), ("2026-06-02", 11.0)])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _FakeJsonResp(payload))
    out = trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-02")
    assert out == {"2026-06-01": 10.0}


def test_yahoo_closes_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=429)
    )
    assert trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03") == {}


def test_yahoo_closes_empty_result_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp({"chart": {"result": None, "error": None}}),
    )
    assert trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03") == {}


def _kraken_ohlc_payload(rows, pair_key="XXBTZUSD", error=None):
    # rows: list of (date_str, close); Kraken row = [time,o,h,l,c,vwap,vol,count]
    candles = [[_ts(d), "0", "0", "0", str(c), "0", "0", 0] for d, c in rows]
    return {"error": error or [], "result": {pair_key: candles, "last": 0}}


def test_kraken_closes_parses_series(monkeypatch):
    rows = [("2026-06-01", 60000.0), ("2026-06-02", 61000.0)]
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_kraken_ohlc_payload(rows)),
    )
    out = trading._kraken_closes("XBTUSD", "2026-06-01")
    assert out == {"2026-06-01": 60000.0, "2026-06-02": 61000.0}


def test_kraken_closes_error_array_returns_empty(monkeypatch):
    payload = _kraken_ohlc_payload([], error=["EGeneral:Invalid"])
    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _FakeJsonResp(payload))
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}


def test_kraken_closes_empty_result_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp({"error": [], "result": {"last": 0}}),
    )
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}


def test_kraken_closes_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=500)
    )
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}

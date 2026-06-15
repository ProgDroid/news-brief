"""Paper-tracker pure logic: ticker resolution, returns, and quote parsing."""

import pytest

import trading


INSTRUMENTS = {
    "AAPL_US_EQ": {"isin": "US0378331005", "currencyCode": "USD"},
    "SHEL_US_EQ": {"isin": "US7802593050", "currencyCode": "USD"},
    "SHEL_FR_EQ": {"isin": "FR0000000001", "currencyCode": "EUR"},
    "RRl_EQ": {"isin": "GB00B63H8491", "currencyCode": "GBP"},
    "EXV1d_EQ": {"isin": "DE000A0H08H3", "currencyCode": "EUR"},
    "XYZ_PL_EQ": {"isin": "PL0000000001", "currencyCode": "PLN"},
}
CACHE = {"fetched_at": "2026-01-01T00:00:00+00:00", "instruments": INSTRUMENTS}


# ── _parse_symbol ─────────────────────────────────────────────────────────────
def test_parse_symbol_us():
    assert trading._parse_symbol("aapl.us") == ("aapl", "us")


def test_parse_symbol_uk():
    assert trading._parse_symbol("rr.uk") == ("rr", "uk")


def test_parse_symbol_no_market_returns_none():
    assert trading._parse_symbol("garbage") is None


# ── resolve_symbol (renamed from resolve_stooq_symbol; same outputs) ──────────
def test_override_is_authoritative():
    assert trading.resolve_symbol("FOO", CACHE, {"FOO": "foo.us"}) == "foo.us"


def test_exact_t212_us_ticker():
    assert trading.resolve_symbol("AAPL_US_EQ", CACHE, {}) == "aapl.us"


def test_plain_symbol_base_match_prefers_us_listing():
    assert trading.resolve_symbol("SHEL", CACHE, {}) == "shel.us"


def test_lse_two_part_ticker_strips_market_marker():
    assert trading.resolve_symbol("RRl_EQ", CACHE, {}) == "rr.uk"


def test_xetra_eur_resolved_by_isin_country():
    assert trading.resolve_symbol("EXV1d_EQ", CACHE, {}) == "exv1.de"


def test_plain_lse_marker_symbol_strips_marker():
    # A signal can carry the plain marker-laden LSE symbol ('RRl', no underscore,
    # like the real-world 'SGLNl'/'ARMGl'); it base-matches 'RRl_EQ' and the
    # trailing 'l' marker must still be stripped -> 'rr.uk', not 'rrl.uk'.
    assert trading.resolve_symbol("RRl", CACHE, {}) == "rr.uk"


def test_plain_xetra_marker_symbol_strips_marker():
    assert trading.resolve_symbol("EXV1d", CACHE, {}) == "exv1.de"


def test_unknown_currency_returns_none():
    assert trading.resolve_symbol("XYZ_PL_EQ", CACHE, {}) is None


def test_unknown_symbol_returns_none():
    assert trading.resolve_symbol("NOPE", CACHE, {}) is None


# ── _signal_return ────────────────────────────────────────────────────────────
def test_signal_return_directionality():
    assert trading._signal_return("bullish", 100.0, 110.0) == pytest.approx(0.10)
    assert trading._signal_return("bearish", 100.0, 110.0) == pytest.approx(-0.10)


# ── _yahoo_format_symbol ──────────────────────────────────────────────────────
def test_yahoo_format_us():
    assert trading._yahoo_format_symbol("aapl", "us") == "AAPL"


def test_yahoo_format_lse():
    assert trading._yahoo_format_symbol("rr", "uk") == "RR.L"


def test_yahoo_format_xetra():
    assert trading._yahoo_format_symbol("exv1", "de") == "EXV1.DE"


def test_yahoo_format_paris():
    assert trading._yahoo_format_symbol("mc", "fr") == "MC.PA"


# ── _yahoo_quote ──────────────────────────────────────────────────────────────
# A JSON-returning fake response used by the Yahoo/Alpaca tests.
class _FakeJsonResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _yahoo_payload(currency="USD", close=123.45, open_=120.0, volume=1000):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": currency,
                        "regularMarketPrice": close,
                    },
                    "indicators": {
                        "quote": [
                            {
                                "open": [open_],
                                "close": [close],
                                "volume": [volume],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_yahoo_quote_us_parses_fields(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp(_yahoo_payload())
    )
    q = trading._yahoo_quote("aapl", "us")
    assert q == trading.Quote(close=123.45, open_=120.0, volume=1000.0)


def test_yahoo_quote_lse_converts_pence_to_pounds(monkeypatch):
    # GBp = pence; 2750 pence -> 27.50, open 2700 -> 27.00
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(
            _yahoo_payload(currency="GBp", close=2750.0, open_=2700.0)
        ),
    )
    q = trading._yahoo_quote("rr", "uk")
    assert q.close == 27.5
    assert q.open_ == 27.0


def test_yahoo_quote_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=429)
    )
    assert trading._yahoo_quote("aapl", "us") is None


def test_yahoo_quote_nonpositive_close_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_yahoo_payload(close=0.0)),
    )
    assert trading._yahoo_quote("aapl", "us") is None


def test_yahoo_quote_empty_result_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp({"chart": {"result": None, "error": "x"}}),
    )
    assert trading._yahoo_quote("aapl", "us") is None


def test_yahoo_quote_nonnumeric_close_returns_none(monkeypatch):
    # A garbled (non-numeric) close must degrade to None, not raise — the
    # file's None-on-failure contract: never crash the caller.
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_yahoo_payload(close="N/A")),
    )
    assert trading._yahoo_quote("aapl", "us") is None


def _alpaca_snapshot(close=200.0, open_=198.0, volume=5000):
    return {
        "dailyBar": {"o": open_, "h": 0, "l": 0, "c": close, "v": volume},
        "latestTrade": {"p": close},
        "prevDailyBar": {"o": 0, "h": 0, "l": 0, "c": close, "v": volume},
    }


def test_alpaca_quote_parses_daily_bar(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "k")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "s")
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp(_alpaca_snapshot())
    )
    q = trading._alpaca_quote("AAPL")
    assert q == trading.Quote(close=200.0, open_=198.0, volume=5000.0)


def test_alpaca_quote_no_keys_returns_none(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "")

    def _boom(*a, **k):
        raise AssertionError("should not call the network without keys")

    monkeypatch.setattr(trading.requests, "get", _boom)
    assert trading._alpaca_quote("AAPL") is None


def test_alpaca_quote_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "k")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "s")
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=429)
    )
    assert trading._alpaca_quote("AAPL") is None


def test_fetch_quote_us_prefers_alpaca(monkeypatch):
    calls = []
    monkeypatch.setattr(
        trading,
        "_alpaca_quote",
        lambda s: calls.append(("alpaca", s)) or trading.Quote(1.0, None, None),
    )
    monkeypatch.setattr(
        trading,
        "_yahoo_quote",
        lambda b, m: calls.append(("yahoo", b, m)) or trading.Quote(9.0, None, None),
    )
    q = trading.fetch_quote("aapl", "us")
    assert q.close == 1.0
    assert calls == [("alpaca", "AAPL")]  # Yahoo not tried when Alpaca succeeds


def test_fetch_quote_us_falls_back_to_yahoo(monkeypatch):
    monkeypatch.setattr(trading, "_alpaca_quote", lambda s: None)
    monkeypatch.setattr(
        trading, "_yahoo_quote", lambda b, m: trading.Quote(9.0, None, None)
    )
    assert trading.fetch_quote("aapl", "us").close == 9.0


def test_fetch_quote_uk_uses_yahoo_only(monkeypatch):
    def _boom(s):
        raise AssertionError("Alpaca has no UK data; must not be called")

    monkeypatch.setattr(trading, "_alpaca_quote", _boom)
    monkeypatch.setattr(
        trading, "_yahoo_quote", lambda b, m: trading.Quote(27.5, None, None)
    )
    assert trading.fetch_quote("rr", "uk").close == 27.5


def test_fetch_quote_unparseable_symbol_returns_none():
    assert trading.fetch_quote("garbage", "zz") is None


def test_fetch_benchmark_prefers_yahoo_gspc(monkeypatch):
    seen = []
    monkeypatch.setattr(
        trading,
        "_yahoo_fetch",
        lambda s: seen.append(s) or trading.Quote(5000.0, None, None),
    )
    monkeypatch.setattr(
        trading, "_alpaca_quote", lambda s: trading.Quote(500.0, None, None)
    )
    assert trading.fetch_benchmark() == 5000.0
    assert seen == ["^GSPC"]


def test_fetch_benchmark_falls_back_to_spy(monkeypatch):
    monkeypatch.setattr(trading, "_yahoo_fetch", lambda s: None)
    monkeypatch.setattr(
        trading,
        "_alpaca_quote",
        lambda s: trading.Quote(500.0, None, None) if s == "SPY" else None,
    )
    assert trading.fetch_benchmark() == 500.0

"""Paper-tracker pure logic: ticker resolution, returns, and Stooq parsing."""

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


def test_unknown_currency_returns_none():
    assert trading.resolve_symbol("XYZ_PL_EQ", CACHE, {}) is None


def test_unknown_symbol_returns_none():
    assert trading.resolve_symbol("NOPE", CACHE, {}) is None


# ── _signal_return ────────────────────────────────────────────────────────────
def test_signal_return_directionality():
    assert trading._signal_return("bullish", 100.0, 110.0) == pytest.approx(0.10)
    assert trading._signal_return("bearish", 100.0, 110.0) == pytest.approx(-0.10)


# ── fetch_stooq_price ─────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _patch_stooq(monkeypatch, csv_text):
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeResp(csv_text)
    )


def test_stooq_parses_close(monkeypatch):
    _patch_stooq(
        monkeypatch,
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "aapl.us,2026-06-08,22:00:00,1,2,3,123.45,1000",
    )
    assert trading.fetch_stooq_price("aapl.us") == 123.45


def test_stooq_nd_sentinel_returns_none(monkeypatch):
    _patch_stooq(
        monkeypatch,
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "nope.us,N/D,N/D,N/D,N/D,N/D,N/D,N/D",
    )
    assert trading.fetch_stooq_price("nope.us") is None


def test_stooq_zero_price_returns_none(monkeypatch):
    # 0.0 would later divide-by-zero in _signal_return; must be rejected
    _patch_stooq(
        monkeypatch,
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "halt.us,2026-06-08,22:00:00,0,0,0,0,0",
    )
    assert trading.fetch_stooq_price("halt.us") is None


def test_stooq_short_response_returns_none(monkeypatch):
    _patch_stooq(monkeypatch, "Symbol,Date,Time,Open,High,Low,Close,Volume")
    assert trading.fetch_stooq_price("aapl.us") is None

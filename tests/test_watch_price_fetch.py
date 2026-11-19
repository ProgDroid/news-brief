"""The Yahoo adjusted-close fetcher for price rows (news-brief-vap).

Canned payloads through an injected `get`; the network is never touched. It
must reproduce what yfinance's auto_adjust=True gives watch_check.py.
"""

from datetime import date, datetime, timezone

import pytest

from watch import YAHOO_HEADERS, fetch_adj_closes


def _ts(d: date) -> int:
    return int(
        datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()
    )


D1, D2, D3 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)


def _payload(timestamps, closes, adj=None, currency="USD", result=True):
    if not result:
        return {"chart": {"result": None, "error": {"code": "Not Found"}}}
    indicators = {"quote": [{"close": closes}]}
    if adj is not None:
        indicators["adjclose"] = [{"adjclose": adj}]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency},
                    "timestamp": timestamps,
                    "indicators": indicators,
                }
            ]
        }
    }


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def _get(data, status=200, calls=None):
    def get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append((url, params, headers, timeout))
        return _Resp(data, status)

    return get


def test_uses_adjclose():
    data = _payload([_ts(D1), _ts(D2)], [10.0, 11.0], adj=[9.5, 10.5])
    assert fetch_adj_closes("SPY", get=_get(data)) == [(D1, 9.5), (D2, 10.5)]


def test_falls_back_to_close_where_adjclose_missing():
    data = _payload([_ts(D1), _ts(D2)], [10.0, 11.0], adj=[9.5, None])
    assert fetch_adj_closes("SPY", get=_get(data)) == [(D1, 9.5), (D2, 11.0)]
    no_adj = _payload([_ts(D1)], [10.0])
    assert fetch_adj_closes("SPY", get=_get(no_adj)) == [(D1, 10.0)]


def test_drops_none_bars():
    data = _payload([_ts(D1), _ts(D2)], [None, 11.0], adj=[None, 10.5])
    assert fetch_adj_closes("SPY", get=_get(data)) == [(D2, 10.5)]


def test_duplicate_dates_keep_the_last():
    data = _payload(
        [_ts(D1), _ts(D2), _ts(D2) + 3600], [1.0, 2.0, 3.0], adj=[1.0, 2.0, 3.0]
    )
    assert fetch_adj_closes("SPY", get=_get(data)) == [(D1, 1.0), (D2, 3.0)]


def test_no_unit_conversion_for_gbp():
    data = _payload([_ts(D3)], [111.9], adj=[111.9], currency="GBp")
    assert fetch_adj_closes("VOD.L", get=_get(data)) == [(D3, 111.9)]


def test_empty_result_raises():
    with pytest.raises(ValueError):
        fetch_adj_closes("NOPE", get=_get(_payload([], [], result=False)))


def test_mismatched_lengths_raise():
    data = _payload([_ts(D1), _ts(D2)], [10.0], adj=[9.5])
    with pytest.raises(ValueError):
        fetch_adj_closes("SPY", get=_get(data))


def test_http_error_raises():
    with pytest.raises(ValueError):
        fetch_adj_closes("SPY", get=_get({}, status=429))


def test_request_shape():
    calls = []
    fetch_adj_closes("SPY", get=_get(_payload([_ts(D1)], [1.0]), calls=calls))
    assert calls == [
        (
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            {"interval": "1d", "range": "1y"},
            YAHOO_HEADERS,
            15,
        )
    ]


def test_bars_are_dated_in_the_exchange_timezone():
    """Review I1, probed on Yahoo 2026-11-19: FX daily bars are stamped at
    London midnight, 23:00 UTC under BST. Dated by UTC, every summer FX bar
    lands a day early and the forming bar is judged as completed."""
    stamp = int(datetime(2026, 7, 13, 23, 0, tzinfo=timezone.utc).timestamp())
    data = _payload([stamp], [5.0], adj=[5.0])
    data["chart"]["result"][0]["meta"]["exchangeTimezoneName"] = "Europe/London"
    bars = fetch_adj_closes("EURUSD=X", get=_get(data))
    assert bars == [(date(2026, 7, 14), 5.0)]


def test_missing_exchange_timezone_falls_back_to_utc():
    data = _payload([_ts(D1)], [1.0], adj=[1.0])  # no exchangeTimezoneName
    assert fetch_adj_closes("SPY", get=_get(data)) == [(D1, 1.0)]

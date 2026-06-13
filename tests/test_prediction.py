"""Prediction seam: PolyGram read client + Claude matcher + prediction lifecycle."""

import json

import trading


def test_trading_exposes_polygram_creds_attrs():
    # The creds are read in common and re-exported via trading's import for gating.
    assert hasattr(trading, "POLYGRAM_EMAIL")
    assert hasattr(trading, "POLYGRAM_PASSWORD")


# ── _parse_pg_market ──────────────────────────────────────────────────────────
def _raw_market(yes="0.30", no="0.70", closed=False, uma=None):
    return {
        "id": "2410562",
        "question": "Will X happen?",
        "endDate": "2026-07-20T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes}", "{no}"]',
        "clobTokenIds": '["71902280236980528007966111072910269163651886024599423678358797794246690742124", "17567294778637229825908271987925808954865907947626969581496912375435402975317"]',
        "closed": closed,
        "umaResolutionStatus": uma,
    }


def test_parse_pg_market_extracts_prices_and_status():
    p = trading._parse_pg_market(_raw_market(yes="0.30", no="0.70"))
    assert p["market_id"] == "2410562"
    assert p["question"] == "Will X happen?"
    assert p["prices"] == [0.30, 0.70]
    assert p["yes_price"] == 0.30
    assert p["closed"] is False
    assert p["uma_status"] is None
    assert len(p["token_ids"]) == 2


def test_parse_pg_market_reads_resolved_status():
    p = trading._parse_pg_market(
        _raw_market(yes="0", no="1", closed=True, uma="resolved")
    )
    assert p["prices"] == [0.0, 1.0]
    assert p["closed"] is True
    assert p["uma_status"] == "resolved"


def test_parse_pg_market_returns_none_on_garbage():
    assert trading._parse_pg_market({"id": "x"}) is None  # missing arrays
    assert trading._parse_pg_market({"id": "x", "outcomePrices": "not-json"}) is None


# ── login + _polygram_get (401 refresh) ───────────────────────────────────────
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_polygram_login_persists_token(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trading, "POLYGRAM_TOKEN_FILE", tmp_path / "polygram_token.json"
    )
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(
        trading.requests,
        "post",
        lambda url, json=None, timeout=None: _Resp(
            200, {"token": "JWT123", "user": {}}
        ),
    )
    assert trading.polygram_login() == "JWT123"
    saved = json.loads((tmp_path / "polygram_token.json").read_text())
    assert saved["token"] == "JWT123"


def test_polygram_get_refreshes_on_401(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trading, "POLYGRAM_TOKEN_FILE", tmp_path / "polygram_token.json"
    )
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    (tmp_path / "polygram_token.json").write_text(
        '{"token": "STALE"}', encoding="utf-8"
    )
    monkeypatch.setattr(trading, "polygram_login", lambda: "FRESH")

    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if headers.get("Authorization") == "Bearer STALE":
            return _Resp(401, {})
        assert headers.get("Authorization") == "Bearer FRESH"
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(trading.requests, "get", fake_get)
    assert trading._polygram_get("/markets/1") == {"ok": True}
    assert calls["n"] == 2  # one 401, one retry with the refreshed token


def test_polygram_get_returns_none_when_uncredentialed(monkeypatch):
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", None)
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", None)
    assert trading._polygram_get("/markets/1") is None

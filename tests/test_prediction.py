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


# ── _parse_matches ────────────────────────────────────────────────────────────
def test_parse_matches_validates_and_filters_unknown_ids():
    text = (
        "Here are the matches:\n"
        '[{"market_id": "111", "side": "yes", "play_type": "momentum", "similarity": 0.8, "target": 0.6},'
        ' {"market_id": "999", "side": "NO", "play_type": "resolution", "similarity": 0.9, "target": null},'
        ' {"market_id": "111", "side": "BAD", "play_type": "momentum", "similarity": 0.7}]'
    )
    out = trading._parse_matches(text, {"111", "999"})
    # First (side normalised to YES) and second kept; third dropped (bad side).
    assert len(out) == 2
    assert out[0] == {
        "market_id": "111",
        "side": "YES",
        "play_type": "momentum",
        "similarity": 0.8,
        "target": 0.6,
    }
    assert out[1]["market_id"] == "999" and out[1]["target"] is None


def test_parse_matches_empty_on_garbage():
    assert trading._parse_matches("no json here", {"111"}) == []
    assert trading._parse_matches("", {"111"}) == []


def test_parse_matches_drops_id_not_in_candidates():
    out = trading._parse_matches(
        '[{"market_id":"42","side":"YES","play_type":"momentum","similarity":0.9}]',
        {"111"},
    )
    assert out == []


# ── _gather_pg_candidates (dedup + cap) ───────────────────────────────────────
def test_gather_candidates_dedups_and_caps(monkeypatch):
    def fake_search(q):
        return [
            {
                "markets": [
                    _raw_market(),  # id 2410562, open
                    {**_raw_market(), "id": "999", "closed": True},  # closed -> dropped
                ]
            }
        ]

    monkeypatch.setattr(trading, "polygram_search", fake_search)
    cands = trading._gather_pg_candidates(
        [{"topic": "a"}, {"topic": "b", "thesis_ref": "t"}]
    )
    ids = [c["market_id"] for c in cands]
    assert ids == ["2410562"]  # deduped across topics; closed market excluded


# ── run_prediction_matcher ────────────────────────────────────────────────────
def test_run_matcher_calls_claude_and_parses(monkeypatch):
    captured = {}

    class _ClaudeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '[{"market_id":"2410562","side":"YES","play_type":"momentum","similarity":0.75,"target":null}]',
                    }
                ]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _ClaudeResp()

    monkeypatch.setattr(trading.requests, "post", fake_post)
    cands = [
        {
            "market_id": "2410562",
            "question": "Will X?",
            "yes_price": 0.3,
            "end_date": None,
        }
    ]
    out = trading.run_prediction_matcher([{"topic": "x"}], cands)
    assert out == [
        {
            "market_id": "2410562",
            "side": "YES",
            "play_type": "momentum",
            "similarity": 0.75,
            "target": None,
        }
    ]
    assert "tools" not in captured["payload"]  # no web search


# ── price_position prediction dispatch ────────────────────────────────────────
def test_price_position_prediction_reads_held_side(monkeypatch):
    monkeypatch.setattr(
        trading, "polygram_market", lambda mid: _raw_market(yes="0.30", no="0.70")
    )
    yes_pos = {"asset_class": "prediction", "instrument": "2410562", "side_index": 0}
    no_pos = {"asset_class": "prediction", "instrument": "2410562", "side_index": 1}
    assert trading.price_position(yes_pos) == 0.30
    assert trading.price_position(no_pos) == 0.70


def test_price_position_prediction_none_when_unfetchable(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert (
        trading.price_position(
            {"asset_class": "prediction", "instrument": "x", "side_index": 0}
        )
        is None
    )

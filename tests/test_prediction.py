"""Prediction seam: PolyGram read client + Claude matcher + prediction lifecycle."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import common
import polygram_live
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
        [{"topic": "iran-nuclear"}, {"topic": "china-shock"}]
    )
    ids = [c["market_id"] for c in cands]
    assert ids == ["2410562"]  # deduped across token queries; closed market excluded


def test_signal_search_terms_tokenizes_topic_excludes_ticker_and_short():
    terms = trading._signal_search_terms(
        [
            {"topic": "hormuz-fees-dispute", "ticker": "MU"},
            {"topic": "china-shock-2-europe", "ticker": "SMSNl_EQ"},
            {"topic": "hormuz-escalation"},  # 'hormuz' again -> deduped
        ]
    )
    assert {"hormuz", "fees", "dispute", "china", "shock", "europe"} <= set(terms)
    assert "mu" not in terms  # ticker never searched (substring-toxic: MU -> Musk)
    assert "smsnl" not in terms and "smsnl_eq" not in terms
    assert "2" not in terms  # tokens shorter than PG_MIN_TOKEN_LEN dropped
    assert terms.count("hormuz") == 1  # deduped across signals


def test_gather_caps_per_query_and_prefers_high_volume(monkeypatch):
    # One token, one event, 10 open markets with ascending 24h volume.
    def fake_search(q):
        return [
            {
                "markets": [
                    {**_raw_market(), "id": str(i), "volume24hr": i} for i in range(10)
                ]
            }
        ]

    monkeypatch.setattr(trading, "polygram_search", fake_search)
    cands = trading._gather_pg_candidates([{"topic": "hormuz"}])
    ids = {c["market_id"] for c in cands}
    assert len(cands) == trading.PG_PER_QUERY_CAP  # bounded per query
    assert ids == {"9", "8", "7", "6", "5"}  # top-5 by volume, not API order


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


# ── mode_paper opens a prediction position ────────────────────────────────────
def test_mode_paper_opens_prediction_position(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    # A single NEUTRAL signal (no actionable equity/crypto) still drives the matcher.
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": null, "asset_class": "equity", "direction": "neutral", '
        '"confidence": "low", "topic": "fed-cuts", "thesis_ref": null, '
        '"rationale": "macro", "provenance": "web_search"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        trading,
        "_gather_pg_candidates",
        lambda signals: [
            {
                "market_id": "2410562",
                "question": "Will the Fed cut in June?",
                "yes_price": 0.3,
                "end_date": None,
            }
        ],
    )
    monkeypatch.setattr(
        trading,
        "run_prediction_matcher",
        lambda signals, cands: [
            {
                "market_id": "2410562",
                "side": "YES",
                "play_type": "momentum",
                "similarity": 0.8,
                "target": 0.6,
            }
        ],
    )
    monkeypatch.setattr(
        trading, "polygram_market", lambda mid: _raw_market(yes="0.30", no="0.70")
    )

    trading.mode_paper()

    book = trading.load_book()
    assert len(book["positions"]) == 1
    p = book["positions"][0]
    assert p["asset_class"] == "prediction"
    assert p["venue"] == "polygram"
    assert p["instrument"] == "2410562"
    assert p["play_type"] == "momentum"
    assert p["outcome"] == "Yes" and p["side_index"] == 0
    assert p["target"] == 0.6
    assert p["direction"] == "bullish"  # always long the held side
    assert p["entry_price"] == 0.30
    assert p["status"] == "open"


def test_mode_paper_skips_prediction_when_uncredentialed(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", None)
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", None)

    def _boom(*a, **k):
        raise AssertionError("matcher must not run without creds")

    monkeypatch.setattr(trading, "_gather_pg_candidates", _boom)
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": null, "direction": "neutral", "confidence": "low", '
        '"topic": "x", "thesis_ref": null, "rationale": "", "provenance": ""}]}',
        encoding="utf-8",
    )
    trading.mode_paper()  # must not raise, must not call the matcher
    assert trading.load_book() == {"positions": []}


# ── prediction MtM close triggers ─────────────────────────────────────────────
def _pred_position(play_type, side_index=0, entry=0.30, target=None, entry_days_ago=0):
    entry_date = (datetime.now(timezone.utc) - timedelta(days=entry_days_ago)).strftime(
        "%Y-%m-%d"
    )
    return {
        "asset_class": "prediction",
        "ticker": "2410562",
        "instrument": "2410562",
        "play_type": play_type,
        "outcome": "Yes" if side_index == 0 else "No",
        "side_index": side_index,
        "target": target,
        "direction": "bullish",
        "entry_price": entry,
        "entry_date": entry_date,
        "status": "open",
        "close_reason": None,
        "closed_date": None,
        "checkpoints": {},
        "last_mark": None,
        "realized_return": None,
    }


def _mtm(monkeypatch, position, raw_market):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: raw_market)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return trading.mark_to_market({"positions": [position]}, today)["positions"][0]


def test_momentum_closes_on_target_cross(monkeypatch):
    p = _pred_position("momentum", entry=0.30, target=0.60, entry_days_ago=3)
    out = _mtm(
        monkeypatch, p, _raw_market(yes="0.65", no="0.35")
    )  # YES now 0.65 >= 0.60
    assert out["status"] == "closed"
    assert out["close_reason"] == "target"
    assert out["realized_return"] == pytest.approx(0.65 / 0.30 - 1.0)


def test_momentum_force_closes_at_4w(monkeypatch):
    p = _pred_position(
        "momentum", entry=0.30, target=0.99, entry_days_ago=30
    )  # target not hit
    out = _mtm(monkeypatch, p, _raw_market(yes="0.40", no="0.60"))
    assert out["status"] == "closed"
    assert out["close_reason"] == "horizon"
    assert "4w" in out["checkpoints"]


def test_momentum_stays_open_before_horizon(monkeypatch):
    p = _pred_position("momentum", entry=0.30, target=0.99, entry_days_ago=10)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.40", no="0.60"))
    assert out["status"] == "open"


def test_resolution_closes_on_settlement(monkeypatch):
    p = _pred_position("resolution", side_index=0, entry=0.30, entry_days_ago=40)
    out = _mtm(
        monkeypatch, p, _raw_market(yes="1", no="0", closed=True, uma="resolved")
    )
    assert out["status"] == "closed"
    assert out["close_reason"] == "settlement"
    assert out["realized_return"] == pytest.approx(1.0 / 0.30 - 1.0)


def test_resolution_ignores_4w_horizon(monkeypatch):
    # 40 days open, NOT resolved -> resolution must stay open past 4w (unlike equity).
    p = _pred_position("resolution", entry=0.30, entry_days_ago=40)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.50", no="0.50", closed=False))
    assert out["status"] == "open"
    assert "4w" in out["checkpoints"]  # checkpoint still recorded


def test_resolution_max_hold_backstop(monkeypatch):
    p = _pred_position(
        "resolution", entry=0.30, entry_days_ago=trading.PG_MAX_HOLD_DAYS + 1
    )
    out = _mtm(monkeypatch, p, _raw_market(yes="0.50", no="0.50", closed=False))
    assert out["status"] == "closed"
    assert out["close_reason"] == "max_hold"


def test_prediction_mtm_kept_open_when_unfetchable(monkeypatch):
    p = _pred_position("momentum", entry_days_ago=40)
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = trading.mark_to_market({"positions": [p]}, today)["positions"][0]
    assert out["status"] == "open"  # no price -> retried next run


# ── mode_collect failure isolation ────────────────────────────────────────────
def test_collect_trading_failure_does_not_duplicate_brief(monkeypatch):
    import brief

    calls = {"deliver": 0, "cleared": 0}
    monkeypatch.setattr(brief, "load_state", lambda: {"batch_id": "b1"})
    monkeypatch.setattr(brief, "poll_batch", lambda bid: "RAW")
    monkeypatch.setattr(brief, "extract_signals", lambda raw, **kw: ([], "ok"))
    monkeypatch.setattr(brief, "normalize_signals", lambda raw: ([], []))
    monkeypatch.setattr(
        brief,
        "deliver",
        lambda *a, **k: calls.__setitem__("deliver", calls["deliver"] + 1),
    )
    monkeypatch.setattr(brief, "save_signals", lambda *a, **k: None)
    monkeypatch.setattr(
        brief,
        "clear_batch_state",
        lambda: calls.__setitem__("cleared", calls["cleared"] + 1),
    )
    monkeypatch.setattr(brief, "telegram_alert", lambda *a, **k: None)

    def _boom():
        raise RuntimeError("PolyGram down")

    monkeypatch.setattr(brief, "mode_paper", _boom)

    brief.mode_collect()  # must NOT raise

    assert calls["deliver"] == 1  # brief delivered exactly once
    assert (
        calls["cleared"] == 1
    )  # batch cleared despite the trading failure -> no re-collect


# ── Sleeve A: eventId capture ─────────────────────────────────────────────────
def test_gather_candidates_captures_event_id(monkeypatch):
    ev = {
        "id": "evt_hormuz",
        "markets": [
            {
                "id": "2774056",
                "question": "Strait of Hormuz normal by Aug 31?",
                "outcomePrices": '["0.13", "0.87"]',
                "clobTokenIds": '["tokA", "tokB"]',
                "closed": False,
                "endDate": "2026-08-31",
                "umaResolutionStatus": "",
            }
        ],
    }
    monkeypatch.setattr(trading, "polygram_search", lambda q: [ev])
    monkeypatch.setattr(trading, "_signal_search_terms", lambda s: ["hormuz"])
    cands = trading._gather_pg_candidates([{"topic": "hormuz"}])
    assert len(cands) == 1
    assert cands[0]["market_id"] == "2774056"
    assert cands[0]["event_id"] == "evt_hormuz"


def test_sleeve_a_entry_ok_gates(monkeypatch):
    monkeypatch.setattr(common, "PG_A_BAND_LO", 0.75)
    monkeypatch.setattr(common, "PG_A_BAND_HI", 0.92)
    monkeypatch.setattr(common, "PG_A_SPREAD_GATE", 0.03)
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.01)
    assert trading._sleeve_a_entry_ok(0.85, "tok") is True  # in band, tight spread
    assert trading._sleeve_a_entry_ok(0.60, "tok") is False  # below band (longshot)
    assert trading._sleeve_a_entry_ok(0.99, "tok") is False  # above band (crumbs)
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.10)
    assert trading._sleeve_a_entry_ok(0.85, "tok") is False  # spread too wide
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: None)
    assert (
        trading._sleeve_a_entry_ok(0.85, "tok") is False
    )  # unreadable book → fail-closed


def test_open_sleeve_a_live_opens_gated_favorite(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_STAKE", 2.0)
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(
        trading,
        "_gather_pg_candidates",
        lambda s: [
            {
                "market_id": "2774056",
                "question": "Hormuz normal by Aug 31?",
                "yes_price": 0.13,
                "end_date": "2026-08-31",
                "event_id": "evt_h",
            }
        ],
    )
    monkeypatch.setattr(
        trading,
        "run_prediction_matcher",
        lambda s, c: [
            {
                "market_id": "2774056",
                "side": "NO",
                "play_type": "resolution",
                "similarity": 0.8,
                "target": None,
            }
        ],
    )
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(
        trading,
        "_parse_pg_market",
        lambda m: {
            "market_id": "2774056",
            "prices": [0.13, 0.87],
            "yes_price": 0.13,
            "token_ids": ["tokA", "tokB"],
            "closed": False,
            "uma_status": "",
            "end_date": "x",
        },
    )
    monkeypatch.setattr(trading, "_sleeve_a_entry_ok", lambda price, tok: True)
    opened_calls = []

    def fake_open(book, **kw):
        opened_calls.append(kw)
        row = {
            "execution": "live",
            "sleeve": "A",
            "cost_basis": kw["amount"],
            "instrument": kw["market_id"],
            "outcome": kw["outcome"],
            "status": "open",
        }
        book["positions"].append(row)
        return row

    monkeypatch.setattr(polygram_live, "open_live_position", fake_open)
    book = {"positions": []}
    n = trading.open_sleeve_a_live(book, [{"topic": "hormuz"}], "2026-07-21")
    assert n == 1
    kw = opened_calls[0]
    assert kw["sleeve"] == "A" and kw["event_id"] == "evt_h"
    assert (
        kw["market_id"] == "2774056" and kw["token_id"] == "tokB"
    )  # NO → side_index 1
    assert kw["outcome"] == "No" and kw["amount"] == 2.0


def test_open_sleeve_a_live_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", False)
    book = {"positions": []}
    assert trading.open_sleeve_a_live(book, [{"topic": "x"}], "2026-07-21") == 0
    assert book["positions"] == []


def test_open_sleeve_a_live_skips_when_gate_fails(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", True)
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(
        trading,
        "_gather_pg_candidates",
        lambda s: [
            {
                "market_id": "m",
                "question": "q",
                "yes_price": 0.1,
                "end_date": "x",
                "event_id": "evt",
            }
        ],
    )
    monkeypatch.setattr(
        trading,
        "run_prediction_matcher",
        lambda s, c: [
            {
                "market_id": "m",
                "side": "NO",
                "play_type": "resolution",
                "similarity": 0.8,
                "target": None,
            }
        ],
    )
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(
        trading,
        "_parse_pg_market",
        lambda m: {
            "market_id": "m",
            "prices": [0.1, 0.9],
            "yes_price": 0.1,
            "token_ids": ["a", "b"],
            "closed": False,
            "uma_status": "",
            "end_date": "x",
        },
    )
    monkeypatch.setattr(
        trading, "_sleeve_a_entry_ok", lambda price, tok: False
    )  # gate fails
    calls = []
    monkeypatch.setattr(
        polygram_live, "open_live_position", lambda book, **k: calls.append(k)
    )
    book = {"positions": []}
    assert trading.open_sleeve_a_live(book, [{"topic": "x"}], "2026-07-21") == 0
    assert calls == []


def test_sleeve_a_exit_reason(monkeypatch):
    monkeypatch.setattr(common, "PG_A_TAKE", 0.97)
    monkeypatch.setattr(common, "PG_A_STOP", 0.15)
    monkeypatch.setattr(common, "PG_A_TIME_STOP_DAYS", 21)
    monkeypatch.setattr(common, "PG_A_NEAR_DAYS", 10)
    R = trading._sleeve_a_exit_reason
    assert R(0.98, 0.85, 3, 40) == "take"  # repriced to ceiling
    assert R(0.68, 0.85, 3, 40) == "stop"  # 0.85-0.68=0.17 ≥ 0.15 adverse
    assert R(0.86, 0.85, 25, 40) == "time_stop"  # stale, not near settlement
    assert R(0.86, 0.85, 25, 5) is None  # near settlement ⇒ ride it
    assert R(0.86, 0.85, 3, 40) is None  # healthy, hold


def test_sweep_live_exits_closes_on_take(monkeypatch):
    row = {
        "execution": "live",
        "sleeve": "A",
        "status": "open",
        "instrument": "m",
        "outcome": "No",
        "side_index": 1,
        "entry_price": 0.85,
        "cost_basis": 2.0,
        "entry_date": "2026-07-01",
        "end_date": "2026-09-01",
    }
    book = {"positions": [row]}
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(
        trading,
        "_parse_pg_market",
        lambda m: {
            "market_id": "m",
            "prices": [0.02, 0.98],
            "yes_price": 0.02,
            "token_ids": ["a", "b"],
            "closed": False,
            "uma_status": "",
            "end_date": "2026-09-01",
        },
    )
    closed = []

    def fake_close(r, reason):
        r["status"] = "closed"
        r["close_reason"] = reason
        closed.append(reason)
        return True

    monkeypatch.setattr(polygram_live, "close_live_position", fake_close)
    n = trading.sweep_live_exits(book, "2026-07-20")
    assert n == 1 and closed == ["take"] and row["status"] == "closed"


def test_sweep_live_exits_skips_paper_rows(monkeypatch):
    book = {
        "positions": [
            {
                "execution": "paper",
                "status": "open",
                "instrument": "m",
                "asset_class": "prediction",
            }
        ]
    }
    import polygram_live

    monkeypatch.setattr(
        polygram_live,
        "close_live_position",
        lambda r, reason: (_ for _ in ()).throw(AssertionError("paper touched")),
    )
    assert trading.sweep_live_exits(book, "2026-07-20") == 0


def test_mark_to_market_skips_live_rows(monkeypatch):
    called = []
    monkeypatch.setattr(
        trading, "_mtm_prediction", lambda p, td, ts: called.append(p["id"])
    )
    monkeypatch.setattr(
        trading, "mark_to_market", trading.mark_to_market
    )  # ensure real fn under test
    book = {
        "positions": [
            {
                "id": "live1",
                "execution": "live",
                "sleeve": "A",
                "asset_class": "prediction",
                "status": "open",
                "side_index": 1,
                "entry_price": 0.85,
                "entry_date": "2026-07-01",
            },
        ]
    }
    trading.mark_to_market(book, "2026-07-20")
    assert "live1" not in called  # weekly measurement path never touches live rows

"""Signals parsing and normalization — the model-output side of the pipeline.

These functions absorb whatever the model actually emits (mangled delimiters,
prose brackets, synonym enums), so each historical failure mode gets a case.
"""

import logging

import pytest

import brief


SIGNAL = {
    "ticker": "SHEL",
    "topic": "hormuz-disruption",
    "direction": "bullish",
    "thesis_ref": None,
    "confidence": "high",
    "rationale": "Oil supply risk.",
    "provenance": "web_search",
}


# ── normalize_signals ─────────────────────────────────────────────────────────
def test_normalize_passthrough():
    clean, dropped = brief.normalize_signals([dict(SIGNAL)])
    assert dropped == 0
    assert clean[0]["ticker"] == "SHEL"
    assert clean[0]["direction"] == "bullish"


def test_normalize_coerces_synonyms():
    s = dict(SIGNAL, direction="LONG", confidence="med")
    clean, dropped = brief.normalize_signals([s])
    assert dropped == 0
    assert clean[0]["direction"] == "bullish"
    assert clean[0]["confidence"] == "medium"


def test_normalize_drops_unresolvable_and_non_dicts():
    bad_direction = dict(SIGNAL, direction="sideways")
    no_topic = dict(SIGNAL, topic="  ")
    clean, dropped = brief.normalize_signals([bad_direction, no_topic, "junk", 42])
    assert clean == []
    assert dropped == 4


def test_normalize_nulls_nullish_ticker_and_thesis():
    s = dict(SIGNAL, ticker="null", thesis_ref="N/A")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["ticker"] is None
    assert clean[0]["thesis_ref"] is None


def test_normalize_strips_unknown_fields():
    s = dict(SIGNAL, price_target=120, note="extra")
    clean, _ = brief.normalize_signals([s])
    assert set(clean[0]) == {
        "ticker",
        "topic",
        "direction",
        "confidence",
        "thesis_ref",
        "rationale",
        "provenance",
        "asset_class",
        "source_id",
    }


def test_normalize_defaults_asset_class_to_equity():
    s = dict(SIGNAL)  # no asset_class key
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "equity"


def test_normalize_keeps_valid_crypto_asset_class():
    s = dict(SIGNAL, ticker="BTC", asset_class="crypto")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "crypto"


def test_normalize_unknown_asset_class_falls_back_to_equity():
    s = dict(SIGNAL, asset_class="forex")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "equity"


def test_normalize_keeps_source_id():
    s = dict(SIGNAL, source_id="Al Jazeera")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["source_id"] == "Al Jazeera"


def test_normalize_nulls_missing_source_id():
    clean, _ = brief.normalize_signals([dict(SIGNAL)])  # no source_id key
    assert clean[0]["source_id"] is None


def test_signals_request_lists_sources_for_the_model():
    req = brief.build_signals_request(
        "BRIEF", sources=[{"name": "Kyiv Independent", "kind": "regional"}]
    )
    user_text = req["messages"][0]["content"]
    assert "Kyiv Independent" in user_text


# ── Feed source structure ─────────────────────────────────────────────────────
def test_every_feed_has_a_kind():
    valid = {"wire", "analyst", "regional", "primary"}
    for f in brief.RSS_FEEDS:
        assert f.get("kind") in valid, f"{f['name']} missing/invalid kind"
    for s in brief.WEB_SOURCES:
        assert s.get("kind") in valid, f"{s['name']} missing/invalid kind"


def test_sources_diversified_beyond_reuters():
    names = " ".join(f["name"] for f in brief.RSS_FEEDS).lower()
    for needle in ("kyiv", "yonhap", "scmp", "nhk", "38 north", "isw", "al jazeera"):
        assert needle in names, f"expected source containing '{needle}'"


def test_fetch_rss_header_includes_kind(monkeypatch):
    sample = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>Hello</title><description>Body</description>"
        b"<pubDate>Mon, 01 Jan 2026</pubDate></item></channel></rss>"
    )

    # _FakeResp, not a bespoke stub: fetch_rss now inspects status_code to decide
    # whether a failure is retryable, so a stub without one no longer stands in
    # for a Response.
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _FakeResp(200, sample))
    feed = {"name": "Test Wire", "url": "http://x", "category": "geo", "kind": "wire"}
    out = brief.fetch_rss(feed)
    assert "WIRE" in out
    assert "Test Wire" in out


_RSS_SAMPLE = (
    b'<?xml version="1.0"?><rss><channel>'
    b"<item><title>Hello</title><description>Body</description>"
    b"<pubDate>Mon, 01 Jan 2026</pubDate></item></channel></rss>"
)


class _FakeResp:
    """Minimal requests.Response stand-in; raise_for_status mirrors requests'."""

    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise brief.requests.HTTPError(f"{self.status_code} Client Error")


def test_fetch_rss_retries_a_429_and_succeeds(monkeypatch):
    # Nitter rate-limits back-to-back requests, so one of the two X feeds was
    # dropped from the brief most days. A 429 is "come back later", not "gone".
    calls, slept = [], []
    responses = [
        _FakeResp(429, headers={"Retry-After": "2"}),
        _FakeResp(200, _RSS_SAMPLE),
    ]

    def fake_get(*a, **k):
        calls.append(a)
        return responses[len(calls) - 1]

    monkeypatch.setattr(brief.requests, "get", fake_get)
    monkeypatch.setattr(brief.time, "sleep", lambda s: slept.append(s))
    feed = {
        "name": "Papic",
        "url": "http://nitter:8080/x/rss",
        "category": "geo",
        "kind": "analyst",
    }
    out = brief.fetch_rss(feed)
    assert "Hello" in out  # the feed made it into the brief after all
    assert len(calls) == 2
    assert slept == [2.0]  # honoured Retry-After rather than guessing


def test_fetch_rss_gives_up_after_the_retry_budget(monkeypatch):
    calls, slept = [], []

    def fake_get(*a, **k):
        calls.append(a)
        return _FakeResp(429)

    monkeypatch.setattr(brief.requests, "get", fake_get)
    monkeypatch.setattr(brief.time, "sleep", lambda s: slept.append(s))
    feed = {
        "name": "Papic",
        "url": "http://n/x/rss",
        "category": "geo",
        "kind": "analyst",
    }
    assert brief.fetch_rss(feed) == ""  # fail-safe: never blocks the brief
    assert len(calls) == brief.RSS_MAX_ATTEMPTS
    assert slept  # backed off between attempts even with no Retry-After


def test_fetch_rss_does_not_retry_a_403(monkeypatch):
    # A 403 (Mining.com) is a policy refusal — retrying just burns the submit run.
    calls = []

    def fake_get(*a, **k):
        calls.append(a)
        return _FakeResp(403)

    monkeypatch.setattr(brief.requests, "get", fake_get)
    monkeypatch.setattr(brief.time, "sleep", lambda s: pytest.fail("no backoff"))
    feed = {
        "name": "Mining",
        "url": "http://m/feed",
        "category": "macro",
        "kind": "wire",
    }
    assert brief.fetch_rss(feed) == ""
    assert len(calls) == 1


def test_normalize_reclassifies_commodity_tickers_as_index():
    # The extraction schema only offers equity/crypto, so a Brent call can ONLY
    # arrive tagged equity — and then dies in the T212 equity universe with
    # "Paper skip: no instrument for BRENT (equity)". The instrument isn't missing,
    # the class is wrong.
    raw = [dict(SIGNAL, ticker="BRENT", asset_class="equity")]
    clean, _ = brief.normalize_signals(raw)
    assert clean[0]["asset_class"] == "index"


def test_normalize_leaves_real_equities_and_crypto_alone():
    equity = [dict(SIGNAL, ticker="SHEL", asset_class="equity")]
    crypto = [dict(SIGNAL, ticker="BTC", asset_class="crypto")]
    assert brief.normalize_signals(equity)[0][0]["asset_class"] == "equity"
    assert brief.normalize_signals(crypto)[0][0]["asset_class"] == "crypto"


def test_normalize_reclassifies_case_insensitively():
    raw = [dict(SIGNAL, ticker="gold", asset_class="equity")]
    assert brief.normalize_signals(raw)[0][0]["asset_class"] == "index"


def test_fetch_web_source_header_includes_kind(monkeypatch):
    html_page = (
        b"<html><head>"
        b'<meta name="description" content="Some analyst summary">'
        b"</head><body></body></html>"
    )

    class _Resp:
        text = html_page.decode()
        ok = True

        def raise_for_status(self):
            pass

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp())
    source = {
        "name": "BCA Dash",
        "url": "http://x",
        "category": "us",
        "kind": "regional",
    }
    out = brief.fetch_web_source(source)
    assert "[REGIONAL]" in out
    assert "BCA Dash" in out
    assert "Some analyst summary" in out


# ── build_daily_prompt ────────────────────────────────────────────────────────
def _daily_kwargs():
    return dict(
        feed_content="(feeds)",
        web_content="(web)",
        chroma_context="(chroma)",
        yesterday_brief="",
        weekly_summary="",
        fb={"focus": [], "mute": [], "notes": [], "pin": ["iran", "japan"]},
        portfolio="",
    )


def test_prompt_includes_pinned_override_line():
    out = brief.build_daily_prompt(**_daily_kwargs())
    assert "PINNED" in out
    assert "iran" in out and "japan" in out


def test_prompt_has_fixed_spine_and_dynamic_instruction():
    out = brief.build_daily_prompt(**_daily_kwargs())
    assert "TOP STORIES" in out
    assert "MARKET PULSE" in out
    assert "WATCH" in out
    assert "significan" in out.lower()  # dynamic-middle instruction present


def test_top_stories_carry_why_it_matters_lens_and_watch_dedupes():
    out = brief.build_daily_prompt(**_daily_kwargs()).lower()
    # lens pointed at TOP STORIES (the "so what" in-line)
    assert "so what" in out
    assert "market channel" in out
    # WATCH/FORWARD gains the anti-repetition guard
    assert "not already covered in-line" in out


def test_prompt_renders_market_block_when_supplied():
    out = brief.build_daily_prompt(
        market_block="### MARKET PULSE\n- S&P 500: +0.5%", **_daily_kwargs()
    )
    assert "S&P 500: +0.5%" in out


def test_prompt_defaults_pins_when_key_absent():
    kw = _daily_kwargs()
    kw["fb"] = {"focus": [], "mute": [], "notes": []}
    out = brief.build_daily_prompt(**kw)
    assert "ukraine" in out  # default pins surfaced


def test_system_prompt_is_forward_tilted():
    sp = brief.SYSTEM_PROMPT.lower()
    assert "reuters" in sp  # still anchors facts
    assert "forward" in sp or "anticipat" in sp  # explicit forward tilt


def test_system_prompt_teaches_why_it_matters_lens():
    sp = brief.SYSTEM_PROMPT.lower()
    # the three beats are taught...
    assert "non-obvious" in sp  # Context beat
    assert "timeframe" in sp  # Stakes beat (next move + when)
    assert "transmission" in sp  # Connection beat re-pointed to markets
    # ...but NOT as visible output labels
    assert "context / stakes / connection" not in sp


# ── Signals extraction (separate post-gen call) ───────────────────────────────
def test_build_signals_request_forces_emit_signals_tool():
    req = brief.build_signals_request("PROSE BRIEF TEXT")
    assert req["model"] == "claude-sonnet-5"
    # Thinking disabled: tight forced-tool budget must not be eaten by adaptive
    # thinking (the Sonnet 5 default when omitted).
    assert req["thinking"] == {"type": "disabled"}
    assert req["tool_choice"] == {"type": "tool", "name": "emit_signals"}
    assert req["tools"][0]["name"] == "emit_signals"
    assert "PROSE BRIEF TEXT" in req["messages"][0]["content"]
    item = req["tools"][0]["input_schema"]["properties"]["signals"]["items"]
    assert item["properties"]["direction"]["enum"] == ["bullish", "bearish", "neutral"]
    assert item["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert item["properties"]["asset_class"]["enum"] == ["equity", "crypto"]
    assert set(item["required"]) == {
        "asset_class",
        "topic",
        "direction",
        "confidence",
        "rationale",
    }


def test_parse_signals_response_extracts_signal_list():
    resp = {
        "content": [
            {
                "type": "tool_use",
                "name": "emit_signals",
                "input": {"signals": [{"topic": "x", "direction": "bullish"}]},
            }
        ]
    }
    assert brief.parse_signals_response(resp) == [
        {"topic": "x", "direction": "bullish"}
    ]


def test_parse_signals_response_raises_when_no_tool_block():
    resp = {"content": [{"type": "text", "text": "no tool call here"}]}
    try:
        brief.parse_signals_response(resp)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_extract_signals_ok_path_returns_signals_and_ok():
    def fake_call(payload):
        assert payload["tool_choice"]["name"] == "emit_signals"
        return {
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_signals",
                    "input": {"signals": [{"topic": "oil", "direction": "bullish"}]},
                }
            ]
        }

    raw_signals, status = brief.extract_signals("BRIEF", call=fake_call)
    assert status == "ok"
    assert raw_signals == [{"topic": "oil", "direction": "bullish"}]


def test_extract_signals_failsafe_on_call_exception():
    def boom(payload):
        raise RuntimeError("HTTP 529 overloaded")

    raw_signals, status = brief.extract_signals("BRIEF", call=boom)
    assert raw_signals == []
    assert status == "extract_error"


def test_extract_signals_failsafe_on_missing_tool_block():
    def no_tool(payload):
        return {"content": [{"type": "text", "text": "sorry"}]}

    raw_signals, status = brief.extract_signals("BRIEF", call=no_tool)
    assert raw_signals == []
    assert status == "extract_error"


def test_build_signals_request_has_output_headroom():
    # The 2048 ceiling truncated the emit_signals tool call on signal-rich briefs
    # once Sonnet 5's tokenizer inflated the JSON (~30% more tokens). Give the tool
    # call room to finish the array, matching the verify-call bump.
    payload = brief.build_signals_request("BRIEF")
    assert payload["max_tokens"] >= 8192


def test_extract_signals_logs_stop_reason_and_usage(caplog):
    def fake_call(payload):
        return {
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1234, "output_tokens": 567},
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_signals",
                    "input": {"signals": []},
                }
            ],
        }

    with caplog.at_level(logging.INFO, logger="newsbrief"):
        _raw, status = brief.extract_signals("BRIEF", call=fake_call)
    assert status == "ok"
    assert "stop_reason=end_turn" in caplog.text
    assert "out=567" in caplog.text


def test_extract_signals_logs_stop_reason_on_truncation(caplog):
    # A max_tokens-truncated tool call returns an emit_signals block with no valid
    # signals list. We must still log stop_reason so truncation is observable rather
    # than silently swallowed by the fail-safe.
    def truncated(payload):
        return {
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 5000, "output_tokens": 8192},
            "content": [{"type": "tool_use", "name": "emit_signals", "input": {}}],
        }

    with caplog.at_level(logging.INFO, logger="newsbrief"):
        _raw, status = brief.extract_signals("BRIEF", call=truncated)
    assert status == "extract_error"
    assert "stop_reason=max_tokens" in caplog.text


def test_daily_prompt_drops_signals_json_but_keeps_prose_section():
    prompt = brief.build_daily_prompt(**_daily_kwargs())
    assert "@@@SIGNALS@@@" not in prompt
    assert "JSON array" not in prompt
    # human-readable section stays
    assert "POSITION SIGNALS" in prompt
    # word-limit instruction is preserved
    assert "under 600 words" in prompt


def test_post_messages_uses_generous_timeout_and_retries_transient(monkeypatch):
    captured = {"n": 0, "timeouts": []}

    class _OK:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["n"] += 1
        captured["timeouts"].append(timeout)
        if captured["n"] == 1:
            raise brief.requests.exceptions.Timeout("read timed out")
        return _OK()

    monkeypatch.setattr(brief.requests, "post", fake_post)
    out = brief._post_messages({"model": "x"})
    assert out == {"content": []}
    assert captured["n"] == 2  # retried after the first timeout
    assert captured["timeouts"][0] >= 60  # generous timeout, not the old 30s


def test_post_messages_raises_after_exhausting_retries(monkeypatch):
    def always_timeout(url, headers=None, json=None, timeout=None):
        raise brief.requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(brief.requests, "post", always_timeout)
    raised = False
    try:
        brief._post_messages({"model": "x"})
    except brief.requests.exceptions.Timeout:
        raised = True
    assert raised  # propagates so extract_signals fails safe to extract_error


# ── source-tag resolver ───────────────────────────────────────────────────────
def test_source_tag_index_includes_hardcoded_feeds():
    index = brief._source_tag_index()
    # every RSS feed name resolves to its own kind
    for f in brief.RSS_FEEDS:
        assert index[f["name"]]["kind"] == f.get("kind", "wire")


def test_resolve_known_source_returns_kind_and_perspective():
    index = {"Al Jazeera": {"kind": "regional", "perspective": "ARAB"}}
    tags = brief.resolve_source_tags("Al Jazeera", index)
    assert tags == {"kind": "regional", "perspective": "ARAB"}


def test_resolve_unknown_source_is_unknown():
    assert brief.resolve_source_tags("Nonesuch", {}) == {
        "kind": "unknown",
        "perspective": None,
    }


def test_resolve_none_source_is_unknown():
    assert brief.resolve_source_tags(None, {"X": {"kind": "wire"}}) == {
        "kind": "unknown",
        "perspective": None,
    }


def test_hardcoded_feed_wins_over_temp_on_name_collision(monkeypatch):
    monkeypatch.setattr(
        brief,
        "load_temp_sources",
        lambda: [{"name": brief.RSS_FEEDS[0]["name"], "kind": "regional"}],
    )
    index = brief._source_tag_index()
    assert index[brief.RSS_FEEDS[0]["name"]]["kind"] == brief.RSS_FEEDS[0].get(
        "kind", "wire"
    )


def test_annotate_signal_sources_sets_kind_and_perspective(monkeypatch):
    monkeypatch.setattr(
        brief,
        "_source_tag_index",
        lambda: {"Al Jazeera": {"kind": "regional", "perspective": "ARAB"}},
    )
    sigs = [
        {"topic": "t", "source_id": "Al Jazeera"},
        {"topic": "u", "source_id": "Nonesuch"},
        {"topic": "v"},  # no source_id
    ]
    out = brief.annotate_signal_sources(sigs)
    assert out[0]["source_kind"] == "regional"
    assert out[0]["source_perspective"] == "ARAB"
    assert out[1]["source_kind"] == "unknown"
    assert out[2]["source_kind"] == "unknown"
    assert out[2]["source_perspective"] is None

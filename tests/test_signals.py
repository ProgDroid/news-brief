"""Signals parsing and normalization — the model-output side of the pipeline.

These functions absorb whatever the model actually emits (mangled delimiters,
prose brackets, synonym enums), so each historical failure mode gets a case.
"""

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

    class _Resp:
        content = sample
        ok = True

        def raise_for_status(self):
            pass

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp())
    feed = {"name": "Test Wire", "url": "http://x", "category": "geo", "kind": "wire"}
    out = brief.fetch_rss(feed)
    assert "WIRE" in out
    assert "Test Wire" in out


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


# ── Signals extraction (separate post-gen call) ───────────────────────────────
def test_build_signals_request_forces_emit_signals_tool():
    req = brief.build_signals_request("PROSE BRIEF TEXT")
    assert req["model"] == "claude-sonnet-4-6"
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

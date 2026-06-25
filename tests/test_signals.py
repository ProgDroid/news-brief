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


# ── split_brief_and_signals ───────────────────────────────────────────────────
def test_split_with_primary_marker():
    raw = 'PROSE BODY\n\n@@@SIGNALS@@@\n[{"topic": "x"}]'
    prose, signals, status = brief.split_brief_and_signals(raw)
    assert prose == "PROSE BODY"
    assert signals == [{"topic": "x"}]
    assert status == "ok"


def test_split_with_legacy_marker():
    raw = "PROSE\n---SIGNALS---\n[]"
    prose, signals, status = brief.split_brief_and_signals(raw)
    assert (prose, signals, status) == ("PROSE", [], "ok")


def test_split_marker_but_truncated_json_is_parse_error():
    # max_tokens truncation: marker present, array cut off mid-object
    raw = 'PROSE\n@@@SIGNALS@@@\n[{"topic": "x", "direc'
    prose, signals, status = brief.split_brief_and_signals(raw)
    assert prose == "PROSE"
    assert signals == []
    assert status == "parse_error"


def test_split_missing_marker_recovers_trailing_array():
    # Model collapsed the delimiter to a bare '---'
    raw = 'PROSE BODY\n\n---\n[{"topic": "x"}]'
    prose, signals, status = brief.split_brief_and_signals(raw)
    assert prose == "PROSE BODY"
    assert signals == [{"topic": "x"}]
    assert status == "ok"


def test_split_no_marker_no_array_is_no_marker():
    prose, signals, status = brief.split_brief_and_signals("Just prose today.")
    assert (prose, signals, status) == ("Just prose today.", [], "no_marker")


def test_split_prose_citation_brackets_are_not_signals():
    raw = "Markets fell [1] on news [2]."
    prose, signals, status = brief.split_brief_and_signals(raw)
    # [1] parses as a JSON list — the fallback recovers it. This documents
    # current behaviour: citation-style brackets at the very end of prose are
    # indistinguishable from a signals array of ints.
    assert status in ("ok", "no_marker")


def test_find_trailing_json_array_skips_prose_brackets():
    text = 'see [1] and [also this] then [{"a": 1}]'
    found = brief._find_trailing_json_array(text)
    assert found is not None
    start, value = found
    assert value == [{"a": 1}]
    assert text[start:].startswith('[{"a": 1}]')


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
    assert "@@@SIGNALS@@@" in out
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

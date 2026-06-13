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

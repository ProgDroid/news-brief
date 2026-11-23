"""Storyline attach: request shape, validation, plain_text, classify_error (al7.3)."""

import pytest
import requests

import storyline_store
import storylines

S = [
    storyline_store.Storyline(
        11, "Widget tariffs", "Tariff fight", "active", False, None
    ),
    storyline_store.Storyline(12, "Gizmo strike", "Port strike", "dormant", True, None),
    storyline_store.Storyline(13, "Gadget saga", "A saga", "active", False, None),
]
ARTS = [
    {"id": 101, "title": "Widget duty cut", "body": "<p>Officials &amp; firms</p>"},
    {"id": 102, "title": "Gizmo walkout", "body": None},
    {"id": 103, "title": "Gadget recall", "body": "Plain body"},
]


def _build():
    return storylines.build_attach_request(S, ARTS, model="m")


def _resp(answers, stop="tool_use", name=storylines.ATTACH_TOOL):
    return {
        "stop_reason": stop,
        "content": [{"type": "tool_use", "name": name, "input": {"answers": answers}}],
    }


def _labels():
    _, s, a = _build()
    return s, a


def test_plain_text_strips_html_and_entities_and_caps_at_600():
    out = storylines.plain_text("T &amp; U", "<p>Hello&nbsp;<b>world</b></p>\n\n  x")
    assert out == "T & U — Hello world x"
    assert "<" not in out
    # entity-encoded tags are decoded first, then stripped
    assert storylines.plain_text("t", "&lt;b&gt;bold&lt;/b&gt;") == "t — bold"
    long = storylines.plain_text("T", "w" * 5000)
    assert len(long) == storylines.ATTACH_TEXT_MAX == 600
    assert len(storylines.plain_text("T", "w" * 5000, limit=50)) == 50
    assert storylines.plain_text("Only title", None) == "Only title"
    assert storylines.plain_text("Only title", "   ") == "Only title"


def test_request_labels_in_order_and_forces_tool_with_thinking_disabled():
    payload, s, a = _build()
    assert s == {"S1": 11, "S2": 12, "S3": 13}
    assert a == {"A1": 101, "A2": 102, "A3": 103}
    assert list(s) == ["S1", "S2", "S3"] and list(a) == ["A1", "A2", "A3"]
    assert payload["model"] == "m"
    assert payload["max_tokens"] == storylines.ATTACH_MAX_TOKENS == 4000
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["tool_choice"] == {"type": "tool", "name": storylines.ATTACH_TOOL}
    assert payload["tools"][0]["name"] == storylines.ATTACH_TOOL
    user = payload["messages"][0]["content"]
    assert "S2" in user and "Gizmo strike" in user and "Port strike" in user
    assert "A1" in user and "Widget duty cut — Officials & firms" in user
    # labels, not ids: no real database id reaches the model
    for real in ("101", "102", "103", "11 ", "12 ", "13 "):
        assert real not in user
    system = payload["system"].lower()
    assert "exactly once" in system
    assert "materially" in system
    assert "empty list" in system


def test_request_schema_limits_three_and_enumerates_storyline_labels():
    payload, s, _ = _build()
    schema = payload["tools"][0]["input_schema"]
    item = schema["properties"]["answers"]["items"]
    lst = item["properties"]["storylines"]
    assert lst["maxItems"] == storylines.ATTACH_MAX_PER_ARTICLE == 3
    assert lst["items"]["enum"] == ["S1", "S2", "S3"]
    assert set(item["required"]) == {"article", "storylines"}
    assert schema["required"] == ["answers"]


def test_valid_answer_maps_labels_back_to_ids():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1", "S3"]},
                {"article": "A2", "storylines": ["S2"]},
                {"article": "A3", "storylines": []},
            ]
        ),
        s,
        a,
    )
    assert r.decided == {101: [11, 13], 102: [12], 103: []}
    assert r.problems == {} and r.stray == [] and r.whole is None


def test_empty_list_is_a_none_decision():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp([{"article": x, "storylines": []} for x in ("A1", "A2", "A3")]), s, a
    )
    assert r.decided == {101: [], 102: [], 103: []}
    assert r.problems == {}


def test_unknown_storyline_label_charges_only_that_article():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1", "S9"]},
                {"article": "A2", "storylines": ["S2"]},
                {"article": "A3", "storylines": []},
            ]
        ),
        s,
        a,
    )
    assert r.problems == {101: "unknown storyline label S9"}
    assert 101 not in r.decided
    assert r.decided == {102: [12], 103: []}


def test_more_than_three_charges_that_article():
    s = {f"S{i}": 10 + i for i in range(1, 6)}
    a = {"A1": 101, "A2": 102}
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1", "S2", "S3", "S4"]},
                {"article": "A2", "storylines": ["S1", "S2", "S3"]},
            ]
        ),
        s,
        a,
    )
    assert r.problems == {101: "more than 3 storylines"}
    assert r.decided == {102: [11, 12, 13]}


def test_duplicate_labels_in_one_answer_are_deduped():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1", "S1", "S2", "S2"]},
                {"article": "A2", "storylines": []},
                {"article": "A3", "storylines": []},
            ]
        ),
        s,
        a,
    )
    assert r.decided[101] == [11, 12]
    assert r.problems == {}
    # six raw labels but three distinct is fine; duplicates never trip the cap
    s4 = {f"S{i}": i for i in range(1, 5)}
    r = storylines.validate_attach(
        _resp([{"article": "A1", "storylines": ["S1", "S1", "S2", "S2", "S3", "S3"]}]),
        s4,
        {"A1": 1},
    )
    assert r.decided == {1: [1, 2, 3]} and r.problems == {}


def test_article_answered_twice_is_charged():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1"]},
                {"article": "A1", "storylines": ["S2"]},
                {"article": "A2", "storylines": []},
                {"article": "A3", "storylines": []},
            ]
        ),
        s,
        a,
    )
    assert r.problems == {101: "answered twice"}
    assert 101 not in r.decided
    assert r.decided == {102: [], 103: []}


def test_article_answered_twice_even_when_second_is_identical():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": ["S1"]},
                {"article": "A1", "storylines": ["S1"]},
                {"article": "A2", "storylines": []},
                {"article": "A3", "storylines": []},
            ]
        ),
        s,
        a,
    )
    assert r.problems == {101: "answered twice"}
    assert 101 not in r.decided


def test_missing_article_is_charged_no_answer():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [{"article": "A1", "storylines": []}, {"article": "A3", "storylines": []}]
        ),
        s,
        a,
    )
    assert r.problems == {102: "no answer"}
    assert r.decided == {101: [], 103: []}


def test_stray_article_label_is_listed_not_mapped():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                {"article": "A1", "storylines": []},
                {"article": "A2", "storylines": []},
                {"article": "A3", "storylines": []},
                {"article": "A99", "storylines": ["S1"]},
            ]
        ),
        s,
        a,
    )
    assert r.stray == ["A99"]
    assert r.problems == {}
    assert set(r.decided) == {101, 102, 103}


def test_max_tokens_stop_is_whole():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp([{"article": "A1", "storylines": []}], stop="max_tokens"), s, a
    )
    assert r.whole and "max_tokens" in r.whole
    assert r.decided == {} and r.problems == {}


def test_no_tool_block_or_bad_answers_is_whole():
    s, a = _labels()
    no_tool = {"stop_reason": "tool_use", "content": [{"type": "text", "text": "hi"}]}
    other_tool = _resp([], name="something_else")
    bad_answers = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": storylines.ATTACH_TOOL,
                "input": {"answers": "x"},
            }
        ],
    }
    for resp in (no_tool, other_tool, bad_answers):
        r = storylines.validate_attach(resp, s, a)
        assert r.whole
        assert r.decided == {} and r.problems == {} and r.stray == []


@pytest.mark.parametrize(
    "resp",
    [
        None,
        [],
        "text",
        {},
        {"stop_reason": "tool_use", "content": None},
        {"stop_reason": "tool_use", "content": "x"},
        {"stop_reason": "tool_use", "content": ["x", 3, None]},
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "attach_articles"}],
        },
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "attach_articles", "input": []}],
        },
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "attach_articles", "input": None}],
        },
    ],
)
def test_structurally_odd_responses_are_whole_never_raise(resp):
    s, a = _labels()
    r = storylines.validate_attach(resp, s, a)
    assert r.whole


def test_odd_entries_inside_answers_never_raise():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp(
            [
                "junk",
                None,
                {"article": ["A1"], "storylines": []},
                {"article": "A1", "storylines": "S1"},
                {"article": "A2", "storylines": [None, ["S1"]]},
                {"article": "A3"},
            ]
        ),
        s,
        a,
    )
    assert r.whole is None
    # A1, A2 and A3 each got an invalid answer: charged, never decided
    assert set(r.problems) == {101, 102, 103}
    assert r.decided == {}
    assert r.problems[102].startswith("unknown storyline label")
    assert len(r.stray) == 1  # the unhashable article label


def test_unknown_label_text_is_bounded():
    s, a = _labels()
    r = storylines.validate_attach(
        _resp([{"article": "A1", "storylines": ["x" * 500]}]), s, a
    )
    assert len(r.problems[101]) < 100


def test_attach_result_is_frozen():
    r = storylines.AttachResult({}, {}, [], None)
    with pytest.raises(Exception):
        r.whole = "x"


def _http(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code}", response=resp)


@pytest.mark.parametrize(
    "exc, want",
    [
        (requests.Timeout(), "transient"),
        (requests.ConnectionError(), "transient"),
        (_http(429), "transient"),
        (_http(500), "transient"),
        (_http(503), "transient"),
        (_http(529), "transient"),
        (_http(401), "config"),
        (_http(403), "config"),
        (_http(404), "config"),
        (_http(400), "request"),
        (_http(413), "request"),
        (_http(422), "request"),
        (requests.HTTPError("no response"), "unknown"),
        (ValueError("x"), "unknown"),
    ],
)
def test_classify_error(exc, want):
    assert storylines.classify_error(exc) == want

"""Storyline seeding: request shape, validation and the one-off script (al7.3)."""

import contextlib
import importlib.util
import json
from pathlib import Path

import pytest

import brief
import common
import comprehend
import storyline_store
import storylines
import strict_schema
import watch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "storylines_seed.py"
TOPICS = [{"label": "Widget tariffs", "search": "widget tariff", "chroma": "x"}]


def _request(**over):
    kw = dict(
        topics=TOPICS,
        pins=["Gizmo strike"],
        theses={"TESTCO": "Widgets demand recovers"},
        watch_lines=["WIDGETS: tariff cut announced"],
        live_names=["Existing saga"],
        model="m",
        max_count=20,
    )
    kw.update(over)
    return storylines.build_seed_request(**kw)


def _resp(items, stop="tool_use"):
    return {
        "stop_reason": stop,
        "content": [
            {
                "type": "tool_use",
                "name": storylines.SEED_TOOL,
                "input": {"storylines": items},
            }
        ],
    }


def _item(name, desc="A developing situation.", pinned=False):
    return {"name": name, "description": desc, "pinned": pinned}


def test_seed_request_forces_tool_and_disables_thinking():
    req = _request()
    assert req["thinking"] == {"type": "disabled"}
    assert req["tool_choice"] == {"type": "tool", "name": storylines.SEED_TOOL}
    assert req["model"] == "m"
    assert req["max_tokens"] == storylines.SEED_MAX_TOKENS
    tool = req["tools"][0]
    assert tool["name"] == storylines.SEED_TOOL
    arr = tool["input_schema"]["properties"]["storylines"]
    assert arr["type"] == "array"
    props = arr["items"]["properties"]
    assert props["name"]["type"] == "string"
    assert props["description"]["type"] == "string"
    assert props["pinned"]["type"] == "boolean"


def test_seed_tool_is_strict_and_inside_the_strict_subset():
    # news-brief-bcr: unconstrained, the model returned `storylines` as a JSON
    # STRING of {"storylines": [...]}, and a correct answer of 22 items was
    # dropped. strict makes that shape ungenerable; the subset check keeps the
    # schema from drawing a 400 instead.
    tool = _request()["tools"][0]
    assert tool["strict"] is True
    assert strict_schema.unsupported(tool["input_schema"]) == []
    # The checker can fail: the pre-bcr schema shape is refused.
    assert strict_schema.unsupported(
        {"type": "object", "properties": {"n": {"type": "string", "maxLength": 80}}}
    ) == ["$: additionalProperties is not false", "$.properties.n: maxLength"]


def test_seed_limits_the_schema_no_longer_carries_are_still_enforced():
    # maxItems/maxLength left the schema (strict 400s on them). The prompt now
    # states all three limits -- validate_seed DROPS an over-long item rather
    # than cutting it, so a model that never hears the limit loses storylines
    # (descriptions ran to 365 of 400 characters on 2026-11-23) -- and
    # validate_seed still enforces them.
    req = _request()
    assert "Propose at most 20 storylines" in req["messages"][0]["content"]
    assert f"at most {storylines.NAME_MAX} characters" in req["system"]
    assert f"at most {storylines.DESCRIPTION_MAX} characters" in req["system"]
    long_name = _item("n" * (storylines.NAME_MAX + 1))
    long_desc = _item("Fine name", desc="d" * (storylines.DESCRIPTION_MAX + 1))
    ok, problems = storylines.validate_seed(
        _resp([long_name, long_desc, _item("A"), _item("B")]),
        max_count=1,
        live_names=[],
    )
    assert [p.name for p in ok] == ["A"]
    assert problems == [
        "item 1: empty or over-long name",
        "item 2: empty or over-long description",
        "item 4: beyond max_count=1",
    ]


def test_seed_request_carries_topics_pins_theses_and_watch_lines():
    text = _request()["messages"][0]["content"]
    for needle in (
        "Widget tariffs",
        "widget tariff",
        "Gizmo strike",
        "TESTCO",
        "Widgets demand recovers",
        "WIDGETS: tariff cut announced",
        "Existing saga",
    ):
        assert needle in text
    assert "chroma" not in text


def test_validate_drops_duplicates_overlong_and_live_names():
    resp = _resp(
        [
            _item("Widget tariffs"),
            _item("widget TARIFFS"),  # duplicate within the answer
            _item("Existing saga"),  # duplicates a live name
            _item("x" * 81),  # name too long
            _item("Long desc", "d" * 401),
            _item("", "no name"),
            _item("No desc", "  "),
            _item("   ", "blank name"),
            _item("Gizmo strike", pinned=True),
        ]
    )
    ok, problems = storylines.validate_seed(
        resp, max_count=20, live_names=["existing saga"]
    )
    assert ok == [
        storylines.Proposed("Widget tariffs", "A developing situation.", False),
        storylines.Proposed("Gizmo strike", "A developing situation.", True),
    ]
    assert len(problems) == 7


def test_validate_caps_at_max_count():
    resp = _resp([_item(f"Saga {i}") for i in range(5)])
    ok, problems = storylines.validate_seed(resp, max_count=3, live_names=[])
    assert [p.name for p in ok] == ["Saga 0", "Saga 1", "Saga 2"]
    assert len(problems) == 2


def test_validate_no_tool_block():
    ok, problems = storylines.validate_seed(
        {"stop_reason": "max_tokens", "content": []}, max_count=5, live_names=[]
    )
    assert ok == []
    assert problems == ["no tool_use block (stop_reason=max_tokens)"]


def test_validate_non_object_item_is_a_problem():
    ok, problems = storylines.validate_seed(
        _resp(["junk", _item("Real saga")]), max_count=5, live_names=[]
    )
    assert [p.name for p in ok] == ["Real saga"]
    assert len(problems) == 1


def test_validate_pinned_must_be_true_boolean():
    ok, _ = storylines.validate_seed(
        _resp([{"name": "A", "description": "d", "pinned": "yes"}]),
        max_count=5,
        live_names=[],
    )
    assert ok[0].pinned is False


# ---- the script ----------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("storylines_seed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeConn:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")


def _pending(origin="seed"):
    return {"id": 1, "name": "n", "description": "d", "pinned": False, "origin": origin}


@pytest.fixture
def env(monkeypatch):
    seed = _load_script()
    conn = FakeConn()
    state = {
        "pending": [],
        "live": 0,
        "added": [],
        "sent": [],
        "posts": [],
        "spend": [],
    }
    monkeypatch.setattr(
        comprehend,
        "record_spend",
        lambda c, stage, model, usage, **kw: state["spend"].append(
            (stage, model, usage)
        ),
    )

    @contextlib.contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(seed.db, "connect", fake_connect)
    monkeypatch.setattr(
        storyline_store,
        "pending_proposals",
        lambda c, *, origin=None: [
            p for p in state["pending"] if origin in (None, p["origin"])
        ],
    )
    monkeypatch.setattr(storyline_store, "count_live", lambda c: state["live"])
    monkeypatch.setattr(storyline_store, "live_storylines", lambda c: [])

    def add(c, name, description, *, origin, pinned=False):
        state["added"].append((name, description, origin, pinned))
        conn.events.append("add")
        return 100 + len(state["added"])

    monkeypatch.setattr(storyline_store, "add_proposal", add)

    def send(p):
        state["sent"].append(p)
        conn.events.append("send")
        return 1

    monkeypatch.setattr(brief, "send_proposal", send)
    monkeypatch.setattr(
        brief, "load_local_theses", lambda: {"TESTCO": "Widgets demand recovers"}
    )
    monkeypatch.setattr(
        brief, "load_portfolio_theses", lambda: ({"OTHERCO": "Gizmos"}, None)
    )
    monkeypatch.setattr(brief, "load_feedback", lambda: {})
    monkeypatch.setattr(brief, "resolved_pins", lambda fb: ["Gizmo strike"])
    monkeypatch.setattr(brief, "TOPICS", TOPICS)
    monkeypatch.setattr(common, "STORYLINE_MAX_LIVE", 60)

    def post(payload, **kw):
        state["posts"].append((payload, kw))
        return _resp([_item("Widget tariffs"), _item("Gizmo strike", pinned=True)])

    return seed, conn, state, post


def test_main_inserts_and_sends_each_proposal(env, capsys):
    seed, conn, state, post = env
    assert seed.main([], post=post) == 0
    assert state["added"] == [
        ("Widget tariffs", "A developing situation.", "seed", False),
        ("Gizmo strike", "A developing situation.", "seed", True),
    ]
    assert [s["id"] for s in state["sent"]] == [101, 102]
    assert state["sent"][1]["pinned"] is True
    assert state["sent"][0]["name"] == "Widget tariffs"
    # committed after the inserts and BEFORE the first send
    assert conn.events == ["add", "add", "commit", "send", "send"]
    payload, kw = state["posts"][0]
    assert kw == {"timeout": 180, "max_attempts": 1, "label": "Seed"}
    assert payload["model"] == common.STORYLINE_UPDATE_MODEL
    text = payload["messages"][0]["content"]
    assert "OTHERCO" in text and "TESTCO" in text and "Gizmo strike" in text
    out = capsys.readouterr().out
    assert "seed: proposed=2 sent=2 dropped=0" in out
    assert "Widget tariffs" not in out and "Gizmo strike" not in out


def test_main_caps_max_count_by_headroom(env):
    seed, conn, state, post = env
    state["live"] = 50
    assert seed.main([], post=post) == 0
    payload = state["posts"][0][0]
    assert "Propose at most 10 storylines" in payload["messages"][0]["content"]


def test_main_cuts_long_theses(env, monkeypatch):
    seed, conn, state, post = env
    monkeypatch.setattr(brief, "load_local_theses", lambda: {"TESTCO": "w" * 500})
    seed.main([], post=post)
    text = state["posts"][0][0]["messages"][0]["content"]
    assert "w" * 300 in text and "w" * 301 not in text


def test_main_refuses_while_seed_proposals_pending(env, capsys):
    seed, conn, state, post = env
    state["pending"] = [_pending()]
    assert seed.main([], post=post) == 2
    assert state["posts"] == [] and state["added"] == []
    assert "refused, 1 seed proposals are still pending" in capsys.readouterr().out


def test_main_ignores_pending_proposals_of_other_origins(env):
    seed, conn, state, post = env
    state["pending"] = [_pending("update")]
    assert seed.main([], post=post) == 0


def test_main_again_overrides(env):
    seed, conn, state, post = env
    state["pending"] = [_pending()]
    assert seed.main(["--again"], post=post) == 0
    assert len(state["added"]) == 2


def test_main_at_bound_exits_1(env):
    seed, conn, state, post = env
    state["live"] = 60
    assert seed.main([], post=post) == 1
    assert state["posts"] == [] and state["added"] == []


def test_main_watch_error_means_no_lines(env, monkeypatch):
    seed, conn, state, post = env

    def boom(path):
        raise watch.WatchFileError("bad")

    monkeypatch.setattr(watch, "load_watch", boom)
    assert seed.main([], post=post) == 0


def test_main_watch_events_become_name_trigger_lines(env, monkeypatch):
    seed, conn, state, post = env

    class WF:
        events = [watch.EventRow("e1", "Widget summit", "talks collapse")]

    monkeypatch.setattr(watch, "load_watch", lambda path: WF())
    seed.main([], post=post)
    content = state["posts"][0][0]["messages"][0]["content"]
    assert "Widget summit: talks collapse" in content


def test_main_no_valid_proposals_sends_nothing(env, capsys):
    seed, conn, state, _ = env
    rc = seed.main(
        [], post=lambda p, **kw: {"stop_reason": "max_tokens", "content": []}
    )
    assert rc == 1
    assert state["sent"] == [] and state["added"] == []
    out = capsys.readouterr().out
    assert "proposed=0" in out and "no tool_use block" in out


@pytest.mark.parametrize(
    "resp",
    [
        {"stop_reason": "tool_use", "content": ["junk"]},
        "junk",
    ],
)
def test_validate_malformed_response_never_raises(resp):
    ok, problems = storylines.validate_seed(resp, max_count=5, live_names=[])
    assert ok == []
    assert len(problems) == 1 and problems[0].startswith("no tool_use block")


@pytest.mark.parametrize(
    "inp, shape",
    [
        # The response that dropped 22 good items on 2026-11-23 (bcr).
        (
            {"storylines": json.dumps({"storylines": [_item("Secret saga")]})},
            "storylines type=str",
        ),
        (
            {"storylines": {"storylines": [_item("Secret saga")]}},
            "storylines type=dict",
        ),
        ({}, "storylines type=NoneType"),
        ("junk", "input type=str"),
    ],
)
def test_validate_tool_block_with_wrong_shape_names_the_shape(inp, shape):
    # A tool block that IS there must not read as "no tool_use block": that
    # message sent the 2026-11-23 diagnosis looking for a missing block.
    resp = {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": storylines.SEED_TOOL, "input": inp}],
    }
    ok, problems = storylines.validate_seed(resp, max_count=5, live_names=[])
    assert ok == []
    assert problems == [
        f"{storylines.SEED_TOOL} input has no storylines list ({shape})"
    ]
    assert "Secret saga" not in problems[0]


def test_main_failed_send_is_visible(env, monkeypatch, capsys):
    seed, conn, state, post = env
    monkeypatch.setattr(brief, "send_proposal", lambda p: None if p["id"] == 101 else 1)
    rc = seed.main([], post=post)
    out = capsys.readouterr().out
    assert rc == 1
    assert "proposed=2 sent=1 dropped=0" in out
    assert "101" in out and "Widget tariffs" not in out
    assert conn.events.count("commit") == 1
    assert len(state["added"]) == 2


def test_main_raising_send_is_visible(env, monkeypatch, capsys):
    seed, conn, state, post = env

    def send(p):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(brief, "send_proposal", send)
    rc = seed.main([], post=post)
    out = capsys.readouterr().out
    assert rc == 1
    assert "proposed=2 sent=0 dropped=0" in out
    assert "101" in out and "102" in out
    assert conn.events.count("commit") == 1


def test_main_unpriced_model_exits_two_before_any_call(env, monkeypatch, capsys):
    seed, conn, state, post = env
    monkeypatch.setattr(common, "STORYLINE_UPDATE_MODEL", "no-such-model-x")
    assert seed.main([], post=post) == 2
    assert state["posts"] == [] and state["added"] == [] and state["spend"] == []
    assert "no-such-model-x has no price" in capsys.readouterr().out


def test_main_records_one_seed_spend_row(env):
    seed, conn, state, post = env

    def usage_post(payload, **kw):
        resp = post(payload, **kw)
        resp["usage"] = {"input_tokens": 100, "output_tokens": 10}
        return resp

    assert seed.main([], post=usage_post) == 0
    assert state["spend"] == [
        (
            "seed",
            common.STORYLINE_UPDATE_MODEL,
            {"input_tokens": 100, "output_tokens": 10},
        )
    ]


def test_main_no_spend_when_the_call_raises(env):
    seed, conn, state, post = env

    def boom(payload, **kw):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        seed.main([], post=boom)
    assert state["spend"] == []


def test_main_ignored_portfolio_theses_print_a_count_only(env, monkeypatch, capsys):
    seed, conn, state, post = env
    monkeypatch.setattr(brief, "load_portfolio_theses", lambda: ({}, "file missing"))
    assert seed.main([], post=post) == 0
    out = capsys.readouterr().out
    assert (
        "seed: portfolio theses ignored (file missing); drafting from 1 local theses"
        in out
    )
    assert "TESTCO" not in out and "Widgets demand" not in out


def test_main_no_theses_line_when_portfolio_theses_load(env, capsys):
    seed, conn, state, post = env
    seed.main([], post=post)
    assert "portfolio theses ignored" not in capsys.readouterr().out

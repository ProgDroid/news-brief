"""The attach job (plan 2a, task 6): wiring with stubs, plus one DB-gated run.

Storylines and headlines are invented; the repo is public.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

import brief
import comprehend
import common
import db
import scheduler
import storylines
import storyline_store as ss
from storyline_store import Storyline

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
MODEL = "claude-haiku-5-5"
USAGE = {"input_tokens": 100, "output_tokens": 20}


class FakeConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _story(i):
    return Storyline(i, f"Widget tariffs {i}", "invented", "active", False, None)


def _items(n, start=1):
    return [
        {"id": i, "title": f"Gizmo strike {i}", "body": "b"}
        for i in range(start, start + n)
    ]


def _resp(answers, usage=USAGE):
    return {
        "stop_reason": "tool_use",
        "usage": usage,
        "content": [
            {
                "type": "tool_use",
                "name": storylines.ATTACH_TOOL,
                "input": {"answers": answers},
            }
        ],
    }


def _answer_all(payload, labels_for=("S1",)):
    """Answer every article the request carries with the given labels."""
    text = payload["messages"][0]["content"]
    arts = re.findall(r"(?m)^(A\d+):", text)
    return _resp([{"article": a, "storylines": list(labels_for)} for a in arts])


def _http(code):
    r = requests.Response()
    r.status_code = code
    return requests.HTTPError(f"HTTP {code}", response=r)


class Rec:
    """Stubs the store and spend, recording what the pass wrote."""

    def __init__(self, monkeypatch, *, stories=2, items=None):
        self.decisions, self.failures, self.deferrals, self.spend = [], [], [], []
        self.select_kwargs = None
        self.stories = [_story(i) for i in range(1, stories + 1)]
        self.items = items if items is not None else _items(3)
        self.selected = 0
        monkeypatch.setattr(ss, "live_storylines", lambda conn: self.stories)
        monkeypatch.setattr(ss, "undecided_items", self._undecided)
        monkeypatch.setattr(
            ss,
            "record_decision",
            lambda conn, i, sids, *, model: self.decisions.append((i, list(sids))),
        )
        monkeypatch.setattr(
            ss,
            "record_failure",
            lambda conn, i, *, model, problem: self.failures.append((i, problem)),
        )
        monkeypatch.setattr(
            ss,
            "record_deferral",
            lambda conn, i, *, model, problem: self.deferrals.append((i, problem)),
        )
        monkeypatch.setattr(
            comprehend,
            "record_spend",
            lambda conn, stage, model, usage, **kw: self.spend.append(
                (stage, model, usage)
            ),
        )

    def _undecided(self, conn, **kw):
        self.select_kwargs = kw
        self.selected += 1
        return self.items


def _run(post, conn=None, model=MODEL):
    conn = conn or FakeConn()
    return brief.attach_pass(conn, now=NOW, model=model, post=post), conn


def test_off_makes_no_call(monkeypatch, caplog):
    monkeypatch.setattr(common, "STORYLINES_ENABLED", False)
    called = []
    monkeypatch.setattr(brief, "attach_pass", lambda *a, **k: called.append(1))
    monkeypatch.setattr(brief.db, "connect", lambda: called.append("db"))
    with caplog.at_level(logging.INFO):
        brief.mode_attach()
    assert called == []
    assert "Attach: off" in caplog.text


def test_no_live_storylines_makes_no_call_and_writes_nothing(monkeypatch, caplog):
    rec = Rec(monkeypatch, stories=0)
    posts = []
    with caplog.at_level(logging.INFO):
        out, _ = _run(lambda p: posts.append(p))
    assert out["status"] == "no_storylines"
    assert posts == [] and rec.selected == 0
    assert rec.decisions == rec.failures == rec.deferrals == rec.spend == []
    assert out["items"] == 0
    assert "no live storylines" in caplog.text


def test_nothing_to_do_makes_no_call(monkeypatch):
    rec = Rec(monkeypatch, items=[])
    posts = []
    out, _ = _run(lambda p: posts.append(p))
    assert out["status"] == "nothing_to_do" and posts == [] and rec.selected == 1


def test_lookback_and_per_pass_cap_are_passed_to_the_store(monkeypatch):
    rec = Rec(monkeypatch, items=[])
    _run(lambda p: None)
    assert rec.select_kwargs == {
        "now": NOW,
        "lookback_hours": 24,
        "max_attempts": 3,
        "max_defers": 10,
        "limit": 300,
    }
    assert (
        brief.ATTACH_LOOKBACK_HOURS,
        brief.ATTACH_MAX_PER_PASS,
        brief.ATTACH_MAX_ATTEMPTS,
        brief.ATTACH_MAX_DEFERS,
        brief.ATTACH_TIMEOUT,
    ) == (24, 300, 3, 10, 120)


def test_two_chunks_for_61_items(monkeypatch):
    rec = Rec(monkeypatch, items=_items(61))
    sizes = []

    def post(payload):
        resp = _answer_all(payload)
        sizes.append(len(resp["content"][0]["input"]["answers"]))
        return resp

    out, conn = _run(post)
    assert sizes == [60, 1]
    assert out["calls"] == 2 and out["items"] == 61 and out["attached"] == 61
    assert conn.commits == 2
    assert len(rec.decisions) == 61
    assert out["status"] == "ok" and out["error"] is None


def test_transient_error_on_second_chunk_keeps_first_chunk_and_writes_nothing_after(
    monkeypatch,
):
    rec = Rec(monkeypatch, items=_items(61))
    n = []

    def post(payload):
        n.append(1)
        if len(n) == 2:
            raise requests.Timeout("slow")
        return _answer_all(payload)

    out, conn = _run(post)
    assert out["status"] == "transient" and "slow" in out["error"]
    assert len(rec.decisions) == 60 and rec.failures == [] and rec.deferrals == []
    assert out["attached"] == 60 and out["deferred"] == 1 and out["items"] == 61
    assert conn.commits == 1  # chunk 1 committed; nothing written for chunk 2


@pytest.mark.parametrize(
    "boom,problem_has",
    [
        (lambda: _http(400), "400"),
        (lambda: RuntimeError("weird"), "weird"),
        (None, "max_tokens"),
    ],
)
def test_400_and_max_tokens_defer_the_chunk_without_charging_attempts(
    monkeypatch, boom, problem_has
):
    rec = Rec(monkeypatch, items=_items(61))
    n = []

    def post(payload):
        n.append(1)
        if len(n) == 1:
            if boom is None:
                return {"stop_reason": "max_tokens", "usage": USAGE, "content": []}
            raise boom()
        return _answer_all(payload)

    out, _ = _run(post)
    assert rec.failures == []
    assert [i for i, _ in rec.deferrals] == list(range(1, 61))
    assert all(problem_has in p for _, p in rec.deferrals)
    assert len(rec.decisions) == 1  # the second chunk went on
    assert out["deferred"] == 60 and out["attached"] == 1 and out["status"] == "ok"
    # a whole result was still billed; an exception returned nothing to bill
    assert len(rec.spend) == (2 if boom is None else 1)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_config_error_stops_the_pass_and_writes_nothing(monkeypatch, code):
    rec = Rec(monkeypatch, items=_items(61))
    posts = []

    def post(payload):
        posts.append(1)
        raise _http(code)

    out, conn = _run(post)
    assert out["status"] == "config" and len(posts) == 1
    assert rec.decisions == rec.failures == rec.deferrals == rec.spend == []
    assert out["deferred"] == 61 and out["items"] == 61 and conn.commits == 0


def _alert_env(monkeypatch):
    state, sent = {}, []
    monkeypatch.setattr(brief, "load_state", lambda: dict(state))
    monkeypatch.setattr(brief, "save_state", lambda d: state.update(d))
    monkeypatch.setattr(brief, "telegram_alert", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(
        brief.config,
        "clear_runtime_state",
        lambda keys: [state.pop(k, None) for k in keys],
    )

    class Ctx:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(brief.db, "connect", lambda: Ctx())
    monkeypatch.setattr(common, "STORYLINES_ENABLED", True)
    monkeypatch.setattr(common, "STORYLINE_ATTACH_MODEL", MODEL)
    return state, sent


def test_401_stops_the_pass_writes_nothing_and_alerts_once(monkeypatch):
    rec = Rec(monkeypatch)
    state, sent = _alert_env(monkeypatch)
    mode = {"bad": True}

    def post(payload):
        if mode["bad"]:
            raise _http(401)
        return _answer_all(payload)

    monkeypatch.setattr(brief, "_attach_post", post)
    brief.mode_attach()
    brief.mode_attach()
    assert len(sent) == 1 and "config" in sent[0]
    assert rec.decisions == rec.failures == rec.deferrals == []
    assert state.get("attach_alert", "").startswith("config:")
    mode["bad"] = False
    brief.mode_attach()
    assert "attach_alert" not in state and len(sent) == 1


def test_transient_status_does_not_alert(monkeypatch):
    Rec(monkeypatch)
    state, sent = _alert_env(monkeypatch)

    def post(payload):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(brief, "_attach_post", post)
    brief.mode_attach()
    assert sent == [] and state == {}


def test_unpriced_model_makes_no_call_and_alerts(monkeypatch):
    rec = Rec(monkeypatch)
    state, sent = _alert_env(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_ATTACH_MODEL", "claude-invented-9")
    posts = []
    monkeypatch.setattr(brief, "_attach_post", lambda p: posts.append(p))
    brief.mode_attach()
    brief.mode_attach()
    assert posts == [] and rec.selected == 0
    assert len(sent) == 1 and "unpriced" in sent[0]
    out, _ = _run(lambda p: posts.append(p), model="claude-invented-9")
    assert out["status"] == "unpriced" and "claude-invented-9" in out["error"]
    assert posts == []


def test_per_article_problems_charge_only_those_articles(monkeypatch):
    rec = Rec(monkeypatch, items=_items(4))
    answers = [
        {"article": "A1", "storylines": ["S1"]},
        {"article": "A2", "storylines": []},
        {"article": "A3", "storylines": ["S9"]},  # unknown storyline
        {"article": "A9", "storylines": ["S1"]},  # never sent: stray
        # A4 missing
    ]
    out, _ = _run(lambda p: _resp(answers))
    assert rec.decisions == [(1, [1]), (2, [])]
    assert sorted(i for i, _ in rec.failures) == [3, 4]
    assert rec.deferrals == []
    assert (out["attached"], out["none"], out["failed"], out["stray"]) == (1, 1, 2, 1)
    assert out["items"] == 4 == out["attached"] + out["none"] + out["failed"]


def test_spend_recorded_per_returned_response(monkeypatch):
    rec = Rec(monkeypatch, items=_items(61))
    _run(lambda p: _answer_all(p))
    assert rec.spend == [("attach", MODEL, USAGE)] * 2


def test_missing_usage_is_recorded_as_empty(monkeypatch):
    rec = Rec(monkeypatch)
    _run(lambda p: {k: v for k, v in _answer_all(p).items() if k != "usage"})
    assert rec.spend == [("attach", MODEL, {})]


def _scenario(kind):
    if kind == "ok":
        return lambda p: _answer_all(p)
    if kind == "whole":
        return lambda p: {"stop_reason": "max_tokens", "usage": USAGE, "content": []}

    n = []

    def post(p):
        n.append(1)
        if len(n) == 2:
            raise requests.Timeout("t")
        return _answer_all(p)

    return post


@pytest.mark.parametrize("kind", ["ok", "transient", "whole"])
def test_counts_always_sum_to_items(monkeypatch, kind):
    Rec(monkeypatch, items=_items(130))
    out, _ = _run(_scenario(kind))
    assert out["items"] == 130
    assert (
        out["items"] == out["attached"] + out["none"] + out["failed"] + out["deferred"]
    )
    if kind == "transient":
        assert out["deferred"] == 70


def test_log_line_carries_every_count(monkeypatch, caplog):
    Rec(monkeypatch)
    _alert_env(monkeypatch)
    monkeypatch.setattr(brief, "_attach_post", lambda p: _answer_all(p))
    with caplog.at_level(logging.INFO):
        brief.mode_attach()
    line = next(
        r.getMessage() for r in caplog.records if "Attach: status=" in r.getMessage()
    )
    assert line == (
        "Attach: status=ok items=3 calls=1 attached=3 none=0 failed=0 "
        "deferred=0 stray=0"
    )


def test_attach_post_is_one_attempt_with_the_attach_timeout(monkeypatch):
    seen = {}

    def fake(payload, **kw):
        seen.update(kw, payload=payload)
        return {"ok": 1}

    monkeypatch.setattr(brief, "_post_messages", fake)
    assert brief._attach_post({"x": 1}) == {"ok": 1}
    assert seen["timeout"] == 120 and seen["max_attempts"] == 1
    assert seen["label"] == "Attach"


def test_attach_is_scheduled_every_30_minutes_and_dispatched():
    spec = next(s for s in scheduler.SCHEDULES if s.job == "attach")
    assert (spec.kind, spec.every_minutes, spec.grace_minutes) == ("interval", 30, 10)
    assert brief.MODES["attach"] is brief.mode_attach
    assert "attach" in brief.JOB_MODES


def test_knobs_default_off_with_the_haiku_model_and_compose_passes_them():
    assert common.KNOBS["STORYLINES_ENABLED"].default is False
    assert common.KNOBS["STORYLINE_ATTACH_MODEL"].default == "claude-haiku-5-5"
    text = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert "- STORYLINES_ENABLED=${STORYLINES_ENABLED:-}" in text
    assert "- STORYLINE_ATTACH_MODEL=${STORYLINE_ATTACH_MODEL:-}" in text


# -- DB-gated end to end ------------------------------------------------------


@pytest.mark.skipif(not db.is_configured(), reason="No database is configured")
def test_end_to_end_attach_pass_on_a_real_schema():
    conn = db.connect()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        db.run_migrations(conn)
        conn.commit()
        oid = conn.execute(
            "INSERT INTO outlets (name) VALUES ('Test Wire') RETURNING id"
        ).fetchone()[0]

        def item(n, age):
            return conn.execute(
                "INSERT INTO items (outlet_id, url, title, body, content_hash,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    oid,
                    f"https://example.test/{n}",
                    f"Gizmo strike {n}",
                    "b",
                    f"h{n}",
                    NOW - timedelta(hours=age),
                ),
            ).fetchone()[0]

        s1 = ss.add_storyline(
            conn, "Widget tariffs", "invented", origin="operator", max_live=60
        ).storyline_id
        s2 = ss.add_storyline(
            conn, "Gizmo strike", "invented", origin="operator", max_live=60
        ).storyline_id
        i1, i2, old = item(1, 3), item(2, 2), item(3, 30)  # oldest first: A1, A2
        conn.commit()

        def post(payload):
            return _resp(
                [
                    {"article": "A1", "storylines": ["S2"]},
                    {"article": "A2", "storylines": []},
                ]
            )

        out = brief.attach_pass(conn, now=NOW, model=MODEL, post=post)
        assert out["status"] == "ok" and out["items"] == 2
        assert (out["attached"], out["none"]) == (1, 1)
        dec = dict(
            conn.execute(
                "SELECT item_id, outcome FROM item_attach_decisions"
            ).fetchall()
        )
        assert dec == {i1: "attached", i2: "none"}
        assert old not in dec
        rows = conn.execute(
            "SELECT item_id, storyline_id FROM item_attachments"
        ).fetchall()
        assert rows == [(i1, s2)]
        last = dict(
            conn.execute("SELECT id, last_attached_at FROM storylines").fetchall()
        )
        assert last[s2] is not None and last[s1] is None
        spend = conn.execute("SELECT stage, model FROM comprehend_spend").fetchall()
        assert spend == [("attach", MODEL)]
        again = brief.attach_pass(conn, now=NOW, model=MODEL, post=post)
        assert again["status"] == "nothing_to_do"
    finally:
        conn.close()

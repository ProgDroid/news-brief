"""mode_watch: the watch job end to end over fakes (news-brief-8lp).

The DB window query is covered against real Postgres in test_watch_window_db;
here the connection, the model call, Telegram and runtime_state are fakes, so
the wiring, the once-per-row rule and the failure paths are what is tested.
"""

import contextlib
import json

import pytest

import brief
import common
import config
import scheduler
import watch
from watch import Item, WatchFileError

E1 = {
    "id": "e1",
    "kind": "event",
    "name": "Example ceasefire signed",
    "trigger": "A ceasefire or framework agreement is formally signed.",
}
ITEMS = [Item("Truce agreed", "https://example.com/1", "Wire")]


def _resp(verdicts):
    return {
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [
            {
                "type": "tool_use",
                "name": "emit_verdicts",
                "input": {"verdicts": verdicts},
            }
        ],
    }


TRIGGERED = _resp(
    [{"row_id": "e1", "verdict": "triggered", "evidence": ["I1"], "reason": "signed"}]
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake world: watch file path, captured items, state dict, sends, post."""
    path = tmp_path / "watch.json"
    monkeypatch.setattr(brief, "WATCH_FILE", path)

    state: dict = {}
    monkeypatch.setattr(config, "runtime_state", lambda: dict(state))
    monkeypatch.setattr(config, "set_runtime_state", lambda u: state.update(u))
    monkeypatch.setattr(
        config, "clear_runtime_state", lambda keys: [state.pop(k, None) for k in keys]
    )

    sent: list[str] = []
    ok = {"value": True}

    def _send(text):
        sent.append(text)
        return ok["value"]

    for mod, name in [
        (brief, "telegram_send"),
        (common, "telegram_send"),
        (brief, "telegram_send_long"),
        (common, "telegram_send_long"),
        (brief, "telegram_alert"),
        (common, "telegram_alert"),
    ]:
        monkeypatch.setattr(mod, name, _send)

    items = {"value": list(ITEMS)}
    monkeypatch.setattr(watch, "fetch_window_items", lambda conn, as_of: items["value"])
    monkeypatch.setattr(brief.db, "connect", lambda: contextlib.nullcontext(object()))

    calls: list[dict] = []
    reply = {"value": TRIGGERED}

    def post(payload):
        calls.append(payload)
        return reply["value"]

    class Env:
        pass

    e = Env()
    e.path, e.state, e.sent, e.ok, e.items, e.calls, e.reply, e.post = (
        path,
        state,
        sent,
        ok,
        items,
        calls,
        reply,
        post,
    )

    def write(rows):
        path.write_text(json.dumps({"version": 1, "rows": rows}), encoding="utf-8")

    e.write = write
    return e


def test_trigger_sends_once_and_stores_state(env):
    env.write([E1])
    brief.mode_watch(post=env.post)
    assert len(env.sent) == 1 and "Watch e1" in env.sent[0]
    assert "https://example.com/1" in env.sent[0]
    assert env.state["watch.alerted.e1"]["strength"] == 2


def test_second_run_same_verdict_sends_nothing(env):
    env.write([E1])
    brief.mode_watch(post=env.post)
    brief.mode_watch(post=env.post)
    assert len(env.sent) == 1
    assert len(env.calls) == 2  # judged both nights; alerted once


def test_failed_send_stores_no_state_and_raises(env):
    env.write([E1])
    env.ok["value"] = False
    with pytest.raises(RuntimeError, match="e1"):
        brief.mode_watch(post=env.post)
    assert "watch.alerted.e1" not in env.state


def test_missing_file_raises_watch_file_error(env):
    with pytest.raises(WatchFileError, match="missing"):
        brief.mode_watch(post=env.post)
    assert env.calls == []


def test_zero_items_raises_capture_may_be_down(env):
    env.write([E1])
    env.items["value"] = []
    with pytest.raises(WatchFileError, match="capture may be down"):
        brief.mode_watch(post=env.post)
    assert env.calls == []


def test_skipped_rows_and_invalid_verdicts_reported_every_run(env):
    env.write([E1, {"id": "x", "kind": "telepathy", "name": "n"}])
    env.reply["value"] = _resp([])  # no verdict for e1 -> invalid
    brief.mode_watch(post=env.post)
    brief.mode_watch(post=env.post)
    problems = [m for m in env.sent if "not judged" in m]
    assert len(problems) == 2
    assert "x" in problems[0] and "e1" in problems[0]


def test_over_ceiling_warns_once_and_still_judges(env, monkeypatch):
    monkeypatch.setattr(watch, "INPUT_CEILING", 1)
    env.items["value"] = [
        Item("Truce agreed", "https://example.com/1", "Wire"),
        Item("Other", "https://example.com/2", "Wire"),
    ]
    env.write([E1])
    brief.mode_watch(post=env.post)
    brief.mode_watch(post=env.post)
    warnings = [m for m in env.sent if "ceiling" in m]
    assert len(warnings) == 1
    assert len(env.calls) == 2


def test_row_removed_via_an_empty_file_then_re_added_alerts_again(env):
    """Review focus 4 at the mode level: a night with no event rows must still
    clear the old alert keys, or a re-added row stays silenced for ever."""
    env.write([E1])
    brief.mode_watch(post=env.post)
    env.write([])
    brief.mode_watch(post=env.post)
    assert "watch.alerted.e1" not in env.state
    env.write([E1])
    brief.mode_watch(post=env.post)
    assert len([m for m in env.sent if "Watch e1" in m]) == 2


def test_no_event_rows_makes_no_model_call(env):
    env.write([])
    brief.mode_watch(post=env.post)
    assert env.calls == [] and env.sent == []


def test_model_follows_MODEL_unless_WATCH_MODEL_set(env, monkeypatch):
    env.write([E1])
    monkeypatch.setattr(common, "MODEL", "brief-model", raising=False)
    monkeypatch.setattr(common, "WATCH_MODEL", "", raising=False)
    brief.mode_watch(post=env.post)
    monkeypatch.setattr(common, "WATCH_MODEL", "pinned", raising=False)
    brief.mode_watch(post=env.post)
    assert [c["model"] for c in env.calls] == ["brief-model", "pinned"]


def test_watch_model_knob_is_declared_and_in_the_compose_anchor():
    assert common.KNOBS["WATCH_MODEL"].default == ""
    compose = (brief.Path(__file__).parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "NEWSBRIEF_WATCH_MODEL=${NEWSBRIEF_WATCH_MODEL:-}" in compose


def test_watch_is_scheduled_daily_at_0630():
    """Scheduled provisionally 2026-11-19 by user decision, ahead of the replay
    gate news-brief-8lp.1 (still open): an unvalidated watcher beats none."""
    assert "watch" in brief.MODES and "watch" in brief.JOB_MODES
    (spec,) = [s for s in scheduler.SCHEDULES if s.job == "watch"]
    assert (spec.kind, spec.at) == ("daily", "06:30")
    assert spec.grace_minutes == 180

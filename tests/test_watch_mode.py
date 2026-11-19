"""mode_watch: the watch job end to end over fakes (news-brief-8lp).

The DB window query is covered against real Postgres in test_watch_window_db;
here the connection, the model call, Telegram and runtime_state are fakes, so
the wiring, the once-per-row rule and the failure paths are what is tested.
"""

import contextlib
import json
from datetime import date, datetime, timedelta, timezone

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
    rang: list[str] = []  # messages sent with notify=True
    ok = {"value": True}

    def _send(text, notify=False):
        sent.append(text)
        if notify:
            rang.append(text)
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

    e.rang = rang

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


def test_triggered_rings_but_unclear_and_problems_stay_silent(env):
    """User 2026-11-19: a TRIGGERED alert makes a sound; UNCLEAR and the
    rows-not-judged report are 'look sometime' and stay silent."""
    env.write([E1, {"id": "x", "kind": "telepathy", "name": "n"}])
    brief.mode_watch(post=env.post)
    assert len(env.rang) == 1 and "TRIGGERED" in env.rang[0]

    e2 = dict(E1, id="e2")
    env.write([e2])
    env.reply["value"] = _resp(
        [{"row_id": "e2", "verdict": "unclear", "evidence": ["I1"], "reason": "maybe"}]
    )
    brief.mode_watch(post=env.post)
    assert any("UNCLEAR" in m for m in env.sent)
    assert len(env.rang) == 1  # nothing new rang


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
    # The event stage fails; the job still raises, naming the reason, so /jobs
    # goes red and run_job's Telegram alert carries "capture may be down".
    with pytest.raises(
        RuntimeError, match="events: WatchFileError.*capture may be down"
    ):
        brief.mode_watch(post=env.post)
    assert env.calls == []


def test_skipped_rows_and_invalid_verdicts_reported_every_run(env):
    env.write([E1, {"id": "x", "kind": "telepathy", "name": "n"}])
    env.reply["value"] = _resp([])  # no verdict for e1 -> invalid
    # Every event row invalid also turns the job red (r1w); the report must
    # still go out on each run.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="every row invalid"):
            brief.mode_watch(post=env.post)
    problems = [m for m in env.sent if "not judged" in m]
    assert len(problems) == 2
    assert "skipped x:" in problems[0] and "e1" in problems[0]


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


def test_a_skipped_event_row_keeps_its_key_through_mode_watch(env):
    env.write([E1])
    brief.mode_watch(post=env.post)
    broken = dict(E1)
    del broken["trigger"]  # now malformed: skipped, but still in the file
    env.write([broken])
    brief.mode_watch(post=env.post)
    assert "watch.alerted.e1" in env.state


# ── Price stage (news-brief-vap) ─────────────────────────────────────────────

PRICE_AS_OF = datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc)


def _price(rid, signal, level=400.0, condition="below"):
    return {
        "id": rid,
        "kind": "price",
        "action": "add",
        "name": f"Example {rid}",
        "signal": signal,
        "condition": condition,
        "level": level,
    }


def _fetcher(closes: dict, calls: list | None = None):
    """symbol -> last close (a one-bar history ending yesterday), or an
    Exception to raise for that symbol."""

    def fetch(symbol):
        if calls is not None:
            calls.append(symbol)
        value = closes[symbol]
        if isinstance(value, Exception):
            raise value
        return [(date(2026, 11, 18), value)]

    return fetch


def _run(env, fetch):
    brief.mode_watch(post=env.post, fetch=fetch, as_of=PRICE_AS_OF)


def test_price_fired_rings_and_stores(env):
    env.write([_price("p1", "QQQ")])
    _run(env, _fetcher({"QQQ": 390.0}))
    assert len(env.rang) == 1 and "Watch p1" in env.rang[0] and "FIRED" in env.rang[0]
    assert env.state["watch.price.p1"]["fired"] is True
    assert env.calls == []  # no event rows: no model call


def test_price_stays_silent_while_fired_then_rearms(env):
    env.write([_price("p1", "QQQ")])
    _run(env, _fetcher({"QQQ": 390.0}))
    _run(env, _fetcher({"QQQ": 391.0}))
    assert len(env.rang) == 1
    _run(env, _fetcher({"QQQ": 410.0}))  # back above: re-arm
    assert env.state["watch.price.p1"]["fired"] is False
    _run(env, _fetcher({"QQQ": 395.0}))  # crosses again
    assert len(env.rang) == 2


def test_shared_symbol_fetched_once(env):
    calls = []
    env.write([_price("p1", "QQQ"), _price("p2", "QQQ", level=380.0)])
    _run(env, _fetcher({"QQQ": 390.0}, calls))
    assert calls == ["QQQ"]


def test_price_fetch_failure_still_sends_event_alerts_and_exits_nonzero(env):
    env.write([E1, _price("p1", "QQQ")])
    with pytest.raises(RuntimeError, match="prices: 1 of 1 symbols"):
        _run(env, _fetcher({"QQQ": ValueError("down")}))
    assert any("Watch e1" in m for m in env.sent)  # event stage unaffected
    assert any("price unknown p1" in m for m in env.sent)


def test_quarter_or_fewer_failures_report_silently_and_exit_zero(env):
    env.write([_price(f"p{i}", s) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])])
    _run(
        env,
        _fetcher({"AAA": 410.0, "BBB": 410.0, "CCC": 410.0, "DDD": ValueError("x")}),
    )
    problems = [m for m in env.sent if "not judged" in m]
    assert len(problems) == 1 and "price unknown p3" in problems[0]
    assert env.rang == []


def test_shared_symbol_failure_counts_once_toward_the_limit(env):
    # 2 rows on BAD, 2 symbols OK: 1 of 3 symbols failed (33% > 25%) -> red.
    # Counting rows instead (2 of 4 = 50%) would also be red, so pin the
    # message: it must say "1 of 3 symbols".
    env.write(
        [
            _price("p1", "BAD"),
            _price("p2", "BAD", level=300.0),
            _price("p3", "OK1"),
            _price("p4", "OK2"),
        ]
    )
    with pytest.raises(RuntimeError, match="1 of 3 symbols"):
        _run(env, _fetcher({"BAD": ValueError("x"), "OK1": 410.0, "OK2": 410.0}))


def test_no_price_rows_makes_no_fetch(env):
    calls = []
    env.write([E1])
    _run(env, _fetcher({}, calls))
    assert calls == []


def test_unknown_price_rows_appear_in_the_problems_report(env):
    env.write([_price("p1", "OLD")])

    def stale(symbol):
        return [(PRICE_AS_OF.date() - timedelta(days=9), 390.0)]

    # Its only symbol is stale, so 1 of 1 is unusable: red (r1w/M5), and the
    # row is still named in the report.
    with pytest.raises(RuntimeError, match="1 of 1 symbols unusable"):
        _run(env, stale)
    assert any("price unknown p1" in m and "stale" in m for m in env.sent)
    assert env.rang == []


def test_a_crashing_price_stage_still_sends_event_alerts(env, monkeypatch):
    def boom(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(watch, "plan_price_alerts", boom)
    env.write([E1, _price("p1", "QQQ")])
    with pytest.raises(RuntimeError, match="prices: KeyError"):
        _run(env, _fetcher({"QQQ": 390.0}))
    assert any("Watch e1" in m for m in env.sent)


def test_a_failing_event_stage_still_sends_price_alerts(env):
    env.write([E1, _price("p1", "QQQ")])
    env.items["value"] = []  # capture down: the event stage raises
    with pytest.raises(RuntimeError, match="events: WatchFileError"):
        _run(env, _fetcher({"QQQ": 390.0}))
    assert len(env.rang) == 1 and "Watch p1" in env.rang[0]


def test_a_failed_price_send_stores_nothing_and_fails_the_job(env):
    """Review I2: the spec's 'a failed send stores nothing' had no test."""
    env.write([_price("p1", "QQQ")])
    env.ok["value"] = False
    with pytest.raises(RuntimeError, match="p1"):
        _run(env, _fetcher({"QQQ": 390.0}))
    assert "watch.price.p1" not in env.state


def test_one_crashing_price_row_does_not_blind_the_others(env):
    """Review M4 (re-graded Important): level 0 made evaluate_price raise
    ZeroDivisionError and aborted the whole stage."""
    env.write([_price("p0", "ZZZ", level=0.0), _price("p1", "QQQ")])
    _run(env, _fetcher({"ZZZ": 5.0, "QQQ": 390.0}))
    assert len(env.rang) == 1 and "Watch p1" in env.rang[0]
    assert any("price unknown p0" in m and "crashed" in m for m in env.sent)


def test_a_night_where_every_event_row_is_invalid_turns_the_job_red(env):
    """r1w: a run that judged nothing usable (e.g. max_tokens every night)
    recorded green; the spec says a quiet night must be a judged night."""
    env.write([E1])
    env.reply["value"] = {"stop_reason": "max_tokens", "content": []}
    with pytest.raises(RuntimeError, match="events: every row invalid"):
        brief.mode_watch(post=env.post)
    assert any("couldn't judge e1" in m for m in env.sent)  # report still sent


def test_a_skipped_price_row_keeps_its_key_through_mode_watch(env):
    """r1w (M2): the price stage must pass keep_ids too."""
    env.write([_price("p1", "QQQ")])
    _run(env, _fetcher({"QQQ": 390.0}))
    broken = _price("p1", "QQQ")
    del broken["level"]  # malformed now: skipped, still in the file
    # A second, valid row keeps the stage on its main branch: with no valid
    # price rows it takes the empty-file branch, and a keep_ids bug in the
    # main call would go unseen (it did, at first).
    env.write([broken, _price("p2", "OK", level=300.0)])
    _run(env, _fetcher({"OK": 310.0}))
    assert "watch.price.p1" in env.state


def test_an_empty_or_stale_series_counts_toward_the_failure_limit(env):
    """r1w (M5): a symbol answering with no usable bars is as dead as one
    that fails to fetch, and must count toward the 25%."""
    env.write([_price("p1", "EMPTY"), _price("p2", "OK", level=300.0)])

    def fetch(symbol):
        return [] if symbol == "EMPTY" else [(date(2026, 11, 18), 310.0)]

    with pytest.raises(RuntimeError, match="1 of 2 symbols"):
        _run(env, fetch)

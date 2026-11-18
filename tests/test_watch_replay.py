"""scripts/watch_replay.py: the validation-gate tool for news-brief-8lp.

It must run the production judge and must never alert or write state.
"""

import contextlib
import json
from datetime import datetime, timezone

import pytest

import brief
import common
import config
import watch
from scripts import watch_replay as wr
from watch import Item, Verdict, build_judge_input

E1 = {"id": "e1", "kind": "event", "name": "Example ceasefire signed", "trigger": "t"}


def _boom(*a, **k):
    raise AssertionError("replay must not send or write state")


@pytest.fixture
def world(monkeypatch, tmp_path):
    for mod, name in [
        (brief, "telegram_send"),
        (common, "telegram_send"),
        (brief, "telegram_send_long"),
        (common, "telegram_send_long"),
        (brief, "telegram_alert"),
        (common, "telegram_alert"),
    ]:
        monkeypatch.setattr(mod, name, _boom)
    monkeypatch.setattr(config, "set_runtime_state", _boom)
    monkeypatch.setattr(config, "clear_runtime_state", _boom)
    monkeypatch.setattr(wr.db, "connect", lambda: contextlib.nullcontext(object()))

    seen = {}
    link = Item("Truce agreed", "https://example.com/1", "Wire")

    def fake_judge(conn, rows, as_of, *, post, model):
        seen.update(rows=rows, as_of=as_of, model=model)
        ji = build_judge_input(rows, [link])
        return ji, [Verdict("e1", "triggered", [link], "signed")]

    monkeypatch.setattr(watch, "judge_watch", fake_judge)
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps({"version": 1, "rows": [E1]}), encoding="utf-8")
    return rows, seen


def test_replay_prints_verdicts_and_never_sends_or_writes(world, capsys):
    rows, _ = world
    assert wr.main(["--as-of", "2026-10-01T06:30:00Z", "--rows", str(rows)]) == 0
    out = capsys.readouterr().out
    assert "e1" in out and "triggered" in out and "https://example.com/1" in out


def test_replay_calls_the_production_judge_with_the_parsed_date(world):
    rows, seen = world
    wr.main(["--as-of", "2026-10-01T06:30:00Z", "--rows", str(rows)])
    assert [r.id for r in seen["rows"]] == ["e1"]
    assert seen["as_of"] == datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)


def test_replay_naive_as_of_is_utc(world):
    """Review focus 5: a human types a time without a zone; it means UTC,
    never the host's local time."""
    rows, seen = world
    wr.main(["--as-of", "2026-10-01T06:30:00", "--rows", str(rows)])
    assert seen["as_of"] == datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)
    assert seen["as_of"].utcoffset().total_seconds() == 0


def test_replay_bad_rows_file_exits_1(world, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert wr.main(["--as-of", "2026-10-01T06:30:00Z", "--rows", str(bad)]) == 1
    assert "unreadable" in capsys.readouterr().out

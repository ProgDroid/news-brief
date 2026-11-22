"""news-brief-qap: how collect obtains the brief (storylines spec §6.1).

Rule (operator decision 2026-11-22): the batch is cancelled only once a
real-time brief is in hand. A failed real-time call goes back to waiting on the
batch, so the fallback never does worse than the wait it replaces.
"""

from datetime import datetime, timezone

import pytest

import brief
import common
import scheduler
from test_collect_day_key import _stub_collect

DAY = "2026-11-23"
AT_0600 = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
AT_0715 = datetime(2026, 11, 23, 7, 15, tzinfo=timezone.utc)


@pytest.fixture
def rig(monkeypatch):
    """Stub the four steps obtain_brief composes; record every call in order."""
    r = {
        "polls": [],
        "polls_ret": [None],
        "cancels": [],
        "loaded_days": [],
        "request": {"messages": []},
        "realtime_calls": [],
        "realtime_ret": "R",
        "events": [],
    }
    monkeypatch.setattr(common, "BRIEF_FALLBACK_AT", "06:30")

    def _poll(batch_id, max_wait_secs=43200):
        r["polls"].append((batch_id, max_wait_secs))
        r["events"].append("poll")
        seq = r["polls_ret"]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def _cancel(batch_id):
        r["cancels"].append(batch_id)
        r["events"].append("cancel")

    def _load(day):
        r["loaded_days"].append(day)
        return r["request"]

    def _realtime(params, *, timeout, call=None):
        r["realtime_calls"].append(params)
        r["events"].append("realtime")
        ret = r["realtime_ret"]
        if isinstance(ret, BaseException):
            raise ret
        return ret

    monkeypatch.setattr(brief, "poll_batch", _poll)
    monkeypatch.setattr(brief, "cancel_batch", _cancel)
    monkeypatch.setattr(brief, "load_brief_request", _load)
    monkeypatch.setattr(brief, "run_brief_realtime", _realtime)
    return r


def test_obtain_brief_uses_the_batch_when_it_ends_in_time(rig):
    rig["polls_ret"] = ["B"]
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("B", "batch")
    assert rig["cancels"] == [] and rig["realtime_calls"] == []
    assert rig["polls"][0] == ("b1", 1800)


def test_obtain_brief_runs_realtime_before_it_cancels(rig):
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("R", "realtime")
    assert rig["events"] == ["poll", "realtime", "cancel"]


def test_obtain_brief_goes_back_to_the_batch_when_realtime_fails(rig):
    rig["polls_ret"] = [None, "B"]
    rig["realtime_ret"] = None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("B", "batch")
    assert rig["cancels"] == []
    budget = (common.JOB_MAX_RUNTIME_MINUTES - brief.RESUME_MARGIN_MINUTES) * 60
    assert budget - 60 <= rig["polls"][1][1] <= budget


def test_obtain_brief_falls_back_when_the_batch_ended_without_success(rig):
    # An errored batch: the first poll returns None at once, not at a timeout.
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("R", "realtime")
    assert len(rig["polls"]) == 1


def test_obtain_brief_after_the_fallback_time_checks_once_then_falls_back(rig):
    brief.obtain_brief("b1", DAY, now=AT_0715)
    assert rig["polls"][0] == ("b1", 0)
    assert rig["realtime_calls"]


def test_obtain_brief_without_a_saved_request_waits_on_the_batch(rig):
    rig["request"] = None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")
    assert rig["realtime_calls"] == [] and rig["cancels"] == []
    assert len(rig["polls"]) == 2


def test_obtain_brief_reads_the_request_by_the_submit_day(rig):
    brief.obtain_brief("b1", "2000-01-01", now=AT_0600)
    assert rig["loaded_days"] == ["2000-01-01"]


def test_obtain_brief_never_raises(rig):
    rig["realtime_ret"] = RuntimeError("boom")
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")


def test_obtain_brief_with_the_fallback_off_waits_the_full_window(rig, monkeypatch):
    monkeypatch.setattr(common, "BRIEF_FALLBACK_AT", "off")
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")
    assert rig["polls"] == [("b1", 43200)]
    assert rig["cancels"] == [] and rig["realtime_calls"] == []


# ── collect wiring ───────────────────────────────────────────────────────────


def _headers(monkeypatch, tmp_path, obtained):
    _stub_collect(monkeypatch, tmp_path, {"batch_id": "b1", "date": "2000-01-01"})
    headers = []
    monkeypatch.setattr(brief, "obtain_brief", lambda bid, day, *, now: obtained)
    monkeypatch.setattr(
        brief, "deliver", lambda text, header, archive_path: headers.append(header)
    )
    brief.mode_collect()
    return headers


def test_a_realtime_brief_says_so_in_its_header(monkeypatch, tmp_path):
    headers = _headers(monkeypatch, tmp_path, ("R", "realtime"))
    assert len(headers) == 1 and "live fallback" in headers[0]


def test_a_batch_brief_header_is_unchanged(monkeypatch, tmp_path):
    headers = _headers(monkeypatch, tmp_path, ("B", "batch"))
    assert len(headers) == 1 and "live fallback" not in headers[0]


def test_collect_asks_for_the_brief_by_the_submit_day(monkeypatch, tmp_path):
    _stub_collect(monkeypatch, tmp_path, {"batch_id": "b1", "date": "2000-01-01"})
    asked = []

    def _obtain(bid, day, *, now):
        asked.append((bid, day))
        return ("B", "batch")

    monkeypatch.setattr(brief, "obtain_brief", _obtain)
    brief.mode_collect()
    assert asked == [("b1", "2000-01-01")]


# ── schedule ─────────────────────────────────────────────────────────────────


def test_submit_runs_at_0430_before_the_0600_collect():
    """Storylines spec §6.1, operator decision 2026-11-22 (news-brief-qap)."""
    submit = next(s for s in scheduler.SCHEDULES if s.job == "submit")
    collect = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    assert (submit.at, submit.grace_minutes) == ("04:30", 60)
    assert collect.at == "06:00"

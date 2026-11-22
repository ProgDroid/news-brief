"""news-brief-qap: brief submitted at 04:30, collect falls back to real time.

Storylines spec §6.1. Collect polls the batch until BRIEF_FALLBACK_AT, then
cancels it and replays the request submit persisted against /v1/messages.
"""

from datetime import datetime, timezone

import brief
import common

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)


class _FakeResp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload or {}
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise brief.requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self):
        return self._payload


def _status_get(gets, *answers):
    """A fake requests.get answering batch status checks in order.

    Each answer is a processing_status string, or an exception instance to
    raise. The last answer repeats once the list runs out.
    """
    seq = list(answers)

    def _get(url, **kw):
        gets.append(url)
        ans = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(ans, BaseException):
            raise ans
        return _FakeResp({"processing_status": ans})

    return _get


# ── Task 1: fallback timing ──────────────────────────────────────────────────


def test_fallback_wait_is_the_seconds_until_the_fallback_time():
    assert brief.fallback_wait_secs(NOW, "06:30") == 1800


def test_fallback_wait_is_zero_once_the_time_has_passed():
    late = NOW.replace(hour=7, minute=15)
    assert brief.fallback_wait_secs(late, "06:30") == 0


def test_fallback_off_or_garbage_means_no_fallback():
    assert brief.fallback_wait_secs(NOW, "off") is None
    assert brief.fallback_wait_secs(NOW, "6h30") is None


def test_fallback_knobs_are_declared_and_in_the_compose_anchor():
    assert common.KNOBS["BRIEF_FALLBACK_AT"].default == "06:30"
    assert common.KNOBS["BRIEF_REALTIME_TIMEOUT"].default == 600
    compose = (brief.Path(__file__).parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "BRIEF_FALLBACK_AT=${BRIEF_FALLBACK_AT:-}" in compose
    assert "BRIEF_REALTIME_TIMEOUT=${BRIEF_REALTIME_TIMEOUT:-}" in compose


def test_poll_checks_once_when_the_deadline_has_passed(monkeypatch):
    gets, sleeps = [], []
    monkeypatch.setattr(brief.requests, "get", _status_get(gets, "in_progress"))
    monkeypatch.setattr(brief.time, "sleep", sleeps.append)
    assert brief.poll_batch("b1", max_wait_secs=0) is None
    assert len(gets) == 1 and sleeps == []


def test_poll_never_sleeps_past_its_deadline(monkeypatch):
    clock = {"t": 1000.0}
    sleeps, gets = [], []

    def _sleep(s):
        clock["t"] += s
        sleeps.append(s)

    monkeypatch.setattr(brief.time, "time", lambda: clock["t"])
    monkeypatch.setattr(brief.time, "sleep", _sleep)
    monkeypatch.setattr(brief.requests, "get", _status_get(gets, "in_progress"))
    assert brief.poll_batch("b1", max_wait_secs=100) is None
    assert sum(sleeps) == 100  # 60, then the remaining 40
    assert len(gets) == 3


def test_poll_survives_a_failed_status_check(monkeypatch):
    gets = []
    overloaded = brief.requests.HTTPError(
        "529", response=_FakeResp(status=529, text="overloaded")
    )
    monkeypatch.setattr(brief.requests, "get", _status_get(gets, overloaded, "ended"))
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    monkeypatch.setattr(brief, "fetch_batch_results", lambda url: "B")
    assert brief.poll_batch("b1", max_wait_secs=600) == "B"
    assert len(gets) == 2

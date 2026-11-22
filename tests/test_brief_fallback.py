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


# ── Task 2: submit persists the request it batched ──────────────────────────

SUBMIT_TODAY = brief.datetime.now(brief.timezone.utc).strftime("%Y-%m-%d")


def _stub_submit(monkeypatch, tmp_path):
    """Run mode_submit with every input stubbed; return the captured POSTs."""
    posted = []

    def _post(url, json=None, **kw):
        posted.append(json)
        return _FakeResp({"id": "batch_1"})

    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    monkeypatch.setattr(brief, "load_state", lambda: {})
    monkeypatch.setattr(brief, "save_state", lambda updates: None)
    monkeypatch.setattr(brief, "load_temp_sources", lambda: [])
    monkeypatch.setattr(brief, "fetch_feed_blocks", lambda feeds: ["FEED"])
    monkeypatch.setattr(brief, "fetch_mauldin_twie", lambda: "")
    monkeypatch.setattr(brief, "fetch_web_source", lambda s: "")
    monkeypatch.setattr(brief, "brief_memory_enabled", lambda: False)
    monkeypatch.setattr(brief, "claim_verify_enabled", lambda: False)
    monkeypatch.setattr(brief, "load_feedback", lambda: {})
    monkeypatch.setattr(brief, "build_chroma_context", lambda fb: "")
    monkeypatch.setattr(brief, "load_yesterday_brief", lambda: "")
    monkeypatch.setattr(brief, "load_last_weekly_summary", lambda: "")
    monkeypatch.setattr(brief, "fetch_portfolio_weights", lambda: None)
    monkeypatch.setattr(brief, "load_book", lambda: {})
    monkeypatch.setattr(brief, "performance_prompt_block", lambda book: "")
    monkeypatch.setattr(brief, "build_market_pulse", lambda pins: "")
    monkeypatch.setattr(brief, "resolved_pins", lambda fb: [])

    def _no_enrichment(*a, **k):
        raise RuntimeError("enrichment stubbed off")

    monkeypatch.setattr(brief, "build_universe", _no_enrichment)
    monkeypatch.setattr(brief, "build_daily_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(brief.requests, "post", _post)
    return posted


def test_submit_saves_exactly_the_params_it_batched(monkeypatch, tmp_path):
    posted = _stub_submit(monkeypatch, tmp_path)

    brief.mode_submit()

    assert len(posted) == 1
    saved = brief.load_brief_request(SUBMIT_TODAY)
    assert saved == posted[0]["requests"][0]["params"]
    assert saved["messages"] == [{"role": "user", "content": "PROMPT"}]


def test_a_failed_request_save_still_submits_the_batch(monkeypatch, tmp_path):
    posted = _stub_submit(monkeypatch, tmp_path)

    def _boom(params, day):
        raise OSError("disk full")

    monkeypatch.setattr(brief, "save_brief_request", _boom)

    brief.mode_submit()

    assert len(posted) == 1


def test_a_missing_or_corrupt_request_file_loads_as_none(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    assert brief.load_brief_request("2000-01-01") is None
    (tmp_path / "brief_request-2000-01-01.json").write_text("{not json")
    assert brief.load_brief_request("2000-01-01") is None


def test_message_text_joins_without_a_separator():
    msg = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "web_search_tool_result"},
            {"type": "text", "text": ", b"},
        ]
    }
    assert brief._message_text(msg) == "a, b"


# ── Task 3: cancel and the real-time call ───────────────────────────────────

PARAMS = {
    "model": "m",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "PROMPT"}],
}


def _resp(stop_reason, text):
    return {
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _http_error(status):
    return brief.requests.HTTPError(
        f"{status} Error", response=_FakeResp(status=status, text=f"body {status}")
    )


def _answers(*items, sent=None):
    seq = list(items)

    def _call(payload, **kw):
        if sent is not None:
            sent.append(payload)
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return _call


def test_realtime_returns_the_text_of_a_finished_turn():
    call = _answers(_resp("end_turn", "Brief."))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "Brief."


def test_realtime_continues_on_pause_turn_with_the_content_so_far():
    sent = []
    first = _resp("pause_turn", "Part one ")
    call = _answers(first, _resp("end_turn", "part two."), sent=sent)
    assert (
        brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "Part one part two."
    )
    second = sent[1]["messages"]
    assert second[:-1] == PARAMS["messages"]
    assert second[-1] == {"role": "assistant", "content": first["content"]}


def test_realtime_gives_up_after_the_continuation_limit():
    call = _answers(*[_resp("pause_turn", "x")] * 4)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None


def test_realtime_http_failure_returns_none():
    call = _answers(brief.requests.ConnectionError("down"))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None


def test_realtime_max_tokens_is_not_a_brief():
    call = _answers(_resp("max_tokens", "cut"))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None


def test_realtime_retries_an_overloaded_reply(monkeypatch):
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    call = _answers(_http_error(529), _resp("end_turn", "Brief."))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "Brief."


def test_realtime_gives_up_after_three_overloaded_replies(monkeypatch):
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    call = _answers(*[_http_error(529)] * 3, _resp("end_turn", "never reached"))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None


def test_realtime_never_retries_a_timeout():
    sent = []
    call = _answers(
        brief.requests.ReadTimeout("slow"), _resp("end_turn", "x"), sent=sent
    )
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None
    assert len(sent) == 1


def test_realtime_does_not_retry_a_bad_request(monkeypatch):
    sent = []
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    call = _answers(_http_error(400), _resp("end_turn", "x"), sent=sent)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None
    assert len(sent) == 1


def test_cancel_batch_swallows_an_http_error(monkeypatch):
    def _post(url, **kw):
        return _FakeResp(status=409, text='{"error":"already ended"}')

    monkeypatch.setattr(brief.requests, "post", _post)
    brief.cancel_batch("b1")  # must not raise


def test_cancel_batch_posts_to_the_cancel_endpoint(monkeypatch):
    urls = []

    def _post(url, **kw):
        urls.append(url)
        return _FakeResp({"processing_status": "canceling"})

    monkeypatch.setattr(brief.requests, "post", _post)
    brief.cancel_batch("b1")
    assert urls == ["https://api.anthropic.com/v1/messages/batches/b1/cancel"]


def test_post_messages_logs_its_label_and_the_error_body(monkeypatch, caplog):
    def _post(url, **kw):
        return _FakeResp(status=529, text="overloaded_error")

    monkeypatch.setattr(brief.requests, "post", _post)
    try:
        brief._post_messages({}, timeout=1, max_attempts=1, label="Realtime brief")
    except brief.requests.HTTPError:
        pass
    assert "Realtime brief API attempt 1 failed" in caplog.text
    assert "overloaded_error" in caplog.text


# ── Final review fixes ───────────────────────────────────────────────────────


def test_realtime_continuation_keeps_the_container():
    """Server tools docs: send the paused response's container id back, or the
    continuation runs in a new container and loses the turn's earlier work."""
    sent = []
    first = {**_resp("pause_turn", "a"), "container": {"id": "container_1"}}
    call = _answers(first, _resp("end_turn", "b"), sent=sent)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "ab"
    assert "container" not in sent[0]
    assert sent[1]["container"] == "container_1"

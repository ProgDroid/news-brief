# Brief at 04:30 with a real-time fallback: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** submit the daily brief batch at 04:30 UTC instead of 20:00, and have collect fall back to a real-time call when the batch has not ended by 06:30 (bead `news-brief-qap`).

**Architecture:** submit persists the exact request it batched. Collect gets the brief through one new function, `obtain_brief`. It polls the batch until the fallback time, then replays the persisted request against `POST /v1/messages`, continuing on `pause_turn`. If that succeeds it cancels the batch; if it fails it goes back to waiting on the batch. The path taken is logged and marked in the Telegram header.

**Tech Stack:** Python 3.14, `requests` against the Anthropic HTTP API (the project's existing pattern; no SDK is introduced), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-21-storylines-design.md` §6.1 (revision 4).

## Global Constraints

- All times UTC. Submit `daily 04:30`, grace 60 min (unchanged). Collect stays `daily 06:00`, grace 120.
- Knobs are `KNOBS` rows read as `common.X` (never `from common import X`), each with a compose anchor line `- NAME=${NAME:-}`.
- `BRIEF_FALLBACK_AT`: `Knob(str, "06:30")`. The value `off` disables the fallback: collect waits as long as today, which is `poll_batch`'s 12 h window cut to 4 h by the supervisor's `JOB_MAX_RUNTIME_MINUTES=240`. An unparseable value behaves as `off` and logs an error.
- **No failure in the fallback path may crash collect.** A failed status check is logged and polling continues. Any exception inside `obtain_brief` ends in `(None, "none")`, and collect's existing alert fires.
- **Retry rule for the real-time call:** retry an HTTP 429, 500, 502, 503 or 529 (rejected or failed before billable work), up to 3 attempts in all, 30 s then 60 s apart. Never retry a read timeout: that request may have finished and been billed.
- `BRIEF_REALTIME_TIMEOUT`: `Knob(int, 600)` seconds, the read timeout of each real-time call.
- Files submit writes for collect are keyed by the submit day (`state["date"]`), never by collect's `now()` (`news-brief-hw8`; `tests/test_collect_day_key.py` is the pattern).
- A brief is delivered at most once per collect run. A real-time call happens only when the batch yields no succeeded result.
- Text from content blocks is joined with **no separator** (see `fetch_batch_results`' comment: citation boundaries split prose).
- Log any HTTP error's `resp.text`, not only the exception string.
- Repo is public: no holdings, amounts or thesis text in tests or fixtures.
- Gate before each commit: `ruff check .`, `ruff format --check .`, `py -m pytest -q`. Run it in the FOREGROUND.

## Review Focus

1. **Collect starts after 06:30** (a catch-up run): the batch is checked once, and if not ended the fallback starts at once. It must not wait a fixed minute or skip the check. Owned by Task 1 (`test_poll_checks_once_when_the_deadline_has_passed`) and Task 4 (`test_obtain_brief_after_the_fallback_time_checks_once_then_falls_back`).
2. **The batch ended with an errored result** (not slow, just failed): falls back to real time rather than alerting. Task 4 (`test_obtain_brief_falls_back_when_the_batch_ended_without_success`).
3. **No persisted request** (the batch was submitted by the old code, or the file was pruned): no real-time call; collect goes back to waiting on the batch, as today. Task 4 (`test_obtain_brief_without_a_saved_request_waits_on_the_batch`).
4. **`pause_turn` beyond the continuation limit**, or a real-time HTTP failure: returns `None`, never a half-brief, never raises. Task 3 (`test_realtime_gives_up_after_the_continuation_limit`, `test_realtime_http_failure_returns_none`).
5. **The API is down for longer than the real-time retries** (about 90 s): the batch was never cancelled, so collect goes back to waiting on it until just under the 4 h job limit. Today's late-but-delivered outcome is kept. Task 3 (`test_cancel_batch_swallows_an_http_error`) and Task 4 (`test_obtain_brief_goes_back_to_the_batch_when_realtime_fails`).
6. **The API is degraded at 06:00–06:30** (5xx, 529 or timeouts on status checks): polling continues to the deadline, and an overloaded reply to the real-time call is retried rather than ending the day's brief. Task 1 (`test_poll_survives_a_failed_status_check`), Task 3 (`test_realtime_retries_an_overloaded_reply`, `test_realtime_never_retries_a_timeout`), Task 4 (`test_obtain_brief_never_raises`).

**Billing note:** the batch keeps running while the real-time call runs, and is cancelled only after it succeeds. A batch that finishes in those minutes is billed as well (about $0.10–0.20 extra), which is rare after two hours of waiting.

---

### Task 1: Fallback timing (knobs, bounded poll, wait computation)

**Files:**
- Modify: `common.py` (`KNOBS`, next to `RECONCILE_TIMEOUT`)
- Modify: `docker-compose.yml` (the knob anchor, next to `RECONCILE_TIMEOUT=`)
- Modify: `brief.py` (`poll_batch` at about line 2897; new `fallback_wait_secs` beside it)
- Test: `tests/test_brief_fallback.py` (new)

**Interfaces:**
- Produces: `common.BRIEF_FALLBACK_AT: str`, `common.BRIEF_REALTIME_TIMEOUT: int`.
- Produces: `fallback_wait_secs(now: datetime, at: str) -> int | None`. `None` means "no fallback" (`off` or unparseable); otherwise the seconds from `now` to today's `at` (UTC), floored at 0.
- Produces: `poll_batch(batch_id, max_wait_secs=43200)`, unchanged signature. New behaviour: it always makes at least one status check, and never sleeps past the deadline. A `requests.RequestException` from a status check or from `fetch_batch_results` is logged, with `resp.text` when there is a response, and polling continues until the deadline.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timezone
import brief, common

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)

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
    compose = (brief.Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
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
    monkeypatch.setattr(brief.time, "time", lambda: clock["t"])
    def _sleep(s):
        clock["t"] += s
        sleeps.append(s)
    sleeps, gets = [], []
    monkeypatch.setattr(brief.time, "sleep", _sleep)
    monkeypatch.setattr(brief.requests, "get", _status_get(gets, "in_progress"))
    assert brief.poll_batch("b1", max_wait_secs=100) is None
    assert sum(sleeps) == 100  # 60, then the remaining 40
    assert len(gets) == 3

def test_poll_survives_a_failed_status_check(monkeypatch):
    # first GET raises requests.HTTPError (529), second answers "ended"; results fetch -> "B"
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    monkeypatch.setattr(brief, "fetch_batch_results", lambda url: "B")
    ...
    assert brief.poll_batch("b1", max_wait_secs=600) == "B"
```

`_status_get(gets, status)` is a test helper returning a fake `requests.get` that appends the URL to `gets` and answers `{"processing_status": status}`, with `raise_for_status()` a no-op.

- [ ] **Step 2: Run them and confirm they fail**

Run: `py -m pytest tests/test_brief_fallback.py -q`
Expected: FAIL (`fallback_wait_secs` undefined, knobs missing; the deadline-passed poll makes 0 GETs).

- [ ] **Step 3: Implement**

Add both `KNOBS` entries, each with a comment saying what it controls and why the default is what it is (§6.1). Add the two compose lines. Add `fallback_wait_secs`; parse with `datetime.strptime(at, "%H:%M")`. Restructure `poll_batch`'s loop: check (inside a `try` that catches `requests.RequestException`, logs it and falls through), return if `ended`, compute `remaining = deadline - time.time()`, break if `remaining <= 0`, otherwise `time.sleep(min(sleep, 300, remaining))` and grow `sleep` as today.

- [ ] **Step 4: Run them and confirm they pass**

Run: `py -m pytest tests/test_brief_fallback.py tests/test_batch_and_chroma.py -q`. Expected: PASS.

- [ ] **Step 5: Gate and commit**

```bash
git add common.py docker-compose.yml brief.py tests/test_brief_fallback.py
git commit -F <msgfile>   # "feat(collect): fallback timing knobs and a deadline-bounded batch poll"
```

### Task 2: Submit persists the request it batched

**Files:**
- Modify: `brief.py` (`submit_batch` at about line 2862, `mode_submit` at about line 3166, `fetch_batch_results` at about line 2983)
- Modify: `retention.py` (`_families`)
- Test: `tests/test_brief_fallback.py`, `tests/test_retention.py`

**Interfaces:**
- Produces: `brief_request_params(system: str, prompt_user: str, *, web_search: bool = True) -> dict`, the `params` dict `submit_batch` sends (model, max_tokens, thinking, system, messages, tools). `submit_batch` builds its payload from it, so the batched and the replayed requests cannot drift.
- Produces: `save_brief_request(params: dict, day: str) -> None` and `load_brief_request(day: str) -> dict | None`. The file is `DATA_DIR / f"brief_request-{day}.json"`, written with `_write_json_atomic`. `load` returns `None` for a missing or unreadable file, and logs a warning when the file is unreadable.
- Produces: `_message_text(message: dict) -> str`, the no-separator join of `text` blocks. `fetch_batch_results` uses it.

- [ ] **Step 1: Write the failing tests**

```python
def test_submit_saves_exactly_the_params_it_batched(monkeypatch, tmp_path):
    # Stub mode_submit's inputs as tests/test_collect_day_key.py stubs collect's,
    # capture the batch POST body, then:
    saved = brief.load_brief_request(TODAY)
    assert saved == posted["requests"][0]["params"]

def test_a_missing_or_corrupt_request_file_loads_as_none(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    assert brief.load_brief_request("2000-01-01") is None
    (tmp_path / "brief_request-2000-01-01.json").write_text("{not json")
    assert brief.load_brief_request("2000-01-01") is None

def test_message_text_joins_without_a_separator():
    msg = {"content": [{"type": "text", "text": "a"}, {"type": "web_search_tool_result"},
                       {"type": "text", "text": ", b"}]}
    assert brief._message_text(msg) == "a, b"
```

In `tests/test_retention.py`, add `brief_request-*.json` to whichever test enumerates the families, asserting that an old one is pruned and a recent one kept.

- [ ] **Step 2: Run them and confirm they fail**

Run: `py -m pytest tests/test_brief_fallback.py tests/test_retention.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement**

Extract `brief_request_params` from `submit_batch`. In `mode_submit`, call `save_brief_request(brief_request_params(SYSTEM_PROMPT, prompt), today)` before `submit_batch`. If the save fails, log an error and continue: the batch still goes out, and only the fallback is lost. Add `(DATA_DIR, "brief_request-*.json")` to `retention._families`. Switch `fetch_batch_results` to `_message_text`.

- [ ] **Step 4: Run them and confirm they pass**, along with `tests/test_batch_and_chroma.py` (the existing join tests must stay green).

- [ ] **Step 5: Gate and commit.** Message: "feat(submit): persist the batched brief request for collect's fallback".

### Task 3: Cancel and the real-time call

**Files:**
- Modify: `brief.py` (beside `poll_batch`)
- Test: `tests/test_brief_fallback.py`

**Interfaces:**
- Consumes: `_message_text` (Task 2), `_post_messages(payload, *, timeout, max_attempts)` (existing).
- Produces: `cancel_batch(batch_id: str) -> None`, which POSTs `/v1/messages/batches/{id}/cancel`. It never raises: on a `requests.RequestException` it logs the error, plus `resp.text` when there is a response, and returns.
- Modifies: `_post_messages` gains `label: str = "Signals"`, used in its attempt-failed warning in place of the hard-coded "Signals". On an `HTTPError` the warning also includes `resp.text`. Existing callers are unchanged.
- Produces: `run_brief_realtime(params: dict, *, timeout: int, call=None) -> str | None`. `call` defaults to `_post_messages` with `max_attempts=1, label="Realtime brief"`. Retries follow the Global Constraints rule and live in `run_brief_realtime`, around each `call`. It continues on `stop_reason == "pause_turn"` up to `REALTIME_MAX_CONTINUATIONS = 3` times by resending the original messages, plus one assistant message holding every content block received so far, unchanged (thinking blocks included). It adds no "continue" user message. It returns the no-separator text of all responses when the last has `stop_reason == "end_turn"` and the text is non-empty, otherwise `None`. Each response logs `Realtime brief: stop_reason=… elapsed=…s usage(in=… out=… searches=…)`, the same `usage(...)` shape as the batch line, so spec §11's measurement can grep both.

- [ ] **Step 1: Write the failing tests**

```python
def test_realtime_returns_the_text_of_a_finished_turn():
    out = brief.run_brief_realtime(PARAMS, timeout=5, call=_answers(_resp("end_turn", "Brief.")))
    assert out == "Brief."

def test_realtime_continues_on_pause_turn_with_the_content_so_far():
    sent = []
    call = _answers(_resp("pause_turn", "Part one "), _resp("end_turn", "part two."), sent=sent)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "Part one part two."
    second = sent[1]["messages"]
    assert second[:-1] == PARAMS["messages"]
    assert second[-1] == {"role": "assistant", "content": _resp("pause_turn", "Part one ")["content"]}

def test_realtime_gives_up_after_the_continuation_limit():
    call = _answers(*[_resp("pause_turn", "x")] * 4)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None

def test_realtime_http_failure_returns_none():
    def boom(payload, **kw):
        raise brief.requests.ConnectionError("down")
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=boom) is None

def test_realtime_max_tokens_is_not_a_brief():
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=_answers(_resp("max_tokens", "cut"))) is None

def test_realtime_retries_an_overloaded_reply(monkeypatch):
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    call = _answers(_http_error(529), _resp("end_turn", "Brief."))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) == "Brief."

def test_realtime_gives_up_after_three_overloaded_replies(monkeypatch):
    monkeypatch.setattr(brief.time, "sleep", lambda s: None)
    call = _answers(*[_http_error(529)] * 3, _resp("end_turn", "never reached"))
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None

def test_realtime_never_retries_a_timeout(monkeypatch):
    sent = []
    call = _answers(brief.requests.ReadTimeout("slow"), _resp("end_turn", "x"), sent=sent)
    assert brief.run_brief_realtime(PARAMS, timeout=5, call=call) is None
    assert len(sent) == 1

def test_cancel_batch_swallows_an_http_error(monkeypatch):
    monkeypatch.setattr(brief.requests, "post", _raising_post(409, '{"error":"already ended"}'))
    brief.cancel_batch("b1")  # must not raise
```

`_resp(stop_reason, text)` builds `{"stop_reason": …, "content": [{"type": "text", "text": text}], "usage": {...}}`. `_answers(*resps, sent=None)` returns a fake `call(payload, **kw)` that records each payload in `sent` and answers in order, raising any item that is an exception. `_http_error(status)` builds a `requests.HTTPError` whose `.response.status_code` is `status` and `.response.text` is a short body.

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement** `cancel_batch` and `run_brief_realtime` as specified.
- [ ] **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Gate and commit.** Message: "feat(collect): batch cancel and a real-time brief call".

### Task 4: `obtain_brief`, collect wiring and the 04:30 schedule

**Files:**
- Modify: `brief.py` (new `obtain_brief` before `mode_collect`; `mode_collect` at about line 3177)
- Modify: `scheduler.py:75` and the comment block above `SCHEDULES`
- Test: `tests/test_brief_fallback.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `obtain_brief(batch_id: str, submit_day: str, *, now: datetime) -> tuple[str | None, str]`, with the path one of `"batch"`, `"realtime"` or `"none"`. It reads `common.BRIEF_FALLBACK_AT`, `common.BRIEF_REALTIME_TIMEOUT` and `common.JOB_MAX_RUNTIME_MINUTES` once at entry, and records `t0 = time.time()`. **Rule (user decision 2026-11-22, after a side-agent note): the batch is cancelled only once a replacement brief is in hand, so the fallback never does worse than today's wait.** Algorithm:
  1. `wait = fallback_wait_secs(now, at)`. If `None`: `raw = poll_batch(batch_id)` and return `(raw, "batch")`, or `(None, "none")` when `raw` is empty.
  2. `raw = poll_batch(batch_id, max_wait_secs=wait)`. If truthy, return `(raw, "batch")`.
  3. `params = load_brief_request(submit_day)`. If `None`, log an error. Otherwise `text = run_brief_realtime(params, timeout=…)`.
  4. If `text`: `cancel_batch(batch_id)` (the batch may still be running; this stops paying for it), then return `(text, "realtime")`.
  5. Otherwise go back to the batch: `raw = poll_batch(batch_id, max_wait_secs=max(0, budget - (time.time() - t0)))`, where `budget = (JOB_MAX_RUNTIME_MINUTES - RESUME_MARGIN_MINUTES) * 60` and `RESUME_MARGIN_MINUTES = 15` (a constant leaving collect time to deliver before the supervisor kills it). Return `(raw, "batch")` if truthy, else `(None, "none")`.

- [ ] **Step 1: Write the failing tests** (stub `poll_batch`, `cancel_batch`, `load_brief_request` and `run_brief_realtime` on `brief`, recording calls)

```python
def test_obtain_brief_uses_the_batch_when_it_ends_in_time(...):
    # poll -> "B" on its first call
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("B", "batch")
    assert cancels == [] and realtime_calls == []
    assert polls[0] == ("b1", 1800)

def test_obtain_brief_runs_realtime_before_it_cancels(...):
    # poll -> None; saved request present; realtime -> "R"
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("R", "realtime")
    assert events == ["poll", "realtime", "cancel"]

def test_obtain_brief_goes_back_to_the_batch_when_realtime_fails(...):
    # poll -> None, then "B"; realtime -> None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("B", "batch")
    assert cancels == []
    budget = (common.JOB_MAX_RUNTIME_MINUTES - brief.RESUME_MARGIN_MINUTES) * 60
    assert budget - 60 <= polls[1][1] <= budget

def test_obtain_brief_falls_back_when_the_batch_ended_without_success(...):
    # the batch had ended errored: poll returns None at once; realtime -> "R"
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == ("R", "realtime")

def test_obtain_brief_after_the_fallback_time_checks_once_then_falls_back(...):
    brief.obtain_brief("b1", DAY, now=AT_0715)
    assert polls[0] == ("b1", 0)

def test_obtain_brief_without_a_saved_request_waits_on_the_batch(...):
    # load_brief_request -> None; poll -> None, None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")
    assert realtime_calls == [] and cancels == [] and len(polls) == 2

def test_obtain_brief_reads_the_request_by_the_submit_day(...):
    brief.obtain_brief("b1", "2000-01-01", now=AT_0600)
    assert loaded_days == ["2000-01-01"]

def test_obtain_brief_never_raises(...):
    # run_brief_realtime stubbed to raise RuntimeError; poll -> None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")

def test_obtain_brief_with_the_fallback_off_waits_the_full_window(monkeypatch, ...):
    monkeypatch.setattr(common, "BRIEF_FALLBACK_AT", "off")
    # poll -> None
    assert brief.obtain_brief("b1", DAY, now=AT_0600) == (None, "none")
    assert polls == [("b1", 43200)] and cancels == []
```

Record `polls` as `(batch_id, max_wait_secs)`, with the default 43200 recorded when it is not passed.

In `tests/test_scheduler.py`:

```python
def test_submit_runs_at_0430_before_the_0600_collect():
    """Storylines spec §6.1, operator decision 2026-11-22 (news-brief-qap)."""
    submit = next(s for s in scheduler.SCHEDULES if s.job == "submit")
    collect = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    assert (submit.at, submit.grace_minutes) == ("04:30", 60)
    assert collect.at == "06:00"
```

In `tests/test_brief_fallback.py`, a collect-level test reusing `_stub_collect` from `tests/test_collect_day_key.py` (import it). It stubs `obtain_brief` to return `("R", "realtime")` and asserts `deliver` got a header containing `live fallback`. With `("B", "batch")`, the header must not contain it.

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement**

`obtain_brief` as specified. In `mode_collect`, replace `raw = poll_batch(batch_id)` with `raw, path = obtain_brief(batch_id, submit_day, now=datetime.now(timezone.utc))`, and log `Brief obtained: brief_path={path}`. When `path == "realtime"`, append ` <i>(live fallback)</i>` to the header. The existing `if raw:` / alert branch is unchanged. In `scheduler.py`, change submit to `"04:30"` and rewrite the comment so it no longer claims the cron-parity: say submit moved on 2026-11-22 (`news-brief-qap`, spec §6.1) so the brief covers Asia's day and the US close. Wrap steps 2–5 of `obtain_brief` in one `try`: any exception is logged with `log.exception` and returns `(None, "none")`. Three existing tests stub `poll_batch` with a one-argument lambda, which the new `max_wait_secs=` call breaks with a `TypeError`: `tests/test_collect_day_key.py:30`, `tests/test_commands.py:1524` and `tests/test_trading.py:598`. Change each to `lambda bid, **kw: "RAW"`.

- [ ] **Step 4: Run all of the above and confirm they pass**, plus `tests/test_collect_day_key.py`, `tests/test_commands.py` and `tests/test_trading.py`.
- [ ] **Step 5: Full gate with a database** (CLAUDE.md "Build & Test": Postgres container, `DATABASE_URL`, `tests/test_db.py` must report runs and not skips). Commit with message "feat(schedule): brief submitted at 04:30, collect falls back to real time at 06:30", with `news-brief-qap` alone on a line.

### Task 5: A replay script for the real-time call

Every test above stubs the HTTP call, so this script is what puts the real-time path in front of the real API before the bead closes.

**Files:**
- Create: `scripts/replay_brief_realtime.py` (`scripts/` is already copied by the Dockerfile; run as `docker compose run --rm --entrypoint python newsbrief scripts/replay_brief_realtime.py <YYYY-MM-DD>`)
- Test: `tests/test_brief_fallback.py`

**Interfaces:**
- Consumes: `load_brief_request(day)` (Task 2), `run_brief_realtime(params, *, timeout, call=None)` (Task 3), `common.BRIEF_REALTIME_TIMEOUT`.
- Produces: `main(argv: list[str], *, call=None) -> int`. It loads the saved request for the given day, runs it in real time and prints one line: `day=… elapsed=…s ok=… chars=…`. It sends nothing to Telegram and writes nothing. It returns 0 when a brief came back, 1 when none did, and 2 when the request file is missing (the message names the path).

- [ ] **Step 1: Write the failing tests**

```python
def test_replay_prints_a_summary_and_delivers_nothing(monkeypatch, tmp_path, capsys):
    # save a request under 2000-01-01 in tmp_path; stub telegram_send to fail the test if called
    assert replay.main(["2000-01-01"], call=_answers(_resp("end_turn", "Brief."))) == 0
    out = capsys.readouterr().out
    assert "day=2000-01-01" in out and "ok=True" in out and "chars=6" in out

def test_replay_without_a_saved_request_exits_2(monkeypatch, tmp_path, capsys):
    assert replay.main(["2000-01-01"]) == 2
    assert "brief_request-2000-01-01.json" in capsys.readouterr().out
```

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement** `main` and the `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))` guard.
- [ ] **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Gate and commit.** Message: "feat(scripts): replay a saved brief request in real time".

### Task 6: Deploy and the first-run checks (operator)

No code. The bead stays open until this is done.

- [ ] **Step 1:** Push only when the operator says so (the current instruction is not to push). Deploy **between 06:00 and 20:00 UTC** (spec §6.1 cutover: deployed between a 20:00 submit and its 06:00 collect, the 04:30 submit would orphan the first batch).
- [ ] **Step 2:** The evening after deploy, `/jobs` shows submit next due at 04:30.
- [ ] **Step 3:** The next morning, the host log shows `Brief obtained: brief_path=batch`, and `brief_request-<date>.json` exists on the volume for that date.
- [ ] **Step 4:** On the host, run the replay script once for that day's date (about $0.35). Record the printed `elapsed` in the bead notes. Set `BRIEF_REALTIME_TIMEOUT` to about twice that, if 600 is not already comfortably above it.
- [ ] **Step 5:** Update the memory note `batch-pipeline-timing.md`: the live schedule is 04:30/06:00, and the "until qap ships" line goes. Then close `news-brief-qap`.

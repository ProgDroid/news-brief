# Watch-file Event Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a nightly `watch` job that judges the event rows of the operator-pushed `DATA_DIR/watch.json` against the last 48 h of captured items, and alerts on Telegram once per row with code-supplied links.

**Architecture:** a new pure module `watch.py` holds the loader, the input builder, the request builder, the verdict validator and the alert planning. It takes the HTTP call and the DB connection as parameters. A thin `mode_watch` in `brief.py` wires those to Telegram and `runtime_state`. A `scripts/watch_replay.py` runs the same judge for a past date and prints verdicts, for the validation gate. The schedule entry lands last, only after the gate passes.

**Tech Stack:** Python 3.14, psycopg (existing `db.connect`), the Anthropic Messages API via the existing `brief._post_messages`, and pytest. Ruff owns formatting.

**Spec:** `docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md` (read it first). Bead: `news-brief-8lp`.

## Global Constraints

- The gate is three commands, all green: `ruff check .`, `ruff format --check .`, `py -m pytest -q`. Run DB-backed tests with `DATABASE_URL` exported (CLAUDE.md § Build & Test): **a skip is not a pass**.
- Never commit `watch.json`, real row names or triggers. Test rows are made up, like the bead's `"Example ceasefire signed"`. **The repo is public.**
- Never log a row's `trigger` text at INFO. Log row ids and counts only.
- Read knobs as `common.X` at call time, never `from common import X` (memory `newsbrief-flag-access-module-attr`).
- Every Anthropic call sets `"thinking": {"type": "disabled"}` explicitly.
- Tests that send patch `brief.telegram_send`, `common.telegram_send` and `common.telegram_send_long` (memory `telegram-send-long-convention`).
- Never give the model a database id. Item labels are `I1..In`, assigned per run.
- Window = 48 h trailing `as_of`, by `items.created_at`. Input ceiling warning = 5,000 titles (after collapsing). Max 3 links per alert.
- `runtime_state` key prefix for per-row alert state: `watch.alerted.` (one key per row id).
- `git commit` only from the Bash tool (BOM trap). Commit explicit paths, never `-A`. The user runs concurrent sessions.

## Review Focus

1. **A trigger text the model echoes back with HTML in it** (`<`, `&` in a made-up row or a headline). Every alert field is `html.escape`d before sending. Test in Task 4.
2. **The same row id twice in one model response.** The first verdict wins and the duplicate is ignored, never two alerts for one row. Test in Task 3.
3. **A headline with an empty or whitespace title in `items`.** It's dropped from the input, never labelled. Test in Task 2.
4. **A row removed from `watch.json` and later re-added.** It alerts again, because its key was cleared. Test in Task 4.
5. **`as_of` passed as a naive datetime by the replay script.** It's treated as UTC, never as local time. Test in Task 2.

---

### Task 1: The loader (`watch.load_watch`)

**Files:**
- Create: `watch.py`
- Modify: `Dockerfile:39` (add `watch.py` to the flat `COPY` line); `.github/workflows/docker-publish.yml` (add `watch.py` to the push `paths:` filter, plus any second list `tests/test_packaging.py` checks)
- Test: `tests/test_watch_loader.py`

**Interfaces:**
- Produces:
  - `class WatchFileError(Exception)`
  - `@dataclass(frozen=True) class EventRow: id: str; name: str; trigger: str`
  - `@dataclass(frozen=True) class PriceRow: id: str; name: str; action: str; signal: str; condition: str; level: float | None`
  - `@dataclass(frozen=True) class WatchFile: events: list[EventRow]; prices: list[PriceRow]; skipped: list[tuple[str, str]]` (`(row id or "#<index>", reason)`)
  - `def load_watch(path: Path) -> WatchFile`

- [ ] **Step 1: Write the failing tests**

```python
# File-level: each raises WatchFileError whose str() contains the reason word.
@pytest.mark.parametrize("body,word", [
    (None, "missing"),                                   # file absent
    ("{not json", "unreadable"),
    ("[]", "object"),
    ('{"version": 2, "rows": []}', "version"),
    ('{"version": 1, "rows": {}}', "rows"),
    ('{"version": 1, "rows": [EV_E1, EV_E1]}', "duplicate"),
])
def test_file_level_problems_reject_the_whole_file(tmp_path, body, word): ...

def test_file_is_left_byte_identical_after_a_rejection(tmp_path): ...

# Row-level: only the bad row is skipped, named, and the others survive.
@pytest.mark.parametrize("bad_row,reason_word", [
    ({"id": "x", "kind": "event", "name": "n"}, "trigger"),
    ({"id": "x", "kind": "event", "name": "n", "trigger": "  "}, "trigger"),
    ({"kind": "event", "name": "n", "trigger": "t"}, "id"),
    ({"id": "x", "kind": "telepathy", "name": "n"}, "kind"),
    ({"id": "x", "kind": "price", "name": "n", "action": "add", "signal": "QQQ", "condition": "sideways", "level": 1.0}, "condition"),
    ({"id": "x", "kind": "price", "name": "n", "action": "add", "signal": "QQQ", "condition": "below", "level": None}, "level"),
])
def test_bad_row_is_skipped_and_named_while_good_rows_survive(tmp_path, bad_row, reason_word):
    wf = load_watch(write(tmp_path, rows=[EV_E1, bad_row, PRICE_P1]))
    assert [r.id for r in wf.events] == ["e1"] and [r.id for r in wf.prices] == ["p1"]
    assert len(wf.skipped) == 1 and reason_word in wf.skipped[0][1]

def test_above_ma200_needs_no_level(tmp_path): ...       # level None accepted
def test_zero_event_rows_is_valid(tmp_path): ...          # rows=[] -> WatchFile([], [], [])
```

`EV_E1`, `PRICE_P1`: the bead's made-up example rows.

- [ ] **Step 2: Run** `py -m pytest tests/test_watch_loader.py -q`. Expected: it fails with `ModuleNotFoundError: watch`.
- [ ] **Step 3: Implement `load_watch` in `watch.py`.** Read with `path.read_text(encoding="utf-8")` and never rename the file. The reason strings must contain the words the tests assert. Duplicate ids are counted over all rows that have a string id.
- [ ] **Step 4: Add `watch.py` to the Dockerfile COPY line and the workflow paths filter.**
- [ ] **Step 5: Run** `py -m pytest tests/test_watch_loader.py tests/test_packaging.py -q`. Expected: all pass.
- [ ] **Step 6: Commit** `watch.py tests/test_watch_loader.py Dockerfile .github/workflows/docker-publish.yml`, with the message `feat(watch): loader for the operator-pushed watch file`.

### Task 2: The window and the judge input

**Files:**
- Modify: `watch.py`
- Test: `tests/test_watch_input.py` (pure tests) and `tests/test_watch_window_db.py` (DB-backed, using the `store` fixture style from `tests/test_capture_store.py:18`, with `pytestmark` skipif)

**Interfaces:**
- Consumes: `EventRow` (Task 1)
- Produces:
  - `WINDOW_HOURS = 48`, `INPUT_CEILING = 5000`
  - `@dataclass(frozen=True) class Item: title: str; url: str; outlet: str`
  - `def fetch_window_items(conn, as_of: datetime) -> list[Item]`: `items JOIN outlets`, `created_at > as_of - 48h AND created_at <= as_of`, ordered by `created_at`. A naive `as_of` is treated as UTC.
  - `@dataclass(frozen=True) class JudgeInput: text: str; labels: dict[str, list[Item]]; n_titles: int`
  - `def build_judge_input(rows: list[EventRow], items: list[Item]) -> JudgeInput`

- [ ] **Step 1: Write the failing tests**

```python
def test_identical_titles_collapse_to_one_label_keeping_every_url():
    ji = build_judge_input([EV], [Item("Truce agreed", "u1", "A"), Item("Truce agreed", "u2", "B"), Item("Other", "u3", "A")])
    assert ji.n_titles == 2
    assert sorted(i.url for i in ji.labels["I1"]) == ["u1", "u2"]
    assert "I1" in ji.text and "I2" in ji.text and "u1" not in ji.text   # URLs never reach the model

def test_blank_titles_are_dropped(): ...                  # Item("  ", ...) gets no label
def test_rows_appear_by_id_name_and_trigger(): ...
def test_labels_are_opaque_not_db_ids(): ...              # every key matches r"^I\d+$"

# DB-backed
def test_window_is_exactly_the_48h_before_as_of(store):   # items at -49h, -47h, +1h -> only -47h
def test_naive_as_of_is_treated_as_utc(store): ...
```

- [ ] **Step 2: Run both files.** Expected: the pure tests fail on import (`Item` undefined). The DB file **must report failures, not skips**: export `DATABASE_URL` first (CLAUDE.md).
- [ ] **Step 3: Implement** `fetch_window_items` and `build_judge_input`. Title collapsing keys on `title.strip().casefold()`. Labels are assigned in first-seen order.
- [ ] **Step 4: Run both files.** Expected: all pass, and the DB tests count as passed.
- [ ] **Step 5: Commit**, with the message `feat(watch): 48h window and labelled judge input`.

### Task 3: The request and the validator

**Files:**
- Modify: `watch.py`
- Test: `tests/test_watch_verdicts.py`

**Interfaces:**
- Consumes: `EventRow`, `JudgeInput`, `Item`
- Produces:
  - `@dataclass(frozen=True) class Verdict: row_id: str; verdict: str; evidence: list[Item]; reason: str`. `verdict` is one of `"triggered" | "not" | "unclear" | "invalid"`.
  - `def build_judge_request(rows: list[EventRow], ji: JudgeInput, *, model: str) -> dict`: a forced tool `emit_verdicts`, `"thinking": {"type": "disabled"}`, `max_tokens = 512 + 300 * len(rows)`. Schema: `verdicts[]` of `{row_id, verdict: enum[triggered,not,unclear], evidence: [string], reason: string}`.
  - `def validate_verdicts(resp: dict, rows: list[EventRow], ji: JudgeInput) -> list[Verdict]`: exactly one per row, in row order. Never raises on a bad model answer. A response with no tool block gives every row `invalid`, with the reason naming `stop_reason`.
  - `MAX_LINKS = 3`

- [ ] **Step 1: Write the failing tests**

```python
def test_request_forces_the_tool_and_disables_thinking():
    req = build_judge_request([EV], JI, model="m")
    assert req["tool_choice"] == {"type": "tool", "name": "emit_verdicts"}
    assert req["thinking"] == {"type": "disabled"} and req["model"] == "m"

def test_labels_map_to_urls_in_code(): ...                # evidence ["I1"] -> Verdict.evidence == JI.labels["I1"][:3]
def test_unknown_label_makes_that_row_invalid(): ...      # other rows unaffected
def test_unknown_row_id_is_ignored_and_missing_row_is_invalid(): ...
def test_duplicate_row_id_first_wins(): ...               # Review Focus 2
def test_triggered_without_evidence_is_downgraded_to_unclear(): ...
def test_no_tool_block_marks_every_row_invalid_naming_stop_reason(): ...  # resp {"stop_reason": "max_tokens", "content": []}
def test_evidence_is_capped_at_three_links(): ...
```

- [ ] **Step 2: Run.** Expected: fails on import.
- [ ] **Step 3: Implement** both functions.
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit**, with the message `feat(watch): judge request and code-side verdict validation`.

### Task 4: Alert planning (once per row)

**Files:**
- Modify: `watch.py`
- Test: `tests/test_watch_alerts.py`

**Interfaces:**
- Consumes: `EventRow`, `Verdict`
- Produces:
  - `STATE_PREFIX = "watch.alerted."`
  - `def strength(v: Verdict) -> int`: `triggered` → 2, `unclear` with evidence → 1, else 0
  - `def trigger_hash(row: EventRow) -> str`: the first 16 hex chars of sha256 over `row.trigger`
  - `@dataclass(frozen=True) class Alert: row: EventRow; verdict: Verdict; key: str; value: dict` (`value = {"hash": ..., "strength": ...}`)
  - `def plan_alerts(rows: list[EventRow], verdicts: list[Verdict], state: dict) -> tuple[list[Alert], list[str]]`: returns (alerts to send, stale `watch.alerted.*` keys whose row id is no longer in `rows`)
  - `def format_alert(a: Alert) -> str`, `def format_problems(skipped: list[tuple[str, str]], invalid: list[Verdict]) -> str | None`. These are HTML, every interpolated field `html.escape`d. The alert's first line is `🔔 Watch <id>: <name>. TRIGGERED|UNCLEAR`, and each evidence line is `<a href=url>outlet: title</a>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_trigger_alerts(): ...
def test_repeat_at_same_strength_is_silent(): ...         # state holds {"hash": h, "strength": 2}
def test_edited_trigger_alerts_again(): ...               # hash differs
def test_unclear_then_triggered_alerts_again(): ...
def test_triggered_then_unclear_or_not_is_silent(): ...
def test_not_and_unclear_without_evidence_never_alert(): ...
def test_removed_row_key_is_returned_for_clearing(): ...  # Review Focus 4: state has watch.alerted.gone
def test_format_alert_escapes_html(): ...                 # Review Focus 1: name "A<b>&" -> "A&lt;b&gt;&amp;"
def test_format_problems_none_when_nothing_to_report(): ...
```

- [ ] **Step 2: Run.** Expected: fails on import.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: pass.
- [ ] **Step 5: Commit**, with the message `feat(watch): once-per-row alert planning`.

### Task 5: `mode_watch`, the knob, and the job wiring (not scheduled)

**Files:**
- Modify: `brief.py`: add `WATCH_FILE = DATA_DIR / "watch.json"` near `THESIS_FILE`, add `mode_watch`, add `"watch"` to `MODES` and to `JOB_MODES`, and add `watch` to the usage string. **Not** to `scheduler.SCHEDULES`.
- Modify: `common.py`: add the knob `"WATCH_MODEL": Knob(str, "", env="NEWSBRIEF_WATCH_MODEL")` beside `SIGNALS_MODEL`, under its comment.
- Modify: `docker-compose.yml`: add `- NEWSBRIEF_WATCH_MODEL=${NEWSBRIEF_WATCH_MODEL:-}` to the anchor, beside `NEWSBRIEF_SIGNALS_MODEL` (memory `env-var-needs-compose-passthrough`).
- Test: `tests/test_watch_mode.py`

**Interfaces:**
- Consumes: everything in Tasks 1–4; `brief._post_messages(payload, *, timeout, max_attempts)`; `config.runtime_state()`, `config.set_runtime_state(dict)`, `config.clear_runtime_state(keys)`; `brief._alert_once`; `common.telegram_send_long`.
- Produces:
  - `WATCH_TIMEOUT = 180`, `WATCH_MAX_ATTEMPTS = 2` (starting values; resize from the first measured call, per the spec's "left to the builder")
  - `def judge_watch(conn, rows: list[EventRow], as_of: datetime, *, post) -> tuple[JudgeInput, list[Verdict]]` in `watch.py`. It fetches the window, raises `WatchFileError` naming "capture may be down" on zero items, then builds, posts and validates. Before validating it logs `stop_reason`, usage and `n_titles`. This is the **one function the replay script shares**.
  - `def mode_watch(*, as_of: datetime | None = None, post=None) -> None` in `brief.py`

`mode_watch` order: load (a `WatchFileError` propagates, and `run_job` makes it red plus a Telegram alert). If there are no event rows, return. Then `judge_watch`. Then the ceiling check (`n_titles > INPUT_CEILING` → `_alert_once("watch.input_ceiling", …)`, else clear it). Then `plan_alerts`. Send each alert with `telegram_send_long` and store its key **only after a True send**. Clear the stale keys. Send `format_problems(...)` if it isn't None. Finally, raise `RuntimeError` naming the failed row ids if any send returned False.

- [ ] **Step 1: Write the failing tests** (patch `brief.WATCH_FILE`, `watch.fetch_window_items` and the three send channels; fake `config.runtime_state` / `set_runtime_state` / `clear_runtime_state` with an in-memory dict; pass `post=` a fake returning a canned `emit_verdicts` response)

```python
def test_trigger_sends_once_and_stores_state(): ...
def test_second_run_same_verdict_sends_nothing(): ...
def test_failed_send_stores_no_state_and_raises(): ...    # send returns False -> RuntimeError, dict unchanged for that row
def test_missing_file_raises_watch_file_error(): ...
def test_zero_items_raises_capture_may_be_down(): ...
def test_skipped_rows_and_invalid_verdicts_reported_every_run(): ...  # two runs -> two problem messages
def test_over_ceiling_warns_once_and_still_judges(): ...
def test_no_event_rows_makes_no_model_call(): ...         # post fake asserts not called
def test_model_follows_MODEL_unless_WATCH_MODEL_set(monkeypatch): ...
def test_watch_is_a_job_mode_but_not_scheduled():
    assert "watch" in brief.MODES and "watch" in brief.JOB_MODES
    assert all(s.job != "watch" for s in scheduler.SCHEDULES)
```

- [ ] **Step 2: Run.** Expected: fails (`mode_watch` is undefined).
- [ ] **Step 3: Implement** the knob, the compose line, `judge_watch` and `mode_watch`. `post` defaults to `lambda p: _post_messages(p, timeout=WATCH_TIMEOUT, max_attempts=WATCH_MAX_ATTEMPTS)`. The model is `common.WATCH_MODEL or common.MODEL`, read at call time.
- [ ] **Step 4: Run the full gate** (three commands, DB exported). Expected: all green, with the new DB tests counted as passed.
- [ ] **Step 5: Run** `docker compose config >/dev/null && echo OK` (never `up`: a second `getUpdates` consumer 409s the live bot). Expected: `OK`.
- [ ] **Step 6: Commit**, with the message `feat(watch): mode_watch job, WATCH_MODEL knob (unscheduled until the replay gate)`.

### Task 6: The replay script

**Files:**
- Create: `scripts/watch_replay.py`
- Test: `tests/test_watch_replay.py`

**Interfaces:**
- Consumes: `watch.load_watch`, `watch.judge_watch`, `brief._post_messages`, `db.connect`
- Produces: the CLI `py scripts/watch_replay.py --as-of 2026-10-01T06:30:00Z --rows /path/rows.json`. It prints one line per row: `row_id  verdict  n_links  reason`, then each link. **It never sends and never writes state.** Exit 0 if it judged; exit 1 with the error printed on `WatchFileError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_replay_prints_verdicts_and_never_sends_or_writes(monkeypatch, tmp_path, capsys):
    # three send channels patched to raise; config.set_runtime_state patched to raise;
    # judge_watch patched to return a canned (JI, [Verdict]) -> main([...]) == 0,
    # stdout has "e1  triggered"
def test_replay_calls_the_production_judge(monkeypatch): ...  # judge_watch is watch.judge_watch, not a copy
def test_replay_bad_rows_file_exits_1(tmp_path): ...
```

- [ ] **Step 2: Run.** Expected: fails (no script).
- [ ] **Step 3: Implement** `main(argv: list[str] | None = None) -> int`, with argparse and the `sys.path` insertion style used by the existing `scripts/probe_*.py`.
- [ ] **Step 4: Run** the gate. Expected: green.
- [ ] **Step 5: Add a note to bead `news-brief-115`** (`bd update news-brief-115 --append-notes=...`): removing `scripts/` from the image would delete `scripts/watch_replay.py`, the 8lp gate tool. Move it or keep `scripts/` until 8lp's gate has passed.
- [ ] **Step 6: Commit**, with the message `feat(watch): replay script for the validation gate`.

### Task 7: The validation gate (hands-on, operator)

No code. File it as a bead, child of `news-brief-8lp`, labelled `hands-on`.

- [ ] **Step 1: Pick the events, before running anything.** With the operator, pick 3–5 past events covered by the KB since 2026-09-04, and write one made-up `kind: "event"` row each, plus 2 rows that should be `not` on those dates. **Record each row's expected verdict and `as_of` in the bead's notes** before any run.
- [ ] **Step 2: Deploy** Tasks 1–6.
- [ ] **Step 3: The operator runs the replays on the host:** `docker compose run --rm --entrypoint python newsbrief scripts/watch_replay.py --as-of … --rows …`, once per date (the image's entrypoint is `python brief.py`, so `--entrypoint python` is required).
- [ ] **Step 4: Score it against the pre-registered table.** It passes if every expected `triggered` comes back `triggered` or `unclear` with a link that is really the event, and no expected-`not` row reaches strength ≥ 1. On a miss, reopen the design (bodies? another cut?), not the expectations.
- [ ] **Step 5: Record** the scored table in the bead and append the result to the spec.

### Task 8: Schedule it (only after Task 7 passes)

**Files:**
- Modify: `scheduler.py` `SCHEDULES`: `Schedule("watch", "daily", "06:30", None, grace_minutes=180)`, plus one line in the comment block above it saying why (after collect, so it lands with the brief, and it doesn't depend on it).
- Modify: `tests/test_watch_mode.py`: flip `test_watch_is_a_job_mode_but_not_scheduled` to assert the schedule is present and is `daily` at `06:30`.

- [ ] **Step 1:** Flip the test. **Run it.** Expected: it fails.
- [ ] **Step 2:** Add the schedule entry. **Run the gate.** Expected: green.
- [ ] **Step 3: Commit**, with the message `feat(watch): schedule the watch job at 06:30` and `news-brief-8lp` alone on a line in the body (it completes the bead).
- [ ] **Step 4: After deploy, verify by effect.** The next morning, `/jobs` shows a `watch` row with exit 0, and the job log shows `n_titles` and the `stop_reason`. Then close `news-brief-8lp`, naming that observation.

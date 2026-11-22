# Storylines Phase 1 (Dated Memory) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give today's brief dated memory. Ledger claims show when they were first seen, a 7-day window of past headlines replaces the weekly slice, and a thesis evidence log is kept from each brief's POSITION SIGNALS and read back with `/evidence`.

**Architecture:** One new top-level module, `dated_memory.py`, holds the pure parts (a section parser for archived briefs, the window builder, the evidence request and its validator) and owns all `thesis_evidence` SQL, as `claim_store.py` owns the ledger's. `brief.py` wires it in three places: `mode_submit` (window + dated claims), `mode_collect` (evidence extraction after delivery) and the commands daemon (`/evidence`). Everything sits behind one settings row, `BRIEF_DATED_MEMORY_ENABLED`, read once per pass.

**Tech Stack:** Python 3.12, psycopg 3, Postgres 18, the Anthropic Messages API over `requests` (`brief._post_messages`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-21-storylines-design.md` §3 (also §5.3's phase 1 line, §11, §12, §13, §14's `thesis_evidence`). Bead `news-brief-al7.1`.

## Global Constraints

- **The repo is public.** No test, fixture, log line or doc names a real holding, amount or thesis text. Tests use invented keys (`TESTCO`, `cluster:widgets`) and invented headlines. Evidence rows live only in the host database (spec §13).
- **Knob off means today's prompt, byte for byte.** `BRIEF_DATED_MEMORY_ENABLED` defaults to `false`.
- **Knobs are read as `common.X`**, never `from common import X`, and **once per pass**: one local at the top of `mode_submit` and `mode_collect`, then passed down.
- **A new knob needs a `KNOBS` entry and its compose anchor line** (`docker-compose.yml`, the block that holds `BRIEF_FALLBACK_AT`). `tests/test_config.py` enforces the pairing.
- **A new top-level module needs the Dockerfile COPY line, the workflow `paths:` list and BOTH workflow ruff lines** (`.github/workflows/docker-publish.yml:79` `ruff check` and `:80` `ruff format --check`). `tests/test_packaging.py` enforces it, and a new script under `scripts/` needs the `sys.path` shim (`scripts/replay_brief_realtime.py:24-25`, enforced by `test_a_script_run_as_a_path_can_import_the_root_modules`).
- **"Unchanged when off" is proven against golden text from HEAD**, captured before the edit with a frozen clock and committed as a literal, never by comparing the edited function with itself.
- **Every model call sets `thinking` explicitly** (`{"type": "disabled"}` here), or adaptive thinking eats `max_tokens`.
- **Labels, not keys:** the model sees thesis keys as `T1…Tn`. An unknown label invalidates that row (counted and logged, never written), and is never a fall-through.
- **Rows are dated by the submit day** (`state["date"]`), never collect-time `now()` (`news-brief-hw8`).
- **No DELETE** from `thesis_evidence`, ever. The down migration refuses while rows exist.
- **resolution_date stays quarantined** (operator decision 2026-11-22, which overrides spec §3.1's "resolves 12-01"): dated claims render `first_seen` only.
- **Migration number:** this plan takes `0018_thesis_evidence`. The storylines migration (spec §14's `0018_storylines`) becomes `0019`, and that's where the `storyline_id` foreign key gets added. `news-brief-ag1` (census removal) must take the next free number after whichever lands first.
- **The pre-push gate is three commands:** `ruff check .`, `ruff format --check .`, `py -m pytest -q`, with `DATABASE_URL` exported so the DB tests run rather than skip (see `CLAUDE.md` § Build & Test). Run it in the FOREGROUND.

## Review Focus

1. **A week with gaps or no archive.** A missing day, a first deploy with no `briefs/` files, or briefs older than 7 days. Expected: the window spans only the last 7 calendar days before the submit day, skips missing days silently, and is empty (no block at all) when nothing qualifies. Test in Task 2.
2. **A brief the parser doesn't recognise:** a heading the model reworded, a section left out (POSITION SIGNALS is omitted on quiet days by design), HTML entities in the archive. Expected: missing sections contribute nothing, no crash, and `&amp;` reads as `&`. Tests in Task 2; real-archive check in Task 2's preview script.
3. **Collect run twice for one submit day** (a manual `/run collect` after a partial failure). Expected: a day's evidence is never written twice. When the first pass wrote rows, the second logs `already recorded` and makes no model call. When it wrote none (quiet day, crash), the second may call again, and that's harmless. Tests for both cases in Task 5.
4. **A thesis key that disappears** (removed from `theses.json` or the pushed file) while it still has rows. Expected: `/evidence` still offers it in the picker for 30 days and lists its rows. Test in Task 6.
5. **A claim first seen in an earlier year.** `since 12-01` shown in the following December would be read as a few days old. Expected: `MM-DD` within the submit day's year, the full `YYYY-MM-DD` otherwise; a missing or malformed `first_seen` drops the date and keeps the row. Test in Task 3.

---

### Task 1: The `thesis_evidence` table and its store

**Files:**
- Create: `migrations/0018_thesis_evidence_up.sql`, `migrations/0018_thesis_evidence_down.sql`
- Create: `dated_memory.py` (store functions only in this task)
- Modify: `Dockerfile:41` (add `dated_memory.py` to the flat COPY line)
- Modify: `.github/workflows/docker-publish.yml` (`paths:` list after `'watch.py'`; both ruff lines, `:79` and `:80`)
- Test: `tests/test_thesis_evidence.py` (DB-gated, same `skipif` and `conn`/`store` fixtures as `tests/test_claim_store.py:15-39`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class EvidenceRow: thesis_key: str; direction: str; line: str` (`direction` ∈ `"supports"`, `"challenges"`)
  - `save_brief_evidence(conn, on_date: str, rows: list[EvidenceRow], *, model: str) -> int`: inserts with `source='brief'`, returns the count written. `on_date` is `YYYY-MM-DD`.
  - `has_brief_evidence(conn, on_date: str) -> bool`
  - `recent_evidence(conn, thesis_key: str, *, since: str) -> list[dict]`: rows with `on_date >= since`, newest first (`on_date DESC, id DESC`); keys `on_date` (str), `direction`, `line`, `source`.
  - `keys_with_evidence(conn, *, since: str) -> list[str]`: distinct keys with a row on or after `since`, sorted.

Schema (the migration; refine names only if a test needs it):

```sql
CREATE TABLE thesis_evidence (
    id           BIGSERIAL   PRIMARY KEY,
    thesis_key   TEXT        NOT NULL CHECK (thesis_key <> ''),
    storyline_id BIGINT      NULL,      -- FK added by 0019_storylines
    on_date      DATE        NOT NULL,
    direction    TEXT        NOT NULL CHECK (direction IN ('supports', 'challenges')),
    line         TEXT        NOT NULL CHECK (length(line) BETWEEN 1 AND 300),
    item_ids     BIGINT[]    NOT NULL DEFAULT '{}',
    source       TEXT        NOT NULL CHECK (source IN ('brief', 'storyline')),
    model        TEXT        NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source = 'storyline' OR storyline_id IS NULL)
);
CREATE INDEX thesis_evidence_key_date ON thesis_evidence (thesis_key, on_date DESC);
CREATE INDEX thesis_evidence_date_source ON thesis_evidence (on_date, source);
```

The down migration refuses while rows exist, using the nested-IF `to_regclass` shape of `migrations/0017_census_down.sql:1-14`, then drops the table.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_and_read_back_newest_first(store):
    rows = [dated_memory.EvidenceRow("TESTCO", "supports", "Widget orders beat forecast")]
    assert dated_memory.save_brief_evidence(store, "2026-11-20", rows, model="m") == 1
    dated_memory.save_brief_evidence(
        store, "2026-11-21", [dated_memory.EvidenceRow("TESTCO", "challenges", "Regulator opens probe")], model="m"
    )
    got = dated_memory.recent_evidence(store, "TESTCO", since="2026-11-01")
    assert [(r["on_date"], r["direction"], r["source"]) for r in got] == [
        ("2026-11-21", "challenges", "brief"), ("2026-11-20", "supports", "brief"),
    ]

def test_recent_evidence_respects_since(store):  # a row before `since` is not returned
def test_has_brief_evidence_is_per_day(store):    # True for the saved day, False for the next
def test_keys_with_evidence_is_distinct_and_sorted(store)
def test_direction_outside_the_enum_is_refused_by_postgres(store):
    with pytest.raises(psycopg.errors.CheckViolation): ...   # raw INSERT with 'neutral'
def test_brief_row_cannot_carry_a_storyline_id(store)       # CheckViolation
def test_down_refuses_while_rows_exist(store):
    # save one row, then run_migrations(direction="down",
    #   steps=conftest.steps_back_through(store, "0018_thesis_evidence")) raises;
    # the table still exists afterwards
def test_down_drops_the_empty_table(store)                  # negative control for the above
```

- [ ] **Step 2: Run them to see them fail**

Run: `py -m pytest tests/test_thesis_evidence.py -q` with `DATABASE_URL` exported.
Expected: FAIL / errors on `import dated_memory`. A run that reports **skipped** hasn't tested anything: export the database first.

- [ ] **Step 3: Write the two migrations and the store functions in `dated_memory.py`**

The module docstring states its two jobs (spec §3 phase 1; owns all `thesis_evidence` SQL). Add `dated_memory.py` to the Dockerfile COPY line and both workflow lists.

- [ ] **Step 4: Run the tests and the packaging test**

Run: `py -m pytest tests/test_thesis_evidence.py tests/test_packaging.py tests/test_db.py -q`
Expected: all pass, none skipped.

- [ ] **Step 5: Commit**

```bash
git add migrations/0018_thesis_evidence_up.sql migrations/0018_thesis_evidence_down.sql dated_memory.py Dockerfile .github/workflows/docker-publish.yml tests/test_thesis_evidence.py
git commit -m "feat(dated-memory): thesis_evidence table and store (0018)"
```

---

### Task 2: Parse archived briefs and build the 7-day window

**Files:**
- Modify: `dated_memory.py`
- Create: `scripts/preview_dated_window.py`
- Test: `tests/test_dated_window.py` (no database)

**Interfaces:**
- Consumes: `brief.load_last_n_briefs(n) -> list[tuple[str, str]]` (date `YYYY-MM-DD`, archived text, oldest first). The archive is the model's HTML with tags stripped (`brief.deliver`, `brief.py:3233`), so entities like `&amp;` remain.
- Produces:
  - `brief_sections(text: str) -> dict[str, list[str]]`: keys `"top"`, `"threads"`, `"watch"`, `"positions"`, present only when the section has entries. Each entry is its full text, lines joined with `"\n"`, bullet marker stripped.
  - `build_window(briefs: list[tuple[str, str]], today: str) -> WindowResult`
  - `@dataclass(frozen=True) class WindowResult: block: str; days: int; lines: int; chars: int` (`block == ""` when nothing qualifies)
  - `DATED_WINDOW_MAX_CHARS = 12_000`. Spec §3.2's ceiling is ~3k tokens; at an assumed 4 characters a token this is 3–4k tokens (unmeasured, and the Sonnet 5 tokenizer runs about 30% heavier, `brief.py:2759`). The rollout reads the real figure from the first knob-on day's `usage` line.
  - `ENTRY_MAX_CHARS = 200`

Parsing rules, which the tests pin:
- First strip any HTML tags and run `html.unescape`, so the function takes either the raw HTML brief (Task 5 passes it) or the archive.
- **Heading:** a stripped line that doesn't start with `•`, `-`, `*` or a digit, is at most 80 characters, contains at least 3 letters, and has every letter uppercase. The known headings match by substring: `TOP STORIES` → `top`, `THREADS THAT MOVED` → `threads`, `WATCH / FORWARD` → `watch`, `POSITION SIGNALS` → `positions`. Any other heading (`📈 MARKET PULSE — WHAT MOVED`, a dynamic `🇯🇵 JAPAN`, `📊 MACRO SIGNAL`) closes the current section.
- **Entry:** inside a known section, a line starting with `•` or `- ` starts an entry. So does a non-bullet line that follows the heading or a blank line. Any other non-empty line continues the current entry.

Window rules:
- Keep briefs dated from `today − 7` to `today − 1` inclusive, compared as dates (gaps are fine; an older brief is dropped even if `load_last_n_briefs(7)` returned it).
- For each kept brief, oldest first: one line per entry of `top`, `threads` and `watch` (in that order), as `"{MM-DD} [{tag}] {first line of the entry}"`, where tag is `top`, `thread` or `watch`. Each line is cut to `ENTRY_MAX_CHARS`, ending in `…` when cut.
- Over `DATED_WINDOW_MAX_CHARS`: drop whole days, oldest first. If the newest day alone is still over, drop its trailing lines.
- `block` is the heading copy below plus the lines; `chars` counts the whole block.

Heading copy (exact; it goes into the brief prompt):

```text
## THE LAST 7 DAYS — ALREADY REPORTED
Headlines, threads and forward-looks from the past week's briefs, each with the date it ran (MM-DD, UTC). Use them for continuity: when today's news follows from one of these, say so with its date ("follows from 11-14") instead of explaining it again. Do not repeat a development listed here unless something about it changed today, and then lead with what changed. A forward-look listed here that has come due is worth one line on how it turned out.
```

- [ ] **Step 1: Write the failing tests** (synthetic brief texts built in the test file; headings in the real format, e.g. `"🌍 TOP STORIES"`, `"<b>📌 POSITION SIGNALS</b>"`)

```python
def test_sections_split_on_known_and_unknown_headings():
    text = "🌍 TOP STORIES\n• Alpha happened\n• Beta happened\n\n📈 MARKET PULSE — WHAT MOVED\n• Gold up\n\n👁 WATCH / FORWARD\n• Vote on Thursday\n"
    s = dated_memory.brief_sections(text)
    assert s["top"] == ["Alpha happened", "Beta happened"]
    assert s["watch"] == ["Vote on Thursday"]
    assert "Gold up" not in str(s)

def test_dynamic_uppercase_heading_closes_top_stories()      # "🇯🇵 JAPAN" ends the section
def test_continuation_line_joins_its_entry()                 # entry == "First line\nsecond line"
def test_html_tags_and_entities_are_removed()                # "<b>🌍 TOP STORIES</b>\n• A &amp; B" -> ["A & B"]
def test_missing_positions_section_is_absent_not_empty()     # "positions" not in result
def test_window_keeps_only_the_seven_days_before_today():
    briefs = [("2026-11-10", TOP("old")), ("2026-11-15", TOP("kept")), ("2026-11-22", TOP("same day"))]
    w = dated_memory.build_window(briefs, "2026-11-22")
    assert "11-15 [top] kept" in w.block and "old" not in w.block and "same day" not in w.block
    assert w.days == 1
def test_window_is_empty_with_no_qualifying_brief()          # block == "" and days == lines == 0
def test_window_cuts_long_entries_at_200_chars_with_ellipsis()
def test_window_drops_oldest_days_first_over_the_ceiling():  # newest day survives, oldest gone, chars <= DATED_WINDOW_MAX_CHARS
def test_window_orders_top_then_thread_then_watch_within_a_day()
```

- [ ] **Step 2: Run to fail.** `py -m pytest tests/test_dated_window.py -q`. Expected: FAIL (no `brief_sections`).

- [ ] **Step 3: Implement `brief_sections`, `build_window` and `WindowResult` in `dated_memory.py`**

- [ ] **Step 4: Write `scripts/preview_dated_window.py`**

`--today YYYY-MM-DD` (default: today UTC). Start with the `sys.path` shim from `scripts/replay_brief_realtime.py:24-25`. It calls `brief.load_last_n_briefs(7)` and `dated_memory.build_window`, the production functions rather than a copy of them.
- It prints first `BRIEFS_DIR` and the files returned (`files=N` and their dates), then for each file which sections `brief_sections` found, with entry counts.
- Then it prints the block followed by `days= lines= chars=`.

So "no archive files" (a wrong volume or an empty directory) and "files found, no sections recognised" read differently. On the host it runs as `docker compose run --rm --entrypoint python newsbrief scripts/preview_dated_window.py`. This is how the rollout checks the parser against real archived briefs (Review Focus 2).

- [ ] **Step 5: Run to pass.** `py -m pytest tests/test_dated_window.py tests/test_packaging.py -q`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dated_memory.py scripts/preview_dated_window.py tests/test_dated_window.py
git commit -m "feat(dated-memory): brief section parser and the 7-day headline window"
```

---

### Task 3: Dated claims in the ledger block

**Files:**
- Modify: `brief_memory.py:525-565` (`render_established_block`)
- Test: `tests/test_brief_memory.py` (append)

**Interfaces:**
- Produces: `render_established_block(ledger: dict, *, dated: bool = False, today: str | None = None) -> str`. With `dated=False` the output is unchanged. With `dated=True`, `today` (`YYYY-MM-DD`) is required. A `since …` cue goes **first** in the existing parenthetical: `• [japan] (since 11-14, in doubt, corroborated) claim — driver: x`. A row with no other cue reads `(since 11-14)`.
- Date form: `MM-DD` when `first_seen` has the same year as `today`, else `YYYY-MM-DD`. A missing or unparseable `first_seen` adds no cue.
- **`resolution_date` is not rendered** (quarantine, Global Constraints). Add one line to the quarantine comment at `brief_memory.py:79-87` recording that decision: 2026-11-22, `news-brief-al7.1`.
- **The header changes in dated mode.** Today's header says the parenthetical is corroboration (`brief_memory.py:561-564`), which would contradict the date. In dated mode, replace its last sentence ("The parenthetical is how broadly…") with this exact copy; the undated header stays byte-identical:

```text
The parenthetical starts with the date a previous brief first reported the fact (since MM-DD). When today's news follows from one of these, say so with that date ("follows from 11-14") rather than re-explaining it. After the date, the parenthetical says how broadly the fact was corroborated across outlets: lean on 'widely corroborated' facts with confidence and treat 'single-source' ones more tentatively.
```

- [ ] **Step 0: Capture golden output from HEAD, before any edit.** Run the current `render_established_block` on the ledger in `test_undated_block_matches_head` below (two claims: one `challenged` with `source_count` 1, one with `source_count` 5 and a driver), and paste the exact output into the test as a literal `GOLDEN_UNDATED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_undated_block_matches_head():
    # GOLDEN_UNDATED captured in Step 0 from HEAD
    assert bm.render_established_block(LEDGER) == GOLDEN_UNDATED
    assert bm.render_established_block(LEDGER, dated=False) == GOLDEN_UNDATED

def test_dated_header_asks_for_the_date_and_drops_the_old_meaning():
    block = bm.render_established_block(LEDGER, dated=True, today="2026-11-22")
    assert "first reported the fact (since MM-DD)" in block
    assert "The parenthetical is how broadly" not in block

def test_dated_block_puts_since_first_in_the_cue():
    ledger = {"version": 1, "claims": [{"id": "c-1", "claim": "Widget tariff in force", "topic": "trade",
              "first_seen": "2026-11-14", "source_count": 3}]}
    block = bm.render_established_block(ledger, dated=True, today="2026-11-22")
    assert "• [trade] (since 11-14, corroborated) Widget tariff in force" in block

def test_dated_block_uses_the_full_date_across_a_year()     # first_seen 2025-12-01, today 2026-11-22 -> "since 2025-12-01"
def test_dated_block_never_renders_resolution_date():
    # row carries resolution_date "2026-12-01"; neither "resolves" nor "12-01" appears
def test_dated_block_skips_a_malformed_first_seen()          # "not-a-date" -> row present, no "since"
```

- [ ] **Step 2: Run to fail.** `py -m pytest tests/test_brief_memory.py -q -k "dated or head"`. Expected: the golden test PASSES (it's HEAD's output), the dated tests FAIL (unexpected keyword `dated`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole file.** `py -m pytest tests/test_brief_memory.py -q`. Expected: PASS. Then **mutation check**: change one character of the undated header, confirm `test_undated_block_matches_head` fails, and revert.
- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -m "feat(dated-memory): ledger claims render their first_seen date"
```

---

### Task 4: The knob, and wiring the window and dated claims into submit

**Files:**
- Modify: `common.py` (`KNOBS`, beside `BRIEF_REALTIME_TIMEOUT`)
- Modify: `docker-compose.yml:113-114` (anchor lines)
- Modify: `brief.py:2310-2403` (`build_daily_prompt`), `brief.py:3239-3344` (`mode_submit`)
- Test: `tests/test_dated_submit.py`

**Interfaces:**
- Consumes: `dated_memory.build_window`, `dated_memory.WindowResult` (Task 2); `render_established_block(..., dated=, today=)` (Task 3).
- Produces:
  - `KNOBS["BRIEF_DATED_MEMORY_ENABLED"] = Knob(bool, False)`, with a comment naming spec §3 and `news-brief-al7.1`; compose line `- BRIEF_DATED_MEMORY_ENABLED=${BRIEF_DATED_MEMORY_ENABLED:-}`
  - `KNOBS["EVIDENCE_MODEL"] = Knob(str, "claude-haiku-5-5", env="NEWSBRIEF_EVIDENCE_MODEL")`, for Task 5, added here so the compose edit happens once; compose line `- NEWSBRIEF_EVIDENCE_MODEL=${NEWSBRIEF_EVIDENCE_MODEL:-}`. Unlike `SIGNALS_MODEL`, its default is a model id rather than "follow MODEL": the extraction is meant to be the cheap model (spec §3.3).
  - `build_daily_prompt(..., established_block: str = "", dated_window: str = "") -> str`. When `dated_window` is non-empty it **replaces** the `## WEEKLY CONTEXT` block in the same position. When it's empty, the weekly block renders exactly as today. Yesterday's brief (`[:6000]`) is untouched either way (spec §3.2).
  - In `mode_submit`: `dated_on = common.BRIEF_DATED_MEMORY_ENABLED` once, near the top. When it's on:
    - `render_established_block(..., dated=True, today=today)`
    - `build_window(load_last_n_briefs(7), today)`, inside its own `try`. A failure logs `Dated window skipped (brief unaffected): …` and falls back to the weekly slice.
    - one log line: `Dated window: {days} days, {lines} lines, {chars} chars`

- [ ] **Step 0: Capture golden output from HEAD, before any edit.** `build_daily_prompt` reads `datetime.now` (`brief.py:2323`), so freeze it: monkeypatch `brief.datetime` with a subclass whose `now()` returns `2026-11-22 04:30 UTC`. Call the current function with the `args` tuple below and paste the exact result into the test as `GOLDEN_PROMPT`. It includes the `## WEEKLY CONTEXT` block, which no existing test covers.

- [ ] **Step 1: Write the failing tests**

```python
ARGS = ("feeds", "web", "chroma", "YESTERDAY", "WEEKLY-SUMMARY", {}, "", "", "", "", "")

def test_prompt_with_no_window_matches_head(frozen_clock):
    assert brief.build_daily_prompt(*ARGS) == GOLDEN_PROMPT
    assert brief.build_daily_prompt(*ARGS, dated_window="") == GOLDEN_PROMPT

def test_window_replaces_the_weekly_slice_and_keeps_yesterday():
    p = brief.build_daily_prompt("f", "w", "c", "YESTERDAY", "WEEKLY-SUMMARY", {}, "", "", "", "", "",
                                 dated_window="## THE LAST 7 DAYS — ALREADY REPORTED\n11-20 [top] X")
    assert "THE LAST 7 DAYS" in p and "WEEKLY CONTEXT" not in p and "WEEKLY-SUMMARY" not in p
    assert "YESTERDAY" in p

def test_submit_with_knob_off_never_builds_the_window(monkeypatch)
def test_submit_with_knob_on_passes_the_window_and_dated_claims(monkeypatch)
def test_submit_survives_a_window_failure(monkeypatch)
    # build_window raises -> the batch is still submitted, without a dated_window
```

The submit rig to reuse is `_stub_submit` in `tests/test_brief_fallback.py:116-149`. Mind three things it does:
- It **replaces `build_daily_prompt` itself** (line 147). Assert on the keyword arguments that stub receives (`dated_window`; `weekly_summary` passed through), not on prompt text.
- It returns `""` from `load_last_weekly_summary` (line 136). Make that return text, so "the weekly slice is still passed" can be observed.
- It sets `brief_memory_enabled` to False (line 131). The knob-on test sets it True, stubs `db.connect` and `claim_store.load_ledger`, and records the kwargs `render_established_block` received (`dated=True`, `today`).

- [ ] **Step 2: Run to fail.** `py -m pytest tests/test_dated_submit.py -q`. Expected: the golden test PASSES, the rest FAIL.
- [ ] **Step 3: Implement the knobs, compose lines and wiring.**
- [ ] **Step 4: Run.** `py -m pytest tests/test_dated_submit.py tests/test_config.py tests/test_brief_memory.py tests/test_brief_fallback.py -q`. Expected: PASS. `docker compose config -q` exits 0. Mutation check: change one character of the `## WEEKLY CONTEXT` heading, confirm `test_prompt_with_no_window_matches_head` fails, and revert.
- [ ] **Step 5: Commit**

```bash
git add common.py docker-compose.yml brief.py tests/test_dated_submit.py
git commit -m "feat(dated-memory): BRIEF_DATED_MEMORY_ENABLED puts the window and dated claims in the brief"
```

---

### Task 5: Thesis evidence extraction at collect

**Files:**
- Modify: `dated_memory.py` (request, validator, extraction)
- Modify: `brief.py` (`_thesis_keys_quiet`, `_record_thesis_evidence`, a call in `mode_collect` after `clear_batch_state()`, `brief.py:3477`)
- Test: `tests/test_thesis_extraction.py` (no database; store functions stubbed)

**Interfaces:**
- Consumes: `brief_sections` (Task 2); `EvidenceRow`, `save_brief_evidence`, `has_brief_evidence` (Task 1); `common.EVIDENCE_MODEL` (Task 4); `brief._post_messages(payload, *, timeout, max_attempts, label)`.
- Produces in `dated_memory.py`:
  - `build_evidence_request(section: str, labels: dict[str, str], *, model: str) -> dict`: `labels` maps `"T1"` to a thesis key. `max_tokens` 2048, `thinking` disabled, forced tool `emit_evidence`, whose input is `{"rows": [{"thesis": str, "direction": "supports"|"challenges", "line": str}]}`. In `input_schema`, `thesis` carries `"enum": [the labels]` and `direction` carries `"enum": ["supports", "challenges"]`, so a model that answers with the key instead of the label is steered off it rather than rejected wholesale.
  - `validate_evidence(resp: dict, labels: dict[str, str]) -> tuple[list[EvidenceRow], list[str]]`: returns `(rows, problems)`. A row with an unknown label, a bad direction or an empty line becomes a problem string and is not written. A line longer than 300 characters is cut to 300, ending in `…`. Exact duplicates collapse to one. If there's no `emit_evidence` block, the result is `([], ["no emit_evidence answer (stop_reason=…)"])`.
  - `extract_thesis_evidence(brief_text: str, keys: list[str], *, post, model: str) -> EvidenceResult`
  - `@dataclass(frozen=True) class EvidenceResult: status: str; rows: list[EvidenceRow]; problems: list[str]`, where `status` is `"no_section"` (no POSITION SIGNALS, so no call), `"no_theses"` (no keys, so no call), `"ok"` or `"no_answer"`. It logs `stop_reason` and usage before validating, as `watch.judge_watch` does.
- Produces in `brief.py`:
  - `EVIDENCE_TIMEOUT = 90`: a post-delivery call, sized like `RECONCILE_TIMEOUT` (latency is free once the brief has shipped)
  - `_thesis_keys_quiet() -> tuple[list[str], str | None]`: returns `(sorted keys of {**load_local_theses(), **pushed}, push_problem)`, where `pushed, push_problem = load_portfolio_theses()`. **Never `load_theses()`**: it sends a Telegram warning on every call, and this runs from collect and from the commands daemon. It doesn't drop the problem silently, though: submit's warning only fires on days the Trading212 fetch reaches `load_theses` (`brief.py:2183-2223`), so the problem is carried into the `Evidence:` log line.
  - `_record_thesis_evidence(brief_text: str, submit_day: str) -> None`:
    - with no database configured, log `Evidence: skipped, no database` and return
    - if `has_brief_evidence(conn, submit_day)` is true, log `Evidence: already recorded for {day}` and return without calling the model
    - otherwise extract, save, and log `Evidence: status={s} written={n} problems={k} push={ok|<problem>}`, followed by each problem on its own `log.warning` line
  - In `mode_collect`: `dated_on = common.BRIEF_DATED_MEMORY_ENABLED` once, at the top. **After `clear_batch_state()`, beside the trading stage** (`brief.py:3477-3488`), not before it: the evidence call can take up to 2 × 90 s with retries, and the margin before `clear_batch_state` (`RESUME_MARGIN_MINUTES`, `brief.py:3362`) is reserved for delivering the brief. There, `if dated_on:` call it inside its own `try/except Exception`, logging `Evidence: crashed (brief unaffected): …`. It must never stop trading or retention.

System prompt and user template (exact copy; no thesis text, keys only, as spec §5.3 says):

```text
SYSTEM: You turn the POSITION SIGNALS section of a finished investment brief into dated evidence for the reader's theses. Each thesis is given only as a label and its key (a ticker, or cluster:<name>). Record a row only where the section itself says a development confirms or challenges that thesis; never infer one the section does not state. One row per development per thesis, its line a single plain sentence under 300 characters. If nothing in the section bears on a listed thesis, return no rows.

USER:
## THESES
{one line per label: "T1 = TESTCO"}

## POSITION SIGNALS
{section}

Call emit_evidence once.
```

- [ ] **Step 1: Write the failing tests**

```python
def test_request_disables_thinking_and_forces_the_tool():
    req = dated_memory.build_evidence_request("• TESTCO up on orders", {"T1": "TESTCO"}, model="m")
    assert req["thinking"] == {"type": "disabled"}
    assert req["tool_choice"] == {"type": "tool", "name": "emit_evidence"}
    assert "T1 = TESTCO" in req["messages"][0]["content"]
    row = req["tools"][0]["input_schema"]["properties"]["rows"]["items"]["properties"]
    assert row["thesis"]["enum"] == ["T1"]
    assert row["direction"]["enum"] == ["supports", "challenges"]

def test_unknown_label_is_a_problem_not_a_row()           # label "T9" -> rows == [], len(problems) == 1
def test_bad_direction_and_empty_line_are_problems()
def test_long_line_is_cut_to_300()
def test_duplicate_rows_collapse()
def test_no_tool_block_reports_stop_reason()              # problems[0] contains "max_tokens"
def test_no_positions_section_makes_no_call():
    called = []
    r = dated_memory.extract_thesis_evidence("🌍 TOP STORIES\n• x", ["TESTCO"], post=called.append, model="m")
    assert r.status == "no_section" and called == []
def test_no_keys_makes_no_call()
def test_collect_records_evidence_by_the_submit_day(monkeypatch)
    # reuse tests/test_collect_day_key.py's _stub_collect shape; state date "2000-01-01";
    # stub dated_memory.extract_thesis_evidence and save_brief_evidence; assert save got on_date "2000-01-01"
def test_evidence_runs_after_clear_batch_state(monkeypatch)
    # record call order: clear_batch_state comes before the extraction
def test_a_day_with_rows_is_never_extracted_twice(monkeypatch)        # Review Focus 3: has_brief_evidence True -> extract not called
def test_a_day_with_no_rows_may_be_extracted_again(monkeypatch)       # has_brief_evidence False -> extract called
def test_collect_with_knob_off_never_extracts(monkeypatch)
def test_an_extraction_crash_does_not_stop_collect(monkeypatch)
    # extract raises -> run_retention is still called
def test_thesis_keys_quiet_never_sends_telegram_and_reports_the_push(monkeypatch, tmp_path)
    # no pushed file present; patch brief.telegram_send, common.telegram_send and brief.telegram_alert to record;
    # nothing recorded, the local keys are returned, and the problem string starts with "missing"
def test_evidence_log_line_names_a_missing_push(monkeypatch, caplog)  # "push=missing" in the Evidence: line
```

The `_stub_collect` rig patches `db.connect` but not `db.is_configured`, so these tests must also patch `db.is_configured` to return True. Otherwise, with no `DATABASE_URL`, they take the "skipped, no database" branch and pass without reaching the stubbed save.

- [ ] **Step 2: Run to fail.** `py -m pytest tests/test_thesis_extraction.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement both modules' parts.**
- [ ] **Step 4: Run.** `py -m pytest tests/test_thesis_extraction.py tests/test_collect_day_key.py tests/test_obtain_brief.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add dated_memory.py brief.py tests/test_thesis_extraction.py
git commit -m "feat(dated-memory): collect logs thesis evidence from POSITION SIGNALS"
```

---

### Task 6: `/evidence` on Telegram

**Files:**
- Modify: `brief.py`: dispatch in `_handle_telegram_update` (beside `/jobs`, `brief.py:1326`), the callback in `_handle_callback_query` (with the other non-wizard pickers, before `reset:`), `BOT_COMMANDS` (`brief.py:3886`), `HELP_TEXT` (`brief.py:715`)
- Test: `tests/test_evidence_command.py` (no database; store stubbed. Capture sends on **both** `brief.telegram_send` and `common.telegram_send`, as `tests/test_commands.py:23-35` does)

**Interfaces:**
- Consumes: `recent_evidence`, `keys_with_evidence` (Task 1); `_thesis_keys_quiet` (Task 5); `_short_id`, `_picker_send`, `_picker_empty`, `JOBS_DB_TIMEOUT_SECONDS`, `_ledger_unreachable`.
- Produces:
  - `EVIDENCE_DAYS = 30`; the default `since` is the calendar date `today − 30` (UTC), inclusive
  - `_evidence_keys(conn) -> list[str]`: sorted union of `_thesis_keys_quiet()[0]` and `keys_with_evidence(conn, since=<default>)` (Review Focus 4)
  - `_evidence_picker_render(message_id: int | None = None) -> None`: one button per key, `callback_data = f"ev:{_short_id(key)}"`, label `📒 {key}` cut to 40 characters. With no keys it sends `📒 No theses yet — set one with /thesis.`
  - `_evidence_render(key: str, since: str | None = None) -> None`: header `<b>📒 Evidence — {key}</b> (last 30 days)`, or `(since {YYYY-MM-DD})` when `since` is given. Then one line per row, newest first: `{MM-DD} ⬆ supports — {line}` or `{MM-DD} ⬇ challenges — {line}`; rows from an earlier year than today show `YYYY-MM-DD`. Every key and line goes through `html.escape`. Sent with `telegram_send_long`. With no rows: `No evidence logged for <b>{key}</b> in that period.` With an unreadable DB, `Cannot read the evidence log, so this is UNKNOWN rather than empty: …`, the `/jobs` wording.
  - Dispatch:
    - `/evidence` → the picker
    - `/evidence <key>` → `_evidence_render(key)` (exact key, case as typed)
    - `/evidence <key> since:YYYY-MM-DD` → `_evidence_render(key, since)`. This is spec §17.3's "since a given date". Parse it like `/dig`'s `since:` (`brief.py:1392-1395`); a malformed date gets `Format: /evidence KEY since:YYYY-MM-DD`.
    - callback `ev:<id>` → resolve over `_evidence_keys` again (as `close:`/`unpin:` do), else `That thesis is no longer listed.`
  - `BOT_COMMANDS` entry `("evidence", "Thesis evidence log, last 30 days")`. A `HELP_TEXT` entry after the `/positions` and `/performance` lines (`HELP_TEXT` has no `/thesis` entry), showing both forms.

- [ ] **Step 1: Write the failing tests**

```python
def test_evidence_alone_sends_a_picker_of_theses_and_logged_keys(monkeypatch)
    # quiet keys ["TESTCO"], logged keys ["OLDCO"] -> buttons for both (Review Focus 4)
def test_evidence_with_a_key_lists_rows_newest_first_and_escaped(monkeypatch):
    # rows: 11-21 challenges "A <b> B", 11-20 supports "C"
    # sent text: index of "11-21 ⬇ challenges — A &lt;b&gt; B" < index of "11-20 ⬆ supports — C"
def test_evidence_since_passes_the_date_to_the_store(monkeypatch)  # "/evidence TESTCO since:2026-01-01" -> recent_evidence(since="2026-01-01")
def test_evidence_since_with_a_bad_date_shows_the_format(monkeypatch)
def test_evidence_with_no_rows_says_so(monkeypatch)
def test_unreadable_db_reports_unknown_not_empty(monkeypatch)     # db.connect raises -> "UNKNOWN" in sent
def test_callback_resolves_the_short_id_over_the_live_list(monkeypatch)
def test_stale_callback_says_no_longer_listed(monkeypatch)
def test_evidence_is_in_the_command_menu():
    assert ("evidence", "Thesis evidence log, last 30 days") in brief.BOT_COMMANDS
    assert "/evidence" in brief.HELP_TEXT
```

- [ ] **Step 2: Run to fail.** `py -m pytest tests/test_evidence_command.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `py -m pytest tests/test_evidence_command.py tests/test_commands.py -q`. Expected: PASS.
- [ ] **Step 5: Run the full gate in the FOREGROUND, database exported**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```

Expected: ruff clean, the format check clean (stage every file `ruff format` touches), and pytest passing with `tests/test_thesis_evidence.py` **run, not skipped**.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_evidence_command.py
git commit -m "feat(dated-memory): /evidence lists a thesis's last 30 days of evidence"
```

---

## Rollout (the operator, after deploy)

**When to deploy:** after `/jobs` shows today's collect ✅ finished, and before 04:30 UTC; not around Sunday 21:00 (weekly). A deploy restarts the supervisor, which stops a running job and records it `finished` with no alert and no retry (`supervisor.py:803-819`), so deploying while collect runs (from 06:00) can cost that day's brief. The knob is off, so nothing changes until step 2. The migration runs on start.

1. **See the window over the real archive before switching it on:** `docker compose run --rm --entrypoint python newsbrief scripts/preview_dated_window.py --today "$(date -u -d tomorrow +%F)"`. Pass tomorrow: the next 04:30 submit runs on tomorrow's date, and with today's date the window would leave out today's brief, showing 6 days where submit sees 7.
   - It should list `files=` with recent dates, `top`/`watch` sections found in each, then dated `[top]` and `[watch]` lines.
   - `files=0` means the container sees no archive (volume or `DATA_DIR`).
   - Files with no sections found means the parser doesn't recognise the real headings.
   - Either way, stop and report.
2. **Switch it on.** The compose anchor seeds `settings` only when the table is empty, so the row doesn't exist yet and an `UPDATE` would change nothing. Insert it:
   ```sql
   INSERT INTO settings (key, value) VALUES ('BRIEF_DATED_MEMORY_ENABLED', 'true')
   ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value = EXCLUDED.value, updated_at = now();
   ```
3. **Next morning, check the effect, not the row.** In `newsbrief.log` in the data folder:
   - a `Dated window: … lines` line from the 04:30 submit
   - an `Evidence: status=… written=… problems=… push=…` line from collect:
     - `status=no_section` usually means a quiet day, but it is also what a reworded heading looks like. If it keeps appearing on days the brief has a position-signals section, report it.
     - `push=` anything other than `ok` means the pushed theses were left out of extraction.
   - the brief's own usage line, the `usage(in=…)` line carrying `searches=`, compared with the previous days', for the real token cost of the window. On a knob-on day, two other calls print `usage(in=` too.

   Then `/evidence` should list the day's rows for a thesis the brief mentioned.
4. Close `news-brief-al7.1` when step 3 holds.

**Is the claim ledger on?** Dated claims only show if `BRIEF_MEMORY_ENABLED` is on. To check, look for a `Brief-memory: N written` line in `newsbrief.log` from any recent collect (`grep "Brief-memory:" newsbrief.log | tail -3`). The row itself is `SELECT value FROM settings WHERE key = 'BRIEF_MEMORY_ENABLED' AND user_id IS NULL;`, but the log line is the evidence that it takes effect.

## Not in this plan

- Measuring the brief's current input tokens (spec §11's "first task") is for the phase 2 plan, where the brief's composition changes. Phase 1 adds roughly 3–4k gross input tokens a day, unmeasured; rollout step 3 measures it.
- **§3.3's "no double counting", a REQUIREMENT on the phase 2 plan** (recorded on `news-brief-al7`). Under the storylines layout, POSITION SIGNALS is written from `source='storyline'` evidence (spec §7.1.5), so this extractor would record it again as `source='brief'`, doubling `/evidence`. Phase 2 must either pass that day's storyline rows to the extractor and record only what is new, or have `_record_thesis_evidence` refuse with `status=storyline_layout` while `STORYLINE_BRIEF_ENABLED` is on. It can't be built now, because that knob doesn't exist yet.
- The 🧵 section's own format. The parser already reads a `THREADS THAT MOVED` section, so the window works under the new layout once phase 2 defines it.

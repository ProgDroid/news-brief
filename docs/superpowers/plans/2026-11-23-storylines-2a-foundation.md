# Storylines Plan 2a (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Storylines exist and articles attach to them. The tables, a store, Telegram management, a one-off seeding script, the 30-minute `attach` job and the §8.2 offline check that decides whether Haiku may do the attaching. Nothing the operator reads in the brief changes.

**Architecture:** Two new top-level modules:
- `storyline_store.py` owns all SQL for the new tables, as `claim_store.py` owns the ledger's.
- `storylines.py` holds the pure parts: the attach request, its validator, the transient-error test, the seed request and its validator, and the offline-check metrics.

`brief.py` wires them into `mode_attach`, the Telegram text chain and the callback handler. Two scripts run by hand on the host: `scripts/storylines_seed.py` and `scripts/storylines_attach_check.py`. A third, `scripts/brief_cost.py`, prices the brief from the host log. The job runs behind `STORYLINES_ENABLED`, which is read once per pass.

**Tech Stack:** Python 3.12, psycopg 3, Postgres 18, the Anthropic Messages API over `requests` (`brief._post_messages`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-21-storylines-design.md`, revision 5:
- §18 item 1 (this plan's scope);
- §4, §5.2, §5.3 (the attach and propose rows), §6, §8, §10.5, §10.6, §11, §12, §13, §14.

Bead `news-brief-al7.3`.

## Global Constraints

- **The repo is public (spec §13).** No test, fixture, log line or doc names a real holding, a thesis's text, or a storyline drafted from one. Tests use invented storylines (`Widget tariffs`, `Gizmo strike`) and invented headlines. The offline-check export and its per-article results live only in the host data folder.
- **Knobs are read as `common.X`**, never `from common import X`, and **once per pass**.
- **A new knob needs a `KNOBS` entry and its compose anchor line** (`docker-compose.yml:115` area, the `x-newsbrief` block). `tests/test_packaging.py:277` checks one direction of that pairing, so check the other by hand.
- **A new top-level module needs three registrations**, enforced by `tests/test_packaging.py:115/148/160`:
  - the `Dockerfile:41` COPY line;
  - the workflow `paths:` list (`.github/workflows/docker-publish.yml:7-26`);
  - BOTH ruff lines (`:80` and `:81`).
- **A new script** under `scripts/` needs the `sys.path` shim (`scripts/replay_brief_realtime.py:24-25`).
- **Every model call sets `thinking` explicitly** (`{"type": "disabled"}`) and forces its tool (`tool_choice`).
- **Labels, not ids (spec §6).** The model sees storylines as `S1…Sn` and articles as `A1…An`, mapped back in code. An unknown label is a problem for that article, never a fall-through. The pattern to copy is `dated_memory.validate_evidence` (`dated_memory.py:275`), not `comprehend._resolve_label`, which falls through.
- **Decision records, not inferences (spec §6).** An article gets an `item_attach_decisions` row only when a model call came back about it:
  - `attached` or `none`;
  - `failed`, judged badly;
  - `deferred`, when the response judged nothing.

  A transient or configuration stop writes nothing, so "not looked at yet" stays distinguishable.
- **Retry budget (memory `retry-budget-needs-a-verdict`, including its h8p section):** only a judgment about an article charges that article.
  - **Charges `attempts`:** an invalid answer for one article (unknown label, more than 3, answered twice, missing). At `ATTACH_MAX_ATTEMPTS = 3` the article is never selected again.
  - **Charges nobody but counts `defers`:** a response that judged nothing, such as a `max_tokens` stop, no tool block, or an HTTP 400 for the whole request. The chunk's articles get outcome `deferred` with `defers + 1`. `ATTACH_MAX_DEFERS = 10` bounds it, so an article whose own content keeps provoking the failure is eventually retired, following the precedent of `COMPREHEND_MAX_DEFERS` (`common.py:304-313`).
  - **Writes nothing and stops the pass:** a timeout, a connection error, a 429, 5xx or 529, which retry next pass. Also a configuration fault, meaning a 401, 403 or 404 (bad key or unknown model) or an unpriced model, which also raises a deduplicated Telegram alert, because a pass that returns normally keeps `/jobs` green.
  - The call itself makes one attempt (`max_attempts=1`), so retry layers do not multiply.
- **No DELETE** from any new table, ever. Down migrations refuse while rows exist.
- **Migration number:** this plan takes `0019_storylines`. `news-brief-ag1` (census removal) takes the next free number after whichever lands first. Plan 2b's migration is `0020`.
- **Spend is recorded** with `comprehend.record_spend(conn, "attach", model, usage)` (`comprehend.py:529`), into `comprehend_spend`.
  - That table's `stage` CHECK allows only `'triage', 'integration'` (`migrations/0015_comprehend_spend_up.sql:8`), so **0019 widens it** to `('triage', 'integration', 'attach', 'seed', 'attach_check')`.
  - The model is priced **before** the first call (`comprehend.price_of(model)`, which raises `UnpricedModel`), the way `comprehend.py:675-686` does, so a paid call can never fail its own bookkeeping.
  - The later bead that removes comprehension code must keep `record_spend`, `price_of` and that table.
- **The pre-push gate is three commands:** `ruff check .`, `ruff format --check .`, `py -m pytest -q`. Export `DATABASE_URL` so the DB tests run rather than skip (`CLAUDE.md` § Build & Test). Run it in the FOREGROUND.

### Plan-level calls (the spec's §14 says "for the plan to refine")

- **Proposals live only in `storyline_proposals`.** Accepting one creates the `storylines` row, so `storylines.status` is `active | dormant | retired`. The spec's `proposed` and `rejected` statuses are the proposal's `outcome`.
- **Signposts wait.** No signpost exists until plan 2b's nightly update writes them, and operator editing is `f71`. So 0019 has no `storyline_signposts` table, the attach prompt carries no signposts, and the attach output has no suspected-hit field. Plan 2b adds the table, and plan 2c adds the flag with the alerts that use it.
- **`storyline_states`, `storyline_timeline`, `storyline_links` and `storyline_updates` are plan 2b's** (migration 0020), along with the `item_attachments.update_id` foreign key.
- **Item references use `ON DELETE RESTRICT`**, so history can't vanish under the decisions. Items retention (`uh0`) must account for that.
- **Attach looks back 24 hours** (`ATTACH_LOOKBACK_HOURS`) and takes at most 300 articles a pass (`ATTACH_MAX_PER_PASS`). Turning it on, or coming back from an outage, never floods a pass, and articles older than the lookback are simply never decided.

## Review Focus

1. **Attach turned on with a large backlog, or after a long outage.** Expected: only the last 24 hours are considered, at most 300 a pass (oldest first), and the rest are taken next pass. Test in Task 6.
2. **Attach running before any storyline is live** (seeding not yet accepted). Expected: the pass makes no model call, writes no decisions, and logs `Attach: no live storylines`. Otherwise every article would be recorded as attaching to nothing, which is false. Test in Task 6.
3. **A partly bad model answer:** an unknown storyline label, more than three storylines, the same article answered twice, an article missing, or an article label that was never sent. Expected: only the affected articles are charged as `failed`, every other article in the batch is written, and a stray label is counted and logged. A response that judged nothing at all charges nobody (see Global Constraints). Tests in Tasks 5 and 6.
4. **Accept tapped twice, Accept at the bound of 60, or a name that duplicates a live storyline.** Expected: the second tap says it was already decided, and the bound or duplicate case refuses with a message while the proposal stays pending. Accepting again after retiring something works. Tests in Task 3.
5. **The seed script run a second time** while its proposals are still undecided. Expected: it refuses and says how many are pending, unless run with `--again`. Test in Task 4.

---

### Task 1: Price the brief from the host log

Spec §11, revision 5: the brief's cost line is re-priced from up to 30 days of the host log.

**Do not use `est_batch_cost`.** It is hard-coded at "Sonnet 4.6" batch rates of $1.50/$7.50 per million tokens (`brief.py:3219-3221`), while `comprehend.PRICES_PER_MTOK` prices `claude-sonnet-5` at $2/$10, halved by `BATCH_FACTOR`. The script parses the tokens and re-prices them:
- `comprehend.cost_usd(model, usage, batch=...)` for the tokens;
- plus `searches × WEB_SEARCH_USD` (`0.01`, the figure in `brief.py:3218`'s comment, unverified against current pricing and labelled so in the output);
- the model is `--model`, default `common.MODEL`.

**Files:**
- Create: `scripts/brief_cost.py`
- Test: `tests/test_brief_cost.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CostLine: day: str; mode: str; path: str; input_tokens: int; output_tokens: int; searches: int`, where `path` is `"batch"` or `"realtime"`
  - `parse_cost_lines(lines: Iterable[str]) -> list[CostLine]`. It reads `Batch result:` lines (path `batch`) and `Realtime brief:` lines (path `realtime`, full price).
  - `price(c: CostLine, model: str) -> float`
  - `summarise(costs: list[CostLine], model: str) -> str`
  - `main(argv: list[str] | None = None) -> int`: reads `common.DATA_DIR / "newsbrief.log"` and its rotations `newsbrief.log.1` to `.5`.

**The line formats,** real ones with their tails cut:

```
2026-11-23 06:00:12,733 INFO [collect] 2026-11-23 06:00:12,733 WARNING Batch result: stop_reason=end_turn blocks={...} text_len=4059 usage(in=205311 out=9907 cache_read=0 searches=3) est_batch_cost=$0.4123 tail='...'
2026-11-23 09:03:24,011 INFO Realtime brief: stop_reason=end_turn elapsed=87s usage(in=376777 out=6069 searches=8)
```

The day is the first token, and the mode is the `[...]` tag, or `manual` when there is none, as with the replay script's line.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_batch_line`: from the first line above, `CostLine("2026-11-23", "collect", "batch", 205311, 9907, 3)`.
  - `test_parse_realtime_line`: from the second, `CostLine("2026-11-23", "manual", "realtime", 376777, 6069, 8)`.
  - `test_price_uses_project_rates_not_est_batch_cost`: with model `claude-sonnet-5`, batch, 1,000,000 in, 0 out and 0 searches gives `1.00`. The same line as realtime gives `2.00`.
  - `test_one_collect_day_logged_twice_counts_once`: the later line wins, since a rerun replaces the brief.
  - `test_summarise_reports_span_per_mode_and_path`: the output contains `collect: days=3`, the mean, median, min and max, `projected_30d=`, `weekly: days=1`, `realtime_runs=`, `span=<first>..<last>`, and `web_search=$0.01/req (unverified)`.
  - `test_summarise_names_a_short_span`: under 30 days gives `SHORT: only N days in the log` (rotation caps the log at about 30 MB).
- [ ] **Step 2: Run them to see them fail.** `py -m pytest tests/test_brief_cost.py -q` fails with an ImportError.
- [ ] **Step 3: Implement.** One regex per field. Read files with `errors="replace"`, and read the rotations oldest first.
- [ ] **Step 4: Run them to see them pass,** then run the gate.
- [ ] **Step 5: Commit.** `git add scripts/brief_cost.py tests/test_brief_cost.py`, then `git commit -m "feat(scripts): price the brief from the host log (al7.3)"`.

---

### Task 2: Migration 0019 and the storyline store

**Files:**
- Create: `migrations/0019_storylines_up.sql`, `migrations/0019_storylines_down.sql`
- Create: `storyline_store.py`
- Modify:
  - `Dockerfile:41` and the workflow (`paths:` and both ruff lines) for `storyline_store.py`
  - `tests/test_db.py:59` (append `"0019_storylines"`) and `:61-95` (the four new tables)
- Test: `tests/test_storyline_store.py`, DB-gated with the same `skipif`, `conn` and `store` fixtures as `tests/test_thesis_evidence.py:14-38`.

**Schema:**

```sql
CREATE TABLE storylines (
    id               BIGSERIAL   PRIMARY KEY,
    name             TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description      TEXT        NOT NULL CHECK (length(description) BETWEEN 1 AND 400),
    status           TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dormant', 'retired')),
    pinned           BOOLEAN     NOT NULL DEFAULT FALSE,
    origin           TEXT        NOT NULL CHECK (origin IN ('seed', 'proposal', 'operator', 'burst')),
    last_attached_at TIMESTAMPTZ NULL,
    dormant_since    DATE        NULL,
    retired_on       DATE        NULL,
    merged_into      BIGINT      NULL REFERENCES storylines (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'retired') = (retired_on IS NOT NULL))
);
CREATE UNIQUE INDEX storylines_live_name ON storylines (lower(name)) WHERE status <> 'retired';

CREATE TABLE storyline_proposals (
    id           BIGSERIAL   PRIMARY KEY,
    name         TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description  TEXT        NOT NULL CHECK (length(description) BETWEEN 1 AND 400),
    pinned       BOOLEAN     NOT NULL DEFAULT FALSE,
    origin       TEXT        NOT NULL CHECK (origin IN ('seed', 'proposal', 'burst')),
    proposed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ NULL,
    outcome      TEXT        NULL CHECK (outcome IN ('accepted', 'rejected')),
    storyline_id BIGINT      NULL REFERENCES storylines (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((outcome IS NULL) = (decided_at IS NULL)),
    CHECK ((outcome = 'accepted') = (storyline_id IS NOT NULL))
);

CREATE TABLE item_attach_decisions (
    item_id      BIGINT      PRIMARY KEY REFERENCES items (id) ON DELETE RESTRICT,
    outcome      TEXT        NOT NULL CHECK (outcome IN ('attached', 'none', 'failed', 'deferred')),
    model        TEXT        NOT NULL,
    attempts     INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),  -- judged failures only
    defers       INT         NOT NULL DEFAULT 0 CHECK (defers >= 0),    -- responses that judged nothing
    last_problem TEXT        NULL,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE item_attachments (
    item_id      BIGINT      NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    storyline_id BIGINT      NOT NULL REFERENCES storylines (id),
    status       TEXT        NOT NULL DEFAULT 'attached' CHECK (status IN ('attached', 'rejected', 'consumed')),
    update_id    BIGINT      NULL,  -- FK to storyline_updates: plan 2b, 0020
    attached_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, storyline_id)
);
CREATE INDEX item_attachments_storyline ON item_attachments (storyline_id, attached_at);

ALTER TABLE thesis_evidence
    ADD CONSTRAINT thesis_evidence_storyline_fk FOREIGN KEY (storyline_id) REFERENCES storylines (id);

ALTER TABLE comprehend_spend DROP CONSTRAINT comprehend_spend_stage_check;
ALTER TABLE comprehend_spend ADD CONSTRAINT comprehend_spend_stage_check
    CHECK (stage IN ('triage', 'integration', 'attach', 'seed', 'attach_check'));
```

Confirm the existing constraint's name with `\d comprehend_spend` on the test database before writing the DROP. Postgres's default name is `comprehend_spend_stage_check`.

**The down migration:**
- It refuses while any of the four new tables has rows, or while `comprehend_spend` has a row whose stage is one of the three new ones.
- It uses the nested-IF `to_regclass` shape of `0018_thesis_evidence_down.sql`.
- It then restores the two-stage CHECK, drops the foreign key, and drops the tables in reverse order.

**Interfaces:**
- Produces (`storyline_store.py`; nothing in it commits, the caller does):
  - `@dataclass(frozen=True) class Storyline: id: int; name: str; description: str; status: str; pinned: bool; last_attached_at: datetime | None`
  - `class Outcome(NamedTuple): storyline_id: int | None; problem: str | None`. `problem` is one of `"bound"`, `"duplicate"`, `"unknown"`, `"already accepted"`, `"already rejected"`, or None.
  - `live_storylines(conn) -> list[Storyline]`: `active` and `dormant`, ordered by `id`.
  - `count_live(conn) -> int`
  - `add_storyline(conn, name: str, description: str, *, origin: str, pinned: bool = False, max_live: int) -> Outcome`. It checks the bound first, then the duplicate (case-insensitive against live names).
  - `retire_storyline(conn, storyline_id: int, *, on_date: str) -> bool`: False when the storyline isn't live.
  - `add_proposal(conn, name: str, description: str, *, origin: str, pinned: bool = False) -> int`
  - `pending_proposals(conn, *, origin: str | None = None) -> list[dict]`: keys `id`, `name`, `description`, `pinned`, `origin`, oldest first.
  - `decide_proposal(conn, proposal_id: int, accept: bool, *, max_live: int) -> Outcome`. Accept calls `add_storyline` with origin `seed` for a seed proposal and `proposal` otherwise. On `bound` or `duplicate` the proposal stays pending.
  - `undecided_items(conn, *, now: datetime, lookback_hours: int, max_attempts: int, max_defers: int, limit: int) -> list[dict]`: items with `created_at > now - lookback`, plus one of these, oldest first. Keys `id`, `title`, `body`.
    - no decision;
    - `outcome IN ('failed', 'deferred') AND attempts < max_attempts AND defers < max_defers`.
  - `record_decision(conn, item_id: int, storyline_ids: list[int], *, model: str) -> None`: `attached` with the attachment rows and `last_attached_at = now()`, or `none` when the list is empty. Overwrites only a `failed` or `deferred` row, keeping its counters.
  - `record_failure(conn, item_id: int, *, model: str, problem: str) -> None`: upserts `failed`, `attempts + 1`, `last_problem` cut to 300 characters.
  - `record_deferral(conn, item_id: int, *, model: str, problem: str) -> None`: upserts `deferred` (an existing `failed` becomes `deferred`), `defers + 1`, and `last_problem`.
  - `attached_since(conn, *, since: datetime) -> dict[int, int]`: storyline id to attachment count.

- [ ] **Step 1: Write the failing tests.**
  - `test_up_and_down_round_trip_when_empty`
  - `test_down_refuses_while_a_storyline_exists`
  - `test_live_names_are_unique_case_insensitively_but_a_retired_name_is_reusable`
  - `test_add_refuses_at_the_bound`, with `max_live=2` after two adds: `Outcome(None, "bound")`
  - `test_add_refuses_a_duplicate_live_name`
  - `test_retire_sets_retired_on_and_leaves_live_list`
  - `test_retire_twice_returns_false`
  - `test_decide_accept_creates_seed_storyline_and_links_proposal`
  - `test_decide_twice_reports_already` (the second accept gives `"already accepted"`, no new row)
  - `test_decide_at_bound_keeps_proposal_pending`
  - `test_decide_reject_creates_nothing`
  - `test_decide_unknown_id`
  - `test_undecided_items_respects_lookback_limit_attempts_and_defers`. The items:
    - one at −30 h;
    - at −2 h, one with no decision;
    - at −1 h, one each of: failed with attempts 3, failed with attempts 1, deferred with defers 10, deferred with defers 2, and `none`.

    With `max_attempts=3, max_defers=10` it expects exactly the −2 h item, the attempts-1 item and the defers-2 item, oldest first. `limit=1` returns only the oldest.
  - `test_record_decision_attached_writes_attachments_and_last_attached_at`
  - `test_record_decision_never_overwrites_a_final_outcome` (a second call on a `none` row raises or is a no-op, test whichever you implement, and the row is unchanged)
  - `test_record_failure_counts_attempts`
  - `test_record_deferral_counts_defers_not_attempts`
  - `test_spend_accepts_the_attach_stage`: `comprehend.record_spend(store, "attach", "claude-haiku-5-5", {...})` inserts.
  - `test_down_refuses_while_attach_spend_exists`
  - `test_thesis_evidence_storyline_fk_rejects_an_unknown_id`

  Items need an outlet. Insert one invented outlet and the items directly with SQL in a fixture helper.
- [ ] **Step 2: Run them to see them fail.** `py -m pytest tests/test_storyline_store.py -q` with `DATABASE_URL` exported fails, and does NOT report `skipped`.
- [ ] **Step 3: Implement the migration and the store.**
- [ ] **Step 4: Run them to see them pass.** Also run `tests/test_db.py tests/test_packaging.py tests/test_thesis_evidence.py`, then the gate.
- [ ] **Step 5: Commit.** `feat(storylines): 0019 tables and the storyline store (al7.3)`

---

### Task 3: Telegram management: `/storyline` and the proposal buttons

Spec §9.4 (buttons) and §10.6 (commands). `/pin` and `/unpin` stay as they are until plan 2b.

**Files:**
- Modify: `brief.py`, in five places:
  - the text chain in `_handle_telegram_update` (next to `/evidence`, `:1347`)
  - `_handle_callback_query` (`:1140`)
  - `HELP_TEXT` (`:716`)
  - `BOT_COMMANDS` (`:4060`; add `("storyline", "Storylines: list, add, retire")`)
  - new helpers after the evidence helpers (`:3910` area)
- Modify: `common.py` `KNOBS` (`STORYLINE_MAX_LIVE: Knob(int, 60)`) and its compose anchor line
- Test: `tests/test_storyline_commands.py`, which stubs `db.connect` and `storyline_store.*` like `tests/test_evidence_command.py`.

**Interfaces:**
- Consumes: Task 2's store.
- Produces:
  - `send_proposal(proposal: dict) -> int | None`: sends `🧵 Proposed storyline: <b>{name}</b>\n{description}` (plus ` · 📌 pinned` when pinned), HTML-escaped, with the buttons `Accept` (`slp:a:{id}`) and `Reject` (`slp:r:{id}`) via `common.telegram_send_buttons`. Task 4 uses it.
  - `_storyline_list_render() -> None`, `_storyline_retire_picker(message_id: int | None = None) -> None`, `_storyline_add(arg: str) -> None`

**Behaviour:**
- `/storyline` or `/storyline list` lists the live storylines: name, status, 📌 when pinned, and the 7-day attachment count from `attached_since`. With none it says `No storylines yet.`. Sent with `telegram_send_long`.
- `/storyline add Name — description`, with an em dash or ` - ` as the separator, adds an `operator` storyline. With no separator or an empty part it replies with usage. On `bound` it replies `At the limit of {n} live storylines — retire one first (/storyline retire).`. On `duplicate` it says so.
- `/storyline retire` shows a picker of live storylines, with callback `slr:{id}`. A tap retires the storyline dated today (UTC), then edits the message to `Retired: {name}`.
- `slp:a:{id}` and `slp:r:{id}` decide the proposal with `max_live = common.STORYLINE_MAX_LIVE`, then edit the message to `✅ Accepted: {name}`, `❌ Rejected: {name}`, or the problem (`Already accepted.`, the bound message, the duplicate message). A problem leaves the buttons in place so the proposal can be decided later.
- Callback data uses DB ids, which are stable and well under 64 bytes, so `_short_id` isn't needed.

- [ ] **Step 1: Write the failing tests.**
  - `test_list_shows_names_status_and_counts`
  - `test_list_empty`
  - `test_add_parses_em_dash_and_hyphen`
  - `test_add_without_separator_replies_usage`
  - `test_add_at_bound_says_retire_first`
  - `test_retire_picker_then_tap_retires`
  - `test_accept_tap_edits_message_to_accepted`
  - `test_second_accept_tap_says_already` (Review Focus 4)
  - `test_accept_at_bound_keeps_buttons` (Review Focus 4)
  - `test_reject_tap`
  - `test_send_proposal_escapes_html_and_carries_both_buttons`
  - `test_storyline_is_in_bot_commands_and_help`
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** All DB access uses `db.connect(connect_timeout=JOBS_DB_TIMEOUT_SECONDS)` and commits after a write. A DB error replies `Storylines unavailable: {error}`, the way `_evidence_unknown` does.
- [ ] **Step 4: Run them to see them pass,** then run the gate.
- [ ] **Step 5: Commit.** `feat(storylines): /storyline list|add|retire and proposal buttons (al7.3)`

---

### Task 4: The seeding script

Spec §10.5: Sonnet drafts about 20 storylines, and they arrive as proposals with the Accept and Reject buttons.

**Files:**
- Create: `storylines.py` (seed functions in this task)
- Create: `scripts/storylines_seed.py`
- Modify: `Dockerfile:41` and the workflow lists for `storylines.py`
- Modify: `common.py` `KNOBS`, adding `STORYLINE_UPDATE_MODEL: Knob(str, "claude-sonnet-5")` and its compose anchor line
- Test: `tests/test_storylines_seed.py`

**Interfaces:**
- Produces (`storylines.py`):
  - `SEED_TOOL = "propose_storylines"`, `SEED_MAX = 25`
  - `@dataclass(frozen=True) class Proposed: name: str; description: str; pinned: bool`
  - `build_seed_request(topics: list[dict], pins: list[str], theses: dict[str, str], watch_lines: list[str], live_names: list[str], *, model: str, max_count: int) -> dict`. Forced tool and `thinking` disabled. The tool's array has `maxItems = max_count`, and each item has `name` (≤ 80), `description` (≤ 400) and `pinned` (bool). The prompt asks for about 20 storylines that cover the topics, the pins and the theses, each a developing situation that news will add to over weeks. Pinned topics produce `pinned: true`. Names must not repeat `live_names`.
  - `validate_seed(resp: dict, *, max_count: int, live_names: list[str]) -> tuple[list[Proposed], list[str]]`. It drops, with a problem string each:
    - an empty or over-long name or description;
    - a duplicate name, case-insensitive, within the answer or against `live_names`;
    - items beyond `max_count`.

    A response with no tool block gives `([], ["no tool_use block (stop_reason=…)"])`.
- Produces (`scripts/storylines_seed.py`): `main(argv: list[str] | None = None, *, post=None) -> int`. Its steps:
  - Refuse with exit 2 when pending `seed` proposals exist, unless `--again`.
  - Gather the theses with `brief._thesis_keys_quiet()`'s sources, `{**brief.load_local_theses(), **brief.load_portfolio_theses()[0]}`, with values stringified and cut at 300 characters.
  - Gather the pins with `brief.resolved_pins(brief.load_feedback())`, `TOPICS` with `label` and `search` only, and the watch events from `watch.load_watch(brief.WATCH_FILE)` as `name: trigger` lines. A `WatchFileError` means no lines.
  - Set `max_count = min(SEED_MAX, common.STORYLINE_MAX_LIVE - count_live)`, and exit 1 if that is 0 or less.
  - Make one call through `brief._post_messages(payload, timeout=180, max_attempts=1, label="Seed")`.
  - Validate, insert the proposals with origin `seed`, commit, then call `brief.send_proposal` for each.
  - Print `seed: proposed=N dropped=M` and each problem. Never print a name or description: the host terminal is fine, but the habit protects logs.

- [ ] **Step 1: Write the failing tests.**
  - `test_seed_request_forces_tool_and_disables_thinking`
  - `test_seed_request_carries_topics_pins_theses_and_watch_lines`
  - `test_validate_drops_duplicates_overlong_and_live_names`
  - `test_validate_caps_at_max_count`
  - `test_validate_no_tool_block`
  - `test_main_refuses_while_seed_proposals_pending` (Review Focus 5)
  - `test_main_again_overrides`
  - `test_main_inserts_and_sends_each_proposal` (stub `post`, the store and `brief.send_proposal`)
  - `test_main_at_bound_exits_1`
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** then run the gate.
- [ ] **Step 5: Commit.** `feat(storylines): seeding script drafts proposals (al7.3)`

---

### Task 5: The attach request and its validator

Spec §8.1.

**Files:**
- Modify: `storylines.py`
- Test: `tests/test_storylines_attach.py`

**Interfaces:**
- Consumes: `storyline_store.Storyline`.
- Produces:
  - `ATTACH_TOOL = "attach_articles"`, `ATTACH_BATCH = 60`, `ATTACH_TEXT_MAX = 600`, `ATTACH_MAX_PER_ARTICLE = 3`, `ATTACH_MAX_TOKENS = 4000`
  - `plain_text(title: str, body: str | None, limit: int = ATTACH_TEXT_MAX) -> str`: `html.unescape`, then strip tags, collapse whitespace, give `title — body`, and cut to `limit`.
  - `build_attach_request(storylines: list[Storyline], articles: list[dict], *, model: str) -> tuple[dict, dict[str, int], dict[str, int]]`. Returns `(payload, s_labels, a_labels)` with `S1…` in the order given and `A1…` in the order given. The storyline block shows each storyline's label, name and description. Each article shows its label and `plain_text`. The tool's input is `{"answers": [{"article": "A3", "storylines": ["S2"]}]}`, with `storylines` limited by `maxItems = ATTACH_MAX_PER_ARTICLE` and enum-constrained to the S labels. The prompt says:
    - answer every article exactly once;
    - attach only where the article materially develops that storyline;
    - an empty list is the common answer.
  - `@dataclass(frozen=True) class AttachResult: decided: dict[int, list[int]]; problems: dict[int, str]; stray: list[str]; whole: str | None`. `decided` maps an item id to storyline ids, with `[]` meaning none. `problems` maps an item id to its reason. `stray` holds labels that map to no sent article. `whole`, when set, says the response is unusable and every article is charged.
  - `validate_attach(resp: dict, s_labels: dict[str, int], a_labels: dict[str, int]) -> AttachResult`. In order:
    1. `whole` is set when `stop_reason != "tool_use"` or there is no `ATTACH_TOOL` block or `answers` isn't a list.
    2. An article label not in `a_labels` goes to `stray`.
    3. An article answered more than once gets the problem `answered twice`, and none of its answers count.
    4. A storyline label not in `s_labels` gives `unknown storyline label <x>`.
    5. Duplicate labels within an answer are removed. More than 3 distinct labels gives `more than 3 storylines`.
    6. A sent article with no answer gets `no answer`.
  - `classify_error(exc: BaseException) -> str`. It returns:
    - `"transient"` for `requests.Timeout`, `requests.ConnectionError`, and an `HTTPError` with status 429 or ≥ 500 (529 included);
    - `"config"` for an `HTTPError` with status 401, 403 or 404;
    - `"request"` for any other `HTTPError` (400, 413, 422 and so on), which judged nothing and is deferred;
    - `"unknown"` for anything else, which is deferred.

- [ ] **Step 1: Write the failing tests** for the parts of Review Focus 3 listed here.
  - `test_plain_text_strips_html_and_entities_and_caps_at_600`
  - `test_request_labels_in_order_and_forces_tool_with_thinking_disabled`
  - `test_request_schema_limits_three_and_enumerates_storyline_labels`
  - `test_valid_answer_maps_labels_back_to_ids`
  - `test_empty_list_is_a_none_decision`
  - `test_unknown_storyline_label_charges_only_that_article`
  - `test_more_than_three_charges_that_article`
  - `test_duplicate_labels_in_one_answer_are_deduped`
  - `test_article_answered_twice_is_charged`
  - `test_missing_article_is_charged_no_answer`
  - `test_stray_article_label_is_listed_not_mapped`
  - `test_max_tokens_stop_is_whole`
  - `test_classify_error` (parametrised: Timeout, ConnectionError, 429, 500 and 529 give transient; 401, 403 and 404 give config; 400 and 413 give request; ValueError gives unknown)
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** then run the gate.
- [ ] **Step 5: Commit.** `feat(storylines): attach request and validator (al7.3)`

---

### Task 6: The `attach` job

Spec §6 (the `attach` row, decision records, labels) and §12.

**Files:**
- Modify:
  - `brief.py`: add `mode_attach` and `_attach_post` next to `mode_watch` (`:4573`), plus a `MODES` entry (`:4705`) and a `JOB_MODES` entry (`:4727`)
  - `scheduler.py:76-85`: add `Schedule("attach", "interval", None, 30, grace_minutes=10)`
  - `supervisor.py:78`: change `8 schedules x 2 statements` to `9 schedules x 2 statements`, and recompute the arithmetic it feeds (`:75-82`).
  - `common.py` `KNOBS`: add `STORYLINES_ENABLED: Knob(bool, False)` and `STORYLINE_ATTACH_MODEL: Knob(str, "claude-haiku-5-5")`, plus their compose anchor lines
- Test: `tests/test_attach_job.py`, with the pure wiring tests stubbed and one DB-gated end-to-end test.
  - `tests/test_job_interlock.py:365` breaks for certain, because an empty ledger runs every interval job at once. Add `"attach"` to its expected set.
  - Run the whole suite, not a guessed list. Other tests read `len(SCHEDULES)` and adapt on their own (`test_supervisor.py:810` checks the comment above).

**Interfaces:**
- Consumes: Task 2's store, Task 5's request and validator, `comprehend.record_spend`.
- Produces:
  - `ATTACH_LOOKBACK_HOURS = 24`, `ATTACH_MAX_PER_PASS = 300`, `ATTACH_MAX_ATTEMPTS = 3`, `ATTACH_MAX_DEFERS = 10`, `ATTACH_TIMEOUT = 120` (constants in `brief.py`)
  - `attach_pass(conn, *, now: datetime, model: str, post) -> dict`:
    - **It returns counts** `{"items", "calls", "attached", "none", "failed", "deferred", "stray"}`, where `items == attached + none + failed + deferred` always. `deferred` counts every selected item not judged this pass: a deferred chunk, and every chunk after a stop.
    - **It also returns `"status"`,** one of `ok`, `no_storylines`, `nothing_to_do`, `transient`, `config` or `unpriced`, and `"error"` (str or None).
    - **It commits after each chunk,** so work done before a stop is kept.
  - `mode_attach() -> None`. It reads `common.STORYLINES_ENABLED` and `common.STORYLINE_ATTACH_MODEL` once. When the knob is off it logs `Attach: off` and returns. Otherwise:
    - it runs `attach_pass` with `post = _attach_post`;
    - it logs `Attach: status=… items=… calls=… attached=… none=… failed=… deferred=… stray=…`;
    - it passes a verdict to `_alert_once("attach_alert", verdict, state, "🧵")` (`brief.py:4288`). The verdict is `(f"{status}:{error}", message)` for `config` and `unpriced`, and None otherwise, so a fault rings once and clears on the next good pass.
  - `_attach_post(payload: dict) -> dict` is `_post_messages(payload, timeout=ATTACH_TIMEOUT, max_attempts=1, label="Attach")`.

**What `attach_pass` does:**
1. Calls `comprehend.price_of(model)`. On `UnpricedModel` it returns `unpriced` with no call.
2. Gets the live storylines. If there are none, it returns `no_storylines` without selecting items or calling the model.
3. Selects `undecided_items(limit=ATTACH_MAX_PER_PASS, max_attempts=ATTACH_MAX_ATTEMPTS, max_defers=ATTACH_MAX_DEFERS, ...)`. If there are none, it returns `nothing_to_do`.
4. Takes the items in chunks of `ATTACH_BATCH`. For each chunk it builds the request, posts it, and branches on `storylines.classify_error`:
   - **`transient`:** writes nothing and stops the pass with status `transient`.
   - **`config`:** writes nothing and stops the pass with status `config`.
   - **`request`, `unknown`, or a `whole` result:** calls `record_deferral` for every item in the chunk and continues with the next chunk.
   - **Otherwise:** `record_decision` for each decided item, `record_failure` for each problem, and logs the stray labels.
   - **After a post that returned a response:** `record_spend(conn, "attach", model, resp.get("usage") or {})` (a `whole` result was still billed), then commits.

- [ ] **Step 1: Write the failing tests.**
  - `test_off_makes_no_call`
  - `test_no_live_storylines_makes_no_call_and_writes_nothing` (Review Focus 2)
  - `test_lookback_and_per_pass_cap_are_passed_to_the_store` (Review Focus 1, the stubbed half)
  - `test_two_chunks_for_61_items`
  - `test_transient_error_on_second_chunk_keeps_first_chunk_and_writes_nothing_after`
  - `test_400_and_max_tokens_defer_the_chunk_without_charging_attempts`
  - `test_401_stops_the_pass_writes_nothing_and_alerts_once`: two passes produce one alert, and a following `ok` pass clears it.
  - `test_unpriced_model_makes_no_call_and_alerts`
  - `test_per_article_problems_charge_only_those_articles`
  - `test_spend_recorded_per_returned_response`
  - `test_counts_always_sum_to_items` (parametrised over ok, a mid-pass transient error, and a `whole` result)
  - `test_log_line_carries_every_count`
  - `test_attach_is_scheduled_every_30_minutes_and_dispatched`

  Then the DB-gated `test_end_to_end_attach_pass_on_a_real_schema`. It inserts 2 storylines and 3 items, with one older than 24 h, and stubs `post` to attach item 1 to S2 and leave item 2 with none. It expects decisions `attached` and `none`, one `item_attachments` row, `last_attached_at` set on storyline 2, no row for the old item, and one `comprehend_spend` row with stage `attach`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** then run the gate with `DATABASE_URL` exported.
- [ ] **Step 5: Commit.** `feat(storylines): attach job every 30 minutes behind STORYLINES_ENABLED (al7.3)`

---

### Task 7: The offline check

Spec §8.2. The check must call the **production** `build_attach_request` and `validate_attach`, never a copy (memory `reconstruction-drifts-from-production`).

**Files:**
- Modify: `storylines.py` (metrics)
- Create: `scripts/storylines_attach_check.py`
- Test: `tests/test_storylines_check.py`

**Interfaces:**
- Produces (`storylines.py`, pure). A run is `dict[int, set[int]]`, item id to storyline ids, holding only the items that run decided.
  - `jaccard_mean(a: dict[int, set[int]], b: dict[int, set[int]]) -> tuple[float, int]`: the mean Jaccard over items in both runs where at least one set is non-empty, and that subset's size. An empty subset gives `(nan, 0)`.
  - `attach_rate(run: dict[int, set[int]]) -> float`: the share of decided items with a non-empty set.
  - `differ_kinds(h: dict[int, set[int]], s: dict[int, set[int]]) -> dict[str, int]`: over items in both runs whose sets differ, counts of `less` (h ⊊ s), `more` (h ⊋ s) and `elsewhere` (anything else).
  - `check_verdict(j_ss: float, j_hs: float, rate_h: float, rate_s1: float) -> tuple[str, list[str]]`:
    - `"ILL-POSED"` when `j_ss < 0.65` (Haiku isn't judged);
    - otherwise `"PASS"` only if `j_hs >= max(j_ss - 0.10, 0.55)` and `rate_h >= rate_s1 - 0.15`;
    - otherwise `"FAIL"`.

    The list says which bar each value met or missed, with the numbers.
- Produces (`scripts/storylines_attach_check.py`): `main(argv: list[str] | None = None, *, post=None) -> int`. It runs as `--week-ending YYYY-MM-DD [--sample 20]`:
  - **The snapshot:** `DATA_DIR / "storylines_check" / <date> / input.json` holds the items created in `[date − 7 d, date)`, with id, title and body, and the live storylines. It is written on the first run and reused after, so every run scores the same inputs. Print `items=N storylines=M` before any call.
  - **The runs:**
    - `S1` and `S2` use `common.STORYLINE_UPDATE_MODEL`, and `H` uses `common.STORYLINE_ATTACH_MODEL`.
    - Each run goes in chunks of `ATTACH_BATCH`, through the production request builder and validator, posting with `max_attempts=1` and retrying a transient error up to 3 times with a 30 s sleep.
    - Each chunk's result is appended to `runs/<run>.jsonl` as `{chunk, decided, problems}`. A rerun skips chunks already present, so a crash resumes.
  - **The scoring:**
    - Score only items that all three runs decided.
    - Print the exclusions **per run** (`excluded: S1=… S2=… H=… total=…`, where an item failed by two runs counts in both but once in `total`). A Haiku that fails the hard articles would otherwise flatter its own score.
    - Print `WARNING: H failed share exceeds S1's by more than 2 points; read the excluded items before accepting a PASS` when that holds. This is reported beside the verdict, not a new bar: the bars are the operator's (spec §8.2).
    - Print `J(S1,S2)`, `J(H,S1)` with their subset sizes, each run's attach rate, and the `differ_kinds` counts.
    - Print the verdict and its reasons.
    - Print `--sample` random H/S1 disagreements, with a fixed seed of 0, each as title, H's storyline names and S1's. This output is for the operator's terminal only.
  - **Exit codes:** 0 on PASS, 1 on FAIL or ILL-POSED, 2 on a usage or snapshot error.

- [ ] **Step 1: Write the failing tests.**
  - `test_jaccard_ignores_items_both_left_empty`
  - `test_jaccard_partial_overlap` (`{1,2}` vs `{2,3}` gives 1/3)
  - `test_attach_rate`
  - `test_differ_kinds_less_more_elsewhere`
  - `test_verdict_ill_posed_below_065`
  - `test_verdict_pass_uses_the_larger_floor`: `j_ss=0.70`, `j_hs=0.60` gives PASS, and `j_ss=0.70`, `j_hs=0.59` gives FAIL, because the floor is max(0.60, 0.55) = 0.60.
  - `test_verdict_fails_when_haiku_attaches_far_less`: `rate_h = rate_s1 − 0.16` gives FAIL.
  - `test_main_snapshot_is_reused_and_chunks_resume` (stub `post`, and write `tmp_path` as `DATA_DIR`)
  - `test_main_reports_exclusions_per_run_and_warns_when_h_fails_more`
  - `test_main_calls_the_production_request_builder`: monkeypatch `storylines.build_attach_request` with a spy and assert it was called.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** then run the gate.
- [ ] **Step 5: Commit.** `feat(storylines): offline attach check (spec 8.2) (al7.3)`

---

## Rollout (the operator, after deploy)

**When to deploy:** after `/jobs` shows today's collect ✅ finished, and before 04:30 UTC. Not around Sunday 21:00. Migration 0019 runs on start, and every knob is off.

1. **Price the brief:** `docker compose run --rm --entrypoint python newsbrief scripts/brief_cost.py`. Paste the output. The controller re-prices spec §11's brief line and total from it, marking a `SHORT:` span as such, and commits the spec. If the total is above the £20 floor, the first dial is the web searches (§11), recorded on `3km`.
2. **Seed:** `docker compose run --rm --entrypoint python newsbrief scripts/storylines_seed.py`. About 20 proposals arrive on Telegram. Accept or reject each one, and add your own with `/storyline add Name — description`. `/storyline list` shows the result.
3. **Offline check:** `docker compose run --rm --entrypoint python newsbrief scripts/storylines_attach_check.py --week-ending <a recent Monday>`. It costs roughly $8–10, with two Sonnet runs and one Haiku run over about 90 chunks each, and takes an hour or two. Paste the output, minus the sampled disagreements, and read the 20 samples yourself.
   - **PASS:** the controller writes `docs/<date>-storylines-attach-check.md` with counts and percentages only, storyline names replaced by labels (spec §8.2 step 6), and closes `sse` with that result.
   - **ILL-POSED:** rewrite the descriptions the samples show are vague (`/storyline retire` + `add`), then rerun the check. The snapshot keeps the items, so delete `storylines_check/<date>/runs/` first, or use another week. Storylines are re-read only for a new snapshot, so a new week is simplest.
   - **FAIL:** stop. §8.3's fallback (embeddings shortlist, `bqa.7`) is decided with the operator before plan 2b.
4. **Switch attach on** (only after PASS):
   ```sql
   INSERT INTO settings (key, value) VALUES ('STORYLINES_ENABLED', 'true')
   ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value = EXCLUDED.value, updated_at = now();
   ```
5. **The next day, check the effect, not the row.** `grep "Attach:" newsbrief.log | tail -5` should show:
   - `status=ok` with `calls` above 0;
   - `failed` near 0;
   - `deferred` usually 0.

   A `config` or `unpriced` status will already have rung on Telegram. `/storyline list` should show 7-day counts above 0 for the busy storylines.
6. Close `news-brief-c5k`, since attach runs on `claude-haiku-5-5`, and close `news-brief-al7.3`. Plan 2b (`al7.4`) is written next, against the code as built.

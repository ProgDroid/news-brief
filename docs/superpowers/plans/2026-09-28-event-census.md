# Event Census Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the event census: 16 blind-labelled 6-hour windows of captured items, grouped into
events by the operator on a phone page. It includes the retention hold, the precision sample, the
consistency repeat, and the readout that sub-project 1 will score systems against.

**Architecture:** Migration 0017 holds the `census_*` tables and the retention triggers.
- Two new top-level modules:
  - `census_metrics.py`: pure functions, no I/O;
  - `census.py`: the SQL and the census rules.

  Both import only `db`, `common` and the standard library, so both processes can use them.
- `census_prepare`, a `brief.py` mode, freezes the block and the windows once.
- `labeller.py`, a stdlib HTTP server run as its own compose service under a restricted role,
  serves the labelling page.
- The Telegram daemon mints one-time links (`/label`).
- `scripts/census_report.py` prints the readout.

**Tech Stack:** Python 3.12 (the image and CI; the local interpreter is 3.14, so use nothing
3.13+-only), psycopg 3, Postgres 18, stdlib `http.server`, vanilla JS/CSS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` (revision 7).
Executors read the spec and this plan together. § references are to the spec.
**Plan review:** `docs/superpowers/reviews/2026-09-28-event-census-plan-redteam.md`. Its
blockers B1–B7 and fixes F1–F31 are folded in below, cited as (B*n*) or (F*n*).

## Global Constraints

- **Names:** tables `census_*`, mode `census_prepare`, role `census_labeller`, modules
  `census.py` `census_metrics.py` `labeller.py`, static dir `labeller_static/`. Never `gold_*`:
  that name belongs to the existing claim-break gold set (`scripts/score_gold_set.py`).
- **Imports:** `census.py`, `census_metrics.py` and `labeller.py` import only `db`, `common`,
  each other and the standard library. Never `brief`, `comprehend`, `config`, or anything that
  imports `anthropic`.
- **Pinned values:**
  - `CAPTURE_DAY_ONE = date(2026, 9, 4)`, `LOOKBACK_DAYS = 14` (equal to
    `comprehend.CANDIDATE_WINDOW_DAYS`);
  - `BLOCK_DAYS = 14`, `BLOCK_LAG_DAYS = 3`, `MIN_BLOCK_DAYS = 10`;
  - `WINDOW_HOURS = 6`, windows aligned to 00/06/12/18 **UTC** (F8);
  - `MIN_WINDOW_ITEMS = 40`, `BACKLOG_HOURS = 24`;
  - `WINDOWS_PER_STRATUM = 4`, `SEED = 20260928`;
  - `REPEAT_ORDER_NO = 2`, `REPEAT_AFTER_ORDER_NO = 8`, `REPEAT_MIN_DAYS = 7`,
    `TOTAL_SESSIONS = 17`;
  - `PRECISION_PAIRS = 10`, `IDLE_CAP_MINUTES = 5`, `HEARTBEAT_SECONDS = 60`;
  - `GO_MIN_MEAN_GROUPS = 8`, `GO_MAX_MEDIAN_MINUTES = 80`;
  - gap bands: `> 0.50` stop; `0.40 ≤ x ≤ 0.50` within-6h-only; `< 0.40` proceed;
  - `LINK_TTL = 10 min`, `SESSION_TTL = 30 days`;
  - sizing: `d = 0.5`, ρ ∈ {0.05, 0.1, 0.3}.
- The census makes **no model call** and writes nothing to `comprehend_spend`.
- **Time:** every function that takes `now` compares against that value, never SQL `now()`
  (F13). Tests pin `now`.
- **Connections:** one per request or operation, always `with db.connect() as c:`, and never
  left idle in a transaction (F21). An idle transaction hangs the next fixture's `DROP SCHEMA`.
- **HTTP in tests:** `http.client` or raw loopback sockets only. `requests` is blocked by
  `tests/conftest.py:144-171` even for loopback (F20).
- **Labeller responses:** every one, including stdlib `send_error` paths, carries the four §6.2
  headers verbatim (F22). Titles reach the DOM only through `textContent`/`createElement` (B6),
  and links render only for `http`/`https`.
- **Test gate:** the pre-push gate is three commands, plus a real database (repo `CLAUDE.md`,
  "Build & Test"): `ruff check .`, `ruff format --check .`, and `pytest -q` with `DATABASE_URL`
  exported.
  - "Passed" means the DB tests **ran**. Check the `-rs` summary.
  - Run the gate in the **foreground**.
  - From the Bash tool, run Python as `py`.
- **Packaging:** a new top-level module goes into the Dockerfile COPY line and both workflow
  lists **in the task that first imports it** (B2). `test_packaging` must stay green at every
  commit.
- **Commits:** straight to `main`, with explicit paths and never `-A`. Messages end with the
  attribution trailer the session supplies.
- **Docker:** never `docker compose up -d` locally. Only `docker compose config` is allowed.

## Review Focus

1. **Two tabs label the same window** (phone and laptop), with divergent sequence numbers.
   Expected: the write that arrives last wins, and a retried duplicate is ignored → Task 4,
   `test_later_arrival_wins_across_tabs` and `test_retried_write_is_ignored` (B5).
2. **Titles carrying HTML entities** (`AT&amp;T`). Expected: shown as "AT&T", neither
   double-escaped nor raw → Task 6, `test_json_carries_unescaped_text_with_no_literal_lt` plus
   the no-`innerHTML` static test.
3. **Items with an empty or relative URL.** Expected: no link, no broken anchor → Task 6,
   `test_link_scheme_filter`.
4. **An edit after Finish** (a stale tab, or a late retry). Expected: 409; the blind rows are
   unchanged, and the client stops retrying → Task 4,
   `test_assignments_after_finish_are_rejected`; Task 6, `test_assign_after_finish_is_409`.
5. **Items never touched before Finish.** Expected: counted as singletons → Task 2,
   `test_untouched_items_are_singletons`.

---

### Task 0: File the beads

**Files:** none (bead database only)

- [ ] **Step 1:** Create the epic:

  ```bash
  bd create --type=epic --priority=2 --title="KB sub-project 0: event census" \
    --description="Spec docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md rev 7; plan docs/superpowers/plans/2026-09-28-event-census.md"
  ```

  Then create one `--type=task --parent=<epic>` bead per Task 1–10, titled after the task.
- [ ] **Step 2:**
  - File `KB sub-project 1: confirmation (event coreference)` (`--type=epic`), citing spec §14 as
    its mandatory pre-scoring checklist.
  - Run `bd update news-brief-115 --append-notes=...`: `scripts/` must stay in the image while
    the census exists, and Task 8's packaging test now enforces this.
  - Run `bd update news-brief-uh0 --append-notes=...`: the hold rejects a whole `DELETE`
    statement on the first held row, so item retention must itself filter
    `created_at >= census_block.block_end`.

  Verify: `bd show <epic>` lists 10 children.

---

### Task 1: Migration 0017 — census tables and the retention hold

**Files:**
- Create: `migrations/0017_census_up.sql`, `migrations/0017_census_down.sql`
- Test: `tests/test_census_schema.py` (DB, using the `kb` fixture shape of
  `tests/test_comprehend_integration.py:17-25`)

**Interfaces.** The ten tables of spec §7, with these exact decisions:
- `census_block`:
  - `id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id)`;
  - `block_start`, `block_end TIMESTAMPTZ NOT NULL`;
  - `prepared_at TIMESTAMPTZ NOT NULL`;
  - `c439ade_deployed_at TIMESTAMPTZ NOT NULL`;
  - `gap_split_share DOUBLE PRECISION NOT NULL`, `gap_pairs INTEGER NOT NULL`,
    `gap_windows INTEGER NOT NULL`, `gap_deciles DOUBLE PRECISION[] NOT NULL`;
  - `gap_band TEXT NOT NULL CHECK (gap_band IN ('proceed','within_6h_only'))`;
  - `go_override_reason TEXT NULL`.
- `census_windows`:
  - `status CHECK IN ('prepared','open','blind_done','complete','abandoned')`;
  - `pass SMALLINT NOT NULL CHECK (pass IN (1,2))`;
  - `repeat_of BIGINT NULL REFERENCES census_windows(id)`.
- `census_window_items (window_id, item_id)`: primary key on both;
  `item_id REFERENCES items(id) ON DELETE RESTRICT`.
- `census_assignments`:
  - `id BIGSERIAL`, `window_id`, `item_id`, `group_id`;
  - `unsure BOOLEAN NOT NULL`;
  - `tab_id TEXT NOT NULL`, `client_seq INTEGER NOT NULL`;
  - `created_at TIMESTAMPTZ NOT NULL`;
  - `UNIQUE (window_id, tab_id, client_seq, item_id)` (B5);
  - an index on `(window_id, item_id, id DESC)`.
- `census_adjudications`:
  - `CHECK (item_a < item_b)`, `UNIQUE (kind, item_a, item_b)`;
  - `kind CHECK IN ('precision')`;
  - `decision CHECK IN ('same','different')`.
- `census_sessions`: `kind CHECK IN ('link','session')`, `token_sha256 TEXT NOT NULL UNIQUE`.
- `census_events`: `id BIGSERIAL PRIMARY KEY` (F4), and
  `kind CHECK IN ('open','action','heartbeat','finish','abandon')`.
- **Triggers:**
  - `census_hold_row()`: a row trigger, `BEFORE DELETE ON items FOR EACH ROW`. It raises when
    the block row exists and `OLD.created_at < block_end`, and otherwise **`RETURN OLD`** (F2).
  - `census_hold_truncate()`: a statement trigger, `BEFORE TRUNCATE ON items`. It raises
    whenever the block row exists.
  - Message: `RAISE EXCEPTION 'items captured before % are held by the event census; release by
    deleting the census_block row (see docs/2026-10-01-host-runbook-census.md)', v_end;`. The
    placeholder is plpgsql's `%`, never `%s` (F1).
- **Down script:**
  - first a `DO` block raising `the event census has labels; refusing to drop it` when
    `census_assignments` has any row;
  - then `DROP TRIGGER IF EXISTS` and `DROP FUNCTION IF EXISTS` for both functions, then the ten
    tables. `IF EXISTS` throughout (F9 note).

- [ ] **Step 1: Write the failing tests**
  - `test_all_ten_census_tables_exist`.
  - `test_second_census_block_row_is_rejected`: the second default-id insert raises
    `UniqueViolation`; an `id=false` insert raises `CheckViolation`.
  - `test_held_item_delete_raises_and_children_survive`:
    - an item at 2026-09-20 with an assertion and an `item_triage` row, and a block ending
      2026-09-28;
    - the delete raises `psycopg.errors.RaiseException`, with "held by the event census" and
      `2026-09-28` in the message;
    - after rollback, both children exist.
  - `test_cascade_control_without_a_census` (F3): with no block row, the same fixture's delete
    succeeds and both children are gone. This proves the children are cascade-linked.
  - `test_truncate_items_is_refused_while_census_exists`.
  - `test_post_block_item_delete_succeeds`: an item at 2026-09-29 → `rowcount == 1`, and a
    `SELECT` finds nothing (F2).
  - `test_item_at_block_end_is_not_held`: `created_at == block_end` deletes with rowcount 1.
  - `test_releasing_the_block_lifts_the_hold_except_pinned_items`: after `DELETE FROM
    census_block`, an unpinned pre-block item deletes (rowcount 1), and a pinned one raises
    `ForeignKeyViolation`.
  - `test_down_refuses_with_labels_and_runs_when_empty`, using `steps_back_through(c,
    "0017_census")`: with one assignment row, it raises; on a fresh schema it succeeds, and
    `pg_proc` has no `census_hold_%`.
  - `test_adjudication_pair_order_is_checked`: `(5, 3)` raises `CheckViolation`.
- [ ] **Step 2:** Run `py -m pytest tests/test_census_schema.py -q -rs` → FAIL. It must not say
  skipped; if it does, export `DATABASE_URL`.
- [ ] **Step 3:** Write both SQL files.
- [ ] **Step 4:** Run `py -m pytest tests/test_census_schema.py tests/test_kb_schema.py
  tests/test_comprehension_schema.py tests/test_db.py -q -rs` → PASS, 0 skipped.
- [ ] **Step 5: Commit** `migrations/0017_census_up.sql migrations/0017_census_down.sql
  tests/test_census_schema.py` — `feat(census): migration 0017, census tables and the retention
  hold`

---

### Task 2: `census_metrics.py` — pure metrics

**Files:**
- Create: `census_metrics.py`
- Test: `tests/test_census_metrics.py` (no DB)

`census_metrics.py` has no importer until Task 3, so it enters the COPY line there.

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class Assignment: item_id: int; outlet_id: int; group_id: int | None; unsure: bool
@dataclass(frozen=True)
class GoNoGo: ok: bool; mean_m: float; median_minutes: float; values: tuple[int, ...]; reason: str

T975: dict[int, float]  # df 1..15: 12.706 4.303 3.182 2.776 2.571 2.447 2.365 2.306 2.262 2.228 2.201 2.179 2.160 2.145 2.131
T80:  dict[int, float]  # df 1..15: 1.376 1.061 0.978 0.941 0.920 0.906 0.896 0.889 0.883 0.879 0.876 0.873 0.870 0.868 0.866

def groups(assignments: list[Assignment]) -> list[frozenset[int]]
def multi_outlet_groups(assignments: list[Assignment]) -> list[frozenset[int]]
def confirms(group: frozenset[int], outlet_of: dict[int, int], events_of: dict[int, set[int]]) -> bool
def pooled_share(windows: list[list[frozenset[int]]], outlet_of, events_of) -> float
def pair_recall(windows, outlet_of, events_of) -> float
def bcubed_recall(windows, outlet_of, events_of) -> float
def pairwise_agreement(ref: dict[int, int | None], other: dict[int, int | None]) -> tuple[float, float, float]
def adjusted_rand_index(a: dict[int, int | None], b: dict[int, int | None]) -> float
def item_bootstrap_interval(stat, a: dict, b: dict, reps: int = 2000, seed: int = SEED_BOOT) -> tuple[float | None, float | None, int]  # (lo, hi, nan_dropped), revised in Task 9 fix round
def mde(m_values: list[int], rho: float, d: float = 0.5) -> float
def go_no_go(m_values: list[int], active_minutes: list[float]) -> GoNoGo
def split_share(gaps_hours: list[float]) -> float
def deciles(values: list[float]) -> tuple[float, ...]
def gap_band(share: float) -> str
def draw_precision_sample(groups: list[frozenset[int]], outlet_of: dict[int, int], rng: random.Random, n: int = 10) -> list[tuple[int, int]]
def precision_estimate(per_window: list[tuple[int, int]]) -> tuple[float, float | None, float | None]
def active_minutes(stamps: list[datetime], idle_cap_minutes: float = 5) -> float
```

**Decisions:**
- `groups`:
  - an unsure item is removed first;
  - `group_id None` means a singleton;
  - a group left empty disappears.
- `confirms`: some event id is shared by two of the group's items from different outlets.
- `pooled_share` = Σ confirmed / Σ multi-outlet groups over all windows. It is `nan` when there
  are none.
- `pairwise_agreement` and `adjusted_rand_index`:
  - a `None` group is a unique cluster per item (F11);
  - the caller removes unsure items before calling;
  - with no same-group pairs, precision and recall are `nan`, never a `ZeroDivisionError`.
- `item_bootstrap_interval` resamples items with replacement, recomputes `stat(a_sub, b_sub)`,
  drops NaN replicates, and returns the 2.5% and 97.5% percentiles plus the dropped count
  ((None, None, n) when too few remain). `SEED_BOOT = 20260928`.
- `mde`:
  - `K = len`, `m̄ = mean`, `CV = pstdev / mean`;
  - `DE = 1 + ((1 + CV²) m̄ − 1) ρ`;
  - `crit = T975[K−1] + T80[K−1]`;
  - returns `100 · crit · sqrt(d · DE / (K · m̄))`;
  - raises `ValueError` for `K < 2`.
- `draw_precision_sample`:
  - take the multi-outlet groups sorted by `min(item)`;
  - draw `rng.sample(groups, min(n, len(groups)))`;
  - per group, `rng.choice` over its cross-outlet pairs, each written `(min, max)` and sorted.
- `precision_estimate`:
  - `p = Σyes / Σasked`;
  - `var = K/(K−1) · Σ(yes_k − p·asked_k)² / (Σasked)²`;
  - the interval is `p ± T975[K−1]·√var`, clipped to [0, 1];
  - `(p, None, None)` when K < 2.
- `active_minutes`: sort the stamps; sum the consecutive gaps of at most `idle_cap_minutes`.

- [ ] **Step 1: Write the failing tests**
  - `test_untouched_items_are_singletons`: four `None` items → four singletons; no multi-outlet
    groups.
  - `test_unsure_item_is_removed_and_group_rescored`: outlets {1, 1, 2}.
    - The outlet-2 item unsure → not multi-outlet.
    - An outlet-1 item unsure → multi-outlet, with 2 items.
  - `test_confirms_needs_two_outlets_in_one_event`:
    - `{a:{7}, b:{7}}` across outlets → True;
    - `{a:{7}, b:{8}}` → False;
    - the same outlet with a shared event → False.
  - `test_pooled_share_weighs_groups_not_windows`:
    - window A: 4 two-outlet groups, each confirmed through its own shared event;
    - window B: 1 two-outlet group whose items share **no** event (`events_of` gives
      `{x:{90}, y:{91}}`);
    - → `0.8`. The fixture is pinned so M1 cannot flip it.
  - `test_pair_and_bcubed_recall_on_a_hand_partition`: group {a, b, c} across 3 outlets, with the
    system linking only a–b → pair recall `1/3`, B-cubed recall `5/9`.
  - `test_pairwise_agreement_and_ari`:
    - identical partitions → `(1, 1, 1)` and ARI 1;
    - `ref={1:1,2:1,3:2,4:2}` against `other={1:1,2:1,3:1,4:1}` → precision `2/6`, recall 1;
      ARI < 1.
  - `test_all_singletons_are_not_one_cluster` (F11):
    - `ref=other={1:None,2:None,3:None}` → ARI 1, with no exception;
    - `ref={1:None,2:None}` against `other={1:5,2:5}` → precision 0.
  - `test_mde_reproduces_the_spec_table`, with `abs=0.15`:
    - `[8]*16` → 21.8 / 24.4 / 33.0;
    - `[4,12]*8` → 22.6 / 25.8 / 36.0;
    - `[10]*16` → 20.2 / 23.1 / 32.2;
    - `[6]*16` → 24.2 / 26.5 / 34.2.
  - `test_mde_uses_t_for_the_window_count`: `[10]*8` at ρ = 0.1 → 35.5.
  - `test_go_no_go_thresholds`:
    - `([8, 8], [80, 80])` → ok;
    - `([8, 7], [10, 10])` → not ok, with `mean` in the reason;
    - `([9, 9], [81, 81])` → not ok, with `minutes` in the reason.
  - `test_gap_bands_at_the_boundaries`: 0.399 → proceed; 0.40 → within_6h_only; 0.50 →
    within_6h_only; 0.501 → stop.
  - `test_split_share_caps_each_gap_at_one`: `[3, 12]` → 0.75.
  - `test_precision_sample_is_one_pair_per_group_without_replacement`:
    - groups of sizes 2, 2, 6 with `n=10` → 3 pairs from 3 distinct groups, each `a < b` and
      cross-outlet;
    - 12 groups → 10 pairs from 10 distinct groups.
  - `test_precision_sample_draws_groups_at_equal_probability`: across 2000 seeds, one six-outlet
    group and one two-outlet group with `n=1` → each 50% ± 4%.
  - `test_precision_estimate_is_pooled_over_groups` (F10): `[(9, 10), (1, 2)]` → `p = 10/12`,
    not the window mean 0.7.
  - `test_active_minutes_drops_idle_gaps`: stamps at 0, 1, 2, 10 and 11 min → 3.0.
- [ ] **Step 2:** Run `py -m pytest tests/test_census_metrics.py -q` → FAIL (no module).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Same command → PASS.
- [ ] **Step 5: Commit** `census_metrics.py tests/test_census_metrics.py` —
  `feat(census): pure metrics, sizing and sampling`

---

### Task 3: `census.py` windows and `census_prepare`

**Files:**
- Create: `census.py`
- Modify: `brief.py`:
  - add `"census_prepare": mode_census_prepare` to `MODES` (`brief.py:3898`); **not** to
    `JOB_MODES`;
  - add `census_prepare` to the usage string (`brief.py:4057`).
- Modify (B2): `Dockerfile:39`: append `census.py census_metrics.py` to the flat COPY line.
- Modify (B2): `.github/workflows/docker-publish.yml`: add `'census.py'` and
  `'census_metrics.py'` to `paths:`, and to both ruff lines (74 and 75).
- Test: `tests/census_fixtures.py` (shared helpers), `tests/test_census_rules.py` (no DB),
  `tests/test_census_prepare.py` (DB)

**Interfaces:**
- Consumes: Task 2's `split_share`, `deciles`, `gap_band`.
- Produces:
```python
class CensusRefusal(Exception): ...
@dataclass(frozen=True)
class Block: start: datetime; end: datetime
@dataclass(frozen=True)
class WindowStat: start: datetime; stratum: int; item_ids: tuple[int, ...]; backlog_excluded: int; null_published: int
@dataclass(frozen=True)
class GapCheck: split_share: float; pairs: int; windows: int; deciles: tuple[float, ...]; band: str

def compute_block(prepared_on: date) -> Block
def exclusion_reason(title: str | None, published_at: datetime | None, created_at: datetime) -> str | None
def window_stats(conn, block: Block) -> tuple[list[WindowStat], list[tuple[datetime, str]]]
def draw_order(windows: list[WindowStat], seed: int = SEED) -> list[WindowStat]
def gap_check(conn) -> GapCheck
def prepare(conn, today: date, c439ade_deployed_at: datetime, now: datetime) -> str
```
- `tests/census_fixtures.py`:
  - `seed_corpus(conn, start: datetime, days: int, per_window: int, outlets: int = 3) -> None`,
    which inserts outlets and items, with titles distinct per item;
  - `prepared(conn, today=date(2026, 10, 1)) -> None`, which also seeds a few short-gap
    (< 1 h) cross-outlet assertion pairs so the gap check has evidence (ruling R3);
  - `label` and `answer_precision` are **not** here: they need Task 4's functions, so Task 4 adds
    them (ruling R1).

**Decisions:**
- `compute_block`:
  - `end = midnight UTC(prepared_on − 3 d)`;
  - `start = max(end − 14 d, midnight UTC(CAPTURE_DAY_ONE + 14 d))`;
  - refuse below 10 days, naming the earliest valid date (2026-10-01).
- `exclusion_reason` checks, in this order:
  - an empty title after `strip()`;
  - `common.is_quote_page(title)`, called through the module attribute;
  - `published_at is not None and created_at − published_at > 24 h`.
- `window_stats`:
  - one SQL pass over `[start, end)`;
  - every timestamp is converted with `.astimezone(timezone.utc)` before bucketing (F8);
  - a window with fewer than 40 **eligible** items is skipped, with reason
    `fewer than 40 eligible items (n)`;
  - `stratum = start.hour // 6`.
- `draw_order`:
  - one `random.Random(seed)`;
  - for stratum 0..3, `rng.sample(windows sorted by start, 4)`;
  - then round r takes the r-th pick of each stratum, in the order `rng.sample([0, 1, 2, 3], 4)`.
- `gap_check` (F9):
  - `SELECT DISTINCT` item pairs with `a1.item_id < a2.item_id`, sharing an `event_id`, with
    different `outlet_id`;
  - `g = |Δcreated_at|` in hours;
  - `windows` = distinct UTC 6-hour windows touched;
  - **zero pairs → `CensusRefusal("gap check has no merged cross-outlet pairs; cannot
    evaluate")`** (ruling R3: an unanswerable check refuses rather than passing on an empty
    mean).
- `prepare`, in one transaction:
  1. if a block exists, return `already prepared ...`;
  2. gap check, and refuse on `stop`;
  3. `compute_block`, `window_stats`, `draw_order`;
  4. insert the block (`prepared_at = now`), the skipped windows, 16 order rows, 16 pass-1
     windows (`prepared`) and their `census_window_items`;
  5. **one pass-2 window**, with `repeat_of` = order_no 2's id and the same
     `order_no`/`window_start`, **plus a copy of order_no 2's `census_window_items` rows under
     the pass-2 id** (B3);
  6. commit.
- `mode_census_prepare()`:
  - reads `CENSUS_C439ADE_DEPLOYED_AT` (ISO-8601); if it is missing or unparseable, print the
    reason and `sys.exit(2)`;
  - takes `db.advisory_lock(conn, "census_prepare")`. **If the yielded bool is False**, print
    `census_prepare is already running` and `sys.exit(2)` (F31);
  - a `CensusRefusal` is printed, then `sys.exit(2)`.

- [ ] **Step 1: Write the failing tests**

  `tests/test_census_rules.py`:
  - `test_block_on_2026_10_01_is_ten_days_from_09_18`.
  - `test_block_on_2026_09_30_refuses_naming_2026_10_01`.
  - `test_block_later_is_fourteen_days`: `2026-10-20` → `10-03` to `10-17`.
  - `test_lookback_matches_comprehend`.
  - `test_exclusion_reasons`:
    - `"  "` → `empty_title`;
    - published 25 h before → `backlog`;
    - 23 h → `None`;
    - published_at None → `None`.
  - `test_quote_pages_are_excluded_through_common` (F5):
    - the real function first: `"LCO - Reuters"` → `quote_page`;
    - then monkeypatch `common.is_quote_page` to `lambda t: False` → `"LCO - Reuters"` gives
      `None`.
  - `test_draw_order_two_per_stratum_in_first_eight`: 10 synthetic windows per stratum → 16
    distinct; order_no 1–8 hold two per stratum.
  - `test_draw_order_is_deterministic`.
  - `test_draw_order_refuses_a_thin_stratum`: stratum 3 with 3 windows → `CensusRefusal`
    containing `stratum 3`.

  `tests/test_census_prepare.py`:
  - `test_prepare_freezes_16_windows_and_the_repeat`:
    - 17 windows;
    - the pass-2 `repeat_of` is order_no 2;
    - the pass-2 item set equals order_no 2's (B3);
    - pass-1 membership equals the eligible items of the 16.
  - `test_prepare_is_a_no_op_the_second_time`: counts unchanged; the text starts
    `already prepared`.
  - `test_forty_item_rule_counts_eligible_items` (F7): one window with 39 eligible items plus 1
    backlog item → skipped with `fewer than 40 eligible items (39)`; one with 40 eligible → drawn
    or eligible.
  - `test_backlog_excluded_and_null_published_kept`.
  - `test_windows_bucket_in_utc` (F8): `SET TIME ZONE 'Asia/Tokyo'` on the connection before
    `prepare`; every `window_start` has hour ∈ {0, 6, 12, 18} in UTC.
  - `test_gap_pairs_are_distinct_items` (F9): an item with two assertions on one event, paired
    with one other-outlet item → `pairs == 1`.
  - `test_prepare_refuses_when_gap_share_exceeds_half`: seeded 12 h gaps → refused, and no
    `census_*` rows.
  - `test_prepare_records_within_6h_band`: share 0.45 → `within_6h_only`, and `gap_deciles` has
    9 values.
  - `test_prepare_arms_the_hold`.
  - `test_prepare_refuses_with_no_merged_pairs` (R3): a corpus with no assertions → refused,
    with `no merged cross-outlet pairs` in the message, and no `census_*` rows.
- [ ] **Step 2:** Run `py -m pytest tests/test_census_rules.py tests/test_census_prepare.py -q
  -rs` → FAIL.
- [ ] **Step 3:** Implement, including the Dockerfile and workflow edits.
- [ ] **Step 4:** Run `py -m pytest tests/test_census_rules.py tests/test_census_prepare.py
  tests/test_packaging.py -q -rs` → PASS, 0 skipped.
- [ ] **Step 5: Commit** `census.py brief.py Dockerfile .github/workflows/docker-publish.yml
  tests/census_fixtures.py tests/test_census_rules.py tests/test_census_prepare.py` —
  `feat(census): block, windows and census_prepare`

---

### Task 4: `census.py` session state

**Files:**
- Modify: `census.py`
- Test: `tests/test_census_state.py` (DB)

**Interfaces (Produces):**
```python
class WindowClosed(Exception): ...
class BadWrite(ValueError): ...            # item not in window, group from another window, pair not offered
@dataclass(frozen=True)
class Task: kind: str; window_id: int | None; session_no: int; detail: str
#   kind: 'blind' | 'precision' | 'gate_failed' | 'waiting' | 'complete' | 'not_prepared'

def current_task(conn, now: datetime) -> Task
def window_items(conn, window_id: int) -> list[dict]    # id, title, url, outlet; capture order; from census_window_items only
def record_event(conn, window_id: int, kind: str, at: datetime) -> None
def create_group(conn, window_id: int, at: datetime) -> int
def save_assignments(conn, window_id: int, tab_id: str, client_seq: int, rows: list[tuple[int, int | None, bool]], at: datetime) -> None
def latest_assignments(conn, window_id: int) -> dict[int, tuple[int | None, bool]]   # the ONE reader of the log
def window_assignments(conn, window_id: int) -> list[census_metrics.Assignment]      # built on latest_assignments
def finish_blind(conn, window_id: int, at: datetime) -> None
def abandon(conn, window_id: int, reason: str, at: datetime) -> None
def precision_pairs(conn, window_id: int) -> list[tuple[int, int]]
def save_precision(conn, window_id: int, item_a: int, item_b: int, decision: str, at: datetime) -> None
def window_active_minutes(conn, window_id: int) -> float
def go_status(conn) -> census_metrics.GoNoGo | None
```

**Decisions:**
- **Latest state (B5):**
  - the latest row per item is the highest `id`, which is arrival order;
  - `save_assignments` uses `INSERT ... ON CONFLICT (window_id, tab_id, client_seq, item_id) DO
    NOTHING`, so a retried write is ignored;
  - `latest_assignments` is the only SQL that reads the log (M3 mutates exactly this).
- **`save_assignments`**, in one transaction:
  - `SELECT status, blind_done_at FROM census_windows WHERE id = %s FOR UPDATE` first (F17);
  - raise `WindowClosed` if the window is done or abandoned;
  - raise `BadWrite` for an item outside `census_window_items`, or a `group_id` from another
    window (F18);
  - set status `open`.
- **`save_precision`** accepts only a pair in `precision_pairs`, normalised to `a < b` (F18). It
  sets `complete` when none remain. A window with zero multi-outlet groups completes at Finish.
- **`precision_pairs`**: `draw_precision_sample(..., random.Random(window_id))`, minus answered
  pairs.
- **`window_active_minutes`** covers only events at or before `blind_done_at` (F19), including
  `heartbeat`.
- **`go_status`**:
  - `None` until order_no 1 and 2 (pass 1) are each `blind_done` **or abandoned**;
  - if either is abandoned → `GoNoGo(ok=False, reason='window N abandoned; the gate cannot be
    evaluated')` (B4);
  - otherwise `go_no_go` over their multi-outlet counts and active minutes.
- **"Done" for sequencing** means `blind_done` or `abandoned` (B4).
- **Repeat rules (B4):**
  - order_no 2 pass 1 abandoned → `abandon` also marks the pass-2 window abandoned, with reason
    `repeat void: window 2 abandoned`;
  - pass 2 abandoned → order_no 2's precision is released.
- **`current_task` priority:**
  1. no block → `not_prepared`;
  2. `go_status` not ok and no override → `gate_failed`, with the reason as detail;
  3. a window with status `open` (in progress) → `blind` on it (F16);
  4. a `blind_done` window with precision pending and not held → `precision`. Order_no 2 pass 1
     is held until pass 2 is done;
  5. the repeat, when eligible (order_no 8 done and ≥ 7 days since order_no 2's
     `blind_done_at`) and not done → `blind` on it;
  6. the lowest pass-1 `order_no` not done → `blind`;
  7. only the repeat remains, not yet eligible → `waiting`, with the date as detail;
  8. `complete`.
- `session_no` = 1 + the count of done windows (pass 1 and 2).

**Fixture helpers (R1)** — add to `tests/census_fixtures.py`:
- `label(conn, window_id, groups: list[list[int]], now)`, which assigns and finishes;
- `answer_precision(conn, window_id, now)`, which answers every offered pair `same`.

**Fixture rule (F14):** a test that needs later windows first labels windows 1–2 with **≥ 8
multi-outlet groups each** and answers their precision via `answer_precision`, or sets the
override.

- [ ] **Step 1: Write the failing tests**
  - `test_first_task_is_window_one`.
  - `test_later_arrival_wins_across_tabs` (B5): tab `p` writes X→A with seq 25, then tab `l`
    writes X→B with seq 11 → B.
  - `test_retried_write_is_ignored` (B5): tab `p` seq 1 X→A; tab `l` seq 1 X→B; tab `p` retries
    seq 1 X→A → B, and the log has 2 rows.
  - `test_window_assignments_uses_the_same_order`: the same fixture through `window_assignments`
    → X in group B.
  - `test_assignments_after_finish_are_rejected`.
  - `test_foreign_item_or_group_is_rejected` (F18): an item from another window raises
    `BadWrite`; so does a group from another window.
  - `test_membership_ignores_later_items_and_predicate_changes` (F6): after prepare, insert 500
    items in the block and patch `common.is_quote_page` to always return True →
    `window_items(order 1)` is unchanged.
  - `test_repeat_serves_window_two_items` (B3): `window_items(pass2) == window_items(order_no 2)`.
  - `test_precision_follows_finish_and_completes_the_window`.
  - `test_precision_rejects_a_pair_not_offered` (F18).
  - `test_window_two_precision_waits_for_the_repeat_then_is_served` (F15): withheld after
    order_no 2's Finish; served once pass 2 is `blind_done`.
  - `test_gate_blocks_after_two_thin_windows`: 3 groups each → `gate_failed`, with `mean` in the
    detail.
  - `test_gate_override_unblocks`.
  - `test_abandoned_gate_window_blocks` (B4): order_no 1 abandoned and order_no 2 done →
    `gate_failed`, with `abandoned` in the detail.
  - `test_abandoning_window_two_voids_the_repeat` (B4): after windows 3–16 are done, the task is
    `complete`, not `waiting`.
  - `test_abandoned_window_eight_still_releases_the_repeat` (B4).
  - `test_abandoned_repeat_releases_window_two_precision` (B4).
  - `test_open_window_is_finished_before_the_repeat` (F16): order_no 11 is `open` when the repeat
    becomes eligible → the task is order_no 11.
  - `test_repeat_is_served_after_window_eight_and_seven_days`: at 6 days → order_no 9; at 7 days
    → pass 2.
  - `test_waiting_when_only_the_repeat_remains_early`.
  - `test_active_minutes_stop_at_blind_done` (F19): events after `blind_done_at` do not count.
- [ ] **Step 2:** Run `py -m pytest tests/test_census_state.py -q -rs` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Same command → PASS, 0 skipped.
- [ ] **Step 5: Commit** `census.py tests/test_census_state.py tests/census_fixtures.py` —
  `feat(census): session sequencing, autosave and the precision sample`

---

### Task 5: Tokens, the labeller role and its grants

**Files:**
- Modify: `census.py`
- Create: `scripts/census_grants.py`, with the `sys.path` shim (`tests/test_packaging.py:300`)
- Test: `tests/test_census_access.py` (DB)

**Interfaces (Produces):**
```python
LABELLER_ROLE = "census_labeller"
@dataclass(frozen=True)
class Grant: kind: str; obj: str; privilege: str; column: str | None = None   # 'schema'|'table'|'column'|'sequence'
LABELLER_GRANTS: tuple[Grant, ...]

def mint_link(conn, now: datetime) -> str
def open_session(conn, link_token: str, now: datetime) -> str | None
def session_valid(conn, session_token: str, now: datetime) -> bool
def revoke_all(conn, now: datetime) -> int
def apply_labeller_grants(conn, password: str) -> None
def missing_privileges(conn) -> list[str]
```

**Decisions:**
- **`LABELLER_GRANTS`** = `Grant('schema', 'public', 'USAGE')` **first** (B1), then spec §6.1's
  list. Sequences come from `pg_get_serial_sequence` for each table it inserts into.
  - **Deviation, stated:** the schema USAGE is not in the spec's list. Freshly created schemas
    (every test fixture, and any restore into a recreated schema) grant PUBLIC nothing.
- **`apply_labeller_grants`**, idempotent:
  - `DO` block: `CREATE ROLE census_labeller LOGIN` if absent;
  - then `ALTER ROLE census_labeller LOGIN PASSWORD <psycopg.sql.Literal>` (B1);
  - then every GRANT, with objects qualified as `public.<name>`.
- **`missing_privileges`** checks `has_schema_privilege` first, then the rest with
  `public.`-qualified names.
- **Tokens:**
  - `mint_link` stores the sha256 of `secrets.token_urlsafe(32)` with `kind='link'` and
    `expires_at = now + 10 min`;
  - `open_session` uses spec §6.2's atomic `UPDATE ... RETURNING`, with **`kind = 'link'`** (F12)
    and `expires_at > %(now)s` (F13), then inserts a `kind='session'` row with
    `expires_at = now + 30 d`;
  - `session_valid` requires **`kind = 'session'`** (F12), not revoked and not expired, and
    compares hashes with `hmac.compare_digest`. The compare is defence in depth: the lookup is
    already by hash, and M2 targets the kind check instead.
- **`scripts/census_grants.py`** reads `CENSUS_LABELLER_PASSWORD`. It exits 2 naming the
  variable when that is empty, otherwise applies the grants, commits and prints
  `grants applied`.
- **Deviation, stated:** spec §6.1's `.sql` script becomes `.py`. `ALTER ROLE` cannot take a
  bind parameter, psql variables do not exist under psycopg, and the list lives once, in
  `LABELLER_GRANTS`.

- [ ] **Step 1: Write the failing tests**
  - `test_link_is_single_use`.
  - `test_expired_link_is_refused`: `now + 11 min`.
  - `test_revoked_link_is_refused` (spec §10).
  - `test_link_token_is_not_a_session` (F12): a link token passed to `session_valid` → False.
  - `test_session_token_cannot_open_a_session` (F12).
  - `test_revoked_sessions_are_invalid`.
  - `test_session_expires_after_thirty_days`: valid at 29 d, invalid at 31 d.
  - `test_no_plaintext_token_is_stored`: every column of `census_sessions` cast to text contains
    neither token.
  - `test_grants_are_idempotent_and_complete`: apply twice; log in as `census_labeller` (host
    and port via `conninfo_to_dict` of `DATABASE_URL`) → `missing_privileges == []`.
  - `test_missing_privileges_names_a_revoked_grant`: revoke `INSERT ON public.census_events` →
    the list contains `INSERT on census_events`.
  - `test_missing_schema_usage_is_named`: revoke `USAGE ON SCHEMA public` → the list contains
    `USAGE on schema public`, not an exception.
  - `test_labeller_role_cannot_read_settings_or_write_items`: both raise
    `InsufficientPrivilege`.

  Every labeller-role connection is closed in `finally` (F21).
- [ ] **Step 2:** Run `py -m pytest tests/test_census_access.py -q -rs` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `py -m pytest tests/test_census_access.py tests/test_packaging.py -q -rs`
  → PASS, 0 skipped.
- [ ] **Step 5: Commit** `census.py scripts/census_grants.py tests/test_census_access.py` —
  `feat(census): one-time links, sessions and the labeller role`

---

### Task 6: `labeller.py` and its static assets

**Files:**
- Create: `labeller.py`, `labeller_static/label.js`, `labeller_static/label.css`
- Modify (B2):
  - `Dockerfile`: append `labeller.py` to the flat COPY line, and add
    `COPY labeller_static/ ./labeller_static/`;
  - the workflow: add `'labeller.py'` and `'labeller_static/**'` to `paths:`, and `labeller.py`
    to both ruff lines.
- Test: `tests/test_labeller.py` (DB, an in-process server, and one real subprocess)

**Interfaces:**
- Consumes: the Task 4 and Task 5 functions above.
- Produces:
  - `labeller.main() -> int`;
  - `labeller.make_server(base_url: str, bind: str, port: int) -> ThreadingHTTPServer` (F25);
  - environment: `LABELLER_BASE_URL` (required), `LABELLER_BIND` (default `0.0.0.0`),
    `LABELLER_PORT` (default `8765`).

**Routes:**

| Method, path | Behaviour |
|---|---|
| `GET /open?t=<token>` | `open_session`. On success: `303` to `/`, with `Set-Cookie: census_session=<tok>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`. Otherwise 403. |
| `GET /` | Needs a valid cookie. Renders `current_task`: the blind page, the precision page, or a status message. Logs `open`. The header is `Session n/17` **only**, on every session (F23, §4.6). |
| `GET /static/label.js`, `/static/label.css` | Exact names only. |
| `POST /api/groups` `{window_id}` | `{group_id}` |
| `POST /api/assign` `{window_id, tab_id, client_seq, rows}` | 204; 409 `WindowClosed`; 400 `BadWrite`; logs `action` |
| `POST /api/heartbeat` `{window_id}` | 204; logs `heartbeat` (F19) |
| `POST /api/finish` `{window_id}` | 204 |
| `POST /api/abandon` `{window_id, reason}` | 204; 400 if the reason is empty |
| `POST /api/precision` `{window_id, item_a, item_b, decision}` | 204; 400 `BadWrite` |

**Decisions:**
- **Every request:**
  - `Host` must equal the host:port of `LABELLER_BASE_URL`, else 403;
  - a POST also needs `Origin` equal to `LABELLER_BASE_URL`;
  - every route but `/open` and `/static/*` needs a valid cookie;
  - the 403 body is `forbidden`.
- **Headers (F22):** override `end_headers` to append the four §6.2 headers, so every path —
  `send_error` 400/404/414/501 included — carries them.
- **Rendering (B6):**
  - the server emits a static shell plus
    `<script type="application/json" id="data">` whose payload is
    `json.dumps(data).replace("<", "\\u003c")`;
  - titles in it are `html.unescape(title)`, as raw text;
  - `url` is included only when `urlsplit(url).scheme in {'http', 'https'}`, else `null`;
  - `label.js` builds the entire DOM with `createElement`/`textContent`. It uses no `innerHTML`,
    `outerHTML`, `insertAdjacentHTML` or `document.write`, and sets `href` only from the
    pre-filtered `url`.
- **Client (`label.js`):**
  - on load, `tab_id = crypto.randomUUID()` and `client_seq` starts at 1;
  - one ordered queue carries assigns **and Finish** (F24): Finish waits behind unsaved actions;
  - a failure shows `#unsaved` ("not saved") and retries in order every 3 s;
  - a **409 stops the queue** and shows "window closed — reload" (F24);
  - it posts `/api/heartbeat` every 60 s while `document.visibilityState === 'visible'` (F19).
- **Server:**
  - `ThreadingHTTPServer`;
  - each request opens and closes its own `db.connect()` (F21);
  - the self-check at startup: a non-empty `missing_privileges` → `missing grant: <each>` on
    stderr and exit 3; an unreachable DB → exit 3, naming the error.
- **Deviation, stated:** `/open` redirects rather than rendering and using `replaceState`.

- [ ] **Step 1: Write the failing tests.** Fixtures:
  - a `labeller_server` fixture starts `make_server` on `127.0.0.1:0` in a thread, and calls
    `shutdown()` and `server_close()` in its finalizer (F21);
  - requests go through `http.client.HTTPConnection` (F20).

  Tests:
  - `test_service_starts_as_the_labeller_role_and_refuses_without_cookie`:
    - after `apply_labeller_grants`, spawn `[sys.executable, "labeller.py"]` with an env of only
      `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER=census_labeller`,
      `POSTGRES_PASSWORD`, `LABELLER_BASE_URL`, `LABELLER_BIND=127.0.0.1`, `LABELLER_PORT`,
      `NEWSBRIEF_LOG_FILE=0`, `NEWSBRIEF_DATA_DIR`, `PATH`, and `SYSTEMROOT` on Windows;
    - poll for ≤ 10 s;
    - `GET /` → 403 with all four headers;
    - `terminate()` and `wait()` in `finally`.
  - `test_service_exits_naming_a_missing_grant`: revoke `INSERT ON public.census_events` → exit
    3 within 10 s, and stderr contains `INSERT on census_events`.
  - `test_labeller_never_reads_a_knob`:
    - monkeypatch `common.__getattr__` to raise `AssertionError` for any `common.KNOBS` name;
    - drive `/open`, `/`, groups, assign, heartbeat, finish and precision in-process;
    - every response is 2xx or 303.
  - `test_link_flow_sets_cookie_and_redirects`: 303 with `HttpOnly` and `SameSite=Lax`; reuse →
    403.
  - `test_wrong_host_or_origin_is_forbidden`.
  - `test_every_response_carries_the_security_headers`: for a 200, 204, 303, 403, 409, a 404
    (unknown path) and a 501 (`PUT /`), each has the exact CSP, `nosniff`, `no-referrer` and
    `no-store`.
  - `test_json_carries_unescaped_text_with_no_literal_lt` (B6): titles `<script>alert(1)</script>`
    and `AT&amp;T` → the `#data` block's text contains no `<` between its tags; the parsed JSON
    carries the raw `<script>alert(1)</script>` and `AT&T`.
  - `test_link_scheme_filter`: urls `javascript:alert(1)`, `""` and `/relative` → `url: null` in
    the JSON; an `https://` url survives.
  - `test_label_js_uses_no_html_sinks` (B6): the text of `labeller_static/label.js` matches none
    of `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`.
  - `test_assign_after_finish_is_409`.
  - `test_static_paths_are_exact`: `/static/../census.py` → 404.
- [ ] **Step 2:** Run `py -m pytest tests/test_labeller.py -q -rs` → FAIL.
- [ ] **Step 3:** Implement `labeller.py`, `label.js` and `label.css`: the §6.3 controls, Abandon
  with a reason, and the §5 guideline verbatim in a `<details>` element rendered by the server
  as static text.
- [ ] **Step 4:** Run `py -m pytest tests/test_labeller.py tests/test_packaging.py -q -rs` →
  PASS, 0 skipped.
- [ ] **Step 5: Manual smoke test** (local):
  1. seed the test Postgres with `tests/census_fixtures.py`;
  2. `LABELLER_BASE_URL=http://127.0.0.1:8765 py labeller.py`;
  3. mint a link with `py -c`;
  4. in a browser: label five items, reload (the state persists), and stop the DB (the banner
     shows, then the queue drains on restart);
  5. open two tabs and move one item in each (the later arrival holds).

  Report what was observed.
- [ ] **Step 6: Commit** `labeller.py labeller_static/ Dockerfile .github/workflows/docker-publish.yml tests/test_labeller.py` — `feat(census): the labelling page`

---

### Task 7: Telegram — `/label`, `/label reset`, the nudge

**Files:**
- Modify: `brief.py`:
  - handlers next to `/capture` (`brief.py:1320-1330`);
  - `BOT_COMMANDS` (`brief.py:3481`);
  - the help text near `brief.py:760`;
  - `mode_collect`: the nudge goes at the **end**, after the retention step (F27).
- Modify: `docker-compose.yml`: add `- LABELLER_BASE_URL=${LABELLER_BASE_URL:-}` to the
  `x-newsbrief` anchor, with a comment saying it is a plain variable, not a knob.
- Modify: `tests/test_trading.py:593`: stub `brief._census_nudge` in
  `test_collect_trading_failure_does_not_duplicate_brief` (F29).
- Test: `tests/test_commands.py` (append; `_capture(monkeypatch)` for sends and the `_FakeConn`
  pattern of `tests/test_commands.py:1199-1218` for `db.connect`, per F26)

**Interfaces:**
- Produces:
  - `_label_render() -> None`;
  - `_label_reset() -> None`;
  - `_census_nudge() -> None`.

  All connect with `connect_timeout=JOBS_DB_TIMEOUT_SECONDS` and
  `options="-c statement_timeout=5000"` (F27).

**Decisions:**
- **`/label`** sends one message:
  - the progress line `🏷 Session {n}/17 · {detail}`, or the not-prepared, gate-failed, waiting
    or complete text;
  - plus `{LABELLER_BASE_URL}/open?t={token}` for the `blind` and `precision` tasks;
  - an empty base URL → says so and mints nothing;
  - a DB error → `Could not read the census tables: ...`.
- **`/label reset`** → `revoke_all`, then replies with the count.
- **`_census_nudge`**:
  - wraps everything in `try/except Exception` → `log.warning`, with no send;
  - sends `🏷 Session {n}/17 ready · /label` only for `blind`/`precision`.

- [ ] **Step 1: Write the failing tests**
  - `test_label_sends_progress_and_a_link`.
  - `test_label_without_base_url_mints_nothing`.
  - `test_label_reports_a_db_error_rather_than_not_prepared`.
  - `test_label_reset_revokes`.
  - `test_nudge_sends_one_line_when_ready_and_nothing_otherwise`.
  - `test_nudge_error_sends_nothing_and_raises_nothing`.
  - `test_collect_finishes_when_the_nudge_fails` (F28): with `census.current_task` raising,
    `mode_collect` still calls `clear_batch_state` and `run_retention`. Stub the stages as
    `tests/test_trading.py:593` does.
  - `test_label_reaches_telegram_autocomplete`, `test_label_is_documented_in_the_help`.
- [ ] **Step 2:** Run `py -m pytest tests/test_commands.py -q -k "label or nudge or
  collect_finishes"` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `py -m pytest tests/test_commands.py tests/test_trading.py
  tests/test_packaging.py -q -rs` → PASS.
  `test_every_variable_the_anchor_passes_through_is_read_by_something` stays green, because
  `brief.py` reads `os.environ.get("LABELLER_BASE_URL")`.
- [ ] **Step 5: Commit** `brief.py docker-compose.yml tests/test_commands.py tests/test_trading.py` — `feat(census): /label, /label reset and the morning nudge`

---

### Task 8: The compose service, and keeping the scripts in the image

**Files:**
- Modify: `docker-compose.yml`: the `labeller` service
- Test: `tests/test_packaging.py` (append)

**The service.** It does **not** use `<<: *newsbrief`:

```yaml
  # The event census labelling page (spec 2026-09-28 §6.1). Started only with
  # `--profile census`; holds DB credentials for a role that can write only census_* tables.
  # Pin LABELLER_IMAGE to a digest for the whole census, so a deploy cannot change the tool mid-census.
  labeller:
    image: ${LABELLER_IMAGE:-ghcr.io/progdroid/news-brief:latest}
    profiles: [census]
    entrypoint: ["python", "labeller.py"]
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      - POSTGRES_HOST=${POSTGRES_HOST:-postgres}
      - POSTGRES_PORT=${POSTGRES_PORT:-5432}
      - POSTGRES_DB=${POSTGRES_DB:-newsbrief}
      - POSTGRES_USER=census_labeller
      - POSTGRES_PASSWORD=${CENSUS_LABELLER_PASSWORD:-}
      - LABELLER_BASE_URL=${LABELLER_BASE_URL:-}
      - NEWSBRIEF_LOG_FILE=0
    ports:
      - "${LABELLER_BIND:-127.0.0.1}:${LABELLER_PORT:-8765}:8765"
    restart: "no"
    depends_on:
      postgres:
        condition: service_healthy
```

The `127.0.0.1` default is deliberate: an unset bind is unreachable from the phone, which is
loud, rather than exposed.

- [ ] **Step 1: Write the failing tests**
  - `test_labeller_static_ships_in_the_image`: the directory is in `_copy_listed_packages()`,
    and every `/static/<name>` in `labeller.py` exists in it.
  - `test_labeller_static_triggers_a_rebuild`: `labeller_static/**` is in the workflow's
    `paths:`.
  - `test_labeller_service_holds_no_secret_but_its_own`:
    - extract the `labeller:` block by indentation;
    - its env names are exactly the seven listed above;
    - there is no `<<: *newsbrief`;
    - it has `profiles: [census]` and the `labeller.py` entrypoint.
  - (`test_census_runbook_scripts_ship_in_the_image` lives in Task 9, where `census_report.py`
    is created — ruling R2.)
- [ ] **Step 2:** Run `py -m pytest tests/test_packaging.py -q` → the new tests FAIL.
- [ ] **Step 3:** Add the service.
- [ ] **Step 4: Verify**
  - `py -m pytest tests/test_packaging.py -q` → PASS;
  - `POSTGRES_PASSWORD=x docker compose --profile census config >/dev/null; echo REAL_EXIT=$?`
    → `REAL_EXIT=0`;
  - `POSTGRES_PASSWORD=x docker compose config --services` lists `newsbrief` and `postgres`
    only.
- [ ] **Step 5: Commit** `docker-compose.yml tests/test_packaging.py` — `build(census): the labeller compose service`

---

### Task 9: `scripts/census_report.py` — the readout

**Files:**
- Create: `scripts/census_report.py`, with the path shim
- Test: `tests/test_census_report.py` (DB)

**Interfaces:**
- `render(conn, now: datetime) -> str`.
- `main() -> int`: runs `render` inside `SET TRANSACTION READ ONLY` (F30) and prints it. It
  returns 2 when there is no census.

**Sections (§8), in order:**
- **Gap check:** share, pairs, windows, band, and the **deciles** (§4.3 distribution), with
  "biased toward passing (spec §4.3)".
- **Block:** start and end, the `c439ade` deploy date, and whether the block straddles it. The
  by-half split point is the block midpoint.
- **Per window, in session order:** order_no, pass, status, items, groups, singletons, unsure
  share, NULL-published share, multi-outlet groups, and active / wall minutes.
- **Go/no-go**, once `go_status` is not None: both window values, the mean, the median minutes,
  the verdict, and the override.
- **Achieved detectable difference at the current K** (§4.5): printed whenever ≥ 2 pass-1
  windows are done, over the `blind_done` pass-1 windows. It is labelled `(final)` only when all
  16 are done.
- **Once pass 2 of window 2 is done:** consistency, as pairwise P/R/F1 and ARI of pass 2 against
  pass 1 with unsure items removed, **each with `item_bootstrap_interval`** (§4.6), labelled
  "one window: thin". Or `consistency unavailable: repeat void` when abandoned.
- **Blind precision** with its interval, over complete windows.
- **The by-block-half table**, labelled "descriptive only".
- Window 2's scored pass is pass 1 everywhere else.

- [ ] **Step 1: Write the failing tests**
  - `test_report_without_census_says_so`: `main()` → 2, with `not prepared`.
  - `test_report_shows_go_no_go_after_two_windows`.
  - `test_report_prints_mde_at_current_k_before_the_end`: 2 windows done → a line containing
    `current K = 2`.
  - `test_report_final_mde_matches_metrics`: 16 windows of 8 groups each → the ρ = 0.1 value is
    `24.4`, labelled `(final)`.
  - `test_report_scores_window_two_pass_one`.
  - `test_report_prints_gap_deciles_and_c439ade`.
  - `test_census_runbook_scripts_ship_in_the_image` (R2), in `tests/test_packaging.py`:
    `scripts/census_report.py` and `scripts/census_grants.py` sit under a directory in
    `_copy_listed_packages()`. If `news-brief-115` removes `scripts/`, this fails.
  - `test_report_is_read_only` (F30): `render` called inside a `READ ONLY` transaction does not
    raise; plus a negative control, in which a monkeypatched write inside `render` does raise
    `ReadOnlySqlTransaction`.
- [ ] **Step 2:** Run `py -m pytest tests/test_census_report.py -q -rs` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `py -m pytest tests/test_census_report.py tests/test_packaging.py -q -rs`
  → PASS, including `test_census_runbook_scripts_ship_in_the_image` and the path-shim tests for
  `census_report` and `census_grants`.
- [ ] **Step 5: Commit** `scripts/census_report.py tests/test_census_report.py` — `feat(census): the readout`

---

### Task 10: Runbook, mutation checks, the full gate

**Files:**
- Create: `docs/2026-10-01-host-runbook-census.md`
- Create: `docs/superpowers/reviews/2026-09-28-event-census-mutations.md`

- [ ] **Step 1: Write the runbook.** Steps in order, each with its command:
  1. Deploy the image, and confirm `0017_census` is in `schema_migrations`.
  2. **The `c439ade` deploy date, by recognition.**
     - Show the operator, for each local image, `docker image inspect --format '{{index
       .Config.Labels "org.opencontainers.image.revision"}} {{.Created}}'`. The revision proves
       which images contain `c439ade`; `Created` is **build** time, not deploy time.
     - Also show the first `job_runs` row after that image's first start
       (`SELECT min(started_at) FROM job_runs WHERE started_at > '<Created>'`).
     - The operator confirms one timestamp. If the old image is pruned, say so and use the
       `job_runs` evidence alone.
  3. On or after 2026-10-01:
     `docker compose run --rm -e CENSUS_C439ADE_DEPLOYED_AT=<confirmed> newsbrief census_prepare`.
     Record the gap band it prints.
  4. **In `.env`:**
     - `CENSUS_LABELLER_PASSWORD` (`openssl rand -hex 32`);
     - `LABELLER_BIND` (the host's LAN IP);
     - `LABELLER_BASE_URL` (`http://<LAN IP>:8765`);
     - `LABELLER_IMAGE` = the current image **digest** (`docker image inspect --format
       '{{index .RepoDigests 0}}'`), kept for the whole census.

     Also add the `labeller` service and the anchor's `LABELLER_BASE_URL` line to the **host**
     compose file, which is ahead of the repo's (memory `env-var-needs-compose-passthrough`).
     **If the host uses `DATABASE_URL`,** give the labeller the discrete `POSTGRES_*` values of
     that database instead (§6.1).
  5. **Check the password reaches the container:**
     `docker compose run --rm -e CENSUS_LABELLER_PASSWORD --entrypoint sh newsbrief -c 'test -n
     "$CENSUS_LABELLER_PASSWORD"; echo REAL_EXIT=$?'` → `REAL_EXIT=0`. If it is 1, export the
     variable in the shell first.
     Then run `docker compose run --rm -e CENSUS_LABELLER_PASSWORD --entrypoint python
     newsbrief scripts/census_grants.py`. Re-run it after any restore.
  6. Run `docker compose up -d newsbrief`, then `docker compose --profile census up -d
     labeller`, then `docker compose logs labeller`. "missing grant" means step 5 did not run.
  7. Send `/label` from Telegram.
  8. The readout: `docker compose run --rm --entrypoint python newsbrief
     scripts/census_report.py`.
  9. **Go/no-go failure:** bring it to the operator. Only an explicit ruling sets
     `UPDATE census_block SET go_override_reason = '<ruling, date>'`.
  10. **After the census:** `docker compose --profile census stop labeller && docker compose rm
      -f labeller`.
  11. **Release the hold, only when sub-projects 1 and 2 are done with the census:**
      `DELETE FROM census_block;`. Pinned items stay until the `census_*` tables are dropped,
      and the down migration refuses while labels exist. That is deliberate friction.
- [ ] **Step 2: Start the test database FIRST (B7).** Start it as in repo `CLAUDE.md`, with
  `--tmpfs /var/lib/postgresql`, and export `DATABASE_URL`. Then confirm with
  `py -m pytest tests/test_census_schema.py -q -rs`: it reports passed, with 0 skipped.
- [ ] **Step 3: Mutation checks, with failure counts pre-registered.** For each row:
  1. apply the mutation;
  2. run `py -m pytest <file> -q -rs`, and record passed/failed/**skipped**;
  3. restore the source.

  **A DB-file run with skipped > 0 is UNKNOWN, not a count.** A count that differs from the
  prediction is reported, not reconciled.

| # | Mutation | Run | Predicted failures |
|---|---|---|---|
| M1 | `confirms`: drop the different-outlet condition | `test_census_metrics.py` | 1 (`test_confirms_needs_two_outlets_in_one_event`) |
| M2 | `session_valid`: drop `kind = 'session'` | `test_census_access.py` | 1 (`test_link_token_is_not_a_session`) |
| M2b | `session_valid`: drop the expiry and revocation conditions | `test_census_access.py` | 2 (`test_revoked_sessions_are_invalid`, `test_session_expires_after_thirty_days`) |
| M3 | `save_assignments`: drop `ON CONFLICT ... DO NOTHING` (plain insert, unique index dropped too) | `test_census_state.py` | 1 (`test_retried_write_is_ignored`) |
| M3b | `latest_assignments`: order by `client_seq DESC, id DESC` instead of `id DESC` | `test_census_state.py` | 2 (`test_later_arrival_wins_across_tabs`, `test_window_assignments_uses_the_same_order`) |
| M4 | `draw_precision_sample`: sample pairs uniformly across all groups | `test_census_metrics.py` | 2 (`..._one_pair_per_group_without_replacement`, `..._equal_probability`) |
| M5 | `mde`: `1.96 + 0.84` in place of the t quantiles | `test_census_metrics.py` | 2 (`test_mde_reproduces_the_spec_table`, `test_mde_uses_t_for_the_window_count`) |
| M6 | `go_no_go`: `>= 8` → `> 8` | `test_census_metrics.py` | 1 (`test_go_no_go_thresholds`) |
| M7 | `compute_block`: `< MIN_BLOCK_DAYS` → `< MIN_BLOCK_DAYS − 1` | `test_census_rules.py` | 1 (`test_block_on_2026_09_30_refuses_naming_2026_10_01`) |
| M8 | row trigger `<` → `<=` on `block_end` | `test_census_schema.py` | 1 (`test_item_at_block_end_is_not_held`) |
| M9 | drop the `BEFORE TRUNCATE` trigger from the up script | `test_census_schema.py` | 1 (`test_truncate_items_is_refused_while_census_exists`); the down script uses `IF EXISTS`, so it does not add a second |
| M10 | the row trigger `RETURN OLD` → `RETURN NULL` | `test_census_schema.py` | 4 (`test_cascade_control_without_a_census`, `test_post_block_item_delete_succeeds`, `test_item_at_block_end_is_not_held`, `test_releasing_the_block_lifts_the_hold_except_pinned_items`; ruling R4) |

- [ ] **Step 4: The full gate, in the foreground**

```bash
ruff check . ; echo REAL_EXIT=$?
ruff format --check . ; echo REAL_EXIT=$?
py -m pytest -q -rs > .pytest-census.log 2>&1 ; echo REAL_EXIT=$? >> .pytest-census.log
```

Expected:
- three `REAL_EXIT=0`;
- `grep -c "SKIPPED.*census\|SKIPPED.*labeller" .pytest-census.log` gives `0`;
- the passed count rose by the number of tests added in Tasks 1–9.

- [ ] **Step 5:** Close the Task 1–10 beads. Leave the epic open until the census is labelled.
- [ ] **Step 6: Commit** `docs/2026-10-01-host-runbook-census.md
  docs/superpowers/reviews/2026-09-28-event-census-mutations.md` — `docs(census): host runbook
  and mutation record`

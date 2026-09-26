# In-Request NEW Links for Real-Time Integration (`news-brief-6kr`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revision 2 (2026-09-26).** This revision has been through two red-teams:
> - `docs/superpowers/reviews/2026-09-26-in-request-new-links-redteam.md`, whose items are all
>   fixed, as confirmed by the second review's status table;
> - `docs/superpowers/reviews/2026-09-26-in-request-new-links-rev1-redteam.md`, whose three
>   objections and D1–D12 are addressed here. Its section references are cited inline as `[R2:…]`.
>
> The spec is Amendment A plus its revision 1. Where this plan tightens revision 1 (for example,
> the entity-less fall-through lands with the version bump), the reason is cited.

**Goal:** Let items in one real-time integration request link to each other's new events, so
same-hour reports from two outlets stop being mutually blind at phase-1 density. First make
version bumps inert, so the prompt change cannot re-integrate the KB.

**Architecture:** Everything that changes what gets WRITTEN under a model's normal output lands
in Task 5, together with the prompt and `INTEGRATE_PROMPT_VERSION = 3`. Rows written by the new
behaviour are therefore stamped v3, and the runbook can filter on that.
1. Version bumps are inert (§5.6).
2. A neighbour-fault budget (migration 0016, a knob, a tally field, keys).
3. The writer resolves `NEWn` references.
4. The parser validates labels in response order, with per-row isolation, and `run()` defers
   neighbour faults.
5. Switch it on: the schema, the prompt, v3, the entity-less-reference fall-through, and a
   cold-start test end to end.
6. The runbook measurement, and close-out.

**Tech Stack:** Python 3, psycopg 3, Postgres 18, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`: Amendment A
and its revision 1, plus §5.4 and §5.6. The evidence is `docs/2026-09-26-clustering-recall-spike-result.md`.

## Global Constraints

- **The pre-push gate is three commands, with a database exported**, and the DB tests must
  report runs, not skips: `ruff check .` · `ruff format --check .` · `pytest -q`. Start Postgres
  per CLAUDE.md (`--tmpfs /var/lib/postgresql`, `MSYS_NO_PATHCONV=1`).
- **Run Python as `py`.** Read knobs as `common.X`. **A new knob is a `common.KNOBS` entry plus a
  `docker-compose.yml` anchor line** (see `COMPREHEND_MAX_DEFERS`: `common.py:289`,
  `docker-compose.yml:134`).
- **Commit straight to `main`, with explicit paths.** No host actions. Comprehension stays off in
  production.
- **Before Task 5, no commit changes what is written for output the model currently produces.**
  Tasks 3–4 add paths that fire only on `NEWn` labels, which prompt v2 never asks for. The one
  change that is live early is Task 4's per-row isolation, which only narrows a whole-batch
  failure to a single item. That is deliberate.
- **`NEW` labels are recognised only when they are strings matching `^NEW[1-9][0-9]*$`**,
  checked BEFORE `_resolve_label`. Any other `candidate` value keeps today's meaning through
  `_resolve_label`.
- **No single row can fail the batch** [R2: Objection 3]. `_validate_item` runs per row inside
  `try/except (TypeError, AttributeError)`. A row that raises is dropped under
  `validate:malformed`: it is the item's own fault, so it is charged, and its `str` labels count
  as orphaned.
- **A neighbour's fault spends `integrate_link_defers`,** charging first and then deferring,
  against `common.COMPREHEND_MAX_LINK_DEFERS`. It is counted in `Tally.deferred_neighbour`, and
  never in `failed_integration` or `deferred_response` below the ceiling.
- **Each failure key is noted exactly once.** The parser notes `validate:*` only for the item's
  own faults. A neighbour fault is noted once, by the helper, as `defer:*`. That keeps
  `comprehend.py:870-873`'s arithmetic true (`dropped` minus the `validate:*` keys = items never
  returned).
- **Mutation checks report a COUNT, pre-registered, over whole modules WITH the DB exported,**
  never through `-k`. Where a count cannot be derived in advance, say so and pre-register a range
  or a lower bound.
- **Out of scope:** triage, capture, the budget, output trimming (§5.5), batching, re-extraction
  (Task 1 files it as a bead).

## Review Focus

1. **The referencer comes back with `entities: []`.** Expected: its assertion is written, it is
   counted in `events_linked_in_request`, and it is not swallowed. *(Task 5)*
2. **A declarer is dropped, malformed, or rolls back, and a later item references it.**
   Expected: the referencer has `integrate_attempts` unchanged and `integrate_link_defers + 1`,
   `failed_integration` counts only the declarer, and `deferred_response == 0`. *(Tasks 3–4)*
3. **A malformed field anywhere in one row:** `standing: []`, `type: []`,
   `commitment_state: {}`, `events: 5`, `candidate: None/3/True`, or a dropped row with
   `new_label: []`. Expected: only that row is affected, and its neighbours survive.
   *(Task 4, `test_one_malformed_row_never_fails_the_batch`)*
4. **`INTEGRATE_PROMPT_VERSION` is bumped over completed items.** Expected: nothing is
   re-selected, re-written or newly at risk. *(Task 1)*
5. **The model emits rows in an order other than their listing, or references a `NEWn` it never
   declared while still describing the event.** Expected: an event that carries its own `summary`
   and `type` falls back to a counted new event (`new_label_fallback`). It is not charged. A bare
   reference with no description is `undeclared` and charged. *(Task 4)* [R2: D6, D11]

---

### Task 1: Version bumps are inert (§5.6), and the docs that said otherwise are corrected

**Files:**
- Modify: `comprehend.py`:
  - `pending_integration` (`:1094`);
  - `retirement()`'s at-risk query (`:946-955`);
  - `write_extraction`'s `already` guard and the comment above it (`:2099-2118`).
- Modify: `tests/test_comprehend_integration.py`: delete `test_a_bumped_integration_prompt_version_re_integrates` (`:565`), and append three tests.
- Modify (live memory, then `bash scripts/flush-memory.sh`): `~/.claude/projects/G--pythonDev-news-brief/memory/newsbrief-comprehension-pipeline.md`. Run `grep -n INTEGRATE_PROMPT_VERSION` on it first.

- [ ] **Step 1: Write three failing tests,** each targeting one site:
  - `test_pending_integration_does_not_reselect_after_a_version_bump`:
    - integrate `_fresh(iid)` via `write_extraction`, then commit;
    - `monkeypatch.setattr(comprehend, "INTEGRATE_PROMPT_VERSION", v + 1)`;
    - assert `iid not in {it["id"] for it in comprehend.pending_integration(kb, 300)}`.
  - `test_write_extraction_skips_a_completed_item_after_a_version_bump`: the same setup. After
    the bump, it returns `True`, the `events` count is still 1, and the stored version is still `v`.
  - `test_an_item_completed_under_an_old_version_is_not_at_risk`:
    - `_strikes(kb, _item(kb, h="OLD"), 2, integrated=True)`;
    - bump the version;
    - assert `comprehend.retirement(kb) is None`.
- [ ] **Step 2: Run** `py -m pytest tests/test_comprehend_integration.py -q`. Expected: the three FAIL.
- [ ] **Step 3: Implement.**
  - Drop the `OR ... integrate_prompt_version < %s` clause, and its parameter, from
    `pending_integration` and from `retirement()`.
  - `already` becomes `... AND integrated_at IS NOT NULL`.
  - Rewrite the comment above it: re-extraction is an explicit operation, never a deploy side
    effect (Amendment A, `3wb`).
- [ ] **Step 4: Run** `py -m pytest tests/test_comprehend_integration.py tests/test_comprehend_triage.py tests/test_comprehend_labels.py tests/test_comprehend_budget.py -q`. Expected: PASS.
- [ ] **Step 5: Mutation check. Pre-register 1 each:**
  - restore the clause in `pending_integration`;
  - restore it in `retirement()`;
  - restore the version term in `already`.

  Record the counts, and revert.
- [ ] **Step 6: Correct the sibling claims.**
  - Rewrite each "do not bump" passage in the live memory file: a bump is inert, and
    re-extraction has no mechanism yet. Then flush. Leave migration `0009`'s comment alone: it is
    history.
  - `bd create --title="Re-extraction has no mechanism since version bumps became inert" --type=task --priority=3 --description="Amendment A (6kr) made INTEGRATE_PROMPT_VERSION provenance-only. Re-extracting items (ymk) needs an explicit operation that also supersedes old events (3wb)."`
  - Use **`--append-notes`** (never `--notes`, which overwrites) on `news-brief-3wb` and
    `news-brief-ymk`, pointing at the new bead.
- [ ] **Step 7: Commit** `comprehend.py`, `tests/test_comprehend_integration.py`,
  `.claude/memory/newsbrief-comprehension-pipeline.md` and `.beads/issues.jsonl` (after
  `bd export -o .beads/issues.jsonl`), with
  `fix(comprehend): a prompt-version bump never re-integrates a completed item`.

---

### Task 2: A neighbour-fault deferral budget

**Files:**
- Create: `migrations/0016_integrate_link_defers_up.sql`, `migrations/0016_integrate_link_defers_down.sql`
- Modify: `tests/test_db.py:41-57` (the expected list), `tests/test_comprehension_schema.py` (a round-trip test, like `test_0009_rolls_back_and_reapplies` at `:176`)
- Modify: `common.py` (`KNOBS`, next to `COMPREHEND_MAX_DEFERS` at `:289`), `docker-compose.yml` (next to `:134`)
- Modify: `comprehend.py`: `Tally` (`:42`), the response-failure branch of `run()` (`:826-852`)
- Modify: `tests/test_comprehend_triage.py` (append)

**Interfaces:**
- Produces:
  - the column `item_triage.integrate_link_defers INTEGER NOT NULL DEFAULT 0`;
  - the knob `COMPREHEND_MAX_LINK_DEFERS: Knob(int, 10)`;
  - the Tally fields `deferred_neighbour`, `events_linked_in_request`,
    `entityless_reference_written` and `new_label_fallback`, all `int = 0`;
  - `_defer_or_charge(conn, ids: list[int], tally: Tally, *, column: str, ceiling: int, deferred_field: str, deferred_key: str, capped_key: str) -> None`.

    It charges first (`integrate_attempts + 1` where `column >= ceiling`: `failed_integration`,
    `defer_cap_hit`, `capped_key`), then defers (`column + 1` where `column < ceiling`:
    `deferred_field`, `deferred_key`). It asserts
    `column in {"integrate_defers", "integrate_link_defers"}` before interpolating.

- [ ] **Step 1: Migration.**
  - The pair, with a header comment in 0013's style. The comment says why this is not
    `integrate_defers`: a neighbour's fault is not a response fault, and one budget for two
    no-verdict causes lets one exhaust the other.
  - Add `"0016_integrate_link_defers"` to `test_up_creates_the_expected_tables`.
  - Add `test_0016_rolls_back_and_reapplies`, copying 0009's pattern: derive the step count, and
    assert the column is gone and then back [R2: D10].
- [ ] **Step 2: Knob.** Add the KNOBS entry and the compose anchor line
  `- COMPREHEND_MAX_LINK_DEFERS=${COMPREHEND_MAX_LINK_DEFERS:-}`. Add
  `test_the_link_defer_ceiling_is_a_settings_knob_not_a_constant`, copying `:1610` [R2: D4].
- [ ] **Step 3: Refactor the response-failure branch onto `_defer_or_charge`**, with
  `column="integrate_defers"`, `ceiling=int(common.COMPREHEND_MAX_DEFERS)`,
  `deferred_field="deferred_response"`, `deferred_key=f"batch:{name}"` and
  `capped_key=f"batch_capped:{name}"`. These are exactly today's keys (`:1562` asserts
  `batch_capped:ValueError`).
- [ ] **Step 4: Run** the triage module, `tests/test_db.py` and `tests/test_comprehension_schema.py`,
  plus the compose-anchor parity test (`grep -rln "anchor" tests/` to find it). Expected: PASS.
- [ ] **Step 5: Write `test_the_link_budget_charges_first_then_defers`.** Call the helper on
  `integrate_link_defers` with two items, one at `ceiling - 1` and one at `ceiling`. Assert:
  - the end states are `(attempts 0, link_defers = ceiling)` and
    `(attempts 1, link_defers = ceiling)`;
  - `integrate_defers` is untouched;
  - `deferred_neighbour == 1`, `deferred_response == 0`, `failed_integration == 1`.
- [ ] **Step 6: Mutation check.** Swap the helper's two UPDATEs. Pre-register **2**:
  `test_the_defer_budget_is_BOUNDED...` (`:1527`, the only existing test that discriminates
  order, per both reviews' traces) and Step 5's test. Record the count, and revert.
- [ ] **Step 7: Commit** the migration pair, `common.py`, `docker-compose.yml`, `comprehend.py`
  and the three test files, with
  `feat(comprehend): a separate deferral budget for faults that are a neighbour's`.

---

### Task 3: The writer resolves `NEW` references

**Files:**
- Modify: `comprehend.py`: next to `NoEntitySurvived` (`:135`), `write_extraction` (`:2079`), `write_batch`
- Modify: `tests/test_comprehend_integration.py`: `_item` (`:305`) and append

**Interfaces:**
- Consumes: Task 2's helper and fields.
- Produces:
  - the inputs `{"new_ref": "NEWn", "standing": s}` and any event with `"new_label": "NEWn"`;
  - `class UnresolvedNewLabel(ValueError)`, for direct callers;
  - `write_extraction(conn, extraction, index, tally, *, model=None, new_events: dict[str, int] | None = None) -> bool`, where `None` means `{}`;
  - `write_batch` keeps its signature and owns one request-scoped `new_events` dict.

- [ ] **Step 1: Give `_item` an `outlet="Reuters"` parameter**, and change its conflict
  fallback to `SELECT id FROM outlets WHERE name = %s` [R2: D7]. Existing callers are unchanged.
- [ ] **Step 2: Write the failing tests** (two outlets wherever a link is asserted):
  - `test_a_new_label_links_two_items_to_one_event`:
    - item a is `_fresh(a)` with `new_label: "NEW1"`;
    - item b has one entity and `events: [{"new_ref": "NEW1", "standing": "reported"}]`;
    - assert `events` 1, assertions from 2 distinct outlets, `events_linked_in_request == 1`,
      and `events_matched == 1` (a link counts as a match, so
      `scripts/score_comprehension.py:497-506`'s `matched + created = A` stays exact) [R2: D9].
  - `test_a_label_on_a_matched_event_links_to_that_event`: item a is
    `{"candidate_id": ev, "standing": "reported", "new_label": "NEW1"}`, and item b references
    `NEW1`. Both assertions are on `ev`, and `events` is unchanged.
  - `test_a_rolled_back_declaration_is_never_resolved`:
    - item a's declaring event has `"standing": "not_a_standing"`, so the CHECK fails at the
      assertion INSERT, after the event INSERT;
    - item b references `NEW1`;
    - assert `events` 0, `assertions` 0;
    - assert b has `integrate_attempts == 0` and `integrate_link_defers == 1`;
    - assert `failed_integration == 1` (a only), `deferred_neighbour == 1`,
      `failures["defer:new_label_unresolved"] == 1`, and `events_linked_in_request == 0`.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** The order inside `write_extraction` is fixed, as follows [R2: D12]:
  1. the `already` guard;
  2. **reference resolution:** if any `new_ref` is absent from `new_events`, call
     `_defer_or_charge(conn, [item_id], tally, column="integrate_link_defers", ceiling=int(common.COMPREHEND_MAX_LINK_DEFERS), deferred_field="deferred_neighbour", deferred_key="defer:new_label_unresolved", capped_key="defer_capped:new_label_unresolved")`
     and return `False`. The whole item waits a pass: it is re-offered, and a partial write
     would be a half-extraction;
  3. the empty and entity-less early returns, **unchanged in this task**;
  4. the savepoint.

  In the events loop:
  - a `new_ref` takes `event_id = new_events[ref]`, and counts toward `events_matched`;
  - after either branch, `if ev.get("new_label"): declared_here[label] = event_id`;
  - **after the `with conn.transaction():` block exits normally, and only then:**
    `new_events.update(declared_here)` and
    `tally.events_linked_in_request += <this item's new_ref count>` [R2: D8].
- [ ] **Step 5: Run** the four comprehend modules. Expected: PASS.
- [ ] **Step 6: Mutation check. Pre-register:**
  - record `declared_here[label]` straight into `new_events` **at the event INSERT** → **1**
    (rolled-back). Placement named: at the end of the `with` body it would not be reached, and
    would read 0 [R2: D3];
  - drop the `new_label` recording on the matched branch → **1**;
  - charge instead of deferring an unresolved reference → **1**;
  - increment `events_linked_in_request` inside the savepoint → **1** (rolled-back).

  Record the counts, and revert.
- [ ] **Step 7: Commit** `comprehend.py` and `tests/test_comprehend_integration.py` with
  `feat(comprehend): the writer resolves in-request NEW references`.

---

### Task 4: The parser validates labels in order, rows are isolated, and `run()` defers neighbour faults

**Files:**
- Modify: `comprehend.py`: `parse_integration_response` (`:1827`), `_validate_item` (`:1939`), `run()`'s dropped-charging (`:863-880`)
- Modify: `scripts/inspect_integration.py` (`:91`, `:150`)
- Modify: `tests/test_comprehend_labels.py`, `tests/test_comprehend_triage.py` (append)

**Interfaces:**
- Produces:
  - `parse_integration_response(resp, offered_item_ids, entity_labels, event_labels, tally=None, *, neighbour_faults: set[int] | None = None) -> list[dict]`;
  - `_validate_item(row, item_ids, entity_labels, event_labels, tally=None, *, declared=frozenset(), orphaned=frozenset(), neighbour_faults=None)`;
  - the item-own keys `validate:new_label_shape`, `…_duplicate`, `…_self`, `…_undeclared` and
    `validate:malformed`;
  - the counter `Tally.new_label_fallback`.

- [ ] **Step 1: Write the failing tests** in `tests/test_comprehend_labels.py` (its `_extraction`, `NEW_ENTITY`, `NEW_EVENT`):
  - `test_a_later_item_may_reference_an_earlier_declaration`: Task 3's input shapes, exactly.
  - `test_a_label_on_a_matched_event_is_declared`, with `event_labels={"EVT1": 20}`.
  - `test_bad_new_labels_drop_the_item_with_a_named_cause`, parametrised. Each case uses a bare
    reference (`{"candidate": "NEW1", "standing": "reported"}`, with no summary or type):
    - no declaration anywhere → `undeclared`;
    - a forward reference → `undeclared`;
    - declared twice across items → `duplicate`;
    - declared twice within one item → `duplicate`;
    - a self-reference → `self`;
    - `new_label: "EVT9"` → `shape`;
    - `new_label: 7` → `shape`.

    Each asserts its key `== 1`.
  - `test_an_undeclared_reference_that_describes_its_event_falls_back_to_new`:
    `{"candidate": "NEW1", "summary": "S", "type": "action", "standing": "reported"}` with no
    declaration. The item survives as a new event, `tally.new_label_fallback == 1`, and no
    `validate:new_label_*` key is set [R2: D6, D11].
  - `test_a_reference_to_a_dropped_declarer_is_the_neighbours_fault`: the declarer's entity has
    `type: "not_a_type"`. Assert `out == []`, `nf == {2}`, and no `validate:new_label_*` key.
  - `test_one_malformed_row_never_fails_the_batch`, parametrised over the malformed row:
    - `standing: []`;
    - `type: []` on a new event;
    - `commitment_state: {}`;
    - `events: 5`;
    - `candidate` ∈ {`None`, `3`, `True`} on a new event;
    - a row with `new_label: []`.

    Each sits beside one valid sibling row. Assert that nothing raises and the sibling survives.
    For the `candidate` cases, assert the malformed row ALSO survives, as a new event, because
    `_resolve_label` absorbs those today. For the others, assert
    `failures["validate:malformed"] == 1`, or the specific existing key where one fires first
    [R2: Objection 3].
  - **In `tests/test_comprehend_triage.py`, copy the `fake_integrate` shape of `:546`.** The
    prompt lists `ORDER BY i.id DESC`, so the declarer must be the item created **second**:
    - `test_a_referencer_whose_declarer_was_dropped_is_deferred_not_charged`: the declarer has
      `integrate_attempts == 1`. The referencer has `integrate_attempts == 0` and
      `integrate_link_defers == 1`. Also `failed_integration == 1`, `deferred_neighbour == 1`,
      `deferred_response == 0`, and `failures["defer:new_label_orphaned"] == 1`.
    - `test_a_reference_nobody_declared_is_charged` (a bare reference): `integrate_attempts == 1`,
      and `integrate_link_defers == 0`.
- [ ] **Step 2: Run** both modules. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
  - **`parse_integration_response`:** an ordered loop.
    - Each row's `_validate_item` sits in `try/except (TypeError, AttributeError)`. A raise
      notes `validate:malformed` and drops the row.
    - `declared` holds labels from validated rows.
    - `orphaned` holds `str` `new_label`s taken from the `dict` event members of any dropped
      row (a raise or a `None`), with `isinstance` guards so the collection itself cannot raise.
  - **`_validate_item`:** compute `own` once, and track `seen_here`. For each event, **before
    `_resolve_label`**: when `isinstance(c, str) and _NEW_LABEL.match(c)`, check in this order:
    1. `self`;
    2. in `declared` → `new_ref`;
    3. in `orphaned` → add to `neighbour_faults` and return None, noting nothing;
    4. the event has a `str` summary and a valid `type` → treat it as a NEW event (the existing
       path) and increment `new_label_fallback`;
    5. otherwise, `undeclared`.

    For a `new_label` on either branch: not a `str`, or no pattern match → `shape`; in
    `declared` or `seen_here` → `duplicate`; otherwise copy it into the output.
  - **In `run()`:** `nf: set[int] = set()` per batch, passed as `neighbour_faults`. `dropped`
    excludes `nf`, and `nf` goes through
    `_defer_or_charge(..., column="integrate_link_defers", ceiling=int(common.COMPREHEND_MAX_LINK_DEFERS), deferred_field="deferred_neighbour", deferred_key="defer:new_label_orphaned", capped_key="defer_capped:new_label_orphaned")`.
  - **`scripts/inspect_integration.py`:**
    - replace the per-row `_validate_item` loop (`:150`) with one `parse_integration_response`
      call, then print each offered item's survival and `tally.failures`;
    - fix `:91` to pass the batch's titles as `titles` and a `Tally(enabled=True)` as `tally`,
      reading the surrounding code for the real variable names first.
- [ ] **Step 4: Run** the four comprehend modules. Expected: PASS. Then run
  `py -c "import scripts.inspect_integration"` and report that it imports. Do not claim it ran.
- [ ] **Step 5: Mutation check,** with the DB exported, over whole modules. Pre-register:
  - drop the `isinstance(c, str)` guard → **at least 17**. Every compliant new event has
    `candidate: None` (`:1590-1593`, `:1977`), so this breaks the three non-string cases plus
    the 14 labels-module tests the second review simulated, plus DB-backed parse and `run()`
    tests whose number is not derivable in advance. Report the exact count and name the modules
    [R2: D1];
  - declare from dropped rows too → **2**: the labels orphaned test and the triage orphaned test
    (a `new_ref` then fails to resolve under `defer:new_label_unresolved`) [R2: D2];
  - delete the within-item duplicate check → **1**;
  - remove the per-row `try/except` → the malformed-row cases that raise, **3 or 4**. Read which
    shapes an existing guard already absorbs, and pin the exact number before running;
  - leave `nf` inside `dropped` → **1**;
  - remove the fallback branch → **1**.

  Record the counts, and revert.
- [ ] **Step 6: Commit** `comprehend.py`, `scripts/inspect_integration.py`,
  `tests/test_comprehend_labels.py` and `tests/test_comprehend_triage.py`, with
  `feat(comprehend): validate in-request NEW labels in order, isolate malformed rows`.

---

### Task 5: Switch it on: schema, prompt, v3, the entity-less-reference fall-through, cold start

The fall-through lands HERE rather than in Task 3, so every row it writes is stamped v3 and gets
a counter [R2: Objection 2].

**Files:**
- Modify: `comprehend.py`:
  - `INTEGRATE_PROMPT_VERSION` (`:34`);
  - `_INTEGRATE_SYSTEM` (`:1571-1593`);
  - `_INTEGRATE_TOOL`'s event item (`:1661-1700`);
  - `write_extraction`'s entity-less early return (`:2161`) and the `NoEntitySurvived` raise.
- Modify: `tests/test_comprehend_integration.py`, `tests/test_comprehend_labels.py`, `tests/test_comprehend_triage.py` (append)

- [ ] **Step 1: Write the failing tests.**
  - `test_an_entityless_referencer_is_written`: Task 3's link test, with item b at `entities: []`.
    Assert the link is written, `events_linked_in_request == 1`,
    `entityless_reference_written == 1` and `entityless_extraction == 0`.
  - `test_an_entityless_EVT_match_is_written`: a pre-existing `ev`, and an extraction of
    `entities: []` plus `events: [{"candidate_id": ev, "standing": "reported"}]`. Assert one
    assertion on `ev`, `entityless_reference_written == 1`, and the assertion's
    `prompt_version == 3`.
  - `test_an_entityless_extraction_with_a_new_event_is_still_terminal`: no event is created, and
    `entityless_extraction == 1`.
  - `test_the_event_schema_offers_new_label`, and
    `test_the_prompt_lets_later_items_reference_new_labels`: assert that `"NEW1"`,
    `"still list"` and `"earlier in your response"` (case-insensitive) are all in
    `_INTEGRATE_SYSTEM`.
  - `test_a_cold_start_pass_links_two_outlets_in_one_request` (the triage module, through
    `comprehend.run`):
    - an empty KB, and two tracked-story items from two outlets;
    - the fake returns raw JSON: the declarer (created second) with a NEW event and
      `new_label: "NEW1"`, and the referencer with `entities: []` and `candidate: "NEW1"`;
    - assert `events_linked_in_request == 1`;
    - assert `scripts.score_comprehension.corroboration_by_outlet(kb)` reports that event with
      2 outlets. Read its return shape first; never re-derive its SQL.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `INTEGRATE_PROMPT_VERSION = 3`.
  - **The entity-less early return** stays terminal only if some event is NEW (no
    `candidate_id` and no `new_ref`). Otherwise, fall through and increment
    `entityless_reference_written` after the savepoint commits.
  - **Inside the savepoint,** `if not entity_ids: raise NoEntitySurvived` applies only when
    `extraction["entities"]` was non-empty.
  - **Schema:** the `candidate` description becomes *"A label from CANDIDATE EVENTS (e.g.
    'EVT1'), or a NEW label that an item EARLIER IN YOUR RESPONSE declared (e.g. 'NEW1'). Omit
    it for an event you are the first to report."* Add
    `"new_label": {"type": "string", "description": "Only when an item LATER in your response
    reports the same event: a label NEW1, NEW2, ... that it sets as its `candidate`."}`.
  - **`_INTEGRATE_SYSTEM`, CANDIDATE LABELS paragraph** [R2: D5]: "Set `candidate` to one of
    those labels ONLY to refer to that exact listed item. OMIT `candidate` entirely for anything
    new -- never invent or number a label yourself" becomes "Set `candidate` to one of those
    labels ONLY to refer to that exact listed item, or to a NEW label as described below. OMIT
    `candidate` for an event you are the first to report -- never invent an ENT or EVT label."
    Then append this paragraph verbatim:
    ```
    IN-REQUEST LINKS. When two items in this request report the SAME event, set `new_label` \
    (NEW1, NEW2, ...) on that event in the item that comes FIRST in your response, and in each \
    item LATER in your response set `candidate` to that NEW label instead of describing the \
    event again. Each item must still list its own actors under `entities`. Reference only a \
    NEW label declared earlier in your response, never one declared in the same item.
    ```
- [ ] **Step 4: Run** the four comprehend modules. Expected: PASS.
- [ ] **Step 5: Mutation check. Pre-register:**
  - restore the unconditional entity-less return → **3** (both entity-less write tests and the
    cold-start test);
  - revert the schema property → **1**;
  - delete the appended paragraph → **1**.

  Record the counts, and revert.
- [ ] **Step 6: Commit** `comprehend.py` and the three test files, with
  `feat(comprehend): ask for in-request NEW links (integration prompt v3)`.

---

### Task 6: Runbook measurement, full gate, beads

**Files:**
- Modify: `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md`, Step 5 (`:143-220`) and Step 7 (`:261`)

- [ ] **Step 1: Runbook Step 5, first days.**
  - Record `events_linked_in_request`, `entityless_reference_written`, `new_label_fallback`,
    `deferred_neighbour`, and every `defer_capped:*` / `validate:new_label_*` /
    `validate:malformed` key.
  - **Link failure:** a steady `events_linked_in_request == 0` alongside `new_label_fallback`
    or `…_undeclared` counts that rise means the model is mis-using labels.
  - **Recurring neighbour faults:** any `defer_capped:*` key means neighbour faults keep recurring
    for the same item.
- [ ] **Step 2: Runbook Step 7, after 7 days: three terms, not a ratio** [R2: Objection 1].
  Assertions from one micro-batch share `created_at` (`now()` is transaction time). The query
  counts co-batched pairs of v3 items from different outlets, split into linked (sharing an
  event) and not linked:
  ```sql
  WITH batch AS (   -- one row per (micro-batch, item, outlet), v3 only
    SELECT DISTINCT a.created_at AS txn, a.item_id, i.outlet_id
    FROM assertions a JOIN items i ON i.id = a.item_id
    WHERE a.prompt_version >= 3),
  pairs AS (        -- cross-outlet item pairs written in the same micro-batch
    SELECT x.item_id AS a_item, y.item_id AS b_item
    FROM batch x JOIN batch y
      ON y.txn = x.txn AND y.item_id > x.item_id AND y.outlet_id <> x.outlet_id)
  SELECT count(*) AS co_batched_pairs,
         count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM assertions p JOIN assertions q ON q.event_id = p.event_id
           WHERE p.item_id = a_item AND q.item_id = b_item)) AS linked_pairs
  FROM pairs;
  ```
  Also record the multi-outlet events with `events.created_at` after the flip and
  `events.prompt_version >= 3`. Entity-less references cannot be told apart in the tables:
  `event_entities` is keyed by event, not by item. Sum `entityless_reference_written` across the
  week's `Comprehend:` log lines instead.
  - **Reading it.** `co_batched_pairs` is every cross-outlet pair that could have linked, since
    most such pairs are different events. `linked_pairs` is those that did. The pre-registered
    expectation is derived from the phase-1 density measured for `news-brief-4le`, not assumed.
  - Look up RT's blind share at that density in the M1 table (`docs/2026-09-26-clustering-recall-spike-result.md`,
    M1 result). Expect roughly that share of true same-event pairs to be co-batched, and expect
    `linked_pairs` to be a clear majority of the co-batched pairs whose titles are similar.
    Record `similarity(title, title) >= 0.5` alongside as a proxy for same-event.
  - `linked_pairs == 0` with high-similarity co-batched pairs present means the links are not
    happening. Report it.
- [ ] **Step 3: The full gate**, DB exported: ruff clean, format clean, `pytest -q` passed with 0
  skipped in the comprehend modules.
- [ ] **Step 4: Beads.**
  - **Close `news-brief-6kr`,** with a reason naming the observation still owed: the Step 7
    terms after the restart.
  - **File the probe gap:** `inspect_integration.py` was broken with nothing noticing, so it
    wants a smoke test.
  - **Export:** `bd export -o .beads/issues.jsonl`.
- [ ] **Step 5: Commit** the runbook and `.beads/issues.jsonl` with
  `docs(runbook): measure in-request links after the phase-1 restart`.

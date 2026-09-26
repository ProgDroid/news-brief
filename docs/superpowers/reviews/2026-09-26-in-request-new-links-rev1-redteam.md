# Red-team, revision 1: in-request NEW links (`news-brief-6kr`)

**Plan:** `docs/superpowers/plans/2026-09-26-in-request-new-links.md` (revision 1)
**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`: Amendment A, and Amendment A revision 1 (which governs), §5.4, §5.6
**First red-team:** `docs/superpowers/reviews/2026-09-26-in-request-new-links-redteam.md`
**Evidence:** `docs/2026-09-26-clustering-recall-spike-result.md`
**Reviewed against:** `comprehend.py` (working tree at `7d43f94`), `scripts/score_comprehension.py`,
`scripts/probe_corroboration.py`, `scripts/inspect_integration.py`, `db.py`, `migrations/0006`, `0013`,
`tests/test_db.py`, `tests/test_comprehend_{integration,labels,triage}.py`, and the phase-1 host runbook.

**Method.** I read the code and ran three local probes. Nothing in the repo was modified except this file.
1. Four malformed rows through the real `parse_integration_response`.
2. `re.match` on `None`.
3. A simulation of Task 4's first mutation. It is a pytest plugin in the session scratchpad that wraps
   `_validate_item`, run against the pure labels module, with a guarded twin as the control.

DB-backed tests skip here: no `DATABASE_URL` was exported (41 passed, 123 skipped). A skip is not
evidence, so nothing below leans on a DB test passing. Each claim is labelled **VERIFIED** (read or
run, with a citation) or **INFERRED**.

---

## Objection 1 (a): Task 6's success measurement cannot tell the feature working from the feature failing. So revision 1 item 5 ("success is measured, not merely alive") is not met.

Revision 1 item 5 asks for two things: the number of multi-outlet events that carry an in-request
link, and an M1-derived expectation registered before the run. Task 6 Step 2 gives both. Neither can
discriminate.

**1. The SQL does not compute the population its comment names (VERIFIED, plan lines 443-454).**
- The comment says "multi-outlet events created since the flip".
- The query filters **assertions** on `a.created_at >= '<flip time>'` and groups them by `event_id`. It
  never reads `events.created_at`.
- An event created before the flip, with two post-flip assertions, is counted.
- An event with one pre-flip outlet and one post-flip outlet has `outlets = 1`, so it drops out of the
  denominator.
- `score_comprehension.corroboration_by_outlet` scopes by `e.created_at` (`scripts/score_comprehension.py:181-186`).
  The runbook number is therefore not comparable with the gate's.

**2. It keys on the wrong instant (VERIFIED).**
- The runbook defines `<flip>` as the `COMPREHEND_ENABLED` flip
  (`docs/2026-09-25-host-runbook-cost-redesign-phase-1.md:263-268`).
- Amendment A says 6kr lands "at or soon after the phase-1 restart" (spec line 505-506). Any prompt-v2
  pass between the flip and the 6kr deploy cannot link, but it still feeds the denominator.
- The discriminating column already exists. `assertions.prompt_version`
  (`migrations/0006_knowledge_base_up.sql:124`) is stamped on every write (`comprehend.py:2228-2237`).
  Task 1 keeps it "as provenance only" for exactly this kind of use. The query should filter
  `a.prompt_version >= 3`.

**3. A link that should have happened and did not is invisible to both terms (VERIFIED from the SQL,
INFERRED consequence).**
- A co-batched pair the model failed to link is written as **two single-outlet events**.
- It appears in neither `multi_outlet` nor `linked_in_request`.
- So the ratio is not "the share of linkable pairs that were linked". Let *c* be the link rate on
  co-batched pairs and *r* the rate at which the matcher merges pairs across micro-batches. With
  M1's 25/75 split, the ratio is `0.25c / (0.25c + 0.75r)`.

That formula makes the band unreadable (INFERRED arithmetic):
- *c* = 1 and *r* = 0.44 (the recall@30 figure in memory) gives **43%**, outside the band and "high".
- *c* = 0.3 and *r* = 0.2 gives **33%**.
- *c* = 0.5 and *r* = 0.44 gives **27%**, "in band".

The runbook says nothing about a reading above 30%. A reading inside the band is compatible with
the model ignoring half the links.

**4. The band comes from a row the evidence document disowns (VERIFIED).**
- "Roughly 25% of ≤ 2 h pairs share a micro-batch" is RT's 74.8% at 5/h (spike result `:144`).
- The same document says: "**The 5 row is the floor of this model.** RT's range across seeds is 0,
  because at 5/h almost only pair items survive thinning" (`:157-158`).
- Phase 1's density is unmeasured. The pre-registered guess for its p90 is 15-30/h (`:114`), where
  RT's blind share is 13.1% (10/h) and 6.5% (20/h), per `:145-146`.
- The band is therefore fixed at a density the plan assumes ("about 5 items/h", plan line 456) rather
  than one it reads. The runbook's own Step 7 measures the material rate that should parameterise it.

**Fix.**
- Filter on `a.prompt_version >= 3` and on `e.created_at`.
- Report the three terms, not their ratio:
  - same-transaction cross-outlet links;
  - cross-transaction multi-outlet events;
  - co-transaction cross-outlet **item pairs** that share an event (linked) versus those that do not
    (not linked). Every assertion of a micro-batch shares `now()`, so `created_at` identifies the
    micro-batch, and co-batched pairs can be enumerated directly.
- Derive the band from the measured phase-1 density (`news-brief-4le`), stated as a function of
  density rather than a constant.

---

## Objection 2 (a, b): the entity-less `EVT`-match change is live from Task 3's commit, has no counter, and is stamped with the old version. The spec says it "is measured", and the plan measures nothing that can see it.

**What the spec requires (VERIFIED, spec line 532-533).** "This also changes the existing behaviour
for entity-less `EVT` matches ... It is included on purpose, **and it is measured.**"

**What the plan does (VERIFIED from plan text).**

It is not dormant:
- Task 3's heading says "(dormant until Task 5)".
- But Task 3 Step 3's entity-less branch change fires on any `candidate_id`-only extraction with
  `entities: []`. Today's parser already produces that shape (`comprehend.py:1977-1980`), and today's
  writer discards it (`:2161-2170`).
- The behaviour changes the moment Task 3 is deployed, under prompt v2.

It has no provenance:
- `INTEGRATE_PROMPT_VERSION` stays 2 until Task 5 (plan line 38-39).
- Rows written by the new behaviour carry the same `prompt_version` as rows written before it.
  Provenance cannot separate them, and this is the column Task 1 just re-scoped to provenance.

It has no counter:
- The only new Tally fields are `deferred_neighbour` and `events_linked_in_request` (plan line 154).
- An entity-less `EVT` match increments `events_matched`, exactly like a normal match (`:2186-2188`).
- Meanwhile `entityless_extraction` silently narrows its meaning: it now counts only extractions that
  contain a NEW event.
- Task 6 Step 1 then reads `entityless_extraction` as one of the "three readings" of a steady zero
  (plan line 435-436). Its pre-6kr history is not comparable with its post-6kr values, and nothing
  says so.

**Why it matters (INFERRED from VERIFIED readers).**
- These matches land directly in the numbers the gate reads: `corroboration_by_outlet`
  (`scripts/score_comprehension.py:192-199`), and `score_match_rate_corroboration`'s `A − E`
  (`:514-519`).
- They also land in `probe_corroboration`'s calibration positives (`scripts/probe_corroboration.py:181-203`),
  which treat every cross-outlet merge as ground truth.
- An entity-less match is the least-evidenced merge the pipeline makes. The model named no actor, so
  there is no entity overlap to cross-check it against.
- If it inflates corroboration, nothing on the host can attribute the inflation. That is the
  `fail-closed-needs-status-not-count` and "an unmeasured field is quarantined" shape this repo has
  already written rules against.

**Fix.**
- Add `Tally.entityless_reference_written`, incremented in the new fall-through, and name it in
  Task 6 Step 1.
- Either land the fall-through in Task 5 alongside the version bump, so `prompt_version = 3` marks
  it, or state in the plan that Task 3 is live and stamps v2.
- Add one line to Task 6 Step 7 that reports how many multi-outlet events have an entity-less
  assertion.

---

## Objection 3 (a, and c): the plan's "never raises out of the parser" guarantee is false after execution. One malformed field on one item still fails the whole micro-batch.

**The requirement.**
- Revision 1 item 3: "A malformed field on one item must never fail the whole batch."
- The plan raises that to a Global Constraint: "**A malformed field on one item never raises out of
  the parser.**" (plan line 41-42).
- Review Focus 3 repeats it.

**What the plan guards (VERIFIED, plan lines 332-345).** Only the label fields: `candidate` via
`isinstance(c, str)`, `new_label` via a str check, and the orphan collection via `isinstance`.

**What still raises (VERIFIED, probe run).** Each row below was placed ahead of a valid sibling in one
response and passed through the real `parse_integration_response`:

| malformed field | result |
|---|---|
| event `standing: ["reported"]` | `TypeError: cannot use 'list' as a set element` (`comprehend.py:1974`) |
| entity `type: ["country"]` | same, at `:1965` |
| event `commitment_state: {}` | same, at `:2004` |
| `events: 5` | `TypeError: 'int' object is not iterable` (`:1973`) |

In each case the valid sibling was lost with it.

**Where that goes (VERIFIED).**
- The exception leaves through `run()`'s `except Exception` (`:794`) into the response-failure branch
  (`:826-853`).
- All five items are deferred. The same batch recurs because of `ORDER BY i.id DESC`. After
  `COMPREHEND_MAX_DEFERS` passes every item is charged, and three strikes later four innocent items
  are retired.
- The plan's own test, `test_non_string_labels_never_fail_the_batch`, covers only label fields
  (plan lines 312-316), so the suite will certify the guarantee while it is false.

**The 6-month form (INFERRED).**
- The first red-team's argument applies unchanged. A model swap through the `NEWSBRIEF_MODEL` row
  changes emission habits with no deploy.
- The model has already been measured emitting explicit nulls and wrong containers (`items` as a
  string, as a dict, and double-wrapped: `comprehend.py:1850-1901`). An array where a scalar enum
  belongs is the same family.

**Fix.** Task 4 already rewrites `parse_integration_response` into an ordered loop, so the fix is cheap:
- wrap each row's `_validate_item` in `try/except (TypeError, AttributeError)`;
- note a bounded key such as `validate:malformed`, and treat the row as dropped (charged, orphaning
  its labels);
- add the four shapes above to the parametrised test.

Otherwise, narrow the Global Constraint to what is actually delivered, and say that the spec's
item 3 is only partly met.

---

## Additional defects

### D1. Task 4 Step 5: the first pre-registered count is off by at least 5x (VERIFIED by simulation).
- "Drop the `isinstance(c, str)` guard → **3**" assumes only the three non-string test cases reach
  the check.
- But `ev.get("candidate")` is `None` for **every compliant new event**, because omitting `candidate`
  is the documented way to say "new" (`comprehend.py:1590-1593`, `:1977`). Also,
  `re.match(pat, None)` raises `TypeError` (probe run).
- Simulated against the pure labels module: **14 existing tests fail**. The guarded twin passes
  41/41, so the plugin was live and the failures come from the mutation.
- The DB-backed parse and `run()` tests in the integration, triage and budget modules would add
  more. They were not run, so their count is UNKNOWN.
- Honest pre-registration: 3 new tests + 14 labels tests + an unknown number of DB tests. Measure it
  with the DB exported.

### D2. Task 4 Step 5: "declare from dropped rows too → 1" should be 2 (VERIFIED by trace of the plan's own tests).
- Under the mutation, the orphaned label lands in `declared`, so the referencer becomes a `new_ref`.
- In the writer it is then unresolved and deferred under `defer:new_label_unresolved`.
- The **triage** test `test_a_referencer_whose_declarer_was_dropped_is_deferred_not_charged` asserts
  `failures["defer:new_label_orphaned"] == 1` (plan line 325). It fails too.
- The plan's "only the orphaned test" counts the labels-module test alone. The revision
  over-corrected the first review's defect 2.

### D3. Task 3 Step 5: "move `new_events.update` inside the savepoint → 1" holds only for one placement (INFERRED).
- The rolled-back test injects its fault at the assertion INSERT (plan line 235-236).
- An update placed at the **end** of the `with` body is never reached when that INSERT raises, so the
  test stays green and the count is 0.
- Only "record at the event INSERT" is caught. Name that placement in the mutation.

### D4. `LINK_DEFER_CEILING` is a module constant "not a knob" (plan line 153), with no reason given (VERIFIED).
- This repo's rule is that configuration is a settings row.
- The sibling ceiling has a test that exists only to pin that rule:
  `test_the_defer_ceiling_is_a_settings_knob_not_a_constant` (`tests/test_comprehend_triage.py:1610`).
- A host that sees `defer_capped:new_label_*` (Task 6 Step 1) cannot retune the ceiling without a
  redeploy.

### D5. The prompt contradicts itself after Task 5 (VERIFIED).
- Task 5 changes one clause and appends a paragraph. It leaves "Set `candidate` to one of those
  labels ONLY to refer to that exact listed item. OMIT `candidate` entirely for anything new"
  (`comprehend.py:1590-1593`) in place.
- The appended text tells later items to set `candidate` to a NEW label. The event a later item
  references is also "new", since it is not in CANDIDATE EVENTS.
- Model compliance is the very thing being measured, so the old sentence needs an explicit "or a NEW
  label declared by an earlier item in this response".

### D6. Forward references are charged, and "earlier" is ambiguous (INFERRED, spec-inherited).
- The prompt asks for the declaration "in the FIRST item that reports it", without saying first in
  listing order or first in response order.
- The parser judges response order. The list is sent `ORDER BY i.id DESC` (`:1097`).
- A model that emits rows in any other order, for example re-sorted ascending by `item_id`, turns
  every correct link into a charged `undeclared`.
- Under the pre-6kr contract, the same model output (a NEW event with a summary) would have been
  accepted via the counted fallback (`_resolve_label`, `:1733-1751`).
- The memory `never-give-a-model-raw-database-ids` warns that rejecting an unknown reference, where
  falling through is safe, is the deadlock shape.
- Say in the prompt "EARLIER in your response". Also consider falling back to the new-event path
  when a `NEWn`-referencing event also carries `summary` and `type`, instead of charging it.

### D7. The `_item` two-outlet recipe has a silent trap (VERIFIED).
- `_item`'s conflict fallback is `SELECT id FROM outlets LIMIT 1`
  (`tests/test_comprehend_integration.py:311`).
- "Add an `outlet` parameter" (plan line 213-215) without changing that fallback to
  `WHERE name = %s` returns an arbitrary outlet on the second call for a name that already exists.
- The result is a test whose "2 distinct outlets" rests on insertion luck.

### D8. `events_linked_in_request` is incremented inside the referencer's savepoint (INFERRED from plan line 255-256).
- A later rollback therefore leaves it counted.
- This matches the existing inflation of `events_matched`, `events_created` and `assertions_written`.
- It is now the runbook's headline "alive" signal, though. Increment it after the savepoint exits, as
  `new_events.update` is.

### D9. `events_matched` is unspecified for a `new_ref` (INFERRED).
- `score_match_rate_corroboration` documents `matched + created = A` as "exact given the write path"
  (`scripts/score_comprehension.py:497-506`).
- If a link is not counted in `events_matched`, the in-memory tally and the stored derivation diverge
  silently. State which way it goes.

### D10. Migration 0016 gets no up/down round-trip test (VERIFIED).
- The plan only extends the list in `test_up_creates_the_expected_tables`.
- 0009 and 0014 each have a rollback-and-reapply test (`tests/test_comprehension_schema.py:176`, `:275`).
- Spec §7 asks that "its up and down both run".

### D11. Task 4 changes live behaviour under prompt v2 (INFERRED).
- A `candidate: "NEW1"` invented by the model today is a counted fallback to the new-event path.
- After Task 4 it is `undeclared`: the item is dropped and charged.
- The risk is low under v2. But "dormant until Task 5" is not literally true of Task 3 (Objection 2)
  or Task 4.

### D12. Where reference resolution sits relative to the two early returns is unspecified (VERIFIED from plan text).
- The two early returns are `:2131` (empty) and `:2161` (entity-less).
- An entity-less extraction with a NEW event and an unresolved `new_ref` is either terminal or
  deferred, depending on placement.
- An item deferred for an unresolved reference also discards the rest of its valid extraction, and
  pays for it again next pass.

## First red-team: status of each item

| item | status |
|---|---|
| Obj 1: an entity-less referencer is discarded | **Fixed** (Task 3 fall-through, Task 5 cold-start test through `run()` with two outlets). The fall-through's side effect on `EVT` matches is Objection 2. |
| Obj 2: shared budgets and counters, double-noting | **Fixed** (0016, `deferred_neighbour`, `defer:*` keys noted once, `failed_integration` untouched, pinned in Task 3 and Task 4 tests) |
| Obj 3: non-string labels raise | **Fixed for labels only.** The broader guarantee the plan now claims is Objection 3. |
| D1: mutation count for the order swap | **Fixed** (pre-registers 2, with a `ceiling − 1` case; my trace agrees) |
| D2: "declare from dropped rows" count | **Not fixed.** It over-corrected to 1; it is 2 (D2 above). |
| D3: `inspect_integration.py` | **Fixed** (it now calls `parse_integration_response`, and the `:91` call is repaired). It is still untested, and a bead is filed. |
| D4: `main` harmful between tasks | **Mostly fixed** (prompt and bump moved last). Tasks 3 and 4 are still not dormant (Obj 2, D11). |
| D5: the declarer must be created second | **Fixed** (plan line 318-320, Task 5) |
| D6: key formula | **Fixed** (explicit `deferred_key`/`capped_key`; the existing `batch_capped:` keys are preserved) |
| D7: duplicate within one item | **Fixed** (`seen_here`, plus a parametrised case) |
| D8: order of checks | **Fixed** ("before `_resolve_label`") |
| D9: `new_events=None` | **Fixed** (`None` means `{}`, and an unresolved reference is deferred) |
| D10: sibling docs and re-extraction | **Fixed** for memory, beads and the code comment. 0009 is left alone deliberately, with a reason. |
| D11: the version-constant test | **Fixed** (dropped) |
| D12: the "> 0" criterion | **Replaced by a ratio that cannot discriminate** (Objection 1) |

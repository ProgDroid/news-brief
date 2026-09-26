# Red-team: in-request NEW links for real-time integration (`news-brief-6kr`)

**Plan:** `docs/superpowers/plans/2026-09-26-in-request-new-links.md`
**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`, Amendment A, §5.4, §5.6, §7
**Evidence:** `docs/2026-09-26-clustering-recall-spike-result.md` (M1)
**Prior red-team:** `docs/superpowers/reviews/2026-09-25-comprehension-cost-phase-2-redteam.md`
**Reviewed against:** `comprehend.py` (2288 lines, working tree at `7d43f94`), `scripts/score_comprehension.py`,
`scripts/inspect_integration.py`, `tests/test_comprehend_integration.py`, `tests/test_comprehend_labels.py`,
`tests/test_comprehend_triage.py`, `migrations/0006`, `0009`, the host runbook, beads `3wb`/`ymk`/`6kr`.
**Method:** reading, plus two local probes (the plan's own `grep` command, and `re.match` on non-strings).
Nothing written except this file. Each claim is **VERIFIED** (read or run, cited) or **INFERRED**.

---

## What checks out, stated first so the objections are not read as covering it

- **The gate counts an in-request link when one is written (VERIFIED).** `corroboration_by_outlet`
  counts `count(DISTINCT i.outlet_id)` per event over assertions with
  `a.created_at <= e.created_at + horizon` (`scripts/score_comprehension.py:188`, `:193`). Both
  `events.created_at` and `assertions.created_at` are `DEFAULT now()` (`migrations/0006_knowledge_base_up.sql:93`, `:122`).
  `write_batch`'s outer `conn.transaction()` (`comprehend.py:2283`) is a savepoint inside `run()`'s
  per-micro-batch transaction (commit at `:884`), so the declarer's event and the referencer's
  assertion share one `now()`. A link that reaches the write counts. The gate does not read
  `prompt_version` or `integrate_prompt_version` anywhere (grep of `scripts/`, `brief.py`,
  `migrations/`, `tests/`).
- **Nothing outside `comprehend.py` depends on the old bump semantics (VERIFIED by grep).** The
  readers are `pending_integration` (`:1094`), `retirement()` (`:953`), `write_extraction`'s
  `already` (`:2115`), the test at `tests/test_comprehend_integration.py:565`, and `_strikes`
  (`:1147-1154`, which only stamps the current version). `aged_out_count` already keys on
  `integrated_at IS NULL` (`:1118-1126`). The runbook's recovery SQL keys on `integrated_at IS NULL`
  (`docs/2026-09-25-host-runbook-cost-redesign-phase-1.md:131-132`).
- **Task 1's three tests discriminate (VERIFIED by trace).** `_strikes(..., integrated=True)`
  stamps version 2 before the monkeypatch, so the old `retirement()` predicate counts it and the
  new one does not. This closes phase-2 red-team defect 6.
- **Line numbers** for `pending_integration`, `retirement()`, `already`, `INTEGRATE_PROMPT_VERSION`,
  `_INTEGRATE_SYSTEM`, `parse_integration_response`, `_validate_item`, `Tally`, `NoEntitySurvived`,
  `write_extraction`, the response-failure branch (`:826-852`) and the runbook's Step 5
  (`:143-220`) are correct to within a few lines.

---

## Objection 1 (a): an in-request link whose referencer carries no entities is discarded before it is resolved, permanently and uncounted, and the plan's own prompt steers the model into that shape. No test can see it, because no test drives a successful link end to end.

**The mechanism (VERIFIED).** `write_extraction` has two early returns that run **before** the
savepoint and the events loop:
- `not entities and not events` → `empty_extraction` (`comprehend.py:2131-2140`);
- `not extraction["entities"]` → `entityless_extraction`, marked `integrated_at = now()` and
  returned `True` with **nothing written** (`:2161-2170`).

Task 4 puts `new_ref` resolution "in `write_extraction`'s events loop" (plan line 329), which
is after `:2161`. So a referencer whose extraction is `entities: []` plus one `new_ref` event:
- is marked integrated and never re-offered (Task 1 makes that final);
- writes no assertion, so the pair the feature exists for is lost for good;
- never increments `events_linked_in_request`. It lands in `entityless_extraction`, which the
  runbook check does not mention.

The same holds for a declarer with `entities: []`. Its event is never created, so its referencer
raises `UnresolvedNewLabel` and is deferred. It comes back alone next pass, and the pair is lost again.

**Why this is likely, not hypothetical (INFERRED, from verified text).** The paragraph the plan
appends verbatim tells the model, for each LATER item, to "set `candidate` to that NEW label
**instead of describing the event again**" (plan lines 192-195). The item schema requires only
that `entities` is an array (`comprehend.py:1702`), so `[]` is schema-legal. A model that has just
been told not to re-describe the event has every reason to leave the actors off the item too, since
they belong to the event it points at. `entityless_extraction` exists because the model already does
this (bqa.17, `:2149-2154`).

**The refusal is over-broad for this shape (VERIFIED).** `:2142-2147` justifies it only for NEW events: "`candidate_events`
retrieves BY entity id, so an entity-less event can never be offered". A `new_ref`, or an
`EVT` match, points at an event whose `event_entities` rows the declarer already wrote. The item's
own entities are not needed for retrieval. This hole already swallows entity-less `EVT` matches
today, which is corroboration, and how often it happens is unmeasured. The plan widens its intake.

**Why the suite cannot catch it (VERIFIED from the plan text).**
- Task 4's link test gives item b "the same entity" (plan line 305).
- Task 3's run()-level tests cover only failure paths.
- Task 4's fixtures hand-write `{"new_ref": ..., "standing": ...}` rather than obtaining it from
  Task 2's parser. Nothing ties Task 2's output shape to Task 4's input shape (the
  `tdd-plan-fixtures-drift-from-contracts` pattern).
- Spec §7 requires "Cold start: an empty KB with an in-request `NEW` link". The plan has no such
  test. The deadlock it guards (2026-09-07) lived at the parse layer, which the Task 4 tests bypass.

**Consequence.** After the restart, the runbook's steady `0` reads as "the model is mis-using
labels" (plan Task 5 Step 1). The actual cause is the writer. The gate, which counts only written
links, measures a feature that the writer's early return partly disabled.

**Fix.**
1. In `write_extraction`, narrow the entity-less refusal to extractions that carry a NEW event.
   An extraction whose events are all `candidate_id` or `new_ref` writes its assertions without
   item entities. Alternatively, and weaker, make the prompt require the item's actors on every item.
2. Add one run()-level test that builds the response through `parse_integration_response`, with
   empty candidate maps (cold start), where the referencer has `entities: []`. Assert
   `events_linked_in_request == 1` and a 2-outlet event per `corroboration_by_outlet`, using two
   outlets (`_item` always uses the one `Reuters` outlet, `tests/test_comprehend_integration.py:305-312`).
3. Add `entityless_extraction` to the runbook's reading of a steady `0`.

---

## Objection 2 (b): the plan books a neighbour's fault into the response-shape machinery (`deferred_response`, `integrate_defers`) and into `failed_integration`, and it notes the orphaned key twice. It assumes those counters are free to share. The codebase says in writing that they are not.

**`deferred_response` has one meaning (VERIFIED).** `Tally` (`comprehend.py:65-71`) defines it
as "the model answered and the answer could not be read ... a prompt or a parser problem"
(h8p). Task 3 extracts `_defer_or_charge` "verbatim in behaviour", with `deferred_response`
"counted as today" (plan lines 238-240), and routes both neighbour paths through it (Task 3
Step 3, Task 4 Step 3). Every orphaned or unresolved referencer therefore reads, in the one log line the
operator has, as an unreadable response.

**`integrate_defers` becomes a shared budget for two no-verdict causes (VERIFIED from the plan; the rule is the
repo's own).** The memory `retry-budget-needs-a-verdict.md`, rule 3, says: "Count deferrals in
their own field". `test_a_transport_failure_does_NOT_spend_the_defer_budget`
(`tests/test_comprehend_triage.py:1588-1607`) exists to keep two no-verdict paths distinct for
exactly this reason. Here, an innocent referencer that is deferred four times for a flaky neighbour arrives at
a later shape fault with six defers left instead of ten, and it is charged sooner. Amendment A names
`integrate_defers` too, so this is a spec defect the plan inherited. It is still the plan that
wires it.

**A deferred item is also counted as a failure (VERIFIED).** Task 4 says to "record the savepoint
loss as today". Today's `except` block increments `items_lost_to_savepoint` **and
`failed_integration`** (`comprehend.py:2251-2252`) before the charge. Kept as written, an
`UnresolvedNewLabel` referencer is `failed_integration += 1` and also deferred, and at the ceiling
`_defer_or_charge` adds `failed_integration` a second time. The invariant the h8p tests pin,
"no verdict ⇒ `failed_integration == 0`" (`tests/test_comprehend_triage.py:1465-1468`), has no
counterpart test for either new path.

**The orphaned key is counted twice, and it breaks a documented subtraction (VERIFIED).**
- Task 2 notes `validate:new_label_orphaned` in the parser (plan line 145).
- Task 3 then calls `_defer_or_charge(..., "validate:new_label_orphaned")`, whose deferred branch
  notes `cause` again (plan lines 241, 266).
- One orphaned item therefore shows `2`.
- `run()`'s comment at `comprehend.py:870-873` says `dropped − Σ validate:*` = "items the model
  never returned at all". With `nf` removed from `dropped` but its `validate:` key still counted
  (twice), that difference goes negative. The one diagnostic built to separate those defects stops
  working.

**The runbook check is unobservable (VERIFIED).** "Check that `integrate_attempts` does not rise
from `validate:new_label_orphaned`" (plan Task 5 Step 1). `integrate_attempts` is a bare column
with no cause attached, and the only per-cause record is the tally line, which the double-noting
has just corrupted.

**Fix.**
- Give neighbour faults their own tally field (`deferred_neighbour`) and their own failure key
  (for example `defer:new_label_orphaned`, distinct from the parser's `validate:` key).
- Pick one owner for the note, and state which one.
- In the `UnresolvedNewLabel` branch, do not increment `failed_integration`.
- Either give the defer ceiling its own column, or record in the spec, with a reason, that sharing
  `integrate_defers` is accepted.
- Pin all of this with a test asserting `failed_integration == 0` and
  `deferred_response == 0` for an orphaned referencer.

---

## Objection 3 (b, and c): the new checks run on raw model output ahead of the existing type guards. One `"candidate": null`, one integer candidate, or one malformed dropped row raises out of `parse_integration_response` and turns an item-level defect into a whole-batch one. In 6 months, a model swap makes that routine.

**The mechanism (VERIFIED, probe run).**
- `_NEW_LABEL.match(x)` raises `TypeError` for `x` = `3`, `True` or `None`. Probe output:
  `3 TypeError: expected string or bytes-like object, got 'int'`, and the same for `bool` and
  `NoneType`.
- The plan checks "a `candidate` matching `_NEW_LABEL`" first (plan line 205), but never says to
  guard on `isinstance(str)`.
- Today, `_resolve_label` absorbs exactly these values: `None` → "not a reference" (`:1745`), and
  a non-string or unknown value → counted fallback (`:1747-1751`).
- Step 3 also says to collect the dropped rows' `new_label` strings "from `row.get("events")`
  **without validating them**" (plan line 200-201). On a dropped row:
  - `events` a string → iterating yields characters, and `.get` raises `AttributeError`;
  - `new_label` a list → `set.add` raises `TypeError: unhashable`.
  The row was dropped because it was malformed, so these are exactly the rows most likely to have
  those shapes.

**Where the exception goes (VERIFIED).** A raise inside `parse_integration_response` exits
through `run()`'s `except Exception` (`:794`) into the response-failure branch. **All five items**
in the micro-batch are deferred, then charged at `COMPREHEND_MAX_DEFERS`, then retired at 3
(`:826-853`). One item's malformed field retires four innocent neighbours. That is the
failure this codebase spent bqa.11, bqa.16 and h8p removing.

**Why it is plausible (VERIFIED history, INFERRED rate).**
- The model emitted integer `candidate_id`s as its own sequence numbers (bqa.11,
  `tests/test_comprehend_labels.py:10-15`).
- It emits explicit nulls for enum fields (`:322-328`, `:425-436`).
- The only non-string-candidate regression test covers **entities**
  (`test_a_non_string_candidate_does_not_resolve`, `:160-171`), so nothing in the suite exercises
  the event arm where the new check lives.

**The 6-month form (INFERRED).** M2 is literally a Haiku replay (spike result, "M2"). A swap
through the `NEWSBRIEF_MODEL` settings row changes emission habits without a deploy and without a
test run. The first model that writes `"candidate": null` on new events would convert every
affected batch into a whole-batch deferral. The first signal would be the retirement alert, days
later, naming items that were never at fault.

**Fix.**
- Treat a candidate as a NEW reference only when `isinstance(c, str) and _NEW_LABEL.match(c)`;
  anything else continues to `_resolve_label` unchanged.
- Collect orphaned labels only from `isinstance(ev, dict)` members whose `new_label` is a `str`.
- Add event-arm tests for `candidate` = `None`, `3` and `True` on a new event, asserting the item
  survives. Add a dropped row with `events: "x"` and one with `new_label: []`, asserting the neighbour
  items survive.

---

## Additional verified defects

1. **Task 3 Step 5's mutation pre-registration cannot be computed as written, and is wrong once
   it is (VERIFIED).**
   - `grep -n "COMPREHEND_MAX_DEFERS" tests/` prints `grep: tests/: Is a directory` and exits 2
     (probe run). The probe returns nothing.
   - With `-r` it counts **lines** (3: `test_comprehend_triage.py:1540`, `:1571`, `:1614`), not
     order-discriminating tests.
   - Traced by hand, swapping the two UPDATEs fails only `test_the_defer_budget_is_BOUNDED...`
     (`:1527`). On its second pass `defers = ceiling − 1`, so defer-then-charge gives `(2, 1)` ≠
     `(2, 0)`.
   - The other two tests cannot fail the swap: `:1565` runs at ceiling 0, and `:1440` sits far
     below the ceiling. `:1614` is a knob-membership test.
   - The new `test_a_neighbours_fault_at_the_defer_ceiling_is_charged` starts at
     `defers == ceiling`, where both orders give the same result, so it also cannot fail.
   - Predicted: 3 + 1 = 4. Expected: 1. To pin the order on the new path, add a case at
     `ceiling − 1`.
2. **Task 2 Step 5, "declare from dropped rows too → 2", should be 1 (VERIFIED by trace of the
   plan's own loop).** In the forward-reference case the declaring row comes AFTER the referencer.
   When the referencer is validated, nothing has been added to `declared` under either version, so
   that case still fails as `undeclared`. Only the orphaned test changes.
3. **The claim that `scripts/inspect_integration.py` "needs no change" is false (VERIFIED).**
   - The script does not call `parse_integration_response`. It calls `_validate_item` per row,
     independently (`scripts/inspect_integration.py:150`).
   - With the default `declared=frozenset()`, every valid in-request reference prints `REJECT`. This
     is the reconstruction-drifts-from-production pattern the script's docstring warns about.
   - Separately, and already true today, the script is broken: `candidate_events(conn, entity_ids,
     Tally(enabled=True))` (`:91`) passes the Tally as `titles` and omits the required `tally`, so
     it raises `TypeError` before reaching the model.
4. **Ordering between tasks leaves `main` harmful at Task 2's commit (VERIFIED).**
   - After Task 2, the validator emits `{"new_ref": ..., "standing": ...}`.
   - The pre-Task-4 writer takes the `else` branch and hits `ev["summary"]` → `KeyError`
     (`comprehend.py:2186`, `:2207`), inside the savepoint → **charged**.
   - Orphaned referencers are charged too, because Task 3 has not landed.
   - Rows written in that window are stamped `INTEGRATE_PROMPT_VERSION = 3` by a half-built
     feature, which pollutes the column Task 1 just made provenance-only.
   - A push to `main` publishes the image (`.github/workflows/docker-publish.yml:3-5`), and a
     subagent run can stop between tasks.
   - Fix: land the schema, prompt and version bump **last** (after Tasks 3-4, with Task 2's
     validator dormant until then), or squash Tasks 2-4 into one commit.
5. **Task 3's test recipe has a trap (VERIFIED).** The prompt lists items `ORDER BY i.id DESC`
   (`comprehend.py:1097`), and the fake at `tests/test_comprehend_triage.py:561-567` answers in
   `sent` order. The declarer must therefore be the item created **second**. A literal copy of that
   test (the `bad` item comes second) puts the referencer first, which is a forward reference: it
   is charged as `undeclared`, and the test fails for the wrong reason or the code gets bent to pass
   it. Say so in the step.
6. **`_defer_or_charge`'s key formula does not reproduce the existing keys (VERIFIED).**
   `f"{cause}_capped"` with `cause = "batch:ValueError"` yields `batch:ValueError_capped`. The
   existing key, asserted at `tests/test_comprehend_triage.py:1562`, is `batch_capped:ValueError`.
   The plan hedges ("takes the two keys explicitly if needed"). Make the signature take two keys
   and delete the formula.
7. **A duplicate declaration within one item is not rejected (VERIFIED from the plan text).** The
   `duplicate` check reads only `declared`, which holds labels from earlier validated items (plan
   line 211). An item with two events both carrying `new_label: "NEW1"` passes. The writer's
   `declared_here[label] = event_id` then silently keeps the second.
8. **Order-of-checks is unspecified (VERIFIED).** If the `NEW` test is placed after
   `_resolve_label`, every reference also increments `unmapped_candidate` (`:1749-1750`) and drops
   into the new-event path. That pollutes the non-compliance counter whose purpose is to show the
   model inventing labels. State "before `_resolve_label`".
9. **`write_extraction(..., new_events=None)` with a `new_ref` present** raises `TypeError` or
   `AttributeError` on the lookup, not `UnresolvedNewLabel`, so it is **charged**. This affects
   direct callers and tests only (INFERRED from the signature). Default to `{}` or raise
   `UnresolvedNewLabel`.
10. **The retired bump semantics survive in every sibling doc (VERIFIED).**
    - The committed and live memory `newsbrief-comprehension-pipeline.md:66`, `:246` and `:279`
      ("Do NOT bump `INTEGRATE_PROMPT_VERSION` ...").
    - The `ymk` NOTE ("Do NOT bump ... until 3wb ships a supersede path").
    - The migration `0009` comment (`:28-29`).
    - The plan only appends bead notes. None of these is corrected.
    - Separately, after Task 1 no re-extraction operation exists at all. `ymk` ("Re-extract items
      integrated under the old integration schema") no longer has a mechanism, and none is filed.
      The first prompt fix after this lands, if it assumes a bump re-extracts (as 0009's split was
      designed to), silently does nothing.
11. **`test_the_prompt_version_is_3`** pins a constant. It tests no behaviour and fails on the
    next legitimate bump. Its intent (Task 2 bumps, Task 1 made that safe) is already carried by
    Task 1's three tests.
12. **The runbook's success criterion is still "> 0" (INFERRED as the same gap the phase-2 red-team named in (a)).**
    It proves the mechanism is alive. It does not show that the 25-point blindness M1 measured was
    recovered. A comparable figure is the share of multi-outlet events whose two assertions came
    from one micro-batch, and the plan leaves it unmeasured. This matters because the next gate
    window inherits it (spike result, "Not pre-registered").

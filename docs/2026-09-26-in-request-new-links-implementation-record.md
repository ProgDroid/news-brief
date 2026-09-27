# In-request NEW links (`news-brief-6kr`): implementation record

**Date:** 2026-09-26 · **Commits:** `3b63c0d..b204c5a` on `main` (10 commits) · **Plan:**
`docs/superpowers/plans/2026-09-26-in-request-new-links.md` (revision 2) · **Spec:** Amendment A
and its revision 1, in `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`

**Reviews:**
- `docs/superpowers/reviews/2026-09-26-in-request-new-links-redteam.md`
- `…-rev1-redteam.md`
- `…-final-review.md`

## What shipped

- **Integration prompt v3.**
  - The model may declare `new_label: "NEWn"` on an event.
  - A later item in the same response references it with `candidate: "NEWn"`.
  - The writer resolves a reference only after the declarer's savepoint has committed.
- **Version bumps are inert.** No integration predicate reads `integrate_prompt_version` any
  more. Re-extraction has no mechanism now: `news-brief-wt8`.
- **Neighbour faults have their own budget:**
  - migration 0016 adds `item_triage.integrate_link_defers`;
  - the ceiling is the knob `COMPREHEND_MAX_LINK_DEFERS` (10);
  - `_defer_or_charge` is shared with the response-fault path, whose keys are unchanged.
- **The parser checks labels in response order and isolates each row.** A malformed row drops
  only itself, under `validate:malformed`, and logs its item id and exception type.
- **Entity-less references are written.** An extraction with no entities, whose events are all
  references, now writes its assertions. This also covers entity-less `EVT` matches, which
  were discarded before.
- **The runbook has three new checks:**
  - Step 5: first-days checks;
  - Step 7: a co-batched-pair measurement;
  - Step 7: a multi-outlet v3 events query.

  Both queries were executed on seeded fixtures before they went in.

**Deploys inert:** comprehension is off. Nothing reaches production behaviour until the
phase-1 runbook flips `COMPREHEND_ENABLED`.

## Rulings made during execution (operator did not see these at the time)

| Ruling | Why | Cost if wrong |
|---|---|---|
| Execute on `main`, no worktree | The standing `newsbrief-commit-to-main` instruction; explicit-path commits | A revert of a few commits |
| One controller-owned Postgres container for all implementers | Two sessions starting containers on :5432 collide | An implementer's DB tests skip, and it must say so |
| No push until the final review is clean | A push publishes `:latest` | None |
| Keep `UnresolvedNewLabel` until the final review | The plan listed it "for direct callers". **The final review deleted it (M4)** | None; resolved |
| Task 5 must add a test that an entity-less item with an unresolved reference is deferred | Task 5 opens the entity-less branch, which is where the order becomes load-bearing | One test |
| Execute the Task 6 runbook SQL on a seeded fixture before it goes in | A query in an operator doc that was never run is `metadata-is-not-state` | Minutes |
| Fold three cheap pre-restart items into the single final fix wave | Each had to land before the restart anyway, and only one wave is allowed | A larger re-review |

## Where the pre-registered numbers lost, and what caught it

Each loss below was a test that could not fail. Every one was found by **running** a mutation or
a trace, not by reading. That fits `the-rule-exempts-its-own-origin`: reading review saturates.

1. **Task 3, link-counter placement.** The plan predicted 1 failure for "increment inside the
   savepoint"; it measured 0.
   - The rolled-back test rolls back the *declarer*, so no item both resolves a reference and
     then rolls back.
   - A new test was added: a rolled-back referencer.
   - Moving the increment to the **end** of the `with` block is observationally identical, since
     nothing after it can raise. The regression the guard exists for is an eager per-event
     increment, and that fails 1.
2. **Task 5, pre-check order.** The required mutation was not run, and the task's own change
   made it undetectable.
   - Once the entity-less branch falls through for a reference-only item, moving the reference
     pre-check below it changes nothing for that item.
   - The order is observable only for an entity-less item that **mixes** a NEW event with an
     unresolved reference. That test now exists (1 / 0 as predicted).
   - The same review found `any`→`all` in `has_new_event` unpinned. A regression there would
     write entity-less NEW events that can never be retrieved, which depresses the gate. It is
     now pinned.
3. **The final review found a wrong-link path no task review saw (M1).**
   - A label re-declared after its first declarer was dropped linked the referencer to the
     *second* declarer's event.
   - The gate would have counted that as corroboration.
   - Fixed: `nl in orphaned` is now part of the duplicate check.

The other predictions matched, including Task 4's six (36, which the plan had registered as ≥17,
then 2, 1, 4, 1, 1) and Task 2's order swap (2).

## Open work

**Before the phase-1 restart:**
- ~~`news-brief-jwm`~~ DONE 2026-09-26: prompt **v4** scopes NEW labels in the CANDIDATE
  LABELS sentence to events only, and the runbook's Step 5 first-days list now watches
  `unmapped_candidate`.

**Owed on the host:**
- `6kr` is closed, but its close reason names the observation still owed: runbook Step 7's
  `co_batched_pairs` and `linked_pairs` after 7 days, read against the phase-1 density.

**Deferred, filed as beads:**

| Bead | Priority | What it covers |
|---|---|---|
| `news-brief-wt8` | P3 | Re-extraction mechanism |
| `news-brief-6u6` | P2 | `inspect_integration` smoke test, a `max_tokens` crash, and string recovery |
| `news-brief-4ur` | P3 | Pin the label check order with full-shape tests |
| `news-brief-mzl` | P4 | `new_label_fallback` counts rows that are later dropped; duplicate `item_id` |
| `news-brief-ytw` | P3 | An entity-less declarer's reference is filed under the `unresolved` key, not `orphaned` |
| `news-brief-c8p` | P4 | Link counters count references, not assertions written |

**Unchanged by this work:**
- M1's (density sweep) verdict still waits on phase 1's measured p90 (`news-brief-4le`).
- M2 (Haiku replay, `news-brief-y1x`) is not built.
- `vlg` (the phase-2 plan rewrite) still depends on both.

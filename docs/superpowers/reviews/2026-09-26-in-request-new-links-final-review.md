# Final review: in-request NEW links (`news-brief-6kr`), `e635e9d..dfb484b`

Reviewer: final whole-branch review (opus), 2026-09-26. The branch has 9 commits on `main` and
is not pushed yet.
Inputs: the plan (rev 2), spec Amendment A with revision 1, both red-teams, the SDD ledger, and
the full diff.
Probes run (read-only):
- `ruff check .` exited 0, and `ruff format --check .` exited 0.
- A scratch parser probe, with no DB, run through `parse_integration_response` itself. Its
  output is quoted where it is used below.
- The suite was not re-run. The ledger records 1896 passed and 0 skipped at `dfb484b`.

## Strengths

- **Nothing loses track of a declaration.** Declarations are staged in `declared_here` and folded
  into `new_events` only after the `with conn.transaction()` exits (`comprehend.py:2600-2607`).
  The reference pre-check sits before the empty and entity-less returns and before the savepoint
  (`:2395-2418`). Because of that order, a rolled-back declarer can never resolve a referencer,
  and a reference-only item never reaches the entity-less branch unresolved. Both orders are
  pinned by tests that were confirmed with mutations: the rolled-back referencer test, and the
  mixed NEW+unresolved-ref test from `241bbce`.
- **The parser's output matches what the writer reads, including the matched-event case.** The
  probe confirms the parser emits exactly the shapes the writer consumes:
  - `{"new_ref": "NEW1", "standing": ...}`;
  - a new event carrying `new_label`;
  - `{"candidate_id": cid, "standing": ..., "new_label": ...}`.

  The writer records `declared_here` after either branch (`:2566-2567`). The cold-start test
  (`tests/test_comprehend_triage.py:2159`) drives raw tool-use JSON through `comprehend.run`, so
  one test runs `parse_integration_response`, `write_batch` and `corroboration_by_outlet`. It
  also uses the real `ORDER BY id DESC` listing.
- **Counter accounting holds end to end.** I traced each path:

  | Path | What is noted and charged |
  |---|---|
  | Own-fault drop | 1 `validate:*` + 1 `dropped` + 1 attempt |
  | Malformed row | `validate:malformed` + `dropped` + a log line with `item_id` and the exception type |
  | Orphaned neighbour | Nothing in the parser. Then `defer:new_label_orphaned`, `deferred_neighbour`, and `link_defers+1` |
  | Unresolved neighbour in the writer | `defer:new_label_unresolved`, once. Not `dropped`, and not `items_lost_to_savepoint` |
  | Fallback | `new_label_fallback` and no key |
  | Capped | `defer_capped:*`, `failed_integration`, `defer_cap_hit`, and an attempt |
  | Declarer rollback | `savepoint:<Exc>` and an attempt |

  Each key is noted once per item. The identity at `:956-959` (`dropped` minus the sum of the
  `validate:*` keys = items never returned) holds, because a neighbour fault notes no
  `validate:*` key and `nf` is excluded from `dropped`.
- **The version bump is correctly inert.** None of the three predicates reads
  `INTEGRATE_PROMPT_VERSION` any more (grep over `*.py`). Every remaining use is a provenance
  write. The memory correction rewrote both "do not bump" passages rather than appending to them.
- **The `_defer_or_charge` extraction preserves the old keys and order.** It keeps `batch:` and
  `batch_capped:`, and charges before it defers. The first mutation check (swapping the two
  UPDATEs) hit exactly the 2 tests pre-registered.
- **Deploying this is safe.** The changes and why each is harmless:
  - 0016 is a constant-default `ADD COLUMN`, which is metadata-only on PG 18, and its down
    migration exists.
  - The knob follows the three-part rule (a `KNOBS` entry, a compose anchor line, and the parity
    test).
  - `run()` returns before any integration code while `COMPREHEND_ENABLED` is off.
  - Nothing outside `comprehend.py` reads the new Tally fields, so no alert wiring was missed.

## Issues

### Critical

None.

### Important

**I1. An explicit `"new_label": null` drops the item and charges a strike.**
- **Where:** `comprehend.py:2285-2288`.
- **Probe:** a valid new event with `new_label=None` gives `survives: 0 {'validate:new_label_shape': 1}`.
  The control without the key survives, and `candidate=None` survives.
- **Why:** the same function, about 40 lines above, treats `commitment_state: null` as absent. It
  gives the reason in its own comment: *"An explicit null says the same thing as an omission …
  rejecting one spelling and not the other would reintroduce the bug for the same answer
  (bqa.16, 0011)"*. `new_label` is optional and never required by the schema, so a null is the
  model saying "no label".
- **Consequence:** as written, that answer is an own-fault drop. Three of them retire the item
  permanently. If the item was also a declarer, its referencers are link-deferred as well.
- **The spec is silent here,** and the plan's rule ("not a `str` → shape") did not consider null.
  A reasonable operator would not expect an item to be charged for saying "no label".
- **Fix:** make the guard `nl = ev.get("new_label")` / `if nl is not None:`, so that `[]`, `7`
  and `""` still reach the shape check. Add a parametrised case, `new_label=None` survives with
  no key, to `test_bad_new_labels_…`, or a sibling test.
- **Mutation:** re-instating `"new_label" in ev` must fail exactly that case (1).
- **Timing:** this is inert until the flip, so it can ride a follow-up commit. It must land
  before `COMPREHEND_ENABLED` is set.

### Minor

**M1. A label re-declared after its first declarer was dropped binds silently to the SECOND
declarer's event.** This is the ledger's Task 4 minor. I promote it to must-fix before the flip.
- **Where:** `comprehend.py:2290`.
- **Probe:** item 1 declares `NEW1` and is dropped (bad entity type). Item 2 declares `NEW1` on a
  *different* summary. Item 3 references `NEW1`. Item 3 is emitted as
  `{'new_ref': 'NEW1'}` and links to item 2's event. No key is noted, and `nf == set()`.
- **Why it matters more than a missed link:** this writes an assertion on the wrong event, which
  `corroboration_by_outlet` counts as corroboration. That is the one number the pre-registered
  gate reads. A missed link fails closed. A wrong link inflates the gate.
- **Fix:** `if nl in declared or nl in seen_here or nl in orphaned:` → `validate:new_label_duplicate`.
  Item 3 then falls into the orphaned branch and is deferred as a neighbour fault, which is
  correct. Add one test. Mutation: removing the clause reads 1.

**M2. Stale or wrong counter comments on the Tally fields the runbook tells the operator to
record.** Must-fix.
- **Where:** `comprehend.py:94-99`, `:126-132` and `:141-147`.
- `entityless_reference_written` claims two things the code contradicts:
  - it says the reference "still resolves once the neighbour's declaration lands, so it must not
    be counted as final". In fact it is incremented only after the savepoint commits, for a
    written and final extraction;
  - it describes only NEW references. It also counts entity-less `EVT` matches, which is the
    spec revision 1 §2 behaviour change.
- `events_linked_in_request` says "Apart from events_matched" and "provisional … until every item
  … is judged". In fact every link is *also* counted in `events_matched` (by design, D9), and the
  count is post-commit, not provisional.
- `new_label_fallback` says "a fresh entity/event". Only events carry NEW labels.
- **Fix:** rewrite the three comments to describe the code.

**M3. Plan scaffolding is left in production comments.** Must-fix, in the same commit as M2.
- There are 14 `Task N` / `Tasks 3-4` / `[R2: …]` references in `comprehend.py` and in 0016's
  header.
- `comprehend.py:2468` says "the entity-less fall-through **this task** adds".
- `comprehend.py:2221` cites `` `constraints.md`'s arithmetic invariant ``. That file exists only
  under the gitignored `.superpowers/sdd/…/constraints.md`, so the pointer will dangle once the
  scratch directory is removed. The invariant itself lives at `comprehend.py:956-959`.
- **Fix:** replace these with `news-brief-6kr`, the spec section, or the in-file line they mean.

**M4. `UnresolvedNewLabel` (`comprehend.py:168`) is dead.** Must-fix: delete it, in the same
commit.
- Nothing raises or catches it.
- Its docstring describes a "direct caller" that does not exist.
- Spec revision 1 replaced the raise with a deferral.
- A documented exception with no raiser is metadata that states a mechanism the code does not
  have.

**M5. Runbook: "RT's blind share" is ambiguous in the direction that inverts the expectation.**
Must-fix before the Step 7 reading.
- **Where:** `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md:381`.
- The M1 table's RT column is *visibility* (74.8% at 5/h), and the blind share is 100% minus
  that (25.2%). An operator who reads the column value directly would expect 74.8% co-batched.
- **Fix:** "RT's blind share = 100% − the RT column (e.g. 25.2% at 5/h)".

**M6. The parser declares a label from an entity-less declarer, but the writer never records it.**
Can defer.
- **Where:** `comprehend.py:2112` versus `:2471-2482`.
- Spec revision 1 §2 says "a declarer with no entities therefore declares nothing". The parser
  still adds the label to `declared`.
- The writer takes the terminal path, so the referencer is deferred under
  `defer:new_label_unresolved` rather than `…_orphaned`.
- The outcome is safe: one link-defer, and the item is re-offered alone next pass. But the key
  misattributes the cause, and no test covers it.
- **Fix, if wanted:** in the parser, move labels from a row with no entities and any NEW event
  into `orphaned` rather than `declared`.

**M7. The CANDIDATE LABELS rewrite applies to entities too.** Can defer, but add it to the
runbook watch list.
- The sentence "Set `candidate` to one of those labels … or to a NEW label" sits in a paragraph
  covering ENT and EVT.
- An entity `candidate: "NEW1"` is harmless: `_resolve_label` counts `unmapped_candidate` and
  falls back to a name match.
- But it would raise `unmapped_candidate` for a reason the runbook does not mention. Add
  `unmapped_candidate` to Step 5's first-days list, or scope the sentence to events.

**M8. `events_linked_in_request` and `events_matched` count `new_ref` events, not assertions
written.** Can defer.
- If an item references the same event twice (for example `EVT1` plus a `NEWn` that resolves to
  it), the second INSERT is an `ON CONFLICT DO NOTHING`, but both counters rise twice.
- This is pre-existing for `candidate_id`, and small.

## Deferred-minor triage

| # | Ledger minor | Verdict | Reason |
|---|---|---|---|
| T1 | New tests beside related tests, not at the end of the file | drop | Placement by topic is better for readers, and nothing depends on order |
| T2 | `_defer_or_charge` allowlist is a bare `assert` | drop | The Dockerfile runs `python brief.py` (no `-O`, no `PYTHONOPTIMIZE`), and all 4 call sites pass literals. Swap to `if … raise ValueError` if the helper is touched again |
| T2 | A capped neighbour fault charges the shared `failed_integration`/`defer_cap_hit` | drop | Plan-mandated, mirrors 0013, and `defer_capped:*` keeps the cause attributable |
| T3 | `UnresolvedNewLabel` never raised | **must fix** (M4) | Dead exception whose docstring invents a caller. Delete it |
| T3 | `events_matched` (in-loop) vs `events_linked_in_request` (post-commit) disagree after a rollback | can defer (optional in the M2 sweep) | Pre-existing: every in-loop counter survives a rollback. A one-line comment would do |
| T3 | Same-item declare+reference and duplicate declaration | drop | Resolved by Task 4 (`_self`, `_duplicate`), and each is tested |
| T3 | No test pins the pre-check running before the entity-less return | drop | Resolved in `241bbce` (the mixed NEW+unresolved-ref test, 1/0 mutation) |
| T4 | Check order (fallback after orphaned/self) unpinned | can defer (bead) | The current order is right. Two full-shape tests would pin it |
| T4 | `_NEW_LABEL` uses `$`, so it accepts a trailing `\n` | drop | Probed: `"NEW1\n"` declared and referenced links correctly, and a one-sided newline only misses. Matching is exact-string on both sides, so this can never produce a wrong link or a raise. `fullmatch` is free to adopt in the M2 sweep |
| T4 | `inspect_integration` crashes on a `max_tokens` ValueError, and its early return precedes string recovery | can defer | Pre-existing, in an offline tool. Append it to `news-brief-6u6` (the smoke-test bead) |
| T4 | `new_label_fallback` counted for rows later dropped; a duplicate `item_id` can be both written and link-deferred | can defer (bead) | Only inflates a counter. Duplicate `item_id` handling is pre-existing parser behaviour |
| T4 | A label re-declared after its first declarer was dropped binds to the second | **must fix** (M1) | Writes an assertion on the wrong event, which the gate counts as corroboration |
| T4 | `test_a_reference_nobody_declared_is_charged` passes on old code | can defer | The labels module pins `validate:new_label_undeclared`. Adding the one assertion is still worth doing in the fix commit |
| T4 | `_validate_item` grew by about 70 lines | drop | Readable as is. Splitting now would churn the file for no correctness gain |
| T5 | `Tally.entityless_reference_written` comment is stale | **must fix** (M2) | It describes the opposite finality of a counter the runbook tells the operator to record |
| T5 | "this task adds" wording in a production comment | **must fix** (M3) | Same sweep. Also covers the dangling `constraints.md` pointer |
| T5 | CANDIDATE LABELS rewrite unpinned | can defer | Two prompt tests already pin the new paragraph. The negative assertion is cheap and belongs with M7 |
| T6 | Runbook "RT's blind share" wording | **must fix** (M5) | Read literally it inverts the expectation (74.8% versus 25.2%) |
| T6 | Step 5 bullets give a diagnosis but no action | can defer (before the restart) | Operator-doc quality. Add "file a bead and attach the tally line" |
| T6 | "Multi-outlet v3 events" has no paste-ready query | can defer (before the Step 7 reading) | Spec revision 1 §5 asks for this count. Suggested query below, **not run** |

Suggested query for T6 (unrun; `:flip` is the phase-1 flip timestamp):

```sql
SELECT count(*) FILTER (WHERE outlets >= 2) AS multi_outlet, count(*) AS total
FROM (SELECT e.id, count(DISTINCT i.outlet_id) AS outlets
      FROM events e JOIN assertions a ON a.event_id = e.id JOIN items i ON i.id = a.item_id
      WHERE e.prompt_version >= 3 AND e.created_at >= :flip
      GROUP BY e.id) t;
```

**Must-fix minors: 5.** They are M1–M5, which cover the ledger's T3 `UnresolvedNewLabel`, T4
re-declare, T5 stale comment, T5 "this task", and T6 blind-share items. All of them are comment,
doc, or one-clause changes.

## Declined to judge

- **Whether the model will follow the v3 prompt**, and how often it links: this can only be
  measured after the restart, and the runbook owns that measurement.
- **The M1-derived numeric expectation in runbook Step 7:** it belongs to the spec and to
  `news-brief-4le`'s density measurement. I only checked that the wording points at the right
  column (M5).
- **`COMPREHEND_MAX_LINK_DEFERS` default of 10:** the spec calls it a retunable guess. There is
  no data to judge it against.
- **The plan-mandated change that a raising row now charges an attempt,** where it used to defer
  the whole batch through `integrate_defers`: this was a deliberate plan decision (R2 Objection
  3), mitigated by the new log line. I did not relitigate it.
- **Duplicate `item_id` rows in one response:** the handling is pre-existing and not introduced
  here. It is covered by the T4 bead suggestion.
- **Per-task mutation counts:** each task review verified these, and I did not re-run them.
- **The whole-suite pass:** I was instructed not to run it. I rely on the ledger's 1896/0.
- **`inspect_integration.py` runtime behaviour:** it was only import-checked. `news-brief-6u6`
  owns a smoke test.
- **`.beads/issues.jsonl` contents:** it is a passive export, and I only skimmed it.
- **Performance of the Step 7 self-join SQL at production row counts:** it is a one-off operator
  query.
- **Historical passages in memory and docs** (for example memory line 66's "unreachable while
  version is 1", and R9 in the phase-1 implementation record): these are dated history, not live
  claims.

## Recommendations

1. **One follow-up commit, before the push if convenient**, and in any case before
   `COMPREHEND_ENABLED` flips:
   - I1: null `new_label` counts as absent, plus its test;
   - M1: re-declaring an orphaned label is a duplicate, plus its test;
   - M2, M3 and M4: the comment and dead-code sweep;
   - M5: the runbook wording;
   - optionally the T4 one-line assertion.

   Pre-register the mutation counts as 1 (I1) and 1 (M1). Run the three-command gate with the DB
   exported.
2. File beads for the "can defer" rows: T4 check-order pinning, T4 counter inflation and
   duplicates, T6 runbook actions and query, and the M7 prompt scoping or `unmapped_candidate`
   watch. Append T4's `inspect_integration` crash to `6u6`.
3. When Amendment 3 is written, say explicitly that two new sources of corroboration arrive
   together at the phase-1 flip: in-request links and entity-less `EVT` matches. Say also that
   the runbook's `entityless_reference_written` log sum is the only way to tell them apart.

## Assessment

**Ready to merge? With fixes.**

The architecture is sound. Across the tasks:
- the parser's output matches what the writer reads, including a label on a matched event;
- declarations commit only when the declarer's savepoint commits;
- the counter identity survives every path I traced;
- the deploy is inert with comprehension off.

Nothing here can harm production while the flag is off, so pushing as-is would not break
anything. But two behaviours would go live at the flip and should not:
- **I1:** a null `new_label` retires items, against the repo's own bqa.16 rule;
- **M1:** a re-declared orphaned label can write an assertion on the wrong event, which inflates
  the number the gate reads.

Both are one-clause fixes with a single-count mutation each. The remaining must-fix items are
comments and doc wording that an operator will act on. Land them in one follow-up commit, run the
gate, and this is ready.

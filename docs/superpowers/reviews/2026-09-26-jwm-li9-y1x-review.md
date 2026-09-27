# Review: uncommitted changes (jwm, li9, y1x), 2026-09-26

Test run: `py -m pytest -q tests/test_replay_haiku.py tests/test_score_comprehension.py` against the
local Postgres. Result: 60 passed, 0 skipped, REAL_EXIT=0. The DB-backed tests ran.

A scratch probe was written to reproduce finding B1 against Postgres. It was NOT run: the
permission classifier blocked it, because its first draft reset the schema. So every finding below
is verified by reading the code, except where a passing test already shows the behaviour. For B1's
entity half, a passing test does show it (see B1).

---

## BLOCKER

### B1. The replay offers a batch the entities and events that its OWN members created: future information leaks into the offer

**Where**
- `scripts/replay_haiku.py:272-290` (`index_as_of`, filter `births[...] < batch.as_of`)
- `scripts/replay_haiku.py:489-491` (`integration_candidates(..., as_of=b.as_of)`)
- `comprehend.py:1654` (`e.created_at < coalesce(as_of, now())`)

**What goes wrong**
`as_of` is the ANCHOR's capture time, but a batch also holds up to 4 items captured up to
`RT_WINDOW_HOURS` (1h) before the anchor. The filters that use `as_of` do not exclude what those
items produced:

- **Entities.** `entity_births` dates an entity by the capture time of the earliest item asserting
  one of its events. So an entity first mentioned by a batch member is "born" before `as_of` and is
  offered as an `ENTn` to the batch that contains its creator. In the simulated real-time
  micro-batch that entity does not exist yet: the creator is being integrated in this very call.
- **Events.** `candidate_events` filters by `events.created_at`, which is historical integration
  time. The historical pipeline ran hourly, newest first (`pending_integration` sorts `id DESC`), so
  a batch member captured at 10:05 was typically integrated at the 10:13 pass. That creates its
  event E at 10:13, which is before an anchor captured at 10:40. E is then offered to a batch that
  contains the item whose extraction wrote E's summary.

**Consequences**
- That item "matches" its own event almost for free, in A, A' and H alike. This inflates both
  agreements toward 100% and compresses the A-H vs A-A' difference.
- The rule's tolerance is an absolute 5 points, so the compression biases the verdict toward
  HAIKU QUALIFIES.
- It also changes the pair-stratum task. When the earlier item sits inside the batch, a real-time
  production call has to make that link through an in-request NEW label. The replay offers it as an
  EVT match instead.
- The script prints both "earlier item's event OFFERED" and "earlier item INSIDE the batch". These
  counts are not exclusive, and the overlap is exactly this leak. Nothing refuses on it.

**Evidence the leak is live**
`tests/test_replay_haiku.py:368-373` asserts it as intended behaviour. The pair batch anchored on
`ids[4]` (T0) contains `ids[3]` (T0-30m), because `batch_around` takes everything within 1h.
Moldova's only birth evidence is `ids[3]`'s assertion. The test asserts `"ENT1 Moldova"` is offered.
In that test the event half is masked only because `events.created_at` defaults to the wall clock
(2026-09-26), which is after T0.

**Fix**
Set the cut-off per batch at its EARLIEST member, not the anchor:
- **Entities.** Filter `births[e] < min(it["created_at"] for it in b.items)`. `batch_around` takes
  consecutive material items, so every material item captured in [floor, as_of] is a batch member.
  Anything born in that interval was born of the batch.
- **Events.** Pass `as_of = min(b.as_of, min integrated_at of the batch's items)`, using
  `item_triage.integrated_at`. This excludes every event a batch member created, since such an event
  was created at that member's integration. It keeps events a member merely MATCHED, which existed
  before its integration. It also keeps calling the unchanged production function.

Add a test with a batch member whose event was created before the anchor's capture, and assert
that the event is NOT offered. Invert the `ENT1 Moldova` assertion to match.

---

## MAJOR

### M1. `entity_births` dates an entity from items that never mentioned it, so entities created AFTER `as_of` are offered

**Where**
- `scripts/probe_clustering.py:401-410` (`least(en.created_at, min(i.created_at))` over
  `event_entities` → `assertions` → `items`)
- `comprehend.py:2581-2586`: `write_extraction` inserts `event_entities` for the matching item's
  entities when it matches an EXISTING event (the `candidate_id` or `new_ref` branch).

This is pre-existing code, but y1x now uses it for a paid, binding verdict.

**What goes wrong**
1. Item i1 (captured t1) creates event E.
2. Later, item i5 (captured t5, after the anchor) matches E and introduces a NEW entity X. That
   writes `event_entities (E, X)`.
3. `entity_births` then gives X the birth `min(created_at of items asserting E)` = t1, even though
   X's row was inserted after t5.

So for any anchor between t1 and t5, X passes `births[X] < as_of` and is offered: an entity from
the future. It also widens `entity_ids`, and therefore which events `candidate_events` can reach.

This path is common. It fires whenever a new actor first appears in an item that corroborates an
existing event, and hub events (Iran and the like) gather many such attachments.

The narrowing control (`narrowing_disagreements`) cannot see this. It applies the same `births` to
both sides.

**Fix**
Date an entity only from items that actually name it. One way: for each entity, take the
min `created_at` over asserting items whose text `form_matches` one of the entity's surface forms,
floored at nothing and capped at `en.created_at`. A cheaper conservative alternative is to fall
back to `en.created_at` for entities whose earliest asserting item does not match any of the
entity's forms.

Then add a probe in the dry run: count offered entities whose `en.created_at` is after the batch's
`as_of` AND whose birth item does not name them.

---

## MINOR

### m1. The "hard" $10 cap does not account for every billed call

**Where:** `scripts/replay_haiku.py:319-335`, `353-359`, `569`

**What goes wrong**
- **Retries.** `call_one` retries a transient failure once, so a batch can make up to 6 calls. The
  guard at `:553` reserves the worst case of only 3.
- **Timeouts.** `out.usd` is set only from a successful response. A first attempt that failed with
  a read timeout (`COMPREHEND_INTEGRATE_TIMEOUT` = 300s) may still have been generated and billed
  server-side, and it is never added to `spent`. In a run where one model times out persistently,
  `spent` stays near $0 while billing continues.
- **Abort.** On `Abort`, `run_batch`'s `f.result()` raises for one run. The executor's `__exit__`
  still waits for the sibling calls, which complete and bill, but their `usd` is discarded (`:569`
  is skipped by `break`). The printed "spend" then understates.

**Fix**
- Reserve `2 * worst` per run while retries are enabled.
- On a transient failure, add the worst case for that attempt to `spent`. The actual cost is
  unknown, and a cap should assume the bill.
- In `run_batch`, gather all futures before re-raising, so completed siblings' `usd` is recorded.

### m2. Parse-failure counts include batches that are excluded everywhere else

**Where:** `scripts/replay_haiku.py:570-577`

`parse_failures[r]` is incremented before the transport-incomplete check. So "whole-response parse
failures" counts batches that `done` excludes from every drop and agreement denominator. The
reported count therefore cannot be reconciled against `drop`.

**Fix:** count only for batches appended to `done`, or print both counts.

### m3. The decision encoding is directional, so the same clustering can score as disagreement

**Where:** `scripts/replay_haiku.py:194-212`

A link is encoded as `("LINK", declarer)` on the referencing item, and the declarer's own set gets
nothing.

- **Reversed order.** If A has item 5 declare and item 4 reference, while H has item 4 declare and
  item 5 reference, the partition {4,5} is identical but both items count as disagreements.
- **Declared on an existing event.** A declarer can put `new_label` on a `candidate_id` event,
  which the parser accepts. Then "4 -> LINK(5), 5 -> EVT X" and "4 -> EVT X" also differ, although
  both attach 4 to X.

Rare while models follow listing order, but the effect is asymmetric if Haiku reorders more.

**Fix:**
- Resolve a LINK whose declarer event is a `candidate_id` to that EVT.
- Consider scoring same-cluster membership rather than the directed edge.
- Either way, report how many items carry a LINK in each run.

### m4. The pair stratum is selected by historical Sonnet's own linking decisions

**Where:** `scripts/replay_haiku.py:160-191`, via `cross_outlet_pairs`

Pairs are defined as two items that production (Sonnet, under older prompts) asserted onto one
event. Conditioning on Sonnet having matched raises Sonnet's model-specific match propensity on
those items, which favours A-A' over A-H. This is the "eval set selected by the same signal"
trap (analysis-stats-traps).

It was pre-registered by the operator and the random stratum is printed separately, so this is
flagged, not ruled.

**Fix:** state it beside the per-stratum lines. If pair and random disagree in sign, the random
stratum is the unconfounded one.

### m5. `earlier_id` is nondeterministic across runs

**Where:** `scripts/replay_haiku.py:172`

`cross_outlet_pairs` has no ORDER BY, and `later_of.setdefault` keeps whichever pair arrives first.
For a later item paired with several earlier items, `earlier_id` can differ between the dry run
and the paid run. Only the "OFFERED / INSIDE" diagnostic uses it, and anchors stay deterministic
because the list is sorted.

**Fix:** sort `pairs` (or add ORDER BY) before building `later_of`.

### m6. Tests assert less than their names claim

**Where:** `tests/test_replay_haiku.py:148-157` and `tests/test_score_comprehension.py`, the li9 block at the end of the file

- `test_the_spend_guard_bounds_a_call_that_uses_its_whole_output_budget` compares
  `worst_case_usd` against an estimate built from the same request with a smaller divisor. It is
  close to tautological. Nothing tests the loop's cap: that the run stops before
  `spent + worst > cap`, and that `STOPPED EARLY: spend cap` is printed.
  **Fix:** monkeypatch `SPEND_CAP_USD` to about 1.5 batches' worst case, then assert the number of
  calls and the STOPPED line.
- `largest_gap`'s docstring claims "earliest first on a tie". No li9 test has two equal gaps.
  **Fix:** add one.

---

## Checked and found correct

- **y1x extraction.** `integration_candidates` is byte-for-byte the old inline code plus the `as_of`
  pass-through (default None, which gives production's `now()`). `run()` passes the same mutable
  `index`, so later `add_entity` calls behave as before. `tally.candidate_cap_hit` is unchanged
  for `run()`. The only change in `inspect_integration` is that its throwaway Tally now counts cap
  hits, which is harmless.
- **Mirroring production.** Items are id DESC. Quote pages are dropped (`material_items` calls
  `common.is_quote_page`). The payload shape is `dict(it, outlet=...)`. The request comes from
  `build_integration_request`, and only `model` differs across A, A' and H (the e2e test pins
  this). Labels come from the same `label_map`. `parse_integration_response` is the same function.
- **Thread safety.** `_post_messages` uses a bare `requests.post` with no shared Session.
  `common.X` goes through `config._cached`, which is lock-guarded and opens its own connection per
  refresh. The request dict is shared read-only. The lazy `import brief` is protected by the import
  lock. No findings.
- **Agreement and drop arithmetic.** The denominator is shared (all three kept). Neighbour faults,
  rejected rows, missing items and whole-response parse failures all land in the drop.
  Transport-incomplete batches are excluded from both. `decide`'s inclusive boundary is rounded
  correctly.
- **li9 `largest_gap` SQL.**
  - An empty cohort gives `max()`=NULL, which is filtered, so it returns None.
  - A single event yields only the tail row.
  - `lag` over created_at with duplicates gives zero gaps.
  - The lead gap is excluded as designed.
  - Both bounds are timestamptz.
  - The strict `>` matches zp8's strict `<`: at exactly H, both pass.
  - Ordering: zp8 runs first, then li9.
  - `total > 0` implies a non-None gap, because every event has its creating assertion.
  - The tail-after-restart case is covered by
    `test_a_hole_at_the_cohort_tail_is_not_measurable_even_after_a_restart`.

---

## Disposition (2026-09-26, same session)

Every finding was verified against the code and fixed. None was declined. Each fix was then
mutation-checked: 9 mutations, 9 failure counts matching pre-registered predictions.
- **B1:** `Batch.cutoff` is the earliest member's capture time, and it is used for both
  entities and events.
- **M1:** `naming_births` replaces `entity_births` in the replay. The spike's own use is left
  as published; its bias direction is recorded in the spike result doc.
- **m1:** a reserve for every attempt, failed attempts charged at their worst case, and
  siblings counted on abort.
- **m2:** parse failures are counted over complete batches only.
- **m3:** order-free cluster encoding; a label declared on an existing event resolves to it.
- **m4:** the bias is printed beside the strata.
- **m5:** pairs are sorted.
- **m6:** a cap-loop test, a failed-attempt charging test, and a `largest_gap` tie test.

# Red-team: comprehension cost redesign, phase 2 plan

**Plan:** `docs/superpowers/plans/2026-09-25-comprehension-cost-phase-2.md`
**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md` §5, §6.2
**Reviewed against:** `comprehend.py` at `c6a116e` (1742 lines), `config.py`, `common.py`, `brief.py:2790-2935`,
`scripts/score_comprehension.py`, `tests/test_comprehend_integration.py`, the phase-1 plan's
interfaces, and the claude-api skill's `python/claude-api/batches.md`.
**Method:** reading only. No network, no DB, nothing executed. Each claim is labelled
**VERIFIED** (read in code or plan text, line cited) or **INFERRED**.

---

## (a) Batching removes a same-pass visibility the real-time pipeline already HAS, and the plan puts back only the part that title trigram ≥ 0.35 can find. Nobody has measured that part.

**What the gate counts (VERIFIED).** `corroboration_by_outlet` (`score_comprehension.py:191-201`)
counts `count(DISTINCT i.outlet_id)` per event, over assertions with
`a.created_at <= e.created_at + horizon`. An in-request `NEW` link writes the second assertion in
the same outer transaction as the event (`write_batch`'s `conn.transaction()`, `comprehend.py:1741`),
so it shares `now()` and passes the horizon. **When a link fires, the gate counts it.** The
mechanism is sound. What is in doubt is how often it fires.

**What real time already sees (VERIFIED).** `run()` commits after every micro-batch
(`comprehend.py:550`). `candidate_events` filters `e.created_at < coalesce(as_of, now())`
(`:1037`), where `now()` is the start of the *current* transaction. Entities created mid-pass are
pushed into the index (`:1644`). So micro-batch *k* of a pass is offered the events that
micro-batches 1..*k*−1 created in the same pass. Today the only blind pairs are two items that
land in the **same 5-item micro-batch**, which is exactly the floor spec §2.3 describes ("Pairs
whose items fell into the same micro-batch of 5 could never be matched").

**What phase 2 does (VERIFIED from the plan text).** Task 5 Step 7.3 builds every request's
candidates at submit time, before any of them has run. Requests in one submission are therefore
**mutually blind**. The blind set grows from "same micro-batch of 5" to "same submission"
(`COMPREHEND_MAX_ITEMS` = 300, `common.py:268`). A pass that waited behind an in-flight batch holds
two hours of material. The only thing that restores visibility inside a submission is Task 4's
grouping:

- the only edge is `similarity(a.title, b.title) >= 0.35` (Task 4 Step 3, `similar_pairs`). There
  is no entity edge, even though candidate retrieval itself works *by entity*
  (`comprehend.py:1032-1034`);
- it is **seed-star, not transitive**. `cluster` adds only the seed's direct neighbours, so with
  A–B 0.5, B–C 0.5 and A–C 0.3, C is left out;
- it is **capped at 8**. A story reported by more than 8 outlets in one hour is split. Each split
  mints its own `NEW` event, and those events cannot see each other.

The spec itself calls 0.35 "uncalibrated" (§5.3), and the plan's comment argues that a wrong
grouping "costs a slightly larger prompt, never a false merge". That covers false positives only.
A **false negative**, meaning two outlets' headlines about one event scoring below 0.35, costs
exactly the corroboration the phase exists to keep. The `candidate_events` docstring
(`comprehend.py:977-982`) records that true same-event pairs populate the lowest lexical bands.
That was measured title-vs-summary, not title-vs-title (INFERRED as a warning sign, not a
measurement).

**Consequence.** The plan's Goal line ("without losing the third of corroboration that arrives
within two hours") is asserted, not tested. No test, runbook step or pre-registered number
measures grouping recall. Runbook step 4 checks `events_linked_in_request > 0`, which proves only
that the mechanism is alive, not that it replaced what was lost. The gate is binding and has no
appeal (Amendment 2 spent the one void). If the new pipeline scores below the 10% floor because
the grouping was too narrow, the verdict falls on the event layer rather than on this plan.

**Fix (before Task 4 is written).**
1. Pre-register and measure grouping recall on the KB you already have. Take every multi-outlet
   event and every pair of its assertions from different outlets whose items were published
   within 2 h of each other. Report the share with `similarity(ia.title, ib.title) >= 0.35`, and
   the share that also survives seed-star with a cap of 8. This sample is biased toward pairs the
   old matcher found, so the figure is a **ceiling** on recall, not a floor. State the ceiling
   accordingly.
2. If that recall is not near 1, add a second grouping edge, **shared `SurfaceIndex` entity hit**
   (already computed per item in `run()`, `:420-425`), and use capped connected components rather
   than seed-star.
3. Make runbook step 4 compare against real time, not against zero. For example, measure the share
   of post-flip multi-outlet events whose two assertions share a `batch_id`, next to the phase-1
   within-pass share.

Also verified, same family: when a declaring item fails validation, the referencing items are
dropped with `validate:new_label_undeclared` (Task 3 Step 1, `test_a_dropped_declarer_does_not_declare`).
`run()`'s dropped-charging (`comprehend.py:529-545`) then **charges each of them an attempt**, and
the plan moves that charging verbatim into `_apply_extraction` (Task 5 Step 8.3). That contradicts
the plan's own rule in Task 3 Step 6: "UnresolvedNewLabel is a fault in the item's neighbour ...
it stays pending and uncharged". The rule is enforced at the write layer and violated at the
validation layer. In a cluster of 8, one malformed declarer puts a strike on seven innocent
neighbours, and strikes are a one-way door at 3. Fix: return undeclared-reference drops separately
from `parse_integration_response`, and exclude them from `dropped`, or defer them through the
`integrate_defers` budget.

---

## (b) The plan treats the budget as if it were transactional with `comprehend_batches`. It is not: `Budget.debit` commits on its own connection. And collect credits first, then applies, and is not idempotent.

**VERIFIED:** phase 1's `Budget.debit` calls `config.set_runtime_state` (phase-1 plan, `Budget`
block). That function opens **its own connection and its own transaction** (`config.py:394-402`,
`with db.connect() as conn: with conn.transaction(): ... INSERT ... ON CONFLICT`). A budget write
therefore commits immediately and independently of the pass's `conn`.

The plan's ordering assumes otherwise:

- **Submit (Task 5 Step 7.7):** "INSERT the row, `budget.debit(reserved)`, commit." The debit is
  durable before the row. This is a small window, but a death between the two leaves a debit with
  no row.
- **Collect (Task 5 Step 8.3):** "`budget.debit(-reserved_usd)` **first** (release the
  reservation), then for each result ...". Step 8.4 marks the row `collected` only at the end. In
  between:
  - `batch_results` is a **streamed network download** that sits outside any `try`. Step 8.1 wraps
    only `batch_retrieve`.
  - `_apply_extraction` inherits the real-time block's per-batch `conn.commit()`s
    (`comprehend.py:475, 520, 550`), so applied requests are committed one by one.
  - `run()` catches only `_AccountAbort` (Step 9). Anything else (a `ChunkedEncodingError` or
    `ConnectionError` mid-stream, a DB error, `UnpricedModel` if `PRICES_PER_MTOK` was edited under
    an in-flight manifest) propagates out of the pass.

**Consequence of one interrupted collect (VERIFIED by tracing the plan's steps):** the row stays
`in_flight` and the credit is already durable. The next pass credits `reserved_usd` **again** and
re-applies every result:
- `record_spend` inserts **duplicate ledger rows**. `comprehend_spend` has no `custom_id` column and
  no uniqueness on `(batch_id, …)` (phase-1 `0015`).
- `dropped` items and `invalid_request_error` items are **charged a second attempt from one
  verdict**.
- `integrate_defers` is bumped again for unparseable results.
- Succeeded items are safe only because of `write_extraction`'s `already` guard.

The net effect is that each interrupted collect mints roughly `reserved − spend already applied`
of budget that was never accrued. The bucket is the control that stands between this pipeline and
the £15/day top-ups (§1). The host has a recorded network-fault history: the ~90 s DNS fault cited
at `comprehend.py:206-207`.

Review Focus 3 ("a failed submit leaves no row and no debit") is correct only because a failed
`batch_create` happens before any write. Nothing in the plan pins the collect-side equivalent.

**Fix.**
1. Stop modelling the reservation as a debit and credit on a side-channel. Derive it: the effective
   balance is `bucket − sum(reserved_usd) WHERE status='in_flight'`, read in the pass's own
   transaction. Only real spend is ever debited.
2. Make collect idempotent per request. Add `custom_id` to `comprehend_spend` with
   `UNIQUE (batch_id, custom_id)`. Apply each request, write its ledger row and debit its spend in
   one transaction, and skip `custom_id`s already in the ledger on a re-collect.
3. Flip `status='collected'` in the same transaction as the last request.
4. Wrap `batch_results` iteration so a transport fault means "wait" (row stays `in_flight`,
   nothing credited, nothing charged).
5. Add a test with a fake whose `results_iter` raises after the first result, run twice. Assert
   the balance, the ledger row count and `integrate_attempts` equal a single clean collect.

---

## (c) In 6 months: a batch the API no longer knows (404) stalls comprehension permanently and silently. The plan's own 25 h expiry cannot fire in that case, and its test cannot see it.

**VERIFIED from the plan text.** Review Focus 4 names the case exactly: "a row with status
`in_flight` whose id the API returns 404 for". But Task 5 Step 8.1 says: "`batch_retrieve(batch_id)`
inside `try` ... **any other exception → set `tally.batch_in_flight = True` and return (wait)**."
The 25 h check lives in Step 8.2, **after** a successful retrieve. A 404 raises `HTTPError` from
`raise_for_status()` and returns at 8.1 on every pass, forever. The pinned test
(`test_a_batch_unresolved_past_25h_expires_and_requeues`) uses "the fake still `in_progress`",
which never takes the exception path. Review Focus 4 will be ticked as covered while its named
case is broken.

Then nothing moves (VERIFIED from the plan text):
- `submit_batch` stops on the in-flight row (Step 7.1), backed by the partial unique index;
- the real-time fallback is skipped whenever `tally.batch_in_flight` (Step 9);
- no alert watches `comprehend_batches`. The budget never exhausts, because nothing is spent, and
  `retirement()` sees no strikes.

The KB freezes. The first instrument to notice is the gate's `zp8` "corpus is FROZEN" refusal
(`score_comprehension.py:266-300`). If that happens inside the Amendment 3 window, it spends the
7-day cohort, and `news-brief-li9` (continuity) is not built.

**How it happens within 6 months:**
- **Most likely, VERIFIED path:** `run()` returns before *anything*, collect included, when
  `COMPREHEND_ENABLED` is false (`comprehend.py:309-312`). The plan's rollback reasoning covers
  only `COMPREHEND_BATCH_ENABLED`. On 2026-09-25 the operator flipped `COMPREHEND_ENABLED` off at
  exactly the moment the account emptied (spec §1), which is the moment a batch is most likely in
  flight. Batch results are retained for 29 days (skill `batches.md`, "Results available for 29
  days after creation"). A pause longer than that, or any pause that outlives the batch object,
  turns re-enabling into a permanent stall. That the batch object or `results_url` then returns
  404 is INFERRED from the retention statement, not verified.
- **INFERRED, unverified:** rotating `ANTHROPIC_API_KEY` to a key in another workspace. Batches are
  workspace-scoped objects, so the old id would 404.
- **VERIFIED:** a `results_url` fetch failure in Step 8.3 is also outside any `try`, and it is not
  covered by the 25 h rule either.

**Fix.**
1. Evaluate age first, independent of the API. If `now() − submitted_at > 25 h`, expire, requeue
   uncharged and release the reservation, whatever `batch_retrieve` returns. Treat a 404 from
   retrieve or results as terminal immediately.
2. Add an episode-keyed liveness alert on `comprehend_batches` (in-flight age above 26 h), copying
   the `capture.liveness` contract.
3. Decide deliberately whether collect runs when `COMPREHEND_ENABLED` is false. Collecting only
   writes what was already paid for, which argues for yes.
4. Test with a fake `retrieve` that raises `HTTPError(404)` on a row aged 26 h.

---

## Additional verified defects

1. **Task 3's rolled-back-declaration test does not exercise its own docstring, and its
   pre-registered mutation count will come back 0.** The fixture raises on the *first*
   `_resolve_entity` call. Entity resolution runs **before** the event INSERT
   (`comprehend.py:1639-1646` versus `:1665`), so item a rolls back before any event row or
   `declared_here` entry exists. The mutation in Task 3 Step 8 (record into `new_events` inside the
   savepoint) therefore changes nothing, and the pre-registered "1 fails" reads 0. Inject the fault
   **after** the event INSERT instead, for example on the assertion INSERT or on a second event.
   Separately, `_attempts(kb, b)` does not exist; the module's helper is `_strikes` (`:1083`).
2. **"Drop requests from the END (the oldest clusters)" drops the newest singletons first.**
   `request_groups` returns `clustered + chunk(singles)` (Task 4 Step 3), so the tail of the list is
   singleton packs, including the newest singles. They are shed before any cluster, however old.
   That contradicts spec §5.8 ("dropping the oldest clusters first") and D4 (newest-first). Sort the
   groups by their newest member id before trimming.
3. **`tally.batch_submit_failed` is set (Task 5 Step 7.6) but is not among the `Tally` fields the
   task declares.** On a non-slotted dataclass the assignment succeeds, but the attribute is
   **excluded from the dataclass repr**, which is the `Comprehend: {tally}` log line
   (`comprehend.py:558`). A failed submit is therefore invisible in the log. Declare it as a field.
4. **The errored-type value is not what the plan compares against, per the very doc Task 5 Step 1
   mandates.** The skill's `python/claude-api/batches.md` branches on
   `result.result.error.type == "invalid_request"`. The plan charges only on `invalid_request_error`.
   Step 1 asks the implementer to record the *nesting*, not the *value*. The "anything else" branch
   is uncharged and has **no ceiling**, unlike real time's `integrate_defers` cap. So if the value
   is misread, every refused request is deferred forever: re-reserved and re-submitted every pass,
   never retired, and unbilled. Which string the wire actually carries is UNVERIFIED offline. Step 1
   must record the value, and the uncharged branch needs a defer budget.
5. **402 behaviour at batch create is unverified.** The plan assumes an empty balance surfaces as a
   402 on `POST /v1/messages/batches` (the `test_a_402_at_submit_aborts_the_pass` shape). Nothing
   read here documents whether billing is refused at creation or per request as an `errored`
   result. If it is per request, those results fall into the uncharged, uncapped branch (item 4),
   and no `_abort` or billing alert fires. Confirm before relying on phase 1's `0rg` protections
   through this path.
6. **Task 1 Step 5's mutation target is ambiguous.** The `OR integrate_prompt_version < %s` clause
   appears in `pending_integration` **and** in `retirement()` (`comprehend.py:596`). The guard in
   `write_extraction` has a different shape (`>= %s`, `:1582`). The test being rewritten
   (`tests/test_comprehend_integration.py:565`) calls `write_extraction`, not `pending_integration`.
   Unless the rewrite calls `pending_integration`, "restoring the clause fails 1" has nothing to
   fail. No test pins `retirement()`'s new predicate either: the existing at-risk tests
   (`:1107`, `:1118`) run at the current version and pass under either predicate.
7. **Submit is not crash-atomic with the API.** A process death after `batch_create` returns and
   before the row commits leaves a billed batch with no row. Its items are submitted again next
   pass, so the spend is doubled. It is low probability, but it is cheap to close: insert the row
   with a placeholder status before `batch_create`, in the same pattern as the `integrate_defers`
   charge-first ordering.

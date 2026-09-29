# Red team, revision 6: gold set by window census (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md`, revision 6
**Prior reviews:** `...-redteam.md` (rev 1), `-r2.md` to `-r5.md`. Objections that rev 6 resolved
are not repeated.
**Stance:** hostile staff engineer, asking whether this is the wrong thing to build.
**Evidence rules:**
- **Verified** means I read it in the repo at the cited line.
- **Computed** means arithmetic I ran with `py` and scipy (inputs named inline).
- **Estimate** means arithmetic on assumed inputs, which I name.

No production data was queried.

---

## Verdict

Rev 6 applied r5 cleanly where it applied it:
- **The two-stage machinery is gone.** All 16 windows are labelled blind up front, with one test
  at 0.05. This deletes r5 blockers 2 and 3 and objection (a).
- **The §4.4 table is right for the formula it states.** I reproduced every cell. For example,
  m̄ = 8, CV = 0.5 gives 22.6 / 25.8 / 36.0, and m̄ = 6, CV = 0 gives 24.2 / 26.5 / 34.2
  (**computed**). The control of 30.5 also reproduces. `t₀.₉₇₅,₁₅ + t₀.₈₀,₁₅ = 2.998`.
- **The precision sample is fixed** (r5 blocker 5): groups are drawn without replacement, one
  pair per group, with a group-weighted estimator and `CHECK (item_a < item_b)`.
- **The gap check's population is fixed** (r5 blocker 6): the ranking restriction is dropped,
  with the right reason.
- **Retention is now code, not a bead note.** The `uh0` note is superseded to match
  (**verified**, `.beads/issues.jsonl:153`).
- **The r5 "not blocking" items are all taken:** stratum refusal, deploy date by recognition,
  both window values printed, and the `brief.py` mode caveat.

What is wrong now is mostly what rev 6 **added**:
1. The test and the reported number estimate **different things**. The test is also undefined
   for a window with no multi-outlet group, which is plausible (objection a, blockers 1 and 2).
2. The adjudicated partition corrects operator errors **in one direction only**, and merges
   **transitively from single pair rulings**. Under a recall-only primary test, that means an
   over-linking system cannot lose. Sampling makes the construction undefined (objection b).
3. The cumulative partition turns the only scored set into a development set over the months of
   sub-project 1 (objection c).

**Not ready for an implementation plan.** Four blockers are listed below. Each is a paragraph of
spec text, not a redesign, but all four are **pre-registration content**. A plan written against
the current text would leave choices open until after the data exists.

---

## Objection (a): a simpler design. Use one estimand: a window-level randomization test on the pooled difference that the spec already reports.

§4.4 now reports three things.

| Role | What the spec uses | Estimand |
|---|---|---|
| **Test** | Paired t on "the per-window difference in the confirmed share"; windows unweighted; 15 df | The **mean of window shares** (each window counts 1/16) |
| **Point estimate** | "The pooled group-level share is reported as the point estimate beside it" | The **group-weighted** share (each window counts m_k / Σm) |
| **Interval** | "§8's intervals are the same t interval" | Centred on the **unweighted** mean |

So the printed point estimate is not the centre of its printed interval, and it can fall outside
it. The two estimands diverge exactly where the spec says the action is.

§4.1 has no size cap *"because busy hours are where retrieval fails"*. Take a gain concentrated
in busy windows, for example 4 windows with m = 16 and Δ = 0.25, and 12 windows with m = 5 and
Δ = 0 (**estimate**, illustrative inputs):
- pooled difference **12.9 points**;
- unweighted difference **6.2 points** (**computed**).

The test divides the effect the spec expects by two before testing it, and the table beside it
reports the undivided number.

**The DE formula belongs to the other estimator.** `DE = 1 + ((1 + CV²) m̄ − 1) ρ` is the design
effect of the **pooled** (ratio) estimator. For the unweighted mean of window shares, the variance
is `d/K · mean_k[(1 − ρ)/m_k + ρ]`. Unequal sizes then inflate the within-window term through
E[1/m], not the between-window term through (1 + CV²). I computed the MDE under the estimator
the test actually uses, with m_k negative binomial at the stated mean and CV, conditioned on
m_k ≥ 1 (**computed**):

| m̄ | CV | §4.4 (pooled DE) | Unweighted estimator |
|---|---|---|---|
| 8 | 0.5 | 22.6 / 25.8 / 36.0 | 24.5 / 26.8 / 34.4 |
| 10 | 0.5 | 21.0 / 24.6 / 35.4 | 22.6 / 25.1 / 33.4 |
| 8 | 0.8 | 23.7 / 27.9 / 40.3 | 27.7 / 29.6 / 36.1 |

(ρ = 0.05 / 0.1 / 0.3.) The numerical gap is small at CV ≤ 0.5. It is not small at CV = 0.8. A
time-of-day stratification with a ≥ 40-item floor (§4.2) makes CV = 0.8 plausible: night windows
near the floor, day windows at ~250 items.

§10's "the DE and achieved-difference formula reproduce the §4.4 table" will pass either way. It
tests the formula against itself, not against the estimator (`the-probe-measured-the-wrong-layer`).

**Windows with no multi-outlet group have no share** (blocker 1). The t-test then loses a degree
of freedom, and §4.4's "15 degrees of freedom" is a promise nothing enforces:
- the go/no-go gates the **mean** m̄ ≥ 8, not each window;
- the unsure rule (§5) can drop a group to one outlet;
- with negative binomial m_k: P(at least one of 16 windows has m_k = 0) is 6% at m̄ = 8,
  CV = 0.5; **50% at CV = 0.8**; 62% at m̄ = 6, CV = 0.8 (**computed**);
- if the 00–06 UTC stratum averages 2.5 multi-outlet groups (Poisson), P(at least one of its 4
  windows is empty) is **29%** (**estimate**).

**The simpler design.** One estimand everywhere: the **pooled** group-level difference, which is
what "was each multi-outlet event connected?" (§3) means.
- **Test:** a paired **sign-flip randomization** over windows. The statistic is
  `T = Σ_k s_k (a_k − b_k) / Σ_k m_k`, where a_k and b_k are the confirmed counts and s_k = ±1.
  Enumerate all 2¹⁶ = 65,536 sign vectors. The test is exact, with the smallest two-sided p at
  2/65,536.
  - An empty window contributes zero to both sums, so no rule is needed for it.
  - Busy windows weigh what their groups weigh.
  - The §4.4 DE formula is already this estimator's, so the sizing table stays.
- **Interval:** invert the same test, or take a window-cluster t on the ratio estimator as the
  printed approximation.

**What it deletes:**
- the 15-df promise;
- the zero-window rule;
- the t-quantile mutation (replaced by a mutation on the sign enumeration);
- the point/interval mismatch.

**Cheapest alternative:** keep the paired t and report the **unweighted** mean as the point
estimate. It is coherent, but it tests the quiet-hour-weighted quantity, which §4.1 argues is the
wrong one.

---

## Objection (b): an assumed requirement. That pair-level pool rulings can be folded into the blind partition one-sidedly and transitively.

§4.4 defines the headline partition as *"blind groups, plus every pool-adjudicated 'same' (§6.4)
merged transitively"*. Four things follow that the spec does not face.

**1. The correction runs in one direction, and the primary test rewards that direction.**
- §6.4 pools two kinds of disagreement:
  - links a system made that the blind partition left apart;
  - "pairs the blind partition joined that any system left apart".
- Only "same" rulings enter the partition. A pool **"different"** on a blind-joined pair is
  collected and then **discarded**. So is a census `precision` "different" (§6.4).
- So operator **misses** are repaired in favour of the system that linked them. Operator **false
  joins** stay, and they count against the system that correctly kept those items apart.
- "Confirms" (§4.4) is recall-only: two items of a group, from two outlets, in one event.
- A system that links more therefore gains on both sides:
  - every "same" it proposes becomes a group it confirms **by construction**;
  - every blind false join is a group it also "confirms";
  - its wrong links cost nothing in the headline.
- The degenerate system, one event per window, scores 100% on the pre-registered primary test.
- §4.4 says sub-project 1 "must pair the headline with a precision measure". But no precision
  condition enters the **decision rule**, and the decision rule is what this spec pre-registers.
  Rev 5's blind headline had the same recall-only test, but blind precision was symmetric noise.
  Rev 6 makes the partition itself move toward whichever system over-links.

**2. Transitive closure lets one pair ruling override a deliberate blind group decision.**
- Suppose the pool asks "x ~ y?" with x in blind group G1 and y in G2. If the answer is "same",
  G1 ∪ G2 becomes one group.
- But G1 and G2 may have been separated **deliberately**. The guideline's boundaries (cases 3, 4
  and 5: reaction, consequence, same-press-conference statements) produce exactly such adjacent
  groups.
- The pair question is asked out of group context. One borderline "same" then asserts that every
  member of G1 is the same occurrence as every member of G2.
- Chains of borderline rulings (strike, casualty report, updated toll, official statement) build
  mega-groups.
- A merge also rescores **uninvolved** systems. A system that confirmed G1 but not G2 moves from
  1/2 to 1/1.
- §10 mutation-tests that the transitive merge is **implemented**. Nothing tests whether it
  should happen.

**3. Sampling makes the partition undefined.**
- §6.4 samples the pool "if the pool is large, with the probability recorded" (`inclusion_prob`,
  §7). A partition built by transitive closure is deterministic in the pairs that happen to be
  sampled.
- No inverse-probability estimator composes with a transitive closure. Unsampled true-"same"
  pairs are simply never merged, so `inclusion_prob` has no consumer. This is r4 blocker 4's
  `1/p` problem, reintroduced.
- Pools can be large: a system that over-merges one 30-item event contributes 435 pairs.
- Under cumulative sampling, a later **novel** system's discoveries are adjudicated only at the
  sampling rate. Pairs that earlier-compared systems also proposed are already adjudicated. That
  biases the headline **against** novel retrieval, which is the thing §4.4 wants the
  adjudication-only share to evidence.

**4. The primary result depends on who else was compared.**
- The pool is "every cross-outlet link **any compared system** made", and the partition is
  cumulative.
- So the primary comparison's per-window shares change with whatever secondaries were declared
  beside it, and with every earlier comparison.
- Groups that only an absent system found are concordant misses. They do not bias the paired
  difference. But under objection (a)'s unweighted t they change m_k, and so each window's share
  and the t statistic.
- The pre-registered p-value of a primary then depends on a set the pre-registration does not
  fix.

**Also:**
- `UNIQUE (kind, item_a, item_b)` (§7) lets a `precision` "different" and a `pool` "same" coexist
  on one pair. "A pair is never asked twice" is false across kinds, and the spec does not say
  which ruling wins.
- Whether the pool UI hides which system proposed a pair is unstated.

**What I would do:**
- **Merge by group, not by pair.** When a pool "same" would join two blind groups that are both
  non-singleton, show both groups and ask "same occurrence?" once. That is recognition, per the
  operator's rule. Only a group-level "same" merges.
- **Apply both directions.** A pool "different" on a blind-joined pair removes that pair's
  support, and the group is re-scored on its remaining connected items. Alternatively, state in
  §4.4 why false joins may stand, and carry blind precision into the decision rule.
- **No sampling in the headline.** Adjudicate the pool fully. If it is too large, the comparison
  is over budget, and the spec says so. A sampled pool may feed only a separately-labelled
  estimate.
- **Pre-register a precision guard in the primary decision.** For example: "B beats A only if B's
  pool precision is not below A's by more than X". Otherwise the primary test can be won by
  over-linking.

---

## Objection (c): the 6-month collision. The cumulative adjudicated partition turns the only scored set into the development set, adjudicated with hindsight.

§4.7 says there is no held-out data, and that sub-project 1 "must not tune on the scored windows".
Rev 6 makes that sentence unenforceable.

**Pool adjudication is an error review of the scored windows.**
- Every comparison shows the operator each system's disagreements with his own labels, on the
  same 16 windows. That is exactly the failure list a developer tunes against.
- He is the developer: this is a solo repo, with commits straight to main.
- By the third comparison he has reviewed every system's mistakes on every scored window. The
  next design is shaped by them, whether or not anyone "tunes".
- The partition is cumulative (§4.4), so the gold labels are shaped by those same systems. The
  "adjudication-only" share becomes a record of which systems were tried, not of operator misses.

**The headline mixes two labelling regimes months apart.**
- **Blind labels** are made days after capture, under §5's "same occurrence ... at the same time"
  rule, with no hindsight.
- **Pool rulings** are made at scoring time, weeks to months later:
  - after the operator knows how each story resolved, which pushes toward "same" for items that
    later turned out to be one occurrence;
  - on links that have rotted or been paywalled. §5 tells him to open the link when unsure.
- r5 objection (c) raised this aging for stage 2. Rev 6 removed stage 2 but moved the aging into
  **every** headline through the adjudicated merge.
- The consistency repeat (§4.6) measures drift over ≥ 7 days, not over months.

**The retention hold then outlives its meaning.** It holds "for as long as the census exists,
which covers sub-projects 1 and 2" (§7). That is fine for storage: roughly 25 days × ~670 items
(**estimate** from §12). But it means the scored set is intended to be reused for as long as two
sub-projects run, with no retirement rule.

**What I would do:**
- **Freeze the adjudicated partition once.**
  - Sub-project 1 declares its whole first slate of systems before any scoring.
  - It adjudicates their union pool in one sitting, with system identity hidden and pairs
    shuffled.
  - The resulting partition is frozen as `adjudicated_v1`.
  - A later system is scored against `adjudicated_v1`. Its new pairs are adjudicated for a
    separately reported "found beyond v1" count, and they never re-enter the frozen headline.
  - This also removes objection (b)4 and the non-comparability of cumulative scores.
- **Record the adjudication date** on every `gold_adjudications` row. `created_at` already
  exists; state that it is used. Print the blind-vs-adjudicated gap as a function of adjudication
  lag.
- **Name a retirement rule for the 16 windows.** For example: after N comparisons, or after the
  first shipped change, a new census block becomes the scored set. Otherwise §4.7's "must not
  tune" is a sentence with no mechanism, which is `metadata-is-not-state` for a process.

---

## Correctness blockers (beyond the three objections)

1. **The test is undefined for a window with no multi-outlet group.** §4.4 fixes "15 degrees of
   freedom", but nothing guarantees every window has a share (objection a gives the odds: 6–62%
   across plausible m̄ and CV).
   - Pre-register the rule: drop the window and use K′ − 1 df, or score the window as a zero
     difference. The two give different p-values.
   - Objection (a)'s randomization test makes this moot.
2. **The point estimate and the interval estimate different quantities** (objection a). Either
   report the unweighted mean or test the pooled difference. As written, the readout can print a
   point estimate outside its own interval.
3. **"Confirmed share" is undefined under two replay runs per arm.** §12 states 2 runs per arm
   because Sonnet agrees with itself on 77% of link decisions. §4.4 defines the test on "the
   confirmed share" of a system, singular. Pre-register how runs combine:
   - the mean of the runs' per-window shares;
   - "confirmed in both";
   - "confirmed in either";
   - a run-level random effect.

   Also pre-register which runs' links enter the pool. The choice moves the number, and run-to-run
   noise inflates the discordance d, which the d = 0.5 sizing does not model.
4. **Which of window 2's two blind passes is scored is unstated.** The window has a pass 1 and a
   pass 2 (§4.6, `gold_windows.pass`, `repeat_of`). The headline, the adjudicated merges and the
   precision sample each need one pass named in advance. Choosing after seeing which pass agrees
   more with a system is a forking path. Pass 1 is the natural pre-registration.

**Plan can resolve. The spec text should still be corrected where it makes a claim:**
- **"By any path" (§3, §7) is false for `TRUNCATE`.** Row-level `BEFORE DELETE` triggers do not
  fire on `TRUNCATE`. The existing suite already uses `TRUNCATE events, assertions CASCADE`
  (**verified**, `tests/test_score_comprehension.py:731`), so the idiom is in use here. Add a
  `BEFORE TRUNCATE` statement trigger on `items`, and state that
  `session_replication_role = replica` bypasses both.
- **0017's down script lifts the hold and destroys the census.** §7 says the hold "lifts only when
  the `gold_block` row is deliberately deleted". A `run_migrations(direction="down")` through 0017
  drops the trigger and every `gold_*` table, and §6.1 contemplates "migration reversal". Make
  0017's down **refuse** while `gold_assignments` has rows. The rollback tests stay green, because
  their DBs are empty: every fixture starts from `DROP SCHEMA public CASCADE`, **verified**
  `tests/test_comprehend_integration.py:20`, `tests/test_claim_store.py:28`. The down script must
  also `DROP FUNCTION` the trigger function, per the precedent at
  `migrations/0006_knowledge_base_down.sql` (last line) and its negative-control test,
  `tests/test_kb_schema.py:803`.
- **§10's "through a cascade from a parent row" test cannot be written against this schema.**
  `items.outlet_id REFERENCES outlets(id)` has no `ON DELETE` action (**verified**,
  `migrations/0006_knowledge_base_up.sql:29`), so no parent delete cascades **into** `items`. The
  cascades the spec means go **out** of `items`, into `assertions`
  (`0006_knowledge_base_up.sql:109`) and `item_triage` (`0009_comprehension_up.sql:9`). The test
  should delete a held item that has both, and assert that the delete raises and both child rows
  survive.
- **"A delete would otherwise cascade silently" is partly true.** `feed_sightings.item_id` is a
  `NO ACTION` FK to `items` (**verified**, `migrations/0008_capture_telemetry_up.sql:54`), and
  capture populates it for most items (`capture.py:149–156`, `:241–245`). So a naive item
  `DELETE` already fails loudly today. The silent case is a retention job that prunes sightings
  first, which is `uh0`'s shape. The trigger is still right; the justification should say this.
- **The hold does not fully lift when `gold_block` is deleted.** `gold_window_items`' `ON DELETE
  RESTRICT` FK (§7) still pins the 16 windows' items until the gold tables go. State it, or say
  the release step removes both.
- **`gold_block` "one row" is not enforced.** With two rows, the trigger's "its `block_end`" is
  ambiguous. Add a singleton constraint, for example `id boolean PRIMARY KEY DEFAULT true CHECK
  (id)`.
- **Roles are cluster-global and survive `DROP SCHEMA public CASCADE`.** The §10 subprocess test
  runs `gold_grants.sql` once per test, so the script's `CREATE ROLE` must be idempotent (a
  `DO $$ ... IF NOT EXISTS` block). Otherwise the second test run fails on an existing role.
- **Holm across secondaries only, with the primary unadjusted, bounds familywise error at 2α, not
  α.** Use gatekeeping (test secondaries only if the primary rejects), which keeps α, or Holm
  across all.
  - "Holm-corrected across the secondaries declared with it" leaves a comparison computed later,
    and undeclared, with no family.
  - With 3 secondaries, the smallest Holm threshold multiplies the MDE by 3.56 / 3.00 = 1.19
    (**computed**), for example 24 to 29 points. Secondaries will essentially never reject, and
    that should be said.
- **Name collision.** An unrelated "gold set" already exists: `tests/test_gold_set.py`,
  `scripts/score_gold_set.py` and `tests/fixtures/gold_set_breaks.json`, the claim-break gold
  set. Name the new module and tables so that "the gold set" is unambiguous in memory and docs,
  for example `census_*`, or say which is which.
- **The pool UI should hide system identity and interleave systems' pairs.** §4.4's per-system
  adjudication-only share needs the proposer recorded, not shown.

**Checked and fine:**
- `common.is_quote_page` (`common.py:745`) reads no knob, and `common.__getattr__` resolves
  knobs only for `KNOBS` names (`common.py:374–394`), so the §10 knob-read test is well-aimed.
- `db` imports only `common.log` (`db.py:16`).
- `items` is insert-only: `ON CONFLICT DO NOTHING` (`capture.py:123–125`). Census text cannot
  drift under the replay.
- The existing item-delete test (`tests/test_comprehension_schema.py:172`) creates no
  `gold_block` row, so the trigger does not break it.
- `CANDIDATE_WINDOW_DAYS = 14` is at `comprehend.py:1519`.
- `similarity(t.title, e.summary)` is inside `candidate_events`, at `comprehend.py:1647` and
  `:1572`.

---

## How revision 6 answered revision 5

| Rev 5 item | Rev 6 | Grade |
|---|---|---|
| (a) Label all 16 now, one test | Adopted | **Resolved** |
| (b) Operator misses and the recall denominator | Adjudicated partition, cumulative, transitive | **Answered in direction.** The construction is one-sided, pair-level, and undefined under sampling (objection b). It reuses the scored set as a dev set (objection c) |
| (c) Retention as code | `BEFORE DELETE` trigger, lifetime covers sub-projects 1 and 2 | **Resolved in intent.** `TRUNCATE`, the down script and an unbuildable cascade test are plan items |
| Blocker 1: unnamed test | Paired t, 15 df, t interval | **Named.** Its estimand differs from the reported point, and it is undefined for empty windows (blockers 1 and 2) |
| Blocker 2: multi-comparison rule | Primary per decision, Holm over secondaries | **Mostly resolved.** FWER is 2α as structured (plan item) |
| Blocker 3: stage-1 MDE claim | Deleted with the stages | **Resolved** |
| Blocker 4: headline after adjudication | Adjudicated partition defined | **Defined.** See objection (b) |
| Blocker 5: precision sample | Without replacement, `CHECK` | **Resolved.** Cross-kind conflicts are a plan item |
| Blocker 6: gap-check population | Restriction dropped | **Resolved** |
| Not blocking: both window values, stratum refusal, deploy date by recognition, `brief.py` mode | All taken | **Resolved** |

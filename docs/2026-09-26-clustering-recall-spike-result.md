# Clustering-recall spike: result

**Date:** 2026-09-26 · **Bead:** `news-brief-1tl` (closed by this result) · **Consumer:** `news-brief-vlg`
**Probe:** `scripts/probe_clustering.py` (pre-registration in its docstring, committed in `353a877`
before the run) · **Run by:** the operator, on the host, against the frozen KB

## Verdict

**NONE: redesign the blindness before batching.** Under the operator's pre-registered rule (at
W = 2 h, the simplest of T, T-cc, TE-cc within 5 points of real time), no variant qualifies, and
none comes close. The best one, TE-cc, trails by 31.7 points, with a 95% CI of −35.5 to −27.9.
This is not noise. **Spec §5.3's grouping, as designed, cannot be built on this evidence.**

## Output, verbatim

```
Cross-outlet same-event pairs captured <= 2h apart: 671
Exactness control: 0 of <= 200 sampled items disagree
Material items in range: 12711; pair items NOT in it: 0
Usable pairs (both items material): 671

RT (1h passes, chunks of 5):  97.2%

--- W = 1h: 263 windows, 290 pairs co-windowed
variant   visible               vs RT (95% CI)   reqs  mean  max
T         69.0%  -28.2 (-31.9, -24.4) pts   2530   4.4    8
T-cc      70.5%  -26.7 (-30.4, -23.0) pts   2484   4.5    8
TE-cc     80.0%  -17.1 (-20.5, -13.8) pts   2260   4.9    8
TE components (uncapped): p50 1, p90 5, max 123, 261 over the cap of 8

--- W = 2h: 149 windows, 443 pairs co-windowed
variant   visible               vs RT (95% CI)   reqs  mean  max
T         53.7%  -43.5 (-47.5, -39.5) pts   2723   4.5    8
T-cc      56.3%  -40.8 (-44.8, -36.8) pts   2653   4.6    8
TE-cc     65.4%  -31.7 (-35.5, -27.9) pts   2196   5.6    8
TE components (uncapped): p50 1, p90 3, max 223, 167 over the cap of 8

Title similarity of usable pairs (what another threshold would buy):
  [0.00, 0.20):   218   32.5%
  [0.20, 0.35):   249   37.1%
  [0.35, 0.50):   124   18.5%
  [0.50, 0.70):    46    6.9%
  [0.70, 1.01):    34    5.1%
Usable pairs sharing a prior-born entity: 503 of 671

PRE-REGISTERED guesses: T 40-60%, TE-cc 85%+, RT 85-95%. Rule: simplest of T, T-cc, TE-cc within 5 pts of RT at W = 2h.
VERDICT: NONE: redesign the blindness before batching
```

## Against the pre-registered guesses

| | guess | measured | |
|---|---|---|---|
| T | 40–60% | 53.7% | hit |
| TE-cc | 85%+ | 65.4% | **missed low by ~20 pts** |
| RT | 85–95% | 97.2% | **missed high** |

**Both misses have one cause: density.** Membership averages ~42 material items per 1 h window
(2,530 requests × 4.4 items over 263 windows).
- **RT:** 42 items make ~8 micro-batches, so two items rarely share one. Only ~19 of the 290 pairs
  co-windowed at 1 h were blind (~7%).
- **TE-cc:** the entity edge is available for 75% of pairs (503 of 671), but hub entities join
  items into components of up to 223. The cap of 8 then cuts those components apart. 167
  components exceeded the cap at W = 2 h.

I checked for a probe defect before accepting the numbers. The exactness control read 0/200,
every pair item was in the membership, and the entity-edge figure (75%) is consistent with
TE-cc's gain over T-cc.

**Only the pairs a submission can blind.** At W = 2 h, 228 of the 671 pairs fall in different
windows and are visible to every variant. Of the 443 co-windowed pairs, the grouping put
T ≈ 30%, T-cc ≈ 34% and TE-cc ≈ 48% in one request. These are derived from the percentages above,
so they are approximate.

## Two findings beyond the verdict

1. **A title trigram is a weak signal for grouping.** 70% of true same-event pairs score below
   0.35, and a third score below 0.20. Lowering the threshold would not help: the pairs sit where
   unrelated headlines also sit (`probe_corroboration`'s negative class).
2. **Real time is already nearly blind-free at this density** (97.2% visible). Same-micro-batch
   blindness is not what holds corroboration down in the current pipeline.

## The caveat that decides the next step, stated rather than used to soften the verdict

The historical material set is dense: 86% of items were material under the rules half that D5
removed. The probe's docstring already said this bias favours RT and hurts grouping. **It did not
predict how large the bias would be, and at a different density the comparison moves.**
- At a few items per hour, one submission fits in one or two requests. Batching then sees nearly
  everything.
- At that same density, RT's single micro-batch per pass is where the blindness sits.

Phase 1 changes the density, and nobody knows the new material rate yet (spec §4, "the biggest
unknown"). The verdict holds for the density measured. **Whether the blindness survives at
phase-1 density is an open question, not a reason to discount this result.** Any re-run must be
pre-registered before phase 1's rate is known.

## Next: two measurements, pre-registered 2026-09-26 before either was built

The operator chose to **measure both routes, then choose**, and to break any tie **on the
numbers** rather than by a rule set in advance.

### M1: density sweep (free; bead filed alongside this section)

- **Method.** `probe_clustering.py --sweep`. Keep every pair item. Thin the other material items
  at random to **5, 10, 20 and 42 (unthinned) items per capture-hour**. Use 5 seeds, and report
  the mean and range for each variant and for RT.
- **Model assumption, stated:** multi-outlet news is what triage keeps material, so the pair
  items survive thinning.
- **Rule (operator).** Phase 1 measures material items per capture-hour, for items captured after
  its flip, over the first 72 h. Apply the 5-point rule at the sweep row **at or above phase 1's
  p90**: batching must survive a busy hour, not only a typical one.
- **Guesses:**
  - some variant comes within 5 pts of RT at ≤ 10 items/h, and none does at 20;
  - phase 1's p90 is 15–30 items/h.

  Together, these predict that batching **fails** M1.

### M2: Haiku replay (≈ $5, on the host; bead filed alongside this section)

- **Method.** About 150 historical 5-item integration batches, weighted toward the later item of
  each cross-outlet pair. Each is rebuilt as of its capture time (entities filtered by birth,
  events through `candidate_events(as_of=…)`). Each is run through Sonnet twice (A, A′) and Haiku
  4.5 once (H), through production's `build_integration_request` and
  `parse_integration_response`. Nothing is written.
- **Metric.** Agreement on (item → matched event) decisions.
- **Rule (operator).** Haiku qualifies if agreement(A,H) ≥ agreement(A,A′) − 5 pts **and** its
  validation-drop rate is at most 5 pts worse than A's.
- **Guesses:** A-vs-A′ 80–90%, A-vs-H 65–80%. This predicts Haiku **fails narrowly**.

If both routes fail, the options left are the reconcile pass or pausing phase 2 (reversing D8).
Both are the operator's to choose.

## M1 result (host, 2026-09-26): the control holds; batching qualifies only at ≤ 5 items/h

```
Cross-outlet same-event pairs captured <= 2h apart: 671
Exactness control: 0 of <= 200 sampled items disagree

=== M1 density sweep at W = 2h (5-pt rule; row chosen by phase-1 p90)
CONTROL unthinned (40.7 items/h): RT  97.2%  T  53.7%  T-cc  56.3%  TE-cc  65.4%
  must equal the spike's published row: RT 97.2%  T 53.7%  T-cc 56.3%  TE-cc 65.4%

target/h  real/h               RT                      T                   T-cc                  TE-cc
       5     5.5  74.8% [74.8-74.8]       65.0% ( -9.8) --       66.9% ( -7.9) --       83.0% ( +8.2) ok
      10    10.2  86.9% [86.4-87.9]       58.7% (-28.2) --       60.7% (-26.2) --       76.6% (-10.3) --
      20    19.6  93.5% [93.0-93.9]       55.4% (-38.2) --       57.9% (-35.6) --       69.2% (-24.3) --

No verdict: apply the 5-pt rule at the row at or above phase 1's measured p90 material items per capture-hour (news-brief-4le).
```

- **The control reproduces the spike exactly**, so the thinned rows can be trusted.
- **Implication of the pre-registered rule, stated before phase 1 measures anything: M1 passes
  only if phase 1's p90 is ≤ 5 material items per capture-hour.** Any p90 above 5 selects the
  10 row, where every variant fails.
- **Guess scoring.** "A variant qualifies at ≤ 10/h" was wrong: it qualifies at 5/h only. "None at
  20" was right. The phase-1 p90 guess (15–30/h) is still pending.
- **The 5 row is the floor of this model.** RT's range across seeds is 0, because at 5/h almost
  only pair items survive thinning.

### Not pre-registered, and the more important finding: real time goes blind at low density

RT falls from 97.2% (unthinned) to **74.8% at 5/h**. A quiet hourly pass is one micro-batch of 5,
so two outlets' reports from the same hour share it and cannot see each other. **Phase 1 lowers
density by design (D5), so it lowers corroboration visibility whether or not phase 2 ever
ships.** The next gate window inherits that loss.

**Spec §5.4's in-request `NEW`-label linking removes this blindness without any batching.** A
pair is either in one request (linked) or in successive micro-batches (offered). This is
structural visibility, and whether the model actually uses `NEW` labels is its own measurement.
Filed separately.

## M2 built (2026-09-26): refinements fixed before any run

`scripts/replay_haiku.py` (`news-brief-y1x`). The M2 pre-registration above left four details
open. The operator settled them on 2026-09-26, **before the script existed**, and they are
fixed in its docstring:

- **Batches.** 100 are anchored on the later item of a random cross-outlet pair (≤ 2 h apart,
  both items material). 50 are anchored on a random material item. Each anchor is batched
  with up to 4 material items captured in the hour before it, which is a real-time
  micro-batch. The two kinds are shuffled together, with seed `20260926`, so a stop at the
  spend cap still holds both.
- **Spend.** Hard cap **$10**. Before each batch, the guard holds back a worst case for every
  attempt of all three runs (a transient fault is retried once): input at 3 chars/token, and
  the full 8192-token output budget. A failed attempt may still have been billed, so it is
  charged against the cap at its worst case. Calls that finish alongside an abort are still
  counted. A free `--dry-run` prints the batches and a projection built from
  `comprehend_spend`'s historical means.
- **Population.** Agreement is measured over items **all three** runs extracted, so A-A′ and
  A-H share one denominator. The operator chose "all items both kept" over a match-only
  subset. The match-only subset (items where any run matched something) is printed beside it
  and not ruled on.
- **Decision.** An item's decision is the set of existing events it matched, plus the
  in-request clusters it belongs to. A cluster is the set of items that share one
  `NEW`-labelled event, the declarer included. Label names differ between runs, so
  clusters are compared as item sets, and it does not matter which item the model made the
  declarer. A `NEW` label declared on an existing event resolves to that event.
- **Boundary.** Both 5-point clauses are inclusive. The differences are rounded to 1e-9
  before comparison, because `0.85 − 0.05` is `0.7999…` in binary. Rule, guesses and
  `MIN_BATCHES = 30` are unchanged from the section above.

**Cut-off and births, corrected by the pre-run review**
(`docs/superpowers/reviews/2026-09-26-jwm-li9-y1x-review.md`: one blocker, one major, six
minor, all fixed and each fix mutation-checked before any run):
- **The batch's cut-off is its EARLIEST member's capture, not the anchor's.** A real-time
  micro-batch integrates its members together, so nothing they produce exists when the call
  is made. With the anchor as cut-off, a member captured 30 minutes earlier would have been
  offered its own entity and its own event (backlog-integrated before the anchor). Both
  agreements would have been pushed toward 100%, and the verdict toward "qualifies".
- **An entity is born with the first asserting item whose text NAMES it**, capped at
  `entities.created_at`. `probe_clustering.entity_births` dates it from ANY item asserting a
  tagged event. But `write_extraction` tags a MATCHED older event with the matching item's
  new entities, so that rule dates such an entity before it existed.
- **Consequence for the spike above, stated and not re-run.** TE-cc's entity edge used
  `entity_births`, so it saw entities early. That inflates TE-cc's visible share, which is
  the direction that favours batching. TE-cc still missed the rule by 31.7 points, so the
  NONE verdict stands with this bias removed. M1's sweep inherits the same upward bias on
  TE-cc: its one qualifying cell (TE-cc at 5/h, +8.2) is an upper estimate.
- **Pair batches are selected by production's own past links** (historical Sonnet asserted
  both items onto one event), which favours A-A′ on that stratum. The script prints both
  strata and names the random stratum as the unconfounded one.

What stays **out** of the measurement, stated rather than hidden:
- The replay runs the production functions `integration_candidates` (extracted from
  `run()` for this purpose, and now shared with `inspect_integration.py`),
  `build_integration_request` and `parse_integration_response`. There is no copy of the
  candidate ranking.
- Events are offered as of **`events.created_at`**, and the backlog wrote that late. So a
  pair's earlier event may be missing at the batch's cut-off. The script prints how many pair
  batches were offered the earlier item's event. That count bounds how much signal the match
  decisions carry. When the earlier item is INSIDE the batch, the link can only be made
  in-request, exactly as in real time.
- A 4xx other than 429 **aborts** the run. The case in mind is Haiku refusing the
  `thinking: disabled` shape (runbook step 5, documented-not-observed). Scoring it as 100%
  dropped would fail Haiku on a request-shape fault.

**Run order on the host:** `--dry-run` (free), then `--limit 2` (a smoke run of roughly
$0.10, no verdict), then the full run. Record the full run's output verbatim in this
document, and close `y1x` on it.

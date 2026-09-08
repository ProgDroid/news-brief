# Red-team: candidate event ranking (`2026-09-08-candidate-ranking-design.md`)

**Reviewer stance:** hostile. The brief was to argue this should not be built as specified,
not to improve it. Findings are ordered by how much of the spec they invalidate.

**What I actually read:** the spec; `scripts/probe_corroboration.py` in full;
`comprehend.py` (`candidate_events` at :797, the integration loop at :330-440, the caps at
:790-792, `Tally` at :39-80); `scripts/score_comprehension.py:140-190`; `common.py:265-278`;
the parent spec §6, §6.1, §8.2, §10, §12.

---

## Summary of the three objections

| # | Category | One line |
|---|---|---|
| (a) | Simpler design | `ORDER BY shared-entity-count DESC` in the existing SQL — 57% vs 69%, and it makes objection (c) structurally impossible |
| (b) | Assumed, not established | The bake-off's evaluation set is *selected by* the same lexical signal the winning ranker *sorts by*; and the 17.1% headline is not §8.2's metric |
| (c) | 6-month collision | `COMPREHEND_CANDIDATE_POOL_CAP` is a recency truncation, i.e. the same bug at a larger n — and every number in the spec was measured at roughly one-third of steady-state density |

---

## (b) first, because it is the load-bearing one

### b.1 The 800-pair evaluation set is drawn from the signal being evaluated

Trace the probe:

- `probable_misses(rows, ents, BANDS[0][0], counts)` — `probe_corroboration.py:712` — keeps
  only cross-outlet pairs whose **title-to-title** Jaccard (`similarity()`, :144) is
  `>= 0.10`, and returns them `sorted(misses, key=lambda m: -m["score"])` (:333).
- `subset = [m for m in misses if m["score"] >= BAKEOFF_MIN_SCORE][:BAKEOFF_MAX_PAIRS]`
  — :768, with `BAKEOFF_MIN_SCORE = 0.20` and `BAKEOFF_MAX_PAIRS = 800`.
- The spec reports "800 known missed pairs", i.e. the cap **bound**. So the evaluation set is
  not "pairs above 0.20" — it is the **top 800 pairs ranked by title-title lexical
  similarity**.
- The winning strategy, `rank_title_similarity` (:421) / `batch_similarity` (:544), sorts the
  pool by `similarity(later_title, event.summary)`.

`events.summary` is a model paraphrase of the item that created the event. Title-to-title
overlap and title-to-summary overlap are the same signal read through one paraphrase step.
The eval set therefore consists, by construction, of exactly the pairs on which the winning
ranker is strongest — and it *excludes*, by construction, the majority class the spec itself
identifies: positives reaching down to `p50 = 0.136` (§1.3), i.e. more than half of all known
true duplicates score below the eval set's entry threshold and cannot appear in it.

§1.3 anticipates the charge and gets it wrong:

> Comparisons **between** buckets and **between** rankings remain valid: one cut applies to
> all of them, so it cannot favour a strategy.

A cut applied uniformly is neutral only between strategies that are *independent* of the cut's
variable. It is neutral between recency and entity-overlap. It is not remotely neutral between
recency and a **lexical** ranker when the cut is itself lexical. The correct statement is: one
cut applies to all of them, and it selects the sub-population on which one specific arm is
guaranteed to do best. This is `the-probe-measured-the-wrong-layer` in its textbook form —
a well-formed, correctly computed number answering an adjacent question.

The tell the memory corpus names ("a secondary signal contradicting the headline") is present
in the spec's own text: §1.3 says lexical similarity is *useless as a detector here* and §1.2
says lexical similarity is *3.9× better as a ranker*. §1.3's reconciliation ("ranking is
easier than detection") is a true general statement doing work it cannot do here, because the
measurement of the ranker was taken only where the detector fires.

**The 58% is not a floor, as §7 claims. It is an upper bound**, and the direction of the error
is the one that matters: on the ~88% of true duplicates the detector cannot see, similarity
ranking has never been measured at all, and there is a specific reason to expect it to
underperform there — those are precisely the pairs whose vocabulary does not overlap.

**What would settle it:** re-run `bake_off` with `subset` drawn *randomly* from
`probable_misses` at threshold 0.10 rather than top-800-by-score, or better, evaluate on the
**positive class** (`calibration_pairs`, :179 — pairs the model itself merged, which are
labelled independently of lexical score) instead of on `probable_misses`. That set exists in
the probe already and is not used for the bake-off. Until one of those runs, the 15% → 58%
delta is not evidence that the production system will improve by 3.9×.

### b.2 The `800/800` control does not control for what the spec says it controls for

§1.2: "The recency arm reproduces `was_retrievable`'s SQL verdict on **800/800**, so the
deltas rest on the correct baseline."

Compare `was_retrievable` (:336-362) with `candidate_pool` (:371-399): same `entity_ids`, same
`CANDIDATE_WINDOW_DAYS`, same `created_at < miss.later.created_at`, same `occurred_at DESC`.
The control therefore verifies that Python's `sorted(..., reverse=True)[:30]` agrees with
Postgres's `ORDER BY ... LIMIT 30` over an identical row set. It is very close to
tautological, and it is **not** a control for whether the reconstruction resembles production.

Production's `candidate_events` (`comprehend.py:807-815`) differs from the reconstruction in
three ways:

1. `e.occurred_at >= now() - interval` — the window is anchored to **now**, not to the later
   event's creation time. The reconstruction is anchored to `miss.later.created_at`.
2. Production has **no `created_at <` predicate at all**. It can and does offer events created
   after the item in question — a different pool.
3. Production's `entity_ids` come from `SurfaceIndex.match` over the batch's item text and are
   truncated to `CANDIDATE_ENTITY_CAP = 40` (`comprehend.py:362-365`). The probe uses
   `event_entities` — the entity set the model *assigned after the fact* — with **no cap**.
   `was_retrievable`'s own docstring says so: *"Deliberately GENEROUS: the ideal entity set, no
   entity cap."*

So "Production today operates at 15% recall" (§1.2, bolded) is not a measurement of
production. It is a measurement of an idealised reconstruction of production, on a
lexically-selected sample, with reconstructed batches (`batch_neighbours`, :493, whose own
docstring concedes *"exact historical batches were never recorded"*). Every one of those
deviations is defensible individually; the sentence that states the result as a fact about the
live system is not.

### b.3 The spec optimises a metric that is not §8.2's floor

§1 quotes §8.2's floor —

> ≥10% of events carry assertions from 2 or more distinct outlets

— and then immediately reports "The cumulative match rate is **17.1%**". These are two
different statistics, and `scripts/score_comprehension.py` implements them as two different
functions:

- `corroboration_by_outlet` (:140) — `count(DISTINCT i.outlet_id) >= 2` per event, over
  events. **This is §8.2's floor, the headline.**
- `score_match_rate_corroboration` (:154) — `(assertions - events) / assertions`. This is
  §8.2's *secondary* line ("Report `events_matched / (events_matched + events_created)`
  **beside** the headline").

17.1% is `(3617 - 2999) / 3617`. That is the secondary. The spec never reports the headline
number at any point, and the two measure different things: an event matched twice **by the
same outlet** raises the match rate and does nothing for outlet diversity.

This matters operationally, not pedantically, because of parent-spec §12.2: 41% of captured
volume is Google News proxy feeds whose `summary` is the headline restated, and those proxies
re-return items. Similarity ranking is outlet-blind — it will surface same-outlet and
proxy-duplicated events at least as readily as genuine cross-outlet coverage, because those
are the pairs with the *highest* lexical overlap. So the plausible outcome of this change is
that the number the spec optimises (17.1%) moves substantially while the number the gate
actually reads (`corroboration_by_outlet`) moves much less.

§8.2 pre-registered the response to exactly this: *"If the two disagree, trust the
disagreement."* The spec has built its case on the side of the disagreement §8.2 told us not
to trust, without ever printing the other side.

---

## (a) The simpler design: rank in SQL by shared-entity count

**The change:** leave `candidate_events` a single query returning ≤31 rows. Change only the
ordering.

```sql
SELECT e.id, e.summary
FROM events e
JOIN event_entities ee ON ee.event_id = e.id AND ee.entity_id = ANY(%s)
WHERE e.occurred_at >= now() - make_interval(days => %s)
GROUP BY e.id, e.summary, e.occurred_at
ORDER BY count(*) DESC, e.occurred_at DESC, e.id DESC
LIMIT %s
```

Measured at **57%** single-item recall (`rank_entity_overlap`, :409) against title
similarity's 69% and recency's 36%. Two thirds of the measured gain over the incumbent, for a
three-line diff.

**What it drops, stated plainly:**

- The ~12-point single-item lexical edge (69% → 57%), whose batched value is unknown because
  entity overlap was never carried into `BATCH_STRATEGIES` (:585).
- Discrimination *within* an entity cluster. Two distinct Iran/UN events on the same day share
  the same entity set; only lexical content separates them. Entity overlap will rank both
  identically and fall back to recency between them. This is a real and named loss.
- The shared `tokens()`/`similarity()` contract (§2.1) and its contract test — which is a
  saving, not a loss: see below.

**What it buys that the spec's design cannot:**

1. **No pool.** The ranking happens over the *entire* in-window candidate set, in the index,
   with no intermediate truncation. Objection (c) below does not exist for this design — not
   mitigated, structurally absent.
2. **No new knob.** §2.4's `COMPREHEND_CANDIDATE_POOL_CAP` is a guessed value with no measured
   relationship to recall, which means nobody will ever know what to set it to.
3. **No new coupling between production and a diagnostic script.** §2.1 makes
   `comprehend.py` export `tokens()`/`similarity()` so `scripts/probe_corroboration.py` can
   import them, and §6 adds a contract test asserting production reproduces the probe's
   ordering on a fixture. That inverts the dependency: a debug script becomes a permanent
   consumer of a production API, and a *reasoned improvement* to the similarity function
   (stemming, `html.unescape()` — which parent-spec §12.2 says is already needed, since
   `&nbsp;` reaches the matcher literally) now breaks a test whose failure message says
   "production drifted from the probe". The argument in §2.1 ("the two would drift silently")
   is a real concern with a cheaper answer: re-run the probe. It is a diagnostic, not an
   oracle.
4. **Zero per-batch CPU.** No `pool × batch_items` Jaccard loop, no re-tokenising every
   summary once per item in the batch (`similarity()` at :144 tokenises both arguments on
   every call; the spec's design calls it `|pool| × |batch|` times per batch and caches
   nothing).
5. **It is the one arm whose 57% is not confounded.** Per (b.1), the eval set was selected by
   lexical similarity. Entity overlap is independent of that selection rule, so its 57% is
   measured on a sample biased *against* it. Title similarity's 69% is measured on a sample
   biased *for* it. On a fair sample the gap is smaller than 12 points and may invert.

**The honest caveat:** entity overlap batched has never been measured. Adding it is one
dictionary entry in `BATCH_STRATEGIES` and one four-line function. That run should happen
before either design is chosen, and it costs a probe invocation. If entity overlap batched
lands anywhere near similarity batched, there is no case for the spec's design at all.

---

## (c) The constraint this collides with: `COMPREHEND_CANDIDATE_POOL_CAP` is the same bug

### c.1 The fix reintroduces its own failure mode one layer up

§2 step 1: *"`candidate_events` fetches an **uncapped** pool for the batch's entity set,
bounded by a new `COMPREHEND_CANDIDATE_POOL_CAP` and counted when the bound binds."*

"Uncapped, bounded by a cap" is a contradiction the spec does not resolve, and the unresolved
part is the load-bearing part: **in what order is the pool truncated?** Postgres will not hand
back "the pool" — it hands back rows in whatever order the plan produces, and to bound the
result you must either `LIMIT` without `ORDER BY` (arbitrary, non-reproducible — which also
kills §2.3's determinism claim, since ties can only be broken among rows you actually
received) or `ORDER BY e.occurred_at DESC LIMIT pool_cap`.

The second is the only sane choice, and it is **recency truncation** — precisely the mechanism
§1.1 diagnoses as the bug. The design does not eliminate recency-ordered burial; it moves it
from n=30 to n=`POOL_CAP` and puts a Python sort behind it. On any batch where the pool cap
binds, the similarity ranker is choosing among the `POOL_CAP` most recent events, and a
duplicate older than that is as invisible as it is today.

### c.2 Every number in the spec was measured at roughly one third of steady-state density

The spec states it itself, in §1.3, as an aside about the negative class:

> The negative class is empty because it needs items 5+ days apart and **the KB is younger**.

`CANDIDATE_WINDOW_DAYS = 14`. So at the moment of measurement the 14-day window contained
under 5 days of events — the corpus is ~4 days old (capture enabled 2026-09-04 per
`newsbrief-capture-feature`; 2,999 events today). At steady state the same window holds
**roughly three times** the events it held during the bake-off. Candidate pools for hub
entities triple. The recency window's ability to reach a duplicate falls proportionally.

This is not speculation about the future; the spec reports the trend already in progress:
§1 says the match rate "was ~26% early in the corpus's life" and is 17.1% now, and "declines
as the KB grows". That decline is the pool-density curve. Nothing in this design flattens it —
it re-baselines it at a higher n and lets it resume the same slope. `POOL_CAP` is being chosen
against a density that will have tripled before the first month of operation is out, and there
is no measured recall-vs-pool-cap curve anywhere in the spec, so nobody will know what to
raise it to or by how much.

Meanwhile the CPU cost is superlinear in the thing you must raise: `|pool| × |batch items|`
Jaccard computations, `× 58 batches × 24 passes/day`, forever. Raising `POOL_CAP` to chase the
density is the only remediation the design offers, and it is the expensive one.

### c.3 The counter that is supposed to make this visible is already known not to work

§2.4 promises a tally counter *"because a cap nobody can see is indistinguishable from an
absence of data."* §7 then admits: *"`candidate_cap_hit` fires on essentially every batch, and
this spec does not distinguish which cap is firing."*

That is the same promise, made in the parent spec §6 (*"A cap reached is counted in the run
tally … the count is what will say so rather than a guess"*), already kept, and already
useless — because `comprehend.py:364` and `comprehend.py:817` increment **the same field** for
two different caps. A third cap counter will be a third boolean pinned at true. The spec is
proposing the instrumentation pattern that has already been demonstrated, in this exact
function, not to carry information — and is not fixing the conflation it depends on.

### c.4 The gate collision

§4 is the most dangerous section in the spec, and it is presented as a set of notes.

- The pre-registered gate is **one-shot**. The corpus memory records the rule: *"A
  pre-registered gate is one-shot: fix bugs that distort what it READS before flipping the
  flag."* The flag is flipped — 2,999 events exist. The gate is now firing against live data.
- §4 concedes the KB will contain two populations, that `score_comprehension.py` computes over
  the whole KB, and that a run spanning the cutover "measures a blend of two systems".
- Its remedy is a **follow-up bead adding a window argument to the gate**. So the plan is:
  change the matcher's input, then change the gate's scope, then read the gate.
- Adding a window argument to a pre-registered criterion after the run has begun un-registers
  it. The pre-registered statistic was "≥10% of events carry assertions from 2+ distinct
  outlets" over the KB. "≥10% over a window starting at the cutover" is a different statistic,
  chosen after seeing that the first one was failing, and defined by a boundary that this
  change created. Whatever it returns will not be a pre-registered result, and the whole value
  of §8.2 was that it was written down first.
- §4 also documents that the safe path — bumping `INTEGRATE_PROMPT_VERSION` and re-extracting
  — is unavailable, because `news-brief-3wb` means re-extraction *doubles* events and
  assertions rather than superseding them. So the KB cannot be brought to a single population
  by any means currently available. The two populations are permanent.

The disciplined sequencing is the reverse of the spec's: **read the gate now, on the corpus
the pre-registration described**, take the fail, and treat the failure as the measurement it
was designed to be. Then change the matcher, and re-register the criterion explicitly as a new
one for the new regime. The spec's ordering fixes the matcher first and leaves the gate
unreadable in both directions — it can neither pass cleanly nor fail attributably.

---

## Things I checked that did *not* turn into objections

Recorded so they are not re-litigated.

- **§2.3's tie-break deviation** is correct and well-argued (given a pool that reaches the
  sort at all — see c.1).
- **§3's rejections** are individually sound. `pg_trgm` is rightly rejected as unmeasured;
  the reserved-slots number (54% vs 58%) is a real measurement; the hybrid's 5-pair margin
  really is noise at n=800.
- **§1.4 on syndication** is supported by the probe: `is_syndication` (:161) normalises source
  suffixes and unicode punctuation before comparing, and the reported rate is 0% outside the
  top band.
- **`merges_from_pairs`** (:617) is correct union-find; the earlier `C(k,2)` bug is genuinely
  fixed.
- **The probe's own self-criticism header** (:18-40) is unusually good and several of its
  recorded corrections are exactly right. My objections are about what the spec concluded from
  the probe, not about the probe's construction — with the single exception of the eval-set
  selection in b.1, which the probe does not flag anywhere.

---

## What I would require before any of this is built

1. Re-run the bake-off on an eval set that is **not** selected by lexical score — random
   sampling at threshold 0.10, or the `calibration_pairs` positive class. If the 15%→58%
   delta survives, b.1 is answered and the design's central claim stands.
2. Add `rank_entity_overlap` to `BATCH_STRATEGIES` and report it batched. One dictionary
   entry. It decides between design (a) and the spec's design on evidence.
3. Print `corroboration_by_outlet` — §8.2's actual headline — beside the 17.1%.
4. Run the pre-registered gate **before** changing the matcher.
5. Split `candidate_cap_hit` into two fields before adding a third cap.

Items 1–3 are one probe run and roughly twenty lines. Item 4 is a script invocation. None of
this requires the design to be right or wrong in advance; it requires the numbers to mean what
the spec says they mean.

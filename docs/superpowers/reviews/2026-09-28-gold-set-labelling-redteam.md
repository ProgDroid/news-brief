# Red-team: gold set of labelled item pairs (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` (draft, 2026-09-28)
**Stance:** hostile staff engineer. The question was whether this is the wrong thing to build, not
whether it is well written. Claims marked **verified** were checked against the repo. Claims marked
**estimate** are arithmetic on figures already recorded in the repo, and each one names its inputs.
No production data was queried, because the corpus lives on the host.

---

## Verdict in one paragraph

The goal is right: ground truth before redesign. The instrument is wrong for sub-project 1, the
consumer it exists to serve. A two-stage anchor sampler over a growing ±30-day pair universe, with
16 strata, per-round inclusion probabilities and a Horvitz–Thompson combination, will spend about
600 operator taps to produce **roughly 6–30 `same_event` positives (estimate)**. Almost all of them
will sit in the high-trigram cells, and **about 0–2** will sit below similarity 0.35, where the
spike measured **70% of true same-event pairs** to live. The statistics are unbiased in
expectation, but the sample that actually gets drawn will be enriched for the production
retriever's own signal. The spec cites Trap 5 and Trap 8 as its motivation, and it then
reproduces both, one in its sampler and one in its consistency check. Trap 7 (compute the minimum
detectable effect before running) is violated outright, because §7 defers the MDE to the readout,
after the labour is spent.

---

## Objection (a): a simpler design gets most of the value. Exhaustively cluster a few short capture windows.

**The objection.** Sub-project 1 is event coreference, and the spec's own working hypothesis is
that "coreference is a clustering problem" (§2). The design that answers that question most
directly, and with the least statistical machinery, is a **complete census of a few short
windows**:
- Take K random capture windows of about 3–4 h each, roughly 100 items per window at 700–900
  items/day. Stratify the windows by hour of day, because volume is bursty.
- The operator assigns every item in a window to a cluster: "same event as one of my clusters so
  far, or new".
- Transitivity then labels **every within-window pair**, about 5,000 per window.

This design has:
- no inclusion probabilities, no strata, no rounds, no duplicate rule, and no random branch;
- recall and precision that are exact within the window;
- complete coverage of the low-trigram positives the pair sampler cannot reach (see (b));
- native support for the clustering framing: pairwise F1, B-cubed, and cluster purity. The pair
  sampler can only judge clustering through HT-weighted pairwise metrics.

**Evidence.**
- **Positive density.** The spike found 671 production-detected cross-outlet pairs ≤2 h apart
  among 12,711 material items, about 0.05 detected pairs per item
  (`docs/2026-09-26-clustering-recall-spike-result.md`, output block lines 17–20). The probe
  docstring says the detector leans lexically similar, so it is a floor
  (`scripts/probe_clustering.py` header, "Two biases"). Clusters make pairs grow quadratically:
  an event covered by 5 outlets gives 10 pairs. **Estimate:** three 100-item windows yield
  about 30–60 cross-outlet `same_event` pairs for about 300 taps. That is **4–8× the positives
  per tap** of the pair sampler (see (b)), and none of them is weighted.
- **The operator's task.** Assigning an item to one of his own clusters is **recognition, not
  recall** (global rule, "Ask for RECOGNITION, never RECALL"). The list comes from his own prior
  taps, not from a system under test. So the spec's objection to "anchor plus candidates",
  that omissions are invisible (§3), does not apply the same way: every cluster is his, and
  paging reaches all of them.
- **The rare-target problem goes away.** In the spec's design, about 85–95% of taps are
  "unrelated". Human observers miss rare targets sharply more often when prevalence is low
  (Wolfe, Horowitz & Kenner, *Nature* 435:439, 2005, "Rare items often missed in visual
  searches"). In a clustering task, every tap is a positive decision.

**What it costs, stated honestly.**
- Pairs that cross a window boundary are unlabelled. The population is "pairs within a window of
  length L". That is an honest, named scope rather than a hidden weight, and confirmation lives
  within hours: the probe uses a 48 h window and the spike a 2 h one.
- One wrong merge mislabels many pairs at once. Intervals must therefore resample windows or
  clusters, never pairs (Trap 1: state the unit before the test).
- Development (`same_story`, over days to weeks) is not covered. Defer it to sub-project 2, on
  the same argument the spec already uses to defer connections to sub-project 3 (§3).
- Telegram is awkward for "pick a cluster" once a window has more than about 8 open clusters.
  A button page of the most recent clusters with "More…" works, but a one-page local HTML
  list is easier. The operator chose Telegram, and this would reopen that decision.

**What I would do instead.**
- Build a window-census labeller, and state sub-project 1's decision rule and MDE before
  choosing K.
- If Telegram pair-taps are non-negotiable, the **minimum simplification** is:
  - **freeze one snapshot and draw all 600 pairs in one pass** (then reveal 30 a day), so there
    is no per-round π, no moving population, and no cross-round discard rule;
  - **restrict to ≤48 h** and drop the time strata;
  - enumerate the ≥0.20 pairs through a pg_trgm `%` index join, so that stratum sizes N_h are
    **known**;
  - use the textbook stratified estimator.

  §4.2's claim that "scoring every pair is infeasible" is **not established**. **Estimate:** at
  about 20k eligible items, the ±30-day pair universe is about 2×10⁸ pairs, and the ≤48 h
  universe about 3×10⁷. Both are a one-off query, not infeasible, and the sparse ≥0.20 part is
  index-assisted.

---

## Objection (b): an assumed requirement. Nothing established that ~600 pairs drawn this way can serve sub-project 1.

**The objection.** The volume (~600, checkpoint at 300) is an effort number (§3, §10: "a
usability target, not a gate"). It was not derived from what sub-project 1 must decide. The spec
never states sub-project 1's decision or its minimum detectable effect. §7 prints "the smallest
difference in same-event recall that the current n could resolve" **at 300 and 600**, which is
after the operator has done the work. That is Trap 7 exactly: "Compute the smallest effect your
measurement can resolve BEFORE running it" (`analysis-stats-traps`, Trap 7). The arithmetic,
done now, says the realized gold set cannot estimate recall where recall is actually lost.

**Evidence: yield per draw (estimate, with inputs named).**

Inputs:
- About 20k eligible items: 700–900/day since 2026-09-04, excluding quote pages (memory
  `newsbrief-capture-feature`).
- |C(a)| ≈ 20k, since the ±30-day window currently spans the whole corpus.
- The similarity distribution of true pairs from the spike: 32.5% below 0.20, 37.1% in
  0.20–0.35, 18.5% in 0.35–0.50, and 12% at 0.50 or above.
- Unrelated headline pairs mostly score below 0.20 under pg_trgm.

Typical cells for an anchor in the <1 d band:
- <0.20: about 1,500 items;
- 0.20–0.35: about 30;
- 0.35–0.50: about 2;
- ≥0.50: 0–1.

W(a) is about 25.

| Source of a `same_event` label | Per-draw probability (estimate) |
|---|---|
| Random branch: 0.1 × k̄/\|C(a)\|, with k̄ ≈ 1 partner per item | about 5×10⁻⁶ |
| ≥0.50, <1 d cell (non-empty for about 10% of anchors, ~70% positive) | about 0.8% |
| 0.35–0.50, <1 d cell (non-empty for about 30% of anchors, ~40% positive) | about 1.3% |
| 0.20–0.35, <1 d cell (about 30 items, ~1% positive) | about 0.07% |
| <0.20 cells | about 0.002% |
| 1–3 d cells and beyond | about 0.3% |
| **Total** | **about 1–5%, so 6–30 positives in 600, of which perhaps 60% are cross-outlet** |

**Positives expected below similarity 0.35: about 0–2.** That band holds 70% of the true
same-event pairs the spike measured. These are exactly the pairs where "unrelated headlines also
sit" and a trigram threshold cannot help (spike doc, "Two findings", item 1). Retrieval is the
cause the spec names in §1.

**Weights.** A positive in the <0.20, <1 d cell has
q ≈ (1/|P|)·(0.1/20000 + 0.9/25/1500) ≈ 2.9×10⁻⁵/|P|. A positive in the 0.35–0.50 cell has about
5.4×10⁻²/|P|. That is a **weight ratio of about 1,900:1**. The HT-weighted recall is therefore
either undefined in the region that matters (zero positives sampled there) or decided by one
label. It is unbiased in expectation, but it will not be accurate for any sample that is actually
drawn.

**Trap 5 comes back through the stratifier.**
- The stratification variable is pg_trgm title similarity (§4.2, which calls this a feature:
  "the extension production already uses").
- Production's retriever ranks candidates by `similarity(t.title, e.summary)` (`comprehend.py:1647`),
  a pg_trgm title-to-summary score. That is the same signal family.
- The sample that gets drawn will hold roughly 90% of its positives in ≥0.35 cells, so any
  trigram-based retriever will score near-perfect recall on it.
- §10 claims that "the labels stay valid for any approach … because no system chose them". That
  holds for the weights **in expectation** and fails for the sample actually drawn. Trap 5's own
  test: *would this pair be in my eval set if the trigram retriever were wrong?* Mostly no.

**The random branch buys almost nothing.** 60 draws, 10% of the operator's effort, against a
pair-level prevalence of about 5×10⁻⁵ gives **about 0.003 expected positives**. It will print
0/60, and the rule of three then bounds prevalence at ≤5%, which is already known to be true by
four orders of magnitude. §7 calls it "the only unbiased read on how rare positives are". It is
unbiased and uninformative.

**What recall needs, for scale.** Resolving recall to ±7.5 points at p ≈ 0.5 needs about 170
positives with a design effect of 1. HT weights spanning 10³ imply a Kish design effect far above
1. The design falls short by one to two orders of magnitude.

**What I would do instead.**
1. Make sub-project 1's spec state its decision first. For example: choose between the current
   retrieve-then-judge approach and a clustering approach if their cross-outlet recall differs by
   at least X points.
2. Derive the positives needed from X, and pre-register the expected yield (the table above,
   corrected against a free dry-run on the host).
3. Only then choose the design.
4. **Measure precision separately**, by labelling a random sample of **each system's own asserted
   links**. That is unbiased for precision and not a Trap 5 violation. Keep independent sampling
   for recall, where it is actually needed.
5. If pair sampling survives:
   - put the weight on near-time cells and drop the 3–30 d strata;
   - drop the random branch, or cut it to about 2% and label it a smoke test;
   - add an **independent stratifier that is not a trigram**. Time gap alone is one candidate.
     Shared entities is another, if it is recorded but applied in only one arm (§5 keeps
     entities out on purpose, and that is right for the primary arm).

---

## Objection (c): the constraint this design will hit in 6 months. Every comparison becomes a full-span as-of replay.

**The objection.** §10: "the labels stay valid for any approach." Valid is not the same as
**scorable**. Sub-project 1's candidates are **stateful**:
- production retrieval reads `events`, `event_entities` and `occurred_at`/`created_at` as of the
  call (`comprehend.py:1641–1662`);
- any clustering approach consumes the whole item stream in order.

To score a system on a gold pair, you must know what that system would have concluded about
those two items as of their capture. The anchor is uniform over **all of P** and the partner lies
within ±30 days, so the ~1,200 gold items are spread evenly across the **entire capture history**.
Every approach sub-project 1 wants to compare therefore needs a correct as-of replay of the whole
span.

**Evidence: correctness risk.**
- The first as-of replay here (M2) had a **blocker** found only by review. The cut-off had to be
  the batch's earliest member, not its anchor, or agreement "would have been pushed toward
  100%". Entity births had to be re-dated, because `write_extraction` tags matched older events
  with new entities. (Spike doc, "Cut-off and births, corrected by the pre-run review".)
- The memories `a-ledger-dates-what-it-records` and `reconstruction-drifts-from-production` record
  the same class twice more.
- **Estimate:** this is the most error-prone step in the repo's recent history, and this design
  makes it mandatory for every comparison.

**Evidence: cost (estimate).**
- M2 spent **$5.53 for 450 calls** covering 150 five-item batches × 3 runs, about 2,250 item
  extractions, so roughly **$0.0025 per item-extraction** (spike doc, M2 result).
- A stateful system must replay every item in the span, not only the gold items. Capture runs at
  about 800 items/day. By the time 600 labels exist (weeks to months at 30/day), the span is
  2–3 months, or **50–70k items**.
- **Estimate:** about $120–175 per system arm per evaluation on Sonnet, before any re-run. Three
  to five arms in sub-project 1 would cost $500+. Alternatively, sub-project 1 restricts itself
  to stateless comparisons, which is a quiet narrowing of "any approach".

**Evidence: the baseline evaporates.** Production's own historical links are the only
free-to-score system: pair (a, b) is linked if both items assert one event. Sub-project 1
exists to replace that system. Six months from now, "the current system" will be a different
pipeline, and the historical links will be the scores of a retired design.

**What I would do instead.** Concentrate the gold items in time so that replay cost and replay
risk scale with **K windows**, not with the full history. The window census from (a) does this
by construction: a replay covers each window plus a look-back, a few thousand items and single
dollars per arm. If pair sampling survives, restrict anchors to a handful of pre-registered capture
days and draw pairs within them. Either way, **state in the spec that scoring requires replay**,
and budget the harness as part of sub-project 0, not as a surprise in sub-project 1.

---

## The sampling math (§4.2–4.3), checked

**Correct:**
- **p = q_a(b) + q_b(a)** is right. The events {anchor a, candidate b} and {anchor b,
  candidate a} are disjoint.
- The candidate relation is symmetric: a ±30-day window, and pg_trgm `similarity` is symmetric.
- cell_a(b) and cell_b(a) share band labels but have **different sizes**, and W(a) ≠ W(b). The
  formula handles this, as long as the implementation really runs b's candidate query (§4.2
  says it does).
- π ≈ 1 − (1 − p)ⁿ is right for n with-replacement draws **within one fixed population**.

**Errors and gaps:**

1. **Discarding cross-round duplicates biases the per-round HT, and fixing it is free.**
   - T̂_r = Σ_{s_r} y/π_r is unbiased only if s_r contains *every* pair drawn in round r.
   - Discarding a pair drawn in round r because round s < r already holds it removes it from
     s_r, so E[T̂_r] < T_r.
   - With p ≈ 10⁻⁶–10⁻¹⁰ the bias is negligible in size. The fault is in the design: the discard
     is a **queue** concern (do not show a pair twice) mixed into the **estimator**.
   - Fix: log every draw in a `gold_draws` table (round, anchor, candidate, branch, p, and a
     duplicate flag) and reuse the existing label.
   - That also permits **Hansen–Hurwitz**, the natural estimator for with-replacement PPS:
     T̂_r = (1/n_r) Σ_draws y/p. It has an unbiased variance estimate from the spread across
     draws.
   - The spec stores only a duplicate *count* (§5, `gold_rounds`), so neither correction is
     possible afterwards.

2. **Rounds sample different populations.**
   - |P| and every C(a) grow daily, as new items enter the ±30-day window of existing anchors.
   - A pair first drawn in round 3 also had non-zero probability in rounds 4…R, but it stores
     only round 3's π.
   - Each round's HT estimates its own snapshot's total. "Combined, each weighted by its own
     draw count" (§4.3) is a draw-weighted blend of snapshots, not an estimate of any one
     population's prevalence or recall.
   - Freezing one snapshot and drawing everything at once removes this whole class of problem
     (see (a)).

3. **The estimator for a ratio is unstated.**
   - Prevalence and recall are ratios, so they need a Hájek or ratio estimator with linearised
     variance.
   - With about 10 positives and weights spanning 10³, normal-approximation intervals are not
     valid. §7's "with intervals" must name its method: for example a bootstrap over draws, or
     over anchors, since pairs sharing an anchor are correlated.

4. **The §4.5 checks cannot fail on the realistic defects.**
   - "Every π in (0, 1]" holds for any positive p, including one computed with the wrong
     |cell|, a W(a) summed over empty cells, or |P| from a filter that differs from the
     anchor filter.
   - §9's "matches a hand-computed case" reads the formula; it does not run the sampler.
   - Add an **empirical-frequency test**: on a seeded corpus of about 30 items, run about 10⁶
     draws and chi-square the observed pair frequencies against p. That is the check that
     executes the code (global rule, `the-rule-exempts-its-own-origin`).

5. **Band boundaries in float4.**
   - pg_trgm's `similarity()` returns `real` (float4).
   - Trigram similarities are ratios of small integers, so exact 0.20, 0.35 and 0.50 values
     occur. float4(0.35) ≠ float64(0.35).
   - If |cell| is counted with bands computed in SQL, and the drawn pair's band is recomputed
     in Python from the stored `similarity` (§4.5's "cell must match recorded band values"),
     the two can disagree. The result is either a spurious refusal or a silently wrong |cell|.
   - Compute bands in one place only. This is the same class as M2's `0.85 − 0.05` boundary.

6. **π storage.** π values around 10⁻⁹ need `DOUBLE PRECISION`, not `REAL`. Compute them as
   `-expm1(n·log1p(-p))`. Better still, store p and n and derive π.

7. **The population predicate must be the production function.**
   - `common.is_quote_page` is a Python regex (`common.py:745`), and the sampler needs it in
     SQL for |P| and C(a).
   - A SQL re-derivation drifts silently (memory `reconstruction-drifts-from-production`). Any
     disagreement makes |P| wrong for every q.
   - Either filter in Python over the snapshot, or assert that the SQL and Python predicates
     agree on the full snapshot before a round.

8. **The time gap uses `created_at`, which is capture time.**
   - The 2026-09-04 first-fill (1,926 items) and every later feed addition stamp weeks-old
     articles with one capture time, so they land in the <1 d band.
   - Record `published_at` beside `gap_hours`, and exclude first-fill passes from the
     population, or band by `coalesce(published_at, created_at)`.

9. **Raw titles carry outlet suffixes** (" - Reuters", " | Al Jazeera"). These inflate trigram
   similarity for same-outlet pairs. `probe_corroboration.normalise_title` already strips them.
   This is an efficiency problem, not a bias, but it moves stratified weight toward same-outlet
   pairs.

## Other findings

- **Migration number collision (verified).** `migrations/0016_integrate_link_defers_up.sql`
  already exists (commit `b204c5a`). The spec's "migration 0016" must be 0017.
- **The consistency re-shows are Trap 8 in the spec that cites Trap 8.**
  - Pairs are marked for re-show at *sampling* time, with probability 0.05. That gives about
    30 repeats, of which about 85–95% are obvious "unrelated" pairs that agree by construction.
  - Expected `same_event` repeats: **about 0.5**. The figure will read around 95% whatever the
    operator's consistency on positives is.
  - Fix: decide the re-show at *label* time, with a known label-conditional probability (for
    example 100% of `same_event`, `same_story` and `cant_tell`, and 3% of `unrelated`). Report
    agreement per label, or kappa on the non-trivial subset.
- **`ON DELETE RESTRICT` adds nothing new.** `feed_sightings.item_id` already references `items`
  with the default NO ACTION (`migrations/0008_capture_telemetry_up.sql:54`), so item deletion is
  already blocked. Retention (`news-brief-uh0`, open) must handle that FK anyway. The spec's
  reasoning is sound. Its "verified" count of 4 `DELETE FROM` statements, none on `items`, is
  correct for the non-test code.
- **The basis for each judgement is thin, and unmeasured.** Bodies average 154 characters, and
  only about 25% carry 150 characters or more (memory `newsbrief-comprehend-cost`,
  `newsbrief-capture-feature`). "Up to 400 chars" is mostly a headline plus a lead.
  - No labelling guideline defines event granularity. For example: is "Fed holds rates" the same
    event as "Powell says cuts not imminent"? Test-retest consistency cannot detect a definition
    that drifts across 600 labels.
  - Write a one-paragraph guideline with 5 worked boundary cases before round 1.
- **`same_story` forces the 30-day window** that halves the `same_event` yield. It also still
  cannot reach §1's "weeks and longer". Sub-project 2 will need a different window in any case,
  and the spec freezes the constants "only before the first round". The spec already deferred
  connections as too fuzzy to judge from two headlines, and the same argument applies to
  `same_story`.
- **Wiring checks that passed:**
  - `_handle_update` is the single-user gate (`brief.py:3552`);
  - callbacks are answered first (`brief.py:1142`);
  - `rmsrc:` and `close:` sit before the wizard-state guard (`brief.py:1147–1167`), and an
    `lbl:` route must go there too, or `w = _WIZARD.get(chat_id)` swallows it as a stale
    wizard button;
  - `lbl:<pair_id>:<code>` fits within the 64-byte `callback_data` limit.

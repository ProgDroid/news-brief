# Red team, revision 5: gold set by window census (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md`, revision 5
**Prior reviews:** `...-redteam.md` (rev 1), `-r2.md`, `-r3.md`, `-r4.md`. Objections rev 5
resolved are not repeated.
**Stance:** hostile staff engineer, asking whether this is the wrong thing to build.
**Evidence rules:** **verified** means read in the repo at the cited line. **Computed** means
arithmetic I ran with `py` + scipy 1.17.1 (inputs named inline). **Estimate** means arithmetic on
figures the repo records. No production data was queried.

---

## Verdict

Rev 5 took r4 cleanly where it applied its decisions:
- the detector tier, its LLM call, the spend-row CHECK violation (r4 blocker 1) and the
  mis-specified `1 / p` weight (r4 blocker 4) are gone, because the census makes no model call
  (§6.1, §7);
- the MDE table is now **right for a fixed-α test**. I reproduced every cell of both columns
  (**computed**; e.g. m̄ = 8, CV = 0, ρ = 0.1: 37.6 at K = 8, 24.4 at K = 16);
- the block arithmetic is right: P = 2026-10-01 gives [09-18, 09-28], 10 days; P = 09-30 gives
  9 days and refuses. The flood claim is corrected (§4.2 step 3);
- the bootstrap now resamples windows only, the counter counts sessions, the precision sample is
  group-first, and the knob-read trap has a test;
- the uh0 note exists (**verified**, `.beads/issues.jsonl:153`, `notes`, updated 2026-09-28).

The O'Brien–Fleming thresholds are also correct **as z-scale OBF**: two equally-spaced looks at
two-sided α = 0.05 give nominal 0.0052 and 0.0480 (**computed**, bivariate normal with
correlation √0.5; Lan–DeMets OBF spending at t = 0.5 gives 0.0056).

What is wrong is what the two-stage rule **does** to the design, not its constants:

1. At its own stage-1 threshold, stage 1 cannot screen anything plausible, so the extension is
   near certain. The two-stage rule then saves labour only by **deferring** windows 9–16 until
   after sub-project 1 has shown the operator system output, which breaks the census's own
   blindness rule. Labelling all 16 blind now is simpler and strictly better (objection a).
2. With census adjudication removed, nothing bounds operator misses, and the misses are
   **differential**: they fall on lexically dissimilar pairs, which is pg_trgm's failure mode and
   the thing sub-project 1 exists to fix. The spec never says whether a pool-adjudicated
   "same" re-enters the headline (objection b).
3. The retention exemption is a note with the wrong lifetime and no enforcement, and the deferred
   stage 2 is what drags the census into the window where it bites (objection c).

**Not ready for an implementation plan.** Six further blockers are listed at the end. None is
large; four are one-paragraph spec edits. But two of them (the unnamed test statistic and the
multi-comparison rule) are pre-registration content, and a plan written against the current text
would build a stage machine whose decision rule is undefined.

---

## Objection (a): a simpler design. Label all 16 windows blind now, test once at 0.05, and delete the two-stage machinery.

**The claim in §4.4 is wrong for the rule §4.4 adopts.** "Stage 1 (K = 8) is a coarse screen. It
can show that an approach is far better or far worse, around 35–40 points." The 35–40 column is
computed at α = 0.05 (`t₇: 2.365 + 0.896 = 3.26`). But the same section says stage 1 "decides
only at nominal p < 0.005". At that threshold the critical sum is `t₇,0.9975 + t₇,0.80 = 4.029 +
0.896 = 4.93`, **1.51×** the table's (**computed**).

| m̄ | CV | Stage 1, as tabled (α = 0.05) | Stage 1, as ruled (p < 0.005) | K = 16 (α = 0.05) |
|---|---|---|---|---|
| 6 | 0 | 37 / 41 / 53 | **56 / 62 / 80** | 24 / 27 / 34 |
| 8 | 0 | 34 / 38 / 51 | **51 / 57 / 77** | 22 / 24 / 33 |
| 10 | 0.5 | 32 / 38 / 54 | **49 / 57 / 82** | 21 / 25 / 35 |

(ρ = 0.05 / 0.1 / 0.3; same formula and d = 0.5 as §4.4.)

**So stage 1 almost never stops** (**computed**, noncentral t₇, m̄ = 8, CV = 0, ρ = 0.1):

| True end-to-end difference | P(stage 1 decides) | Power at K = 16 |
|---|---|---|
| 10 points | 0.02 | 0.20 |
| 20 points | 0.07 | 0.62 |
| 30 points | 0.19 | 0.93 |
| 40 points | 0.39 | 0.995 |

The spec's own illustration puts end-to-end differences at 7–11 points (§4.4, citing r4). At any
difference below 30, the extension is triggered at least 80% of the time. The two-stage rule buys
an early stop only for differences so large they would be obvious by eye.

**It may not be able to stop at all.** §4.4 does not name the test (blocker 1). If it is the
natural exact test for 8 paired clusters, a sign-flip or randomization test over per-window
differences, the smallest attainable two-sided p is `2 / 2⁸ = 0.0078`. **p < 0.005 is then
unreachable at K = 8** (**computed**). A window bootstrap at K = 8 cannot resolve a 0.25% tail
either.

**And the only way it saves labour is the way it breaks blindness.** Stage 2 is labelled only
"if sub-project 1's first comparison is inconclusive at stage 1" (§3). That comparison needs the
pooled adjudication of §6.4, in which the operator rules on every link a system made that his
blind partition rejected. He then blind-labels windows 9–16, drawn from **the same 10-day block**
and therefore the same running stories, after seeing system opinions on those stories. That
violates "the operator sees no system opinion at any point of the census" (§3, carried forward).
It re-creates, for half the scored data, the learning drift r4 objection (a) removed. It also
makes stage-1 and stage-2 labels non-exchangeable, which the cumulative OBF statistic assumes.

If the operator instead labels 9–16 before any comparison, the two-stage rule saves nothing and
is just a 16-window census with an extra look.

**The simpler design.**
- Label all 16 windows (17 sessions with the repeat) blind, before sub-project 1 shows him
  anything. At the §4.5 budget that is ≤ 23 h; the stage-1-only path is ≤ 12 h.
- One pre-registered test per comparison at α = 0.05 on K = 16. The K = 16 column is the design.
- Keep the window-2 go/no-go as the only stop. Its floor of 8 is already derived from the K = 16
  column (§4.5), which is the spec quietly admitting 16 is the plan.

**What it deletes:** the `stage` column in `gold_window_order`, the stage-complete logic in the
nudge (§6.5), the OBF constants and their mutation test (§10), and the multi-comparison ambiguity
in blocker 2. **What it costs:** up to ~11 more operator hours, spent in the one case (a ≥ 50-point
difference) where they would not have been needed.

---

## Objection (b): an assumed requirement. That operator misses are few and non-differential enough for the blind partition to be the recall denominator.

**Rev 5 removed the only measurement of operator recall and put nothing in its place.** §4.7
says a missed pair "counts against the systems that link it, until sub-project 1's pooled
adjudication rules on it", and "no standalone operator recall figure is produced". That handles
the **precision** side. It is silent on the **recall** side, which is the headline.

**Direction of the bias, and why it matters.**
- A missed same-event pair drops out of the headline's denominator: a two-item event becomes two
  singletons, and every system is excused from it.
- Which pairs get missed is not random. The operator scans ~170 items in capture order (§6.3)
  with one search aid, an **operator-typed text filter**. That is a lexical tool. Pairs he finds
  share words. Pairs he misses do not: guideline case 1, "Israel strikes depot near Tyre" and
  "Lebanon says Israeli strike killed two", is the type.
- Production's ranking is pg_trgm similarity over what the item reads like (`9c40935`, "rank
  candidates by what the item reads like"; `comprehend.py:1647`, `similarity(t.title, e.summary)`
  inside `candidate_events`, `comprehend.py:1572`). Its failure mode is the same lexically
  dissimilar pair.
- So the blind denominator is enriched for pairs lexical retrieval already finds. The headline is
  inflated for everyone, more for lexical systems, and **a new retrieval method's gain on hard
  pairs is invisible in the headline** and shows up only as "false positives" until adjudicated.
- Sub-project 1 exists to fix retrieval (§1: "the current evidence points at retrieval"). The
  bias points against exactly the comparison it will run, on top of a K = 16 MDE of ~25 points.

**The consistency repeat cannot bound it.** Window 2's pass 2 is the same operator with the same
lexical aid. Shared systematic misses agree with each other, so blind-to-blind recall overstates
true recall.

**The spec never says whether adjudication feeds back into recall.** §4.4 pins the headline to
"the **blind** partition". §6.4 accumulates pooled "same" decisions in `gold_adjudications`. If
they do not merge groups, the bias above is permanent. If they do, the headline is no longer the
blind partition, and its denominator depends on which systems happened to be compared (the
two-system pooling problem: a pair only a later system finds was never in an earlier
comparison's denominator).

**What I would do.**
- Pre-register an **adjudicated partition**: blind groups plus every pool-adjudicated "same",
  merged transitively. Make it the headline in sub-project 1, with the blind partition as the
  sensitivity. This fixes the direction: a system that finds a missed pair gets recall credit.
- Pool cumulatively: when system C is compared, earlier comparisons are not re-scored, but the
  headline for C uses every adjudication to date. State that.
- Print the share of headline groups that exist only because of adjudication, per system that
  proposed them. That share is the operator-miss rate the census no longer measures.

---

## Objection (c): the 6-month collision. Stage 2 lands months from now on September items, after the exemption that protects it has quietly expired or been skipped.

**When stage 2 actually happens.** Its trigger is sub-project 1's first comparison (§3), which
needs a replay harness, at least two systems, two runs per arm at $160–240 (§12), and pooled
adjudication. Realistically that is weeks to months out. Windows 9–16 are then labelled on items
from 2026-09-18 to 09-28, in December or later. Three things collide there.

**1. The exemption's lifetime is shorter than its consumers.** §3 exempts items "until
sub-project 1 closes". But §13 hands the same windows to sub-project 2, which "extend[s] windows
contiguously later" and "join[s] groups across a boundary". That needs the items between windows,
and sub-project 2 depends on 1 (§2), so it starts **after** the exemption ends. The replay "from
capture day one" (§12) is also what sub-project 2 would score against.

**2. The exemption is a note, not a mechanism.**
- It lives in the `notes` of `news-brief-uh0`, priority 3 (**verified**,
  `.beads/issues.jsonl:153`). The note ends "Add a dependency on the sub-project 1 bead once it is
  filed". The bead shows `dependency_count: 0`, and no sub-project 1 bead exists yet.
- The repo's only retention default is 90 days (`retention.py:5`, `NEWSBRIEF_RETENTION_DAYS`).
  Applied to `items`, it reaches 2026-09-04 on 2026-12-03, likely before stage 2 is labelled.
- Deleting an item **cascades** silently into `assertions` (`migrations/0006_knowledge_base_up.sql:109`,
  `ON DELETE CASCADE`) and `item_triage` (`migrations/0009_comprehension_up.sql:9`, same). So a
  retention job that respects only the gold RESTRICT FK deletes pre-block items **and** the
  production KB rows the replay is meant to match. Nothing fails; the comparison baseline is
  just gone.
- `metadata-is-not-state`: a bead note states intent. The implementer of `uh0` has to find it.

**3. The labels themselves age.** Operators open links for unsure items (§5). Three-month-old
news links rot or paywall, so stage 2's unsure share will be higher than stage 1's for reasons
unrelated to the events. His grouping standard also drifts over months, and the repeat measures
drift over ≥ 7 days only (§4.6).

**What I would do.**
- Objection (a) removes most of this: 16 windows labelled now means no stage 2 in December.
- Turn the exemption into code: the item-pruning half of `uh0`, when built, reads
  `gold_block.block_end` and refuses to delete `items.created_at < block_end` while any `gold_*`
  row exists. Add that as a test in 0017's bead, not a note on `uh0`.
- Scope the exemption to "while any gold set is in use", not "until sub-project 1 closes".
- Or take r4's snapshot option: a dump of `items` + `outlets` for [2026-09-04, block_end],
  which also frees production retention.

---

## Correctness blockers (beyond the three objections)

1. **The test behind "nominal p" is unnamed.** §4.4 sizes with t on K − 1 df; §8 computes
   intervals by window bootstrap; the heading says "paired McNemar". These are three different
   inferences. Plain McNemar ignores clustering and is anticonservative; a K = 8 bootstrap cannot
   resolve p = 0.005; a sign-flip test cannot reach it (0.0078). Name one, pre-registered: the
   coherent choice with the sizing is a **paired t-test on per-window differences in the
   confirmed share, K − 1 df**, or a cluster-adjusted McNemar (Obuchowski 1998). Make §8's
   intervals the same method.
2. **The two-stage rule is undefined when sub-project 1 runs several comparisons.**
   - If comparison 1 decides at stage 1, no extension happens. Is comparison 2 on K = 8 tested
     at 0.005 or 0.05? Unstated.
   - A comparison 2 that is inconclusive at K = 8 cannot trigger the extension ("first
     comparison" only), so sub-project 1 is stuck at a 57-point screen.
   - "After it, every comparison uses all 16 windows at p < 0.05, with no interim look" is false
     for any comparison whose stage-1 data was already examined (e.g. A vs C computed alongside A
     vs B). That was an interim look.
   - No familywise control across comparisons is stated, nor explicitly waived.
   Objection (a) deletes this blocker. Otherwise: every comparison declared before the stage-1
   look shares the OBF rule; a comparison first examined after the extension gets 0.05; and the
   familywise policy is written down.
3. **The §4.4 "coarse screen, around 35–40 points" sentence is false for stage 1 as ruled.** It
   is ~57 at ρ = 0.1 (objection a). If the two-stage rule stays, the table needs a third column
   at the stage-1 threshold, and §10's MDE test must reproduce it.
4. **The headline after pooled adjudication is undefined** (objection b). Sub-project 1 inherits
   a metric whose denominator it cannot compute without a decision this spec is the place to make.
5. **The precision sample cannot draw 10 group-uniform pairs under `UNIQUE (item_a, item_b)`.**
   - At the floor, m̄ = 8, most multi-outlet groups are two-outlet with **one** cross-outlet pair.
   - 10 group draws with replacement from 8 groups hit on average **5.9 distinct groups**
     (**computed**; 5.0 at m̄ = 6). Repeat draws of a one-pair group collide with the unique
     constraint (§7), and without replacement the sample is no longer group-uniform.
   - Specify: groups without replacement, `min(10, groups)` pairs, one pair per group, the
     estimator (group-weighted mean), and a `CHECK (item_a < item_b)` so (b, a) cannot bypass the
     uniqueness.
6. **The gap check's population cannot be selected.** §4.3 restricts to "items comprehended under
   the current ranking". The ranking is code (`9c40935`, committed 2026-09-09 10:15 UTC), not a
   recorded version: `item_triage` carries only `triage_prompt_version` and
   `integrate_prompt_version` (`migrations/0009_comprehension_up.sql:31–32`). `gold_prepare`
   collects only `c439ade`'s deploy date (§6.1 item 4). Either collect the `9c40935` deploy
   timestamp as an input and filter on `integrated_at` after it, or drop the restriction, since
   the gap distribution is a property of capture, not ranking. (`a-ledger-dates-what-it-records`:
   do not substitute a migration timestamp.)

**Not blocking, write down:**
- **Information fraction is not 0.5 by construction.** Windows 9–16 have their own m̄ and CV.
  Fixed nominal thresholds assume equal information; Lan–DeMets spending on observed information
  is the robust form. Negligible in practice.
- **The §4.5 floor of 8 is coherent with the K = 16 column** (24 / 26 at ρ = 0.1, CV 0 / 0.5),
  and incoherent with stage 1 (57). It is judged on the mean of two windows from two different
  strata, which is a weak estimate of m̄. Print both window values.
- **`gold_prepare` as a `brief.py` mode** runs `REQUIRED_ENV` and the full operator seed block
  (`brief.py:4010–4040`), so "needs no Anthropic key" is true of the code path, not of the
  process. Harmless in the main container; the usage string at `brief.py:4057` needs the mode.
- **Stratum sufficiency.** Four eligible windows per stratum are needed from ~10 per stratum. The
  refusal when a stratum has fewer than 4 windows of ≥ 40 items is unspecified.
- **Deploy dates are recall.** The operator "supplies" `c439ade`'s deploy date. Per his own rule
  (recognition, not recall), the runbook should offer the host's candidates (image digest
  creation time, `docker inspect` of the running container), and he confirms one.

---

## How revision 5 answered revision 4

| Rev 4 item | Rev 5 | Grade |
|---|---|---|
| (a) Pool at scoring time, drop detectors | Adopted | **Answered.** It removed the only operator-recall measurement, and the recall side of pooling is undefined (objection b, blocker 4) |
| (b) MDE at the wrong layer, K = 8 not "architectural" | t quantiles, honest "unknown" framing, two-stage 8 → 16 | **Answered for the fixed-α table; the stage-1 claim is wrong under its own OBF threshold** (objection a, blocker 3) |
| (c) Replay makes `items` permanent | Exemption noted on `uh0` | **Partial:** note only, lifetime ends before sub-project 2, cascades unaddressed (objection c) |
| Blocker 1: spend CHECK | No model call | **Resolved** |
| Blocker 2: MDE arithmetic | Recomputed | **Resolved** (every cell reproduced) |
| Blocker 3: earliest P, flood | 2026-10-01, flood corrected, `c439ade` recorded | **Resolved** |
| Blocker 4: group-level `1 / p` | Weighting deleted | **Resolved** |
| Pair-uniform precision sample | Group-first | **Answered in intent;** collides with the unique constraint (blocker 5) |
| Knob-read trap | Test | **Resolved** |
| 9-vs-8 counter | Counts sessions | **Resolved** |
| Two-stage bootstrap | Windows only, (K − 1)/K stated | **Resolved;** the test it feeds is unnamed (blocker 1) |
| Gap check biased to pass | Bands pre-registered, bias stated | **Resolved;** its population filter needs an input nobody collects (blocker 6) |
| Held-out split | "None", sub-project 1 names dev data | **Resolved** as stated |

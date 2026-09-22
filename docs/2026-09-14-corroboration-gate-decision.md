# The §8.2 corroboration gate: what it should measure before it fires

**Bead:** `news-brief-yxd` · **Date:** 2026-09-14 · **Status: ACCEPTED 2026-09-14** by the operator; amended into spec §8.2 as Amendment 1 and implemented in `scripts/score_comprehension.py`.

`news-brief-bqa.11` holds a one-shot pre-registered gate
(`scripts/score_comprehension.py`) that has never run against production data.
`bqa.19`'s close-out raised the question of whether it should run at all, since
its corroboration floor appears to sit above what the pipeline can reach. This
records the answer and its reasoning, per `yxd`'s acceptance criterion: **in the
spec, dated, and saying plainly where a number was changed after seeing data.**

Written without access to production Postgres (`live-state-on-deploy-host`).
Every figure below is quoted from a dated measurement, not re-derived here.

---

## 1. What §8.2 says, and what is measured against it

Spec `2026-09-04-comprehension-pipeline-design.md` §8.2:

> **Floor: ≥10%** of events carry assertions from **2 or more distinct outlets**.
> Below this the event layer bought essentially nothing over the claim ledger,
> which is the entire justification for §2.1.
> **Ceiling: ≤60%.** Above this, suspect the matcher is **over-merging**.

| quantity | value | source |
|---|---:|---|
| `corroboration_by_outlet`, whole KB | **6.5%** (262/4038) | `bqa.25`, 2026-09-09 |
| the same figure, drifting as the corpus grows | 6.64% → 6.49% → **6.43%** | `newsbrief-comprehension-pipeline` |
| fresh 7h and 13h windows | 5.7–8.3% | cohort doc §4 |
| clean-anchor 6h cohorts | 2.7% / 3.3% | cohort doc §6 |
| after merging every duplicate the detector finds | 6.7% | `bqa.25` |
| §8.2 floor | 10% | spec |

## 2. The premise as stated does not survive its own source

`yxd` frames this as *floor above ceiling*, and that framing is weaker than it
reads. Two corrections, both from `bqa.25`'s own text:

**The 6.7% is not a ceiling.** `bqa.25` calls it **"a FLOOR twice over"**: the
union-find merge *understates* corroboration, because a 94-event blob collapsing
into one cluster yields a single multi-outlet event where 46 disjoint pairs would
yield 46; and the separator sees roughly **13% of positives**, so the detector
whose merges set the bound is itself missing most of what it is bounding. The
number bounds *what merging the duplicates this detector can see would buy*. It
is not a bound on corroboration in principle, and it cannot license a new floor.

**The gate never reads the ceiling anyway.** `run_gate` compares the live
`corroboration_by_outlet(conn)` — the actual cumulative rate, 6.5% — against
`CORROBORATION_FLOOR`. So the outcome *is* determined, and it is a FAIL. But it
is determined by the measured rate, not by the ceiling argument, and the reason
that distinction matters is §3.

## 3. The defect is the quantity, not the threshold

`bqa.18` recorded this before any of the ceiling work: the gate **"applies a
FIXED floor to a CUMULATIVE non-stationary quantity, which cannot be right at two
corpus sizes."** That is the finding this decision turns on.

The measured corpus was built under three different candidate rankings, whose
batched `recall@30` — the probability a duplicate is ever *offered* to the model,
and therefore the hard ceiling on any corroboration the matcher can record — differ
by a factor of three:

| ranking | shipped | batched recall@30 | source |
|---|---|---:|---|
| recency | original | **15%** | candidate-ranking §1.2 |
| entity overlap | `51c850c`, 2026-09-08 | **27.5%** (115/418) | `bqa.24` close |
| pg_trgm similarity | `9c40935`, 2026-09-09 | **44.0%** (184/418) | `bqa.24` close |

Essentially all of the ~4,038 events in the 6.5% figure were created under the
first two. **The cumulative rate measures a pipeline that no longer exists**, and
it cannot recover: history dilutes it permanently, so even a perfect ranking from
here leaves the cumulative number low for as long as the corpus is dominated by
events the old rankings never offered a duplicate for.

**And the number is drifting the wrong way on its own.** The whole-KB rate has been
recorded at 6.64% → 6.49% → 6.43% as the corpus grows — *waiting for more data makes
the gate harder to pass, not easier*. That is the signature of the defect rather than
a separate finding: a cumulative rate over a growing corpus whose new events are a
shrinking fraction of the whole cannot rise to meet a fixed floor, however good the
current pipeline gets.

Firing the gate on that number records a FAIL attributable to two retired
rankings. That is not a verdict on whether the event layer buys anything over the
claim ledger — it is the `the-probe-measured-the-wrong-layer` shape that §8.2's
own closing paragraph warns about.

## 4. Decision

**Option (3) of `yxd`, with the emphasis moved off the threshold.** Not *lower the
floor until the ceiling clears it* — §2 shows the ceiling cannot justify a number.
Instead:

1. **The corroboration direction becomes cohort-scoped.** It measures events
   created after a named cutover, under one ranking, with the maturity horizon
   from `bqa.19`: an event younger than the horizon is **excluded**, never counted
   as uncorroborated, and "too soon to measure" reports a reason rather than a rate.
   The machinery already exists — `--cohorts` — and is currently observation-only.
2. **The 10% floor does not move.** The honest position is that this quantity has
   never been measured on a corpus built by the current pipeline, not that 10% is
   too high. Moving a pre-registered number on data that cannot support the move is
   precisely what pre-registration exists to prevent.
3. **Ranking and quality attribution move to `recall@30`** on the banded eval set
   no arm selected. It resolved **44.0% vs 27.5%, z = 4.98, p = 6.4e-07**, where
   corroboration resolves ~3.8pp against a detected-duplicate effect of **+0.25pp**
   (cohort doc §6.3). Corroboration stays §8.2's existence test; it is not an A/B
   instrument at this corpus size.
4. **The ≤60% over-merge ceiling is untouched.** Nothing here bears on it, and it
   is the direction that presents as success.

### What was changed after seeing data, stated plainly

**The quantity was.** §8.2 as pre-registered on 2026-09-04 gated the cumulative
whole-KB rate; this amendment gates a cohort. That change was made *after* seeing
6.5%, and a future cohort pass **must not be read as the pre-registered test
passing** — the pre-registered test, on the quantity as originally written, fails
at 6.5%. What is claimed here is narrower: that the original quantity was
confounded by ranking generation in a way that was not visible when it was
written, and that the confound was named independently in `bqa.18` before the
ceiling work existed.

### The caveat this does not remove

Cohort-scoping fixes ranking dilution. It does **not** fix §6.4: `corroboration_by_outlet`
**moves with news volume**, and any two windows differ in volume — the two arms of the
clean-anchor run differed by 37% (59 vs 43 events/hour). An absolute floor on a
volume-sensitive quantity is still shaky. So a cohort measurement landing near 10%
should be read as **not resolved**, not as a pass or a fail.

If that is unacceptable, the alternative is to stop treating corroboration as a
gate at all and keep it as a reported observable — option (1)'s honesty without
spending the one-shot. That option is live and is not obviously worse.

## 5. What shipped

Accepted 2026-09-14. Done in this change:

- **§8.2 Amendment 1** in `docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md`,
  dated, leading with the "changed after seeing data" paragraph so no future cohort
  pass reads as the pre-registered test passing.
- **`gate_corroboration`** in `scripts/score_comprehension.py`: the outlet direction
  measures one cohort `[cutover, now − horizon)`; the 10% floor and 60% ceiling are
  unchanged; too-soon or empty reports NOT MEASURABLE, never 0.0.
- **The gate refuses without a cutover** rather than falling back to the whole-KB
  rate, and takes exactly one `--horizon-hours` (the horizon is not pre-registered).
- **`summarize`** gives three outcomes: PASSED, FAILED, and **NOT RESOLVED** — the
  last for a run where every non-pass was unmeasured. Still a non-zero exit.
- Nine tests, each checked by mutation: reverting the gate to the whole-KB rate
  fails exactly 3, reporting not-measurable as a 0.0 floor failure fails exactly 2,
  and a silent whole-KB fallback with no cutover fails exactly 2 — counts
  pre-registered before the mutations were run.

## 6. What is still outstanding

`bqa.11` is now unblocked in code but needs two things this repo cannot supply:

- **The cutover, read on the host**, not inferred: the instant the pg_trgm image
  (`9c40935`) began serving. `bqa.19` §3 is the record of what inferring it costs —
  a confident wrong table off a migration timestamp that predated the change by 3h27m.
- **~13h of elapsed post-cutover events** before a 6h horizon has anything to measure.

Then:

```sh
docker compose run --rm --entrypoint python newsbrief \
    scripts/score_comprehension.py --cutover <ISO8601> --horizon-hours 6
```

A NOT RESOLVED verdict is the expected outcome if the cohort is thin, and it is the
correct one — it does not spend anything.

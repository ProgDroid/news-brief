# Measuring corroboration in cohorts, and the anchor that was wrong

**Bead:** `news-brief-bqa.19` · **Commits:** `267c97a`, `b0a09c8` · **Date:** 2026-09-09

Companion to `docs/superpowers/specs/2026-09-08-candidate-ranking-design.md`, whose §4
called the missing window argument a requirement rather than a note. This records what
was built, the measurement error the first live run made, and what the run established
anyway.

---

## 1. Why a cumulative number could not answer the question

`corroboration_by_outlet` aggregates over the whole KB. With ~3,000 events, nearly all
created before the candidate-ranking cutover (`51c850c`), even a perfect post-cutover
cohort moves the blended rate by a rounding error. Every post-change run therefore
produces a figure attributable to neither ranking.

Scoping by `events.created_at` is necessary but **not sufficient**, and this is the part
worth remembering. Corroboration accrues over time: an event becomes multi-outlet when a
second outlet's item arrives, minutes or hours later. A cohort born after the cutover is
young; the control is mature. Compare them directly and the new ranking is charged for its
own recency however well it works.

So each event is scored over a **fixed exposure horizon measured from its own creation** —
only assertions written within `h` hours of the event count — and both cohorts are held to
the same one. The control cohort also spans exactly as long as the post-cutover cohort, so
window length is not doing the work either. Event age stops being a variable rather than
being hoped away.

Two guards fall out of this:

- **Maturity.** An event younger than the horizon is *excluded*, not counted as
  uncorroborated. Otherwise the freshest events depress the rate by construction.
- **Not measurable ≠ 0.0.** Too soon after the cutover for any event to have lived out the
  horizon reports a reason, never a rate — the discipline `score_match_rate_corroboration`
  already kept.

## 2. The horizon is a free parameter, so the default is a sweep

Any horizon holds age constant, which is exactly why no single value can be defended.
Defaulting to one number would state something about the horizon as much as about the
ranking. `--horizon-hours` therefore takes several and prints one table (default 6/12/24).

**But the rows are nested, and that nearly went unsaid.** Post span at horizon `h` is
`[cutover, now − h)`, so a *longer* horizon ends the window *earlier*: the 24h row's events
are a strict subset of the 6h row's. Agreement across horizons is close to one observation,
not several. The first version's footer invited the opposite reading. It now names which
row contains which, in the output.

## 3. The anchor was wrong, and the failure was structural

The spec required the cutover timestamp to be recorded on the bead at deploy. It never was.
Rather than reconstruct it from memory, `deploy_anchor` read `schema_migrations.applied_at`
for the newest migration — the ledger the system writes itself at container boot.

That is a recognition affordance instead of a recall question, and it was still wrong:

| | |
|---|---|
| Anchor used | migration `0011` applied **2026-09-08 11:17 UTC** |
| `0011` was added by | `6baaf4c`, committed **10:58 UTC** — 18 min before it ran |
| Ranking change `51c850c` | committed **14:45 UTC** — **3h27m later** |

**`schema_migrations` dates MIGRATIONS, not commits.** `51c850c` shipped without one, so
the ledger could never have dated it at any timestamp. The defect is not that the wrong
migration was picked; it is that a question the instrument cannot answer was given a
confident answer. The output printed `(migration 0011 applied_at)` beside the number, which
reads as provenance rather than as an assumption.

The validity condition was written into `deploy_anchor`'s own docstring — *"VALID ONLY IF
that migration shipped in the same image as the code change being measured"* — and then
asserted from recollection rather than checked against `git log`, which was on the dev
machine the whole time. A stated precondition is not a checked one.

**Fix (`b0a09c8`):** `--cutover` is required. The ledger is demoted to a hint carrying its
condition — *use it only if your change was committed before this timestamp* — plus the git
command that settles it. Nothing is measured until it is answered. That line would have
caught this on sight.

**Residual, undiscriminated.** The container's `.State.StartedAt` was 2026-09-09 06:09:41
UTC, two minutes before the run: the restart that picked up the measurement script.
`StartedAt` shows only the *last* start, so it cannot say whether anything deployed between
11:17 UTC on 09-08 and 06:09 UTC on 09-09. Either the host auto-pulls (ranking live from
~15:00 UTC on 09-08, post window ~71% new ranking, diluted) or it does not (ranking first
served two minutes before the measurement, and the table is old-vs-old). A cheap
discriminator exists if it is ever wanted: the time integration failures dropped to zero
dates the `be418b3`/`71a37b8` deploy, which upper-bounds the `51c850c` deploy because
`51c850c` is the earlier commit and ships in any image containing them.

## 4. What the first run established anyway

The comparison was void; the individual windows were not. Four windows covering ~40h of
event creation on 2026-09-07/08:

| window | events | multi-outlet | rate |
|---|---:|---:|---:|
| 12.9h, later | 1478 | 84 | **5.7%** |
| 12.9h, earlier | 2119 | 140 | **6.6%** |
| 6.9h, later | 1200 | 81 | **6.8%** |
| 6.9h, earlier | 964 | 80 | **8.3%** |

**Every one is below the 10% floor.** This retires a live hypothesis: the cumulative 6.6%
was not an artifact of averaging over a stale corpus. Fresh 7h and 13h windows read the
same. That is a named cause eliminated, which is what `bqa.19`'s acceptance criterion asks
for even though the number did not clear.

**Neither delta is distinguishable from zero.** 6h: −0.92pp, z = −1.13, p = 0.26. 12h:
−1.55pp, z = −1.36, p = 0.17. Combined with the nesting above, the two negative deltas are
close to one weak observation. Nothing here says the ranking made anything worse.

**Event creation is bursty** — 192/h, 140/h, 174/h, 46/h across four consecutive ~6–7h
blocks (the last overnight). Any single window comparison is sensitive to where a burst
falls, which is a further reason not to read ±1pp.

## 5. Next

Re-run with a cutover that is known exactly rather than inferred:

```
docker compose run --rm --entrypoint python newsbrief \
    scripts/score_comprehension.py --cohorts --cutover 2026-09-09T06:09:41Z
```

The 6h horizon needs more than 6h of post-cutover events and the control needs as long
again, so this wants ~13h elapsed. Note the risk this accepts: if the ranking *has* been
live since 09-08 afternoon, the pre-cutover control is also new-ranking and the comparison
is dead — which the run will not be able to tell you, and which is itself worth knowing.

The pre-registered gate in `score_comprehension.py` remains **unspent**. `--cohorts` renders
no verdict and no failing exit code, because a threshold cannot honestly be re-run after
looking and `bqa.11` still holds it.

Where this points next is `news-brief-bqa.18`, whose probe already ships in the image. The
ranking change targeted outcome **B** (the duplicate was never offered). If corroboration
stays near 6% under a clean anchor, the shortfall belongs to **A** (the model declined to
match) or **C** (outlets do not cover the same events) — and C invalidates §8.2's premise
rather than its implementation. Yesterday's probe already put the *detected* ceiling at
9.0%, below the floor, with ~30% only under an extrapolated 12% detector recall. Which of
those holds is the open question of the epic.

---

## 6. The clean-anchor run, and where the pre-registration lost

Run at 19:00 local on 2026-09-09 with `--cutover 2026-09-09T06:09:41Z`, 12.8h elapsed.

| horizon | post-cutover (entity rank) | pre-cutover control (recency) | delta |
|---|---|---|---|
| 6h | n=370, multi=10, **2.7%** | n=270, multi=9, **3.3%** | −0.6pp |
| 12h | no events | n=48, multi=2, 4.2% | — |
| 24h | not measurable | | |

### 6.1 What held, and what did not

| pre-registered | actual | |
|---|---|---|
| no significant difference between cohorts | −0.6pp, z ≈ −0.44 | held |
| both cohorts read **5–8%** | **2.7% and 3.3%** | **lost** |
| ~948 events per cohort at 6h | **370 and 270** | **lost, by 2.6–3.5×** |
| falsifier: post above 9.7% | 2.7% | not triggered |

The directional prediction held. Both quantitative ones lost, and the second is the
informative one.

### 6.2 The event-rate figure had a generator, and the generator is the defect

The pre-registration required checking observed counts against the 139/hour rate before
believing a surprising result. Applying it to the *unsurprising* one: post is 370/6.3h ≈
**59/hour**, control is 270/6.3h ≈ **43/hour**.

The control's 43/hour is not noise. It lands on the **46/hour overnight block** §4 already
measured — because at 12.8h elapsed, the 6h-horizon windows are post ≈ 06:09→12:27 UTC and
control ≈ 23:50→06:09 UTC. **The control cohort is the overnight trough by construction.**

139/hour was computed by averaging 3597 events over 25.8h of a series §4 had *already
recorded as bursty* — 192 / 140 / 174 / 46 per hour. Averaging a bursty series and applying
the mean to one 6-hour window is the error, and correcting 139 to 59 would not fix it: the
same arithmetic re-emits a wrong number at every future cutover. This is
`the-prediction-had-a-generator` — **the program is the defect, the number only its
symptom.** Any cohort comparison anchored at a fixed hour places its control in a
systematically different volume regime from its post arm.

### 6.3 The two errors partly cancelled, so the conclusion survives

- predicted: p ≈ 6.5%, n ≈ 948/948 → SE ≈ 1.13pp, MDE ≈ **3.2pp**
- actual: p ≈ 3%, n = 370/270 → SE ≈ 1.37pp, MDE ≈ **3.8pp**

n came in 2.6× low, but p came in 2× low as well, and a lower base rate shrinks the
variance. The derived quantity — the only thing the argument rested on — moved 3.2 → 3.8pp.
So the structural conclusion is unchanged and marginally stronger: the run resolves ~3.8pp
against a detected-duplicate ceiling of **+0.25pp**.

Worth recording as a pattern: two wrong inputs produced a nearly-right derived figure. Had
only the MDE been checked, both errors would have passed unnoticed.

### 6.4 A second, independent reason this metric cannot A/B a ranking

Both cohorts read roughly half the 5.7–8.3% of §4's windows, and they moved **together**. A
change hitting both arms is not the ranking.

The candidate is volume. Corroboration requires a *second outlet to publish on the same
event within the horizon*, so a low-volume window mechanically yields fewer multi-outlet
events. The two arms here differ in volume by 37% (59 vs 43/hour).

So `corroboration_by_outlet` is not merely underpowered here — **it moves with news volume,
and any two time windows differ in volume.** No anchor hygiene or elapsed time fixes that,
because the confound is a property of comparing two periods at all.

## 7. Verdict on bqa.19

The acceptance criterion: *"`corroboration_by_outlet` is measured after the ranking change
has run for a full window, and either clears 10% or the shortfall is attributed to a named
cause rather than to an unmeasured one."*

**Clause 1 is satisfied.** A 6.3h post-cutover cohort against an equal-length control on an
exactly-known cutover. That is what was missing in §3.

**Clause 2 is satisfied by attribution, not by clearing.** The named cause is not the
ranking: merging **every duplicate the detector can see** moves corroboration to 6.7%
(`bqa.25`), which is itself **below the 10% floor**. The floor is unreachable through this
mechanism, so the shortfall was never something a ranking change could close. The ranking
change is not thereby shown to be worthless — `recall@30` says it works — it is shown to be
measured by the wrong instrument.

**Closed on that basis.** Three things carry forward:

1. **`bqa.11` holds a gate now known to sit above the measured ceiling.** Firing the
   pre-registered 10% test would spend a one-shot pre-registration on a question whose
   answer is already determined. That wants a decision before it runs, not after.
2. **`bqa.25` is the live question** — whether the 6.7% ceiling is few-large-clusters or
   many-small-already-corroborated decides whether the ceiling means anything, and points
   at `bqa.18`'s outcome C, which would invalidate §8.2's premise rather than its
   implementation.
3. **Attribute ranking changes with `recall@30`**, not with corroboration. Corroboration
   remains §8.2's existence test for the event layer; it is not an A/B instrument at this
   corpus size, for the two independent reasons in §6.3 and §6.4.

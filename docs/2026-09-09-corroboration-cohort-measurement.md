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

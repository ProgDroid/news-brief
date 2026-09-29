# Red team, revision 4: gold set by window census (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md`, revision 4
**Prior reviews:** `...-redteam.md` (rev 1), `-r2.md` (rev 2), `-r3.md` (rev 3). Objections that
rev 4 resolved are not repeated. Where rev 4 claims a fix and the fix is partial, that is said.
**Stance:** hostile staff engineer, asking whether this is the wrong thing to build.
**Evidence rules:** **verified** means read in the repo at the cited line. **Computed** means
arithmetic I ran (the script is described inline, inputs named). **Estimate** means arithmetic on
figures the repo records, inputs named. **Unverified** means not reproduced here. No production
data was queried; the corpus lives on the host.

---

## Verdict

Rev 4 took nearly all of rev 3: the standalone `labeller.py` entrypoint, the full grant list and
startup self-check, the blind-partition headline, stratified adjudication with recorded
probabilities, a blind precision sample, a fixed K, 6-hour runs with global group ids, the
deferred repeat adjudication, and the house rules on the detector call. The service wiring now
holds up (check 1).

What remains is not wiring. Three things:

1. **The number that justifies K = 8 is wrong, and it is measured at the wrong layer.** No
   group count and CV reproduce the spec's 27/30/38 (check 2). The honest figures are worse, and
   worse again at the go/no-go floor. The "31–44 points historically" it is compared against are
   visibility and recall@30 differences, not the end-to-end group-level differences the
   headline measures (objection b).
2. **Detector-driven adjudication during the census is the expensive half of the build, and no
   pre-registered decision reads its output.** Pooled adjudication at scoring time gets the same
   protection with less code, no kin bias and no learning drift (objection a).
3. **The gold set is usable only through a replay of the whole corpus from 2026-09-04.** That
   silently makes every `items` row since capture day one a permanent dependency. An open bead
   (`uh0`) plans item retention, and the only rows the spec protects are the gold ones
   (objection c).

There are also four correctness blockers (listed at the end), one of which fails the first
`gold_prepare` run outright.

---

## Objection (a): a simpler design. Adjudicate by pooling at scoring time, not with detectors during the census.

**The objection.** §6.4 builds a detector apparatus that runs **inside** the census:
- an LLM clustering call per window (which is the only reason `gold_prepare` holds the Anthropic
  key, and the reason for the house rules, truncation handling and spend recording in §6.1);
- a trigram tier, and an entity tier with a document-frequency cut, sampling and recorded
  inclusion probabilities;
- `1 / p` weighting, per-detector-set attribution, and blind recall by window order;
- the deferral of window 3's adjudication, and the 500-decoy fixture and `1 / p` mutation test
  (§10).

Its output is **operator blind recall**, plus the final partition as a sensitivity. **No rule in
the spec reads either.** §4.5's go/no-go reads groups and minutes. §12's success criteria record
blind recall but set no threshold on it. The headline is the blind partition (§4.4). So this is a
measurement with no pre-registered consumer.

It also creates the two problems the spec then has to manage:
- **Kin bias.** §4.4 reports each system's recall on adjudicated-only positives "next to the
  detector that proposed them", because the detectors are kin to candidate systems (rev 3
  objection b). Rev 4 prints the bias. It does not remove it.
- **Learning drift.** §6.4 itself names it: showing the operator the detectors' finds after each
  window pushes later blind passes toward detector-findable pairs. That is contamination of the
  **headline** partition, and rev 4's answer is only to report it by window order.

**The simpler design: TREC-style pooling at scoring time.**
1. Sub-project 0 does the blind census, the 10-pair precision sample, and the consistency repeat.
   Nothing else in adjudication.
2. When sub-project 1 compares systems A and B, the operator adjudicates the **pool**: every
   cross-outlet link A or B made between items the blind partition left apart, plus a sample if
   the pool is large. Decisions accumulate in `gold_adjudications`, keyed by item pair, so a
   pair is never asked twice.

**Why this gets more than 80 percent of the value.**
- Kin bias disappears structurally. Every compared system contributes its own claims to the
  pool, so no system is credited for resembling a detector. The adjudicated-only recall table in
  §4.4 becomes unnecessary.
- Learning drift disappears. The operator sees no system opinion until the census is finished,
  which is the spec's own rule for the blind pass (§3, "carried forward").
- It adjudicates exactly the pairs that can change the paired McNemar result: the discordant
  ones. Detector pairs that no scored system proposes cannot change any comparison.
- It deletes the LLM call from `gold_prepare`, so `gold_prepare` needs no Anthropic key and no
  spend row. That also removes blocker 1 below.
- It deletes `gold_detector_pairs`, the entity sampling, the `1 / p` logic and their tests.
  The go/no-go minutes then measure the census, which is what they are meant to bound.

**What it gives up, stated.** An operator-recall figure before any system exists. That figure is
an upper bound (§4.7), it excludes the hub-only stratum (check 3), and nothing reads it. A pooled
figure computed at scoring time is the one that bears on a decision.

---

## Objection (b): an assumed requirement. That the differences sub-project 1 cares about are 31–44 points on the headline metric.

**The objection.** §4.4 concludes that K = 8 "is sufficient for architectural differences (31–44
points historically), and not for ranking-sized ones (14–17 points)". Both historical ranges come
from **upstream layers**, not from the headline's end-to-end, group-level "confirms":

| Figure cited | What it measured | Source (**verified**) |
|---|---|---|
| 43.5 (T vs RT), 31.7 (TE-cc vs RT) | **Visibility**: whether a pair was in one request or offered as a candidate | spike doc, output block and "Only the pairs a submission can blind" |
| 16.5 (pg_trgm vs entity overlap), 14 (entity vs recency) | **Retrieval**: batched recall@30 | `comprehend.py:1582–1606` (`candidate_events` docstring) |

The headline requires three stages to succeed: the pair is visible, the true event is retrieved
into the top 30, and the model links it. The spike shows how lossy the later stages are:
- RT was **97.2% visible** (spike doc), yet batched recall@30 is **44%** (`comprehend.py:1601`);
- M2 found Sonnet agreeing with **itself** on only 77% of link decisions (memory
  `comprehension-cost-redesign-phase-1`).

A visibility gap is multiplied by retrieval and linking rates below 1 before it reaches
"confirms". **Estimate:** 31.7 × 0.44 × 0.5–0.8 is roughly **7–11 points**. This is illustrative,
because the stages are not independent. But the direction is certain: end-to-end differences are
smaller than the upstream ones. The group-level unit compresses them further, because a group
with three or more outlets needs only one confirming pair.

So "sufficient for architectural differences" compares a detectable difference measured at the
headline's layer against effects measured two layers upstream. This is
`the-probe-measured-the-wrong-layer`. Combined with check 2, K = 8 most likely resolves only
differences of about 35 points or more, while the differences sub-project 1 will actually see
are probably under 20.

**What I would do instead.**
- State the MDE claim without the historical comparison, or re-derive the comparison at the
  headline layer. The M2 replay machinery (`scripts/replay_haiku.py`) already computes
  end-to-end link decisions, so an upstream-to-headline attenuation factor is cheap to estimate.
- Then choose between two options. Accept that K = 8 is a **coarse screen** (it can reject an
  architecture that is catastrophically worse) and say so in §12. Or size K for about 15 points,
  which the r3 table puts at roughly 3–4× the operator hours.
- Either way, do not sell K = 8 as able to settle "architectural" questions.

---

## Objection (c): the 6-month collision. The gold set is usable only through a replay from capture day one, which quietly makes the whole `items` table permanent.

**The objection.** §12 makes the gold set's use conditional on "**one chronological replay
starting at capture day one** (2026-09-04), so entity state matches production and there is no
cold start". That means every `items` row from 2026-09-04 to the block end, around 24 days of
the corpus, is a load-bearing input for as long as the gold set is used. Also load-bearing:
their `outlets` rows, and the triage inputs.

The spec protects only the gold rows: `gold_window_items.item_id` has `ON DELETE RESTRICT` (§7).
Everything the replay needs **outside** the 8 windows is unprotected, and nothing states the
requirement.

**The collision is already filed.**
- `news-brief-uh0` is **open** (`.beads/issues.jsonl:153`). It asks for retention on `items`
  among others: "b42.1 adds items, … none with a retention rule."
- The repo's only retention convention is 90 days (`retention.py:5`, `NEWSBRIEF_RETENTION_DAYS`
  default 90). That reaches 2026-09-04 on about **2026-12-03**, well inside six months, and
  inside sub-project 1's likely evaluation period.
- When `uh0` is built, one of two things happens:
  - it deletes pre-block items. The replay then silently stops matching production's entity
    state, which is the exact guarantee §12 relies on. No test fails, because the gold rows
    survive;
  - or it tries to delete gold items and hits the RESTRICT FK. The retention job errors, and the
    gold set becomes the reason retention cannot ship.
- Side note (**verified**): `feed_sightings.item_id` already references `items` with no
  `ON DELETE` clause (`migrations/0008_capture_telemetry_up.sql:54`), so `uh0` must confront
  item FKs anyway. The gold FK adds a second, differently motivated constraint to that work.

**The cost side of the same dependency.** Each replay costs "$40–60 per system per run" (§12).
- M2's Sonnet self-agreement of 77% means one run per arm is not a measurement. With 2 runs per
  arm and 2 arms, one comparison costs about **$160–240**.
- Against the $1.50/day comprehension allowance (`common.py:305`), that is 3.5–5 months of
  production budget per comparison.
- In six months sub-project 1 will have run several comparisons against the same 8 windows. That
  adds a further problem: with **no held-out split**, about 60–80 groups become the tuning set
  and the test set at once.

**What I would do instead.**
- Record the corpus-immutability requirement as a spec-level constraint, and put a comment on
  `uh0` now: items from 2026-09-04 to the block end are exempt from retention while any gold set
  is in use.
- Or snapshot the replay prefix once, as a pg_dump of `items` and `outlets` for that date range
  kept beside the gold tables, so that retention can proceed.
- Pre-register a split: windows 1–4 are a development set sub-project 1 may look at, and
  windows 5–8 are touched once per decision. Otherwise say plainly that there is no held-out
  data.

---

## Specific checks

### Check 1: rev 3's wiring blockers. Resolved.

- **Entrypoint.** `entrypoint: ["python", "labeller.py"]` bypasses `brief.py`'s `REQUIRED_ENV`
  exit (`brief.py:4010–4013`) and its seed block.
- **Imports.** `db` imports only `common.log` (`db.py:16`).
- **Environment and logging.** `common` reads both API keys with `.get` (`common.py:25–26`).
  With `NEWSBRIEF_LOG_FILE=0` it logs to the console only (`common.py:79`).
- **Connection.** `db.conninfo` builds from the discrete `POSTGRES_*` variables when
  `DATABASE_URL` is empty (`db.py:55–73`).
- **Image.** The Dockerfile's default user is non-root (`Dockerfile:60–63`), so dropping the
  anchor's `user:` is safe.
- **Grants.** The list now covers the token UPDATE, `gold_events`, sequences and window
  transitions.

Residual traps, none of them blocking:
- **Knob reads.** Any `common.<KNOB>` read in `labeller.py` goes through `common.__getattr__` →
  `config.knob` → `settings` (`common.py:374–394`). The `gold_labeller` role cannot SELECT
  `settings`. The startup self-check checks only the grants the spec lists, so a knob read on a
  rarely used path would fail at request time. Add a test that the labeller never touches
  `common.__getattr__` for a `KNOBS` name. The subprocess test covers only the paths it
  exercises.
- **Port and external databases.** The labeller's environment omits `POSTGRES_PORT` and
  `DATABASE_URL`. On a host that uses either (`docker-compose.yml:162–172`; memory says the
  host's compose is ahead of the repo's), the labeller reaches a different database. The
  self-check fails loudly there, which is acceptable, but the runbook should say so.

### Check 2: the design-effect and achieved-MDE arithmetic. The formula is right. The stated figures are impossible.

- **Target.** `n = 7.84 × 0.5 / 0.0625 = 62.72`, so 63. **Correct.**
- **Formula.** `DE = 1 + ((1 + CV²) m̄ − 1) ρ` is the standard design effect for unequal cluster
  sizes. **Correct in form.** Its ρ should be the intra-window correlation of McNemar
  **discordance**, which the spec concedes is unmeasurable here. That is fine as stated.
- **The 27 / 30 / 38 central estimate cannot come from this formula** (**computed**). With K = 8
  and d = 0.5, MDE = `2.8 × sqrt(0.5 × DE / (8 m̄))`.
  - I grid-searched m̄ from 1 to 40 and CV from 0 to 3. **No pair reproduces 27/30/38, even
    allowing for rounding.**
  - Matching all three requires `(1 + CV²) ≈ 0.67–0.73`, which means a negative variance.
  - The closest valid input is m̄ = 10, CV = 0, which gives **27 / 30 / 43**. The ρ = 0.3 figure
    is understated by at least 5 points, and more under any realistic CV.

| m̄ (groups per window) | CV | ρ = 0.05 | ρ = 0.1 | ρ = 0.3 |
|---|---|---|---|---|
| 10 | 0 | 26.7 | 30.5 | 42.6 |
| 10 | 0.5 | 27.8 | 32.5 | 46.7 |
| 10 | 1.0 | 30.9 | 37.7 | 57.3 |
| **6 (the go/no-go floor)** | 0 | **32.0** | **35.0** | **45.2** |
| 6 | 0.5 | 32.9 | 36.7 | 49.1 |

- **Normal quantiles with 7 degrees of freedom.** The sizing uses 1.96 + 0.84. The readout's
  own inference bootstraps over K = 8 windows, and §8 says "7 degrees of freedom". The matching
  critical sum is t₇ = 2.365 + 0.896 = **3.26**, not 2.80. That inflates every MDE by **16%**.
  At m̄ = 10 and CV = 0.5 the central figure becomes 32 / **38** / 54. At the go/no-go floor
  (m̄ = 6, CV = 0) it becomes 37 / 41 / 53.
- **The go/no-go floor is not tied to the claim.** §4.5 continues at 6 multi-outlet groups per
  window. At 6, even the optimistic z-based MDE is 35 points at ρ = 0.1. That is above the
  31.7 the spec says K = 8 can resolve. If a floor is kept, derive it from the MDE claim: for a
  35-point MDE at ρ = 0.1 on t₇, m̄ must be about 12 or more.
- **Two-stage bootstrap.** Resampling "windows, then groups" (§8) double-counts the within-window
  variance, a known property of the two-stage cluster bootstrap. Resample windows only. The
  (K − 1)/K underestimate at K = 8 is 0.875 and worth a stated correction.
- **Reporting by block half** gives 4 windows (3 degrees of freedom) per half. That is a
  descriptive table, not a comparison, and §4.2 should call it one (see blocker 3 for why the
  halves may not even straddle the changes they are meant to separate).

### Check 3: did rev 4 actually fix rev 3's adjudication items? Mostly. Two are partial.

- **Headline on blind:** fixed.
- **Blind precision:** fixed. However, a pair-uniform sample of blind-joined pairs is
  quadratically weighted toward big groups (a 6-outlet group contributes 15 pairs), which is the
  weighting rev 2 removed from the headline. Sample a group, then a pair within it.
- **Hub pairs have inclusion probability zero, not a recorded small one.** §6.4 keeps entity
  pairs **only** through entities matched by ≤ 5 items. Pairs whose only shared entities are hubs
  (Iran, Israel, the US: the geopolitical core of this corpus) have p = 0. `1 / p` cannot
  reweight a stratum that is never sampled. Rev 3 asked for "sample the rest with a recorded
  probability". §4.7's "upper bound" label covers this, but the §10 fixture of "500 hub-entity
  decoy pairs" then tests the DF cut, not the `1 / p` weighting it claims to test.
- **The group-level `1 / p` is mis-specified.** Blind recall is group-level. The probability
  that a final group's merge was **discovered** is `1 − Π(1 − p_ij)` over every entity pair
  joining its blind components, and it is 1 if any LLM or trigram pair joins them. Weighting by
  the one shown pair's `1 / p` over-weights groups reachable through several pairs. Define the
  group-level Horvitz-Thompson weight, or drop the weighting along with detector adjudication
  (objection a).
- **Consistency repeat:** fixed in design. Two things remain:
  - the progress counter leaks it: "Window 3/8" in §6.5 has K = 8, but there are 9 labelling
    sessions. Either the denominator shows 9, or the last window reads "9/8";
  - the titles themselves are the fingerprint, and hiding the header cannot hide them. "One
    window is stated as thin" is honest.

### Check 4: rev 3's objection (c), islands. Partially resolved.

- The 6-hour run removes the internal slot boundary, and group ids are now global. Both are
  real gains.
- It is still 8 random 6-hour islands over about 10 days. Development ("days, weeks", §1) needs
  the stretches between them. §13's "extend windows contiguously later" means a new census of
  about 170 items per 6 hours, which is the cost rev 3 named.
- **The §4.3 gate may stop the census before window 1.** The repo's only unrestricted figure
  (bqa.18: about 128 merged cross-outlet pairs a day, against about 30–53 a day within 2 h, per
  rev 2) puts the within-2 h share at about 25–40%.
  - If most of the rest lie beyond 6 h, `E[min(g/6, 1)]` exceeds 50%.
  - Bounds (**estimate**): 25% if every longer gap is just over 2 h, 80% if every longer gap is
    beyond 6 h.
  - The gap sample is also biased short. Comprehension ran under the current ranking only
    around 9–10 Sep and 22–25 Sep, with a 12-day pause between (memory
    `newsbrief-comprehend-cost`), so gaps that span the pause cannot appear.
  - The gate is therefore floor-biased toward **passing**. Pre-register what happens at 40–50%.
    Even a pass there means the headline covers only half the confirmation problem.

---

## Correctness blockers (beyond the three objections)

1. **The `gold_detect` spend row violates a CHECK constraint.**
   - `comprehend_spend.stage` is `CHECK (stage IN ('triage', 'integration'))`
     (`migrations/0015_comprehend_spend_up.sql:8`, **verified**).
   - §6.1 records stage `gold_detect`, and §7's migration 0017 lists only new tables.
   - `record_spend` (`comprehend.py:507–522`) will raise `CheckViolation` after the first paid
     Sonnet call, so the first `gold_prepare` run fails after spending money.
   - §10's test "spend is recorded in `comprehend_spend`" will fail against real Postgres. It
     will pass if the test mocks `record_spend`.
   - **Fix:** 0017 alters the constraint (with a down migration that restores it only after
     deleting the `gold_detect` rows). Or drop the detector (objection a). Note also the repo's
     precedent: `scripts/replay_haiku.py:11` and `scripts/inspect_integration.py:22` deliberately
     stay out of `comprehend_spend`.
2. **The achieved-MDE figures are internally inconsistent** (check 2). The central claim that
   decides K must be recomputed, and the go/no-go floor derived from it.
3. **The block refuses to exist before 2026-10-01.**
   - The block is `[P − 17 d, P − 3 d]` for first preparation date P. It is truncated to start
     at or after 2026-09-18 (capture since 2026-09-04, plus the 14-day look-back).
   - The length is at least 10 days only when P is on or after **2026-10-01**. A `gold_prepare`
     run on 2026-09-30, right after the gate, refuses.
   - For any feasible P, the truncated block starts after the ~2026-09-15 quote-page flood
     onset (`common.py:734–736`). So "the block straddles … the quote-page flood" (§4.2 step 3)
     is false.
   - Whether it straddles `c439ade` (committed 2026-09-25) depends on its unknown host deploy
     date.
   - State the earliest P, and the actual capture changes inside the block.
4. **The group-level `1 / p` weighting is mis-specified** (check 3). The mutation test in §10
   will pass against the wrong estimator, because the fixture has one pair per merge.

Not blocking, but should be written down: the knob-read trap in the labeller (check 1), the
pair-uniform precision sample (check 3), and the 9-versus-8 counter leak (check 3).

---

## How revision 4 answered revision 3

| Rev 3 item | Rev 4 | Grade |
|---|---|---|
| (a) Fixed K, prepare all at once, drop adaptivity | K = 8, one `gold_prepare`, go/no-go only | **Answered in design;** the MDE arithmetic is wrong (check 2) |
| (b) Blind headline, stratified adjudication, precision | All three adopted | **Answered;** hub stratum p = 0, group-level HT weight wrong, and the whole tier has no consumer (objection a, check 3) |
| (c) Runs of adjacent slots, global ids | 6 h runs, `gold_groups` | **Partial:** boundary loss reduced; development still unserved (check 4) |
| Check 1: labeller cannot start | Standalone module | **Answered** (check 1) |
| Check 2: grants | Full list, self-check, re-runnable script | **Answered** |
| Check 3: `DE_w` undefined | Assumed ρ with sensitivity | **Answered in form;** the figures are wrong (check 2) |
| Check 4: blind recall unit, attribution, drift, repeat | Group unit, detector sets, by order, deferral | **Mostly answered;** drift is reported, not prevented (objection a) |
| Gap statistic | `E[min(g/6, 1)]` with n printed | **Answered;** biased toward passing (check 4) |
| Block straddles capture changes | Report by block half | **Premise false** for the flood (blocker 3) |
| Replay cold start | Replay from day one | **Answered, and it creates objection (c)** |
| Unsure ambiguity | One rule | **Answered** |
| Detector house rules and spend | Adopted | **Answered in intent;** the spend row fails its CHECK (blocker 1) |

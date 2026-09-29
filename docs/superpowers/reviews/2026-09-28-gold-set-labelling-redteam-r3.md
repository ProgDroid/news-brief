# Red team, revision 3: gold set by window census (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md`, revision 3
**Prior reviews:** `...-redteam.md` (rev 1), `...-redteam-r2.md` (rev 2). Their objections are not
repeated here unless rev 3's answer to them is inadequate or creates a new problem.
**Stance:** hostile staff engineer, asking whether this is the wrong thing to build.
**Evidence rules:** **verified** means read in the repo at the cited line. **Estimate** means
arithmetic on figures the repo already records, and the inputs are named. **Unverified** means
known platform behaviour that was not reproduced here. No production data was queried, because
the corpus lives on the host.

---

## Verdict

Rev 3 took almost every rev-2 fix: the separate service, hashed tokens, the security headers,
`client_seq`, the group-level headline, the window bootstrap and the persisted slot order. Four
problems remain, and two of them were introduced by rev 3 itself:

1. **The separate `labeller` service cannot start as specified** (check 1). It goes through
   `brief.py`'s `__main__`. That exits 1 without the Anthropic and Telegram keys, and then
   writes seed rows that the `gold_labeller` role cannot read. The grants are also short of what
   the labeller has to do (check 2).
2. **The adjudication pass reintroduces Trap 5, and its recall figure is close to 1 by
   construction** (objection b, check 4). The headline is computed on the *adjudicated*
   partition, and every positive added there was proposed by a detector that resembles a system
   sub-project 1 will score. The entity detector floods the 40-pair cap with hub-entity pairs.
3. **The stopping rule's `DE_w` has no outcome to be computed from** in a sub-project that
   scores no system (check 3). On free inputs, the central projection lands at about 2.5× the
   10 h cap anyway (objection a).
4. **Random 3-hour islands are a dead end for sub-projects 2 and 3**, which the §2 table says
   depend on this one (objection c).

---

## Objection (a): a simpler design. Pre-size K now, prepare every window in one run, and delete the adaptive machinery.

**The objection.** Rev 3 builds an adaptive apparatus:
- a daily scheduled `gold_prepare` that keeps **one** window ahead (§6.1);
- a pilot of 4 windows, a window-bootstrap `DE_w`, and re-projection every 4 windows (§4.4);
- a projection-versus-cap refusal (§9);
- a morning-brief nudge line (§6.5).

That apparatus exists to answer one question: *can Δ = 0.15 be reached within 10 h?* The inputs
already in the repo answer it, most likely **no**. The spec's own fallback, "Δ = 0.20 (about 98
groups) or a different design" (§4.4), is the probable destination, and the apparatus only
delays reaching it.

**Evidence: a projection from free inputs (estimate).**

*Multi-outlet groups per 3 h window.*
- The spike counted 671 production-detected cross-outlet pairs ≤ 2 h apart among 12,711 material
  items, at about 42 material items per hour (spike doc, output block; "Membership averages ~42
  material items per 1 h window").
- 12,711 ÷ 42 ≈ 303 comprehended hours, or about 101 three-hour slots. That gives **about 6.6
  detected pairs per slot**. Rev 2 divided by 22 calendar days and got 3.8, but comprehension was
  paused for much of that span.
- At 1.5–2.5 cross-outlet pairs per group (most groups have 2 outlets, a few have 5 or more),
  that is 2.6–4.4 *detected* groups per slot.
- Correcting for production's unknown recall (assumed 40–80%), for slot-boundary splits (about
  23%, per rev 2) and for the ≤ 2 h restriction gives **about 3–9 true multi-outlet groups per
  window, central 5**.

*Minutes per window.*
- Blind pass: rev 2 estimated 17–47 minutes for 84–140 items.
- Adjudication: up to 40 decisions at 8–15 s each adds 5–10 minutes.
- **Total: 22–57 minutes, central 35.**

*Windows and hours needed, with `DE_w` at 1.0–1.5 (central 1.2):*

| Δ | groups (d = 0.5) | windows (central) | operator hours (central) | range |
|---|---|---|---|---|
| 0.15 | 175 | 42 | **24.5 h** | 7–84 h |
| 0.20 | 98 | 24 | **13.7 h** | 4–47 h |
| 0.25 | 63 | 15 | **8.8 h** | 3–30 h |
| 0.30 | 44 | 11 | **6.2 h** | 2–21 h |

(The group counts use `n = 7.84 × 0.5 / Δ²`, the spec's formula.)

**Pre-registered prediction:** the window-4 projection will exceed 10 h at Δ = 0.15. The
central case exceeds it at Δ = 0.20 too.

**Evidence: the adaptive parts cost throughput and code.**
- **Throughput.** `gold_prepare` is a *daily* `Schedule` (the shape in `scheduler.py:69–76`),
  and it prepares **one** window ahead (§6.1). The operator can therefore finish at most **one
  window a day**, whatever the session length. That contradicts §12's "sessions of any length".
  - At the central 42 windows, sub-project 1 is blocked for six weeks of calendar time.
  - `db.enqueue_manual` (`db.py:313`) already lets the daemon queue a run, but the spec does not
    use it.
- **Code.** The daily cadence adds a `Schedule` row and a `JOB_MODES` entry. The nudge puts a
  gold-table query into `collect`'s delivery path. The readout needs refusal logic and its own
  tests (§10 "§4.4").
- **The only reason to prepare lazily is gone.** It avoided spending on windows never labelled,
  but a window costs one Sonnet clustering call. **Estimate:** 100–150 items × ~60 tokens is
  about 10k tokens in, plus a few thousand out, so cents per window, and $1–4 for K = 15–42.
  That is against a $1.50/day comprehension budget.

**What I would do instead.**
1. Choose Δ now from the table: **0.25 (63 groups)** is the largest target the central estimate
   fits under the cap. Freeze K with a conservative `DE_w` (see check 3), for example K = 15–18.
2. Prepare **all K windows** in one manual run, `docker compose run --rm newsbrief gold_prepare`,
   which is the documented debug path (compose header). Drop the `Schedule`, the one-ahead
   logic and the nudge. Once every window is prepared, `/label` just says which window is next.
3. Keep windows 1–4 as a **feasibility gate** with pre-registered go/no-go numbers (groups per
   window ≥ 3, and active minutes ≤ 40), not as an input to a re-projection loop.
4. Print an **MDE-at-K table** in the readout, so that an abandoned census still says what it
   can resolve. This matters because friction is the operator's master variable, and a census
   that stops at window 12 must still be worth something.

**What this gives up, stated honestly.** In the optimistic corner (9 groups per window,
`DE_w` = 1), the adaptive rule would stop around 20 windows at Δ = 0.15, and a fixed K = 15 plan
would not try. The table puts that corner at the edge of the range, not the centre. Δ = 0.25
also cannot settle fine comparisons such as `sse` (Haiku against Sonnet link direction), but
the formula shows Δ = 0.15 cannot settle a difference of ~5 points either. The differences that
decided things here were 14–44 points: pg_trgm against entity overlap was 16.5, entity overlap
against recency 14, TE-cc against RT 31.7, and T against RT 43.5 (`comprehend.py` docstring of
`candidate_events`; spike doc).

---

## Objection (b): an assumed requirement. That adding detector-found positives makes the gold set a better benchmark. For the comparison sub-project 1 will run, it makes it a worse one.

**The objection.** §4.4 defines the headline population on the **final** partition, "blind plus
adjudicated". Every positive that adjudication adds was, by construction, proposed by one of
three detectors (§6.4), and each detector is kin to a system sub-project 1 will score:

| Detector (§6.4) | Its production or candidate kin |
|---|---|
| Title trigram ≥ 0.35 | Production's ranker, `similarity(t.title, e.summary)`, `comprehend.py:1647` (**verified**) |
| Shared entity via `SurfaceIndex` | Production's candidate gate: `integration_candidates` feeds `index.match` hits to `candidate_events`, `comprehend.py:1537–1566` (**verified**) |
| One Sonnet clustering call per window | The clustering architecture §2 names as sub-project 1's alternative |

A system resembling a detector scores **100% recall on the adjudicated subset**, and a system
that finds a *different* set of the operator's misses scores 0% there, because its finds were
never shown to the operator and so never became positives. This is Trap 5, rev 1's founding
objection. It has come back through adjudication rather than through the stratifier (rev 1) or
the annotator (rev 2). Rev 2 recommended adjudication for measuring the operator's recall.
Rev 3 also folded it into the **headline**, which is a separate decision and the wrong one.

**Evidence that the size of the bias is not small, and that the measurement meant to bound it
cannot (estimate, with inputs).**
- **The entity detector floods the cap.**
  - `SurfaceIndex.build` loads **every** entity ever created, with no time window
    (`comprehend.py:1282–1287`, **verified**).
  - Entity mentions are promiscuous: an entity mention alone made **86% of items material**
    before the rule was narrowed (`comprehend.py:1323–1327`, **verified**).
  - The spike found hub entities chaining items into components of up to **223** at W = 2 h,
    with 75% of pairs sharing an entity (spike doc, "TE components … max 223"; "503 of 671").
  - One hub entity matched by 30 of a window's 100 items yields C(30,2) = 435 pairs. Two or three
    hubs (Iran, the US, Israel) yield **about 500–1,500 left-apart entity pairs per window**.
- **§6.4 shows at most 40 pairs, "chosen uniformly at random".** That is a 3–8% sampling
  fraction, applied equally to the few LLM-proposed pairs, which are the likeliest real misses.
  **Expected LLM pairs shown per window: about 0.4–2**, assuming LLM pairs are 1–5% of the pool.
- **"Blind recall = blind positives ÷ final positives" (§6.4) is not weighted for truncation.**
  "The rest are counted" is a count, not a weight. An unshown true miss is absent from the
  denominator. **So blind recall is biased toward 1** by the flood, on top of being an upper
  bound (§4.6).
- **Adjudication can only merge.** It shows pairs the blind pass left *apart*, never pairs it
  joined, so blind **false merges are never examined**.
  - "Confirms" needs only two items from different outlets (§4.4), so an over-merged group is
    easier to confirm. Every system's headline is inflated, and the number of groups shrinks.

**What I would do instead.**
1. **Compute the headline on the blind partition.** Print the final partition as a sensitivity,
   and print, per system, its recall on the **adjudicated-only** positives next to the detector
   that proposed them. Where that detector is the system's own kin, the difference is the bias,
   stated as a number.
2. **Stratify adjudication by detector, with known inclusion probabilities.**
   - Show **all** LLM-cluster pairs and all trigram pairs first.
   - Keep entity pairs only for entities that match ≤ k items in the window (a document-frequency
     cut, for example k = 5), and sample the rest with a recorded probability.
   - Weight blind recall by the inverse of those probabilities.
3. **Measure blind precision too.** Show a small random sample of *blind-joined* cross-outlet
   pairs for a "same?" check. Otherwise the over-merge direction is unmeasured.

---

## Objection (c): the 6-month collision. Random 3-hour islands cannot serve sub-projects 2 and 3, and the §2 table says they depend on this one.

**The objection.** §2 lists sub-project 2 (Development, "owns `same_story` labels and pairs more
than 3 h apart") as depending on **0** and 1, and sub-project 3 as depending on 1 and 2. When
sub-project 2 starts, the only gold data in hand will be:
- K windows drawn by a **seeded shuffle** across the block, stratified by time-of-day band and
  round-robined (§4.2), and therefore **non-contiguous**;
- group identity **local to a window**, since `gold_assignments.group_no` sits under `window_id`
  (§7);
- nothing between the windows.

Development ("how a story evolves over days, weeks", §1) needs the same event or story followed
**across** time. Between two sampled windows lie unlabelled slots. No amount of later
group-to-group linking can recover what happened in them. Sub-project 2 therefore needs a new
contiguous census, with a new block, new replay span, new tool and new operator hours. Operator
hours are the scarcest input in this project (global rule: "friction is his master variable").

The same choice **permanently fixes the 3 h truncation** that §4.3 and §4.6 accept.
- Pairs split by an aligned slot boundary can never be relabelled, because the neighbouring slot
  was not sampled.
- For a pair `g` hours apart (g < 3), the split probability is g/3. Rev 2's derivation puts the
  loss at about 23% of ≤ 2 h pairs.

**Evidence.**
- §2 table (spec lines 40–46), §4.2 steps 1–2, §4.6 first bullet, §7 `gold_assignments`.
- The repo's only unrestricted figure hints that most production merges are more than 2 h apart.
  bqa.18 counted about 128 merged cross-outlet pairs a day, against about 53 a day ≤ 2 h from
  the spike re-normalised above. That puts the ≤ 2 h share near 40% (**estimate**; rev 2 already
  called this suggestive, not established).

**What I would do instead.** Sample **runs of adjacent slots**, not lone slots. For example, 6 h
pairs of slots, or whole days stratified by weekday. Also make group identity global (a
`gold_groups` table with its own id), so that:
- adjacent windows can be joined later by **group-to-group recognition** ("is this group the
  same event as that one?"), which recovers boundary splits at a cost of tens of decisions, not
  hundreds of items;
- sub-project 2 inherits a contiguous timeline, and can add `same_story` links between groups
  instead of re-censusing items;
- the replay span shrinks to the run length plus 14 days.

**The price, stated honestly.** Adjacent slots share a news cycle, so `DE_w` rises and the number
of independent units falls. Whole days give only 3–5 units, which is the check-3 noise problem
again. Pairs of slots are the middle ground. The spec should **make** this trade explicitly.
Today it makes it by default, in the direction that serves sub-project 1 alone.

---

## Specific checks

### Check 1: can the labeller run from the same image with only database credentials? No, not as specified.

**Verified, step by step.**

1. **Entrypoint.** The image's `ENTRYPOINT ["python", "brief.py"]` (`Dockerfile:69`) plus
   `command: [labeller]` routes through `brief.py`'s `__main__`.
2. **Required environment.** The first statement there is the `REQUIRED_ENV` check
   (`brief.py:4010–4013`). `REQUIRED_ENV = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN")`
   (`common.py:31`). §6.1's environment has neither, so **the service prints "Missing required
   environment variables" and exits 1 on every start.**
3. **Seed writes.** With dummy keys, every mode except `serve` then runs the seed and import
   block (`brief.py:4020–4035`):
   - `ensure_seeded`, whose first statement is `SELECT 1 FROM users` (`config.py:744`);
   - `import_settings_from_env` (SELECT `settings`);
   - `warn_ignored_env_knobs`, which calls `settings()`;
   - `import_sources_from_file` (`sources`);
   - `import_preferences_from_file` (`preferences`, `users`);
   - `import_state_from_file` (`runtime_state`).

   Under §6.1's grants, the first SELECT raises "permission denied for table users". The
   `except` then calls `telegram_alert`. `config.alert_chat_id` cannot read `users` and falls back
   to an empty `TELEGRAM_CHAT_ID`, the POST goes to `api.telegram.org/bot<dummy>/`, and the
   service **exits 1**.
4. **The widening this would need.** For the brief.py route to work, the role needs SELECT on
   `users`, `settings`, `sources`, `preferences` and `runtime_state`. Each import then returns
   early on a populated table. The role also needs placeholder secrets in the environment, and
   that turns §10's test "the compose service uses an explicit environment with no API keys"
   into a test of the wrong thing.
5. **The tests would stay green.** §10's two wiring tests ("`labeller` in `MODES`" and "compose
   has no API keys") can both pass in CI while the service cannot start. This is the
   `tests-asserting-less-than-their-name` class.

**Also verified:**
- **Imports.** Importing `brief` pulls in `trading`, `enrichment`, `validation`, `claim_verify`
  and `scheduler` (`brief.py:36–110`). `scheduler` validates `SCHEDULES` at import. The whole
  application enters a LAN-exposed process.
  - A grep for module-level `NAME = … common.KNOB` reads in the application modules found
    **none**, so import time does not appear to touch `settings`. That is evidence from one
    pattern, not proof.
- **Logging.** `common._log_handlers` opens `DATA_DIR/newsbrief.log` unless
  `NEWSBRIEF_LOG_FILE` is not `1` (`common.py:70–95`).
  - Without the anchor's volume, that file is container-local and harmless.
  - If someone adds the volume "for the logs", two processes rotate one file, which the
    docstring there says loses lines.
  - Set `NEWSBRIEF_LOG_FILE=0`.
- **The link's host.** The daemon must build the link, and the labeller must check `Host` and
  `Origin` against "the configured host" (§6.2). The spec does not say where that value lives.
  - If it is a knob, the anchor line only **seeds** a row on an empty `settings` table (compose
    comments on seed-only knobs; memory `env-var-needs-compose-passthrough`). On this seeded
    host, adding the line does nothing.
  - It has to be a non-knob environment variable, like the `NITTER_BASE_URL` carve-out, declared
    in **both** services.
- **The connection works.** `db.conninfo` builds from `POSTGRES_HOST/USER/PASSWORD/DB` with
  `make_conninfo` (`db.py:24–73`), so `POSTGRES_USER=gold_labeller` plus its own password works.
  The service needs `depends_on: postgres` and the default network. It gets both only if it is
  in the same compose project, which it is.

**Fix.** Use `entrypoint: ["python", "labeller.py"]` with a standalone module that imports only
`db` and `common`. The `common` import is side-effect-free apart from logging. Drop "a `brief.py`
mode in `MODES`" and its test. Add `labeller.py` and the static-asset directory to the
`Dockerfile` COPY allowlist (`Dockerfile:39`), because `tests/test_packaging.py` walks imports
and will not see a static directory.

### Check 2: do the listed grants cover what the labeller must do? No. Five gaps, each fatal to one feature.

| Operation (spec) | Needs | Granted (§6.1)? |
|---|---|---|
| Consume the link token: an atomic `UPDATE … SET consumed_at = now() WHERE token_sha256 = $1 AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now() RETURNING id` (§6.2) | `UPDATE (consumed_at)` + `SELECT` on `gold_sessions` | **No UPDATE.** Without it, "one-time" cannot be enforced by the labeller |
| Log `page_open`, `action` and `finish` (§6.3), the **only** input to active minutes and so to the §4.4 time projection | `INSERT` on `gold_events` | **No.** The stopping rule loses its time input |
| Any INSERT into a table with an `id`. Repo convention is `BIGSERIAL`: **24 of 24** id columns across `migrations/*_up.sql` (**verified**) | `USAGE` on each `*_id_seq` | **No.** The first autosave fails with "permission denied for sequence gold_assignments_id_seq" |
| Abandon with a typed reason (§6.3); the `open` and `blind_done` transitions and "timestamps" (§7) | `UPDATE (status, abandon_reason, <every timestamp column it sets>)` on `gold_windows` | **Partly:** only `(status, completed_at)` |
| The brief.py startup path, if it is kept (check 1) | `SELECT` on `users`, `settings`, `sources`, `preferences`, `runtime_state` | **No** |

Two operational gaps as well:
- **Ordering.** The GRANTs can run only after 0017 is applied, and the supervisor applies
  migrations at startup (`supervisor.py:475`). The runbook must order the steps as deploy, then
  confirm 0017 in `schema_migrations`, then GRANT, then start the labeller.
- **Grants do not survive a reversal.** The repo's premise is that migrations get reverted
  (`db.run_migrations` docstring). A `0017` down/up cycle drops the tables and their grants
  silently. `backup.py` dumps with `--no-owner --no-privileges` (`backup.py:140–141`), and
  `pg_dump` never includes roles, so a restore mid-census loses the role's access too. The
  runbook should carry the GRANT block as a re-runnable script, and the labeller should fail
  loudly on `InsufficientPrivilege` at startup.

### Check 3: is the §4.4 stopping rule with about 4 pilot windows sound? Not as written. Its key quantity is undefined, and with 4 windows it would be noise anyway.

1. **`DE_w` has no outcome.** A design effect is the variance inflation of a specific estimator,
   here "the share of multi-outlet groups confirmed". "Confirmed" needs a system. §13 puts
   "scoring any system" out of scope, so sub-project 0 has no outcome whose between-window
   variance defines `DE_w`. The candidates are:
   - **Kish's size-only DE** (1 + CV² of groups per window). This is computable and truly
     outcome-free, but it ignores the intra-window correlation of the outcome, which is the part
     rev 2 said dominates.
   - **Production's links** as a stand-in system. This reads outcomes, contradicting "never
     outcomes". It is also mostly unavailable: comprehension was paused for much of September
     and has been off since the phase-1 build, until the gate on or after 2026-09-30 (memory
     `comprehension-cost-redesign-phase-1`). When it resumes, it drains newest-first under a
     $1.50/day bucket, so its links on the block will be partial and out of chronological order.
     (Exact on and off dates are host state I cannot query.)
   - **The paired McNemar's relevant DE**, the clustering of *discordance* between two systems.
     This is a property of systems that do not exist yet.

   So "costs no α" is true only of a `DE_w` that is not the one the target needs.
2. **With 4 windows, any variance-based `DE_w` is noise (arithmetic).**
   - A between-window variance from K = 4 has 3 degrees of freedom. The 95% chi-square bounds
     (0.216, 9.35) put the true variance at **0.32× to 13.9×** the estimate.
   - The naive cluster bootstrap of a mean underestimates variance by (K − 1)/K = **0.75** at
     K = 4, and it has only C(7,4) = **35** distinct resamples.
   - Groups per window: under Poisson(5) the 4-window mean has a CV of about 22%. With mega-event
     overdispersion (SD ≈ mean) it is about 50%. The "windows needed" projection inherits that.
3. **Re-projecting every 4 windows and stopping when "effective n reaches the target" is optional
   stopping on a noisy nuisance estimate.** The rule stops preferentially when `DE_w` happens to
   be estimated low, so the effective n at stopping is biased upward. This does not inflate α
   in sub-project 1's test directly, but it makes the delivered power lower than the 80% that
   §3 promises.

**Fix.** Use a fixed K from objection (a), sized with a **conservative pre-registered** DE, for
example Kish's size term times an assumed intra-window correlation ρ = 0.3, with ρ ∈ {0.1, 0.5}
printed as sensitivity. Use the pilot only for the feasibility go/no-go. If adaptivity is kept,
stop on the **upper** 80% bound of `DE_w`, not its point estimate, and state which outcome
defines it.

### Check 4: adjudication's effect on bias, and whether blind recall is computable and meaningful

- **Bias:** see objection (b). The headline on the final partition credits detector-kin systems
  by construction, and merge-only adjudication inflates "confirms" for every system.
- **Computability.**
  - **The unit does not match the headline.** "Blind positives ÷ final positives" is a
    **pair**-level ratio. One adjudicated merge of a 3-item group with a 2-item group adds 6
    pairs, so this is exactly the quadratic weighting that rev 2 had removed from the headline.
    Define blind recall at the headline's unit: the share of final multi-outlet groups that
    already existed, as multi-outlet groups, in the blind partition.
  - **Attribution is undefined.** "Per detector" needs a rule for pairs proposed by several
    detectors, and for merges where the shown pair came from one detector but pulled in items
    proposed by none.
  - **Truncation is not weighted** (objection b). As defined, the figure cannot fall far below 1
    in a window where the cap bites. Mutation test: build a fixture where the blind pass misses
    half the low-lexical pairs, and seed 500 hub-entity decoy pairs. Pre-register how far the
    readout's blind recall falls. Under the current §6.4 it barely moves.
- **Contamination the spec does not mention.**
  - **Learning.** The operator sees the detectors' finds after each window. Later blind passes
    drift toward detector-findable pairs, so blind recall rises over windows for exactly the
    pairs the detectors can see, and the invisible class stays invisible. Print blind recall by
    window order.
  - **The consistency repeat is not blind.** Window 2 is adjudicated right after its first pass
    (§6.4), then served again after a week as pass 2 (§4.5). Pass 2 is informed by the merges he
    accepted in window 2's adjudication, so pass-2 against pass-1 agreement measures memory plus
    learning, not reliability.
    - The header still shows the item count, which is a fingerprint.
    - **Fix:** defer window 2's adjudication until after its pass 2 (it is designated in
      advance), and drop the item count from the pass-2 header.
- **Meaningful?** "An upper bound" is correct in direction (§4.6). An upper bound that sits near
  1 by construction carries no information. With the fixes in (b), it becomes a real measurement
  for the classes the LLM and trigram detectors can see, and it stays silent about the rest.

---

## Rev 2's objections: how revision 3 answered them

| Rev 2 objection | Rev 3's answer | Grade |
|---|---|---|
| (a) Pilot first with a throwaway page | Operator declined (§3) | **Answered by decision.** The sizing risk it guarded against remains: central projection about 24 h at Δ = 0.15 (objection a) |
| (b) Census completeness is unmeasured | Adjudication plus blind recall | **Inadequate, and it created a new problem:** flood, uniform cap, merge-only, and the headline on the adjudicated partition (objection b) |
| (c) Temporary tool welded into production | Separate `labeller` service | **Right direction, broken wiring** (checks 1 and 2). `gold_prepare`, `/label` and the nudge still live in the production container and the `collect` delivery path |
| DE: wrong mean, wrong unit | Group-level headline, window bootstrap | **Unit answered; quantity undefined** (check 3) |
| ρ measurable for free from production links | Not adopted | Now unavailable anyway: comprehension off (check 3) |
| Quadratic estimand | Group-level headline | **Answered.** Blind recall reintroduces it (check 4) |
| K from windows 1–2 is band-biased | 4-window pilot, one per band | **Answered in form;** 4 windows is noise (check 3) |
| Time per window | Active and wall-clock minutes, sessions | **Answered.** The one-window-a-day cap is new (objection a) |
| Security items 1–6 | All adopted | **Answered** |
| §1 quoted a retired 78% | Withdrawn | **Answered** |
| Out-of-order retries | `client_seq`, ties broken by `id` | **Answered** |
| Slot list moves | `gold_slot_order`, persisted once | **Answered** |
| Replay span not stated | 28 days stated | **Answered in span; unpriced.** Estimate: ~670 non-quote items/day × 28 days × $0.0027/item (memory `newsbrief-comprehend-cost`) ≈ **$51 per arm per run**, about 34 days of the production budget. Sonnet's 23% self-disagreement argues for 2 or more runs |
| Gap distribution before fixing W | §4.3 gap check | **Measures the wrong quantity:** see below |
| Window-level escape, pass 2's place in K | Abandon with reason; both count toward time | **Answered** |

---

## Other findings

- **§4.3's gap statistic understates the loss.**
  - "Share of merged pairs more than 3 h apart" counts only pairs that *must* be split. Aligned
    slots also split pairs under 3 h, with probability g/3, so the loss is E[min(g/3, 1)].
    Example: gaps uniform on 0–6 h give a §4.3 share of 50%, but a loss of 75%.
  - Its input, "slots comprehended under the current ranking" (since 2026-09-09), covers only
    the few days comprehension was on (check 3).
  - Print the E[min(g/3, 1)] figure and the number of slots it rests on.
- **The block straddles two known capture changes.**
  - The Reuters proxy moved from `/markets` to `/business` in `c439ade` (2026-09-25,
    `brief.py:150`). Its host deploy time is unknown, because the runbook step 1 status is
    "outstanding".
  - The quote-page flood began around 2026-09-15 (`common.py` comment above `is_quote_page`).
    **Unverified:** whether the 100-item Google News cap let quote pages crowd real Reuters
    Markets items out of capture. If so, the first half of the block under-represents one wire.
  - Stratification is by time-of-day band only. Report the headline by block half, or exclude
    the flood days from the eligible block by pre-registration.
- **Entities have no window, so a "28-day replay" is not a complete state.** `SurfaceIndex`
  matches every entity ever created (`comprehend.py:1282–1287`). A replay that starts cold at
  "block start − 14 days" lacks the entities born from 2026-09-04 onward, so its candidate gate
  differs from production's for the first days. Either start the replay at capture day one, or
  state that every arm shares the cold start.
- **The headline is recall-only.** A system that puts every item in one event confirms 100%.
  Sub-project 1's decision rule must pair it with the precision sample (§4.6), and should
  decompose each unconfirmed group:
  - the item was not triaged material (the population is all items, §4.1, but production
    integrates only material items);
  - the event was not retrieved;
  - it was retrieved but not linked.

  Otherwise "retrieval is the cause" (§1) stays untested.
- **"None of them marked unsure" (§4.4) is ambiguous.** It can mean that a group with one unsure
  item is dropped entirely, or that unsure items are simply never grouped. Pick one, because the
  first drops the hardest groups.
- **The per-window Sonnet call needs the house rules:** `thinking` set explicitly, `max_tokens`
  sized for 150 or more items, and a check of `stop_reason` before parsing (memories
  `newsbrief-model-config`, `signals-parse-error-is-truncation`). Its spend should land in
  `comprehend_spend`, or it becomes an unbudgeted path beside the one phase 1 built after the
  £15/day incident.

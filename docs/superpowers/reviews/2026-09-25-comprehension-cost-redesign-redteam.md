# Red team: comprehension cost redesign (2026-09-25)

**Target:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`
**Stance:** hostile. I am looking for reasons this is the wrong thing to build, not for polish.
**Grounding:** I read `comprehend.py`, `capture.py` (alerts), `common.py` KNOBS,
`scripts/score_comprehension.py` (`gate_corroboration`), the parent spec §5, §7.2.1, §8.2
(Amendments 1–2), §12, the 2026-09-10 cost handover, and the 2026-09-22 runbook. I made no
network calls and did not touch a database.
**Legend:** **[V]** means I checked it in code or docs. **[I]** means I inferred it and it is
not measured.

---

## Objection 1 (a): the bill is volume × material rate × price. The spec spends most of its machinery on price, and the volume lever is nearly free

**The spec's own arithmetic.** Cost is ~$0.0027 per *integrated* item, steady from day to day
(§2.1) **[V, spec]**. After the Reuters fix, inflow is ~1,040 items/day (§4). The phase-1
estimate of $1.40–2.40/day therefore implies **518–889 integrated/day, a 50–85% material
rate**, and the phase-2 estimate of $0.70–1.20 is the same range at the batch half-price
**[V, arithmetic on the spec's figures]**. Batching (phase 2) is the largest part of the spec:
a new `comprehend_batches` table, a JSONB manifest of label maps, a non-blocking multi-request
batch client, pg_trgm clustering, a `NEWn` schema with three validation rejections,
reservation and reconciliation, a six-row failure table, a changed integration select,
Amendment 3, and a fresh 7-day gate window. All of it to take ~50% off a price.

**The pipeline was designed and pre-registered at ~10% material, not 86%.** The parent spec
sized everything at ~120 integrated/day (§5.3, §6.4). §12.1 pre-registered "~10% material" and
~250/day **[V]**. `select_sampled`'s docstring still argues its whole existence from "~120/day"
(`comprehend.py:915-917`) **[V]**. The 86% is a departure from the registered design, and the
handover measured it as the rules half doing nearly all the deciding: `triaged_by_rules` 41–109
against `triaged_by_model` 0–7 per pass **[V, 2026-09-10 handover §4]**.

**D5 does not restore the design, and the spec does not expect it to.** D5 sends entity-only
hits to Haiku. But §4.5 explicitly leaves the prompt alone ("not tightened in this phase"), and
that prompt defines materiality as subject-in-domain: *"Judge the SUBJECT, not the importance.
A minor development in the domain is material"*, where the domain includes "the markets those
move" (`comprehend.py:809-818`) **[V]**. Every feed in `RSS_FEEDS` was chosen *for* that domain
(`brief.py:147` onward) **[V]**. So Haiku will be asked "is this geopolitics or macro news?" about
a corpus selected to be geopolitics and macro news. I expect a high material rate **[I]**, and
the spec's own 50–85% estimate range agrees.

**D1 rejects the lever on the wrong axis.** Its rejection reads "Selectivity only: leaves the
price unchanged." Price is not the bill. At 1,040 items/day:

| design | integrated/day | $/day (integration, at $0.0027) |
|---|---:|---:|
| phase 1 as specced (85%) | 884 | 2.39 |
| phase 2 as specced (85%, batch) | 884 | 1.19 |
| **selective triage at 20%, real-time, no batching** | 208 | **0.56** |
| selective triage at the pre-registered 10% | 104 | 0.28 |

Add roughly $0.14/day for real-time triage. D6 says batching triage saves ~$0.07, so real-time
triage costs about twice that **[I, derived]**. Even so, selective real-time triage comes in
at or below phase 2's *lower* bound, and it needs none of phase 2's infrastructure. The
discounts also **stack**: if a KB consumer ever justifies more volume, batching is still
available.

**The budget has the same shape.** `COMPREHEND_MAX_ITEMS` (300 per pass,
`common.py:268`) is already a settings row, and it already caps both selects per pass
(`comprehend.py:318-319`, `:398`) **[V]**. Per-item cost is flat (§2.1), so an item cap is
already a dollar cap to within about 25%, with no code. It does not need the token bucket's
`runtime_state` accrual clock, pause semantics, in-code price table, `unpriced_model` refusal
path, or phase-2 reservation and reconciliation. Keep the **ledger**: it is cheap, and it answers
the question the handover took three days to answer. Store tokens and model in it, and price
them in the reporting query rather than in the hot path.

**What I would build instead:** phase 1's §4.1 (Reuters), §4.2 (`0rg`) and §4.4's newest-first
ordering, plus the ledger. Rewrite the triage prompt toward the registered ~10–20% material
rate, pre-registering the target before deploy. The `sampled` arm (§5.3) keeps the control
unconfounded. Set `COMPREHEND_MAX_ITEMS` as the hard ceiling. Shelve phase 2 until something
reads the KB.

**The cost of this route, stated honestly:**

- **Coverage.** The KB will hold fewer minor stories. For a "KB-rendered brief" that is
  arguably the right content anyway **[I]**.
- **The gate.** A stricter triage changes the measured system. Phase 2 does that too, and
  Amendment 3 already plans for it.
- **Re-integration.** `item_triage` rows are keyed by `triage_prompt_version`
  (`comprehend.py:391`, `:621`). A naive `TRIAGE_PROMPT_VERSION` bump therefore creates fresh
  rows with `integrated_at IS NULL` and re-integrates every in-horizon item that was already
  done. The prompt change has to guard against that explicitly.

---

## Objection 2 (b): "gaps in the KB's history are a real loss" is asserted, not established. D7, the decision it drives, turns the 09-30 gate run into a binding verdict on a mixed pipeline

**The requirement.** §1 treats KB history gaps as "a real loss". D7 restarts comprehension
after phase 1 because "the KB gap grows". Nothing in the spec measures what a gap costs.

**What the code says instead.**

- **Nothing reads the KB.** The spec says so in §1, and `comprehend.py:11` says "Nothing reads
  what this writes" **[V]**.
- **Comprehension is replayable by design.** "a captured item can be reprocessed indefinitely"
  (`comprehend.py:5-9`) **[V]**. Capture is the irreplaceable layer, and it keeps running
  regardless.
- **Matching can be done as of an item's own date.** `candidate_events` already takes an
  `as_of`, and its 14-day window is computed from it: `coalesce(as_of, now()) - 14 days`
  (`comprehend.py:955-956`, `:1037-1038`) **[V]**. So §4.4's "an item older than that cannot be
  matched against current events anyway" is true only because `run()` passes no `as_of`
  (`comprehend.py:450`). A later backfill could match old items against the events of their
  own time.
- **Phase-1 spend buys no gate evidence.** Amendment 3 puts the cutover at the phase-2 flip
  (§6.2) **[V, spec]**. D7's restart therefore produces data that has no consumer and is
  excluded from the gate. At the $1.50 default, that is ~$21–42 for a 2–4 week gap between the
  phases **[I, the interval is unknown]**.
- **The spec creates gaps itself.**
  - D4 writes `verdict='stale'`, which is terminal: `pending_triage` re-selects only
    `t.id IS NULL OR (verdict='failed' AND attempts < 3)` (`comprehend.py:622`) **[V]**.
  - §4.6 expects "most outage-hit items" to become `stale`.

  So the requirement is not honoured even inside the spec, and D4 converts a *replayable*
  backlog into an *unreplayable* one. That is the only mechanism by which a pause becomes a
  real loss.

**The concrete harm is to the gate.** §6.1 says the 09-30 run will refuse because "the corpus
stopped growing mid-cohort, which is the condition `zp8` built". **That misreads `zp8`.**

- **What `zp8` actually tests.** It compares `max(events.created_at)` over the WHOLE KB
  (`score_comprehension.py:231`) against the *end* of the cohort. It refuses only if no event
  was created in the final `horizon`-long slice: `last_event_at < end - horizon`
  (`:266-267`) **[V]**. It cannot see a hole in the middle of the cohort.
- **The run.** It is `--cutover 2026-09-22T18:13:37Z --horizon-hours 6` at
  2026-09-30 00:13:37Z, so `end` = 09-29 18:13 and the refusal needs no event after
  **09-29 12:13Z** (runbook step 5) **[V]**.
- **If phase 1 restarts before that instant**, which is likely because phase 1 is the P1 fix,
  the check passes. The gate then returns PASS or FAIL over a cohort made of three different
  things:
  - ~3 days of the old pipeline: quote-page-flooded and draining an oldest-first backlog;
  - a multi-day hole;
  - the phase-1 pipeline: Haiku triage, narrowed rules, newest-first, quote pages removed.
- **That verdict is binding.** Amendment 2 and the runbook both say this run's verdict
  "stands whatever it says" and that "a second appeal to instrument error is not available"
  **[V]**. This is exactly the `zp8` failure class ("presents as an ordinary population") one
  level down: a mid-window gap instead of a stopped tail.
- **So the instrument does not decide.** "The instrument decides, not us" is false as written.
  The deploy date of phase 1 decides which branch fires.

**What I would do instead:**

- Keep `COMPREHEND_ENABLED` false until the pipeline that will be gated exists. If a
  phase-1 validation run is wanted, flip it only after 2026-09-30 00:13Z, once the old gate run
  is recorded.
- Or amend §8.2 *now*, before any restart, to declare the 09-22 cohort's admissibility.
- Implement the 14-day horizon as a select-time filter that writes nothing. Do not use a
  terminal `stale` verdict. The backlog then stays replayable, at batch price and as of its own
  date, if a KB consumer ever makes history worth paying for. Run any such backfill outside a
  gate window: backfilled events get a current `created_at`, which the cohort keys on
  (`score_comprehension.py:182-185`) **[V]**.

---

## Objection 3 (c): in six months the budget will hide the next upstream shift instead of stopping it, and it adds a silent-stop path of its own

**The recurring constraint is upstream composition, which this pipeline does not control.** The
Google-proxy feeds have now changed shape at least twice:

- Parent spec §12.2 already saw `(MYCN.O) | Stock Price & Latest News` on 2026-09-05 **[V]**.
- On 2026-09-16, Reuters tripled with no deploy, and "Google began indexing them under
  `/markets`" (§2.2) **[V, spec]**.

That tripling ran for **nine days** and was detected only by the money running out (§1). The
brief was reading quote pages as headlines through `url` for the same nine days (§4.1).
`capture.py`'s alerts are all *absence* detectors: `liveness`, `failing_feeds` and
`item_drought` (`capture.py:525`, `:579`, `:653`) **[V]**. Nothing alerts on a surge.
`is_quote_page` is a title regex for *this* shape. The next shape (live blogs, video pages,
another section path) will not match it **[I, but the base rate is two in three weeks]**.

**What the redesign does to that signal.** It makes spend flat, and that deletes the only
detector that has ever fired. The next surge plays out like this:

1. **It first drains the accumulated bucket.** In phase-2 steady state the spec expects
   $0.70–1.20/day against a $1.50 allowance. The balance therefore climbs to the
   `allowance × max_days` clamp, **$10.50**, within ~2–5 weeks of surplus **[V, arithmetic on
   §4.3 and §5 defaults]**. A surge day can then spend ~$12. That is ~70% of the 24 Sep day
   ($17.80) that motivated this spec, so D3's stated goal ("no cap: an upstream surprise
   becomes a daily top-up again") is not met at its own defaults.
2. **Then the bucket binds at $1.50/day indefinitely.** Newest-first plus the horizon silently
   age out real items while junk takes their budget. The only trace is `Tally.aged_out` in a
   log line.
3. **The exhaustion alert is either spam or silence.** §4.3 clears the episode "when the balance
   recovers", but accrual is continuous (`allowance / 86400` per second). Under sustained
   exhaustion, the balance is positive again at the start of *every* hourly pass.
   - If "recovers" means balance > 0, the alert re-fires up to 24 times a day and will be muted.
   - If it means something with hysteresis, it fires once and then never again.

   The spec does not say which. Neither version names the outlet, so the diagnosis in §2.2
   (per-outlet counts, live fetches) has to be redone by hand **[V that the spec leaves
   "recovers" undefined; I on the consequence]**.

**A second silent stop arrives with model churn.**

- `_integrate_model()` is `common.INTEGRATE_MODEL or common.MODEL`, and `_triage_model()` is the
  same (`comprehend.py:822-824`, `:1201-1202`). `INTEGRATE_MODEL` defaults to `""`
  (`common.py:290`) **[V]**. `MODEL` is the `NEWSBRIEF_MODEL` settings row (`common.py:178`),
  which exists precisely so the brief's model can be swapped *without a redeploy* **[V]**.
- Under §4.3, "a model missing from the table refuses to run". The next time the operator
  bumps the brief's model, comprehension therefore aborts `unpriced_model` on every pass until
  a code deploy adds a price row.
- §4.3 alerts only on exhaustion, so this abort pages nobody.
- If it happens during a gate window, it stalls `last_event_at` and voids the window. That is
  the hazard §6.2 names, arriving by a path §6.2 does not list.
- Sonnet generations turn over on a timescale of months **[I]**. This is a six-month event,
  not a hypothetical.
- The hardcoded prices will also drift from Anthropic's actual prices without any refusal
  at all.

**What I would do instead:**

- Put the detector where the cause is. Add a capture-side *upward* volume alert per outlet,
  mirroring `item_drought`'s own-history method in the other direction (today's items for an
  outlet against its trailing 7-day maximum), with a message that names the outlet. It would
  have caught 16 Sep on 16 Sep, it protects the brief as well as the KB, and it costs no model
  calls.
- Size the budget clamp so that a full bucket cannot buy a day the operator would have refused.
  One day of allowance is enough, or use the item cap from Objection 1.
- Make any refusal to run page the operator through the same alert contract.
- Price the ledger in the reporting query, so that a model change affects a report and never
  stops the pipeline.

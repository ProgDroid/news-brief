---
name: comprehension-cost-redesign-phase-1
description: "Comprehension cost redesign phase 1 (epic news-brief-2r5) is BUILT AND PUSHED 2026-09-25; the host runbook is NOT run, and COMPREHEND_ENABLED must stay off until the old gate runs on/after 2026-09-30 00:13:37Z"
metadata:
  node_type: memory
  type: project
  originSessionId: 9ddd2146-00b2-42bb-a88a-0f15d3efd634
  modified: 2026-09-26T18:14:31.815Z
---

**Why it exists:** after re-enabling comprehension, the operator was topping up the Anthropic account by
~£15/day. The cause: Google News began indexing Reuters **instrument quote pages** (`MSTS.DE - Reuters`,
"stock price & latest news") around 15 Sep. There were ~2,000/day, 78% of the corpus, and a 12-day
pause left a backlog that was drained oldest-first, on Sonnet, with no budget.

**Phase 1 (`3652583..` the implementation-record commit, on main, pushed 2026-09-25):**
- quote pages are filtered at capture and in `brief.fetch_rss`;
- the Reuters proxy moved from /markets to /business;
- a per-outlet surge alert;
- an account failure (402/401/403, or a 400 whose body says "credit balance") aborts the pass and charges NO item (`0rg`);
- stale/quote_page triage verdicts (0014);
- the rules half is material only for tracked claims and stories;
- newest-first with a 14-day horizon;
- the `comprehend_spend` ledger (0015);
- a price table that REFUSES unpriced models;
- a token bucket (`COMPREHEND_DAILY_BUDGET_USD` 1.50, `COMPREHEND_BUDGET_MAX_DAYS` 3);
- abort and budget alerts;
- models frozen once per pass.

**How to apply:**
- **Nothing ran on the host.** Execute `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md` IN ORDER. Step 2 (the old corroboration gate) must run BEFORE comprehension is enabled; `li9` (2026-09-26) now refuses a cohort gap > horizon, interior or trailing, and prints the largest gap. zp8's frozen branch still fires first, so li9 cannot change the 09-30 run unless comprehension restarted too early. The epic stays open until the operator reports the runbook's observations.
- A push to main publishes `:latest`, so the phase-1 image may already be deployed. That is runbook step 1, and it is safe only because comprehension is off.
- The triage model moves to Haiku via the `NEWSBRIEF_TRIAGE_MODEL` row, **not `TRIAGE_MODEL`**, which is accepted and read by nothing. `thinking: disabled` on Haiku 4.5 is documented, not observed. A 400 there is a 4xx, and a 4xx CHARGES items, so watch that first pass.
- Decisions, defects the reviews found, and all 23 rulings: `docs/2026-09-25-comprehension-cost-phase-1-implementation-record.md`.
- Phase 2 (Message Batches plus clustering) is committed but needs its plan REVISED first (bead `vlg`, after a clustering-recall spike).
- **2026-09-26: D1 re-affirmed, so don't re-propose Haiku-for-integration as the phase-2 route.** I raised that Haiku 4.5 in real time costs exactly what a Sonnet batch costs ($1/$5 per MTok) with none of the async hazards. The operator kept batching (the gate was registered on Sonnet, and extraction quality is unmeasured).
- **The spike is BUILT, NOT RUN: `scripts/probe_clustering.py`, bead `1tl`, blocks `vlg`.** Run it on the host with `docker compose run --rm --entrypoint python newsbrief scripts/probe_clustering.py`. It can run before 30 Sep, because it only reads.
  - **Operator's pre-registered rule:** at W=2h, the simplest of T, then T-cc, then TE-cc within 5 pts of real time wins. If none is, the rewrite redesigns the blindness before batching.
  - **My guesses:** T 40–60%, TE-cc 85%+, RT 85–95%.
- **2026-09-26 later, the spike ran.** VERDICT: NONE (TE-cc 65.4% vs RT 97.2%). M1 (the density sweep, `4le`) found that batching qualifies only at ≤ 5 material items/h, and that **RT itself falls to 74.8% at 5/h**. M2 (Haiku replay, `y1x`) RAN 2026-09-27, $5.53: **HAIKU QUALIFIES** under the pre-registered rule (A-A′ 96.5%, A-H 93.2%). **But the rule's population diluted the tolerance ~6.6x (analysis-stats-traps Trap 8). On LINK decisions, Haiku agrees with Sonnet on 55.1%, against Sonnet's 76.6% agreement with itself.** Which way the departures go (fewer, more, or different links) is unmeasured: `sse`. The phase-2 route choice (`vlg`) is the operator's, informed by both. Everything is in `docs/2026-09-26-clustering-recall-spike-result.md`.
- **In-request NEW links (`6kr`) are BUILT on main, as integration prompt v3**, and deploy inert while comprehension is off. Record: `docs/2026-09-26-in-request-new-links-implementation-record.md`.
  - A prompt-version bump no longer re-integrates anything, so do NOT rely on a bump to re-extract (`wt8`).
  - `jwm` DONE 2026-09-26: integration prompt **v4** scopes NEW labels to events; runbook Step 5 watches `unmapped_candidate`.
  - After 7 days, runbook Step 7's co-batched and linked pair counts are owed.
- Open beads from this work:
  - `2ln` (state_store aliasing);
  - `eqs` (are timed-out requests billed?);
  - `711` (commit the spend row immediately);
  - `54x` (triage window stall under exhaustion);
  - `7gp` (select_sampled session TZ).

Related: [[newsbrief-comprehend-cost]], [[newsbrief-comprehension-pipeline]], [[snapshot-config-per-unit-of-work]].

**From the index (moved 2026-10-02):** Phase 2 status as carried by the index: spike + M1 RAN (grouping fails; RT goes blind at low density). M2 `y1x` RAN: HAIKU QUALIFIES by the rule, but on link decisions it agrees with Sonnet only 55% vs Sonnet's self-agreement of 77%; which way it departs is unmeasured (`sse`). `jwm` done (prompt v4), `li9` done (gate refuses mid-cohort holes).

**2026-10-02: the host runbook RAN through step 5.** Step 1 passed by effect (no quote page stored since 2026-09-26 10:00:16Z; 0 dropped in logs, so the c439ade feed move stopped the flood). Step 2: GATE NOT RESOLVED, corpus FROZEN, as pre-registered (`docs/2026-10-02-amendment-2-gate-output.md`). Steps 3-4 done (305 + 12 rows reset). Step 5: `COMPREHEND_ENABLED` flipped on 2026-10-02; operator reports it running as expected. Recorded evidence (cutover, Haiku in `comprehend_spend`, first triage log line) is in the runbook's status table once supplied. **The 'stays OFF' instruction above is SUPERSEDED.**

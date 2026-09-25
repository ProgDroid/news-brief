---
name: comprehension-cost-redesign-phase-1
description: "Comprehension cost redesign phase 1 (epic news-brief-2r5) is BUILT AND PUSHED 2026-09-25; the host runbook is NOT run, and COMPREHEND_ENABLED must stay off until the old gate runs on/after 2026-09-30 00:13:37Z"
metadata:
  node_type: memory
  type: project
  originSessionId: 9ddd2146-00b2-42bb-a88a-0f15d3efd634
  modified: 2026-09-25T22:42:02.912Z
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
- **Nothing ran on the host.** Execute `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md` IN ORDER. Step 2 (the old corroboration gate) must run BEFORE comprehension is enabled; `zp8` cannot see a mid-window hole (`li9`). The epic stays open until the operator reports the runbook's observations.
- A push to main publishes `:latest`, so the phase-1 image may already be deployed. That is runbook step 1, and it is safe only because comprehension is off.
- The triage model moves to Haiku via the `NEWSBRIEF_TRIAGE_MODEL` row, **not `TRIAGE_MODEL`**, which is accepted and read by nothing. `thinking: disabled` on Haiku 4.5 is documented, not observed. A 400 there is a 4xx, and a 4xx CHARGES items, so watch that first pass.
- Decisions, defects the reviews found, and all 23 rulings: `docs/2026-09-25-comprehension-cost-phase-1-implementation-record.md`.
- Phase 2 (Message Batches plus clustering) is committed but needs its plan REVISED first (bead `vlg`, after a clustering-recall spike).
- Open beads from this work:
  - `2ln` (state_store aliasing);
  - `eqs` (are timed-out requests billed?);
  - `711` (commit the spend row immediately);
  - `54x` (triage window stall under exhaustion);
  - `7gp` (select_sampled session TZ).

Related: [[newsbrief-comprehend-cost]], [[newsbrief-comprehension-pipeline]], [[snapshot-config-per-unit-of-work]].

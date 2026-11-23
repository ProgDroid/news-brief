---
name: brief-cost-measurement
description: "How to price the daily brief: NOT from the est_batch_cost log line (stale rates, 1.5x high) but from tokens via scripts/brief_cost.py; measured ~$9/month (2026-11-23)"
metadata:
  node_type: memory
  type: project
  originSessionId: 22ccb82f-0fe3-4abb-bef4-0bc1c66e258c
  modified: 2026-11-23T12:36:02.541Z
---

**Never quote `est_batch_cost` as the brief's cost.** `brief.py`'s "Batch result:" log line prices tokens at hard-coded "Sonnet 4.6" batch rates ($1.50/$7.50 per Mtok); the brief runs `claude-sonnet-5`, which `comprehend.PRICES_PER_MTOK` × `BATCH_FACTOR` prices at $1/$5. So it reads 1.5× high on tokens (bead `news-brief-5ni` fixes the line). On 2026-11-23 I quoted it, told the operator the brief was ~$13/month and the budget "at the £20 floor", and wrote that into spec §11 and bead `3km`; the plan-2a red team caught it.

**Measure with `scripts/brief_cost.py`** (plan 2a Task 1, on the host: `docker compose run --rm --entrypoint python newsbrief scripts/brief_cost.py`). It re-prices every `Batch result:` and `Realtime brief:` line in `newsbrief.log` + rotations from tokens, per mode, with a `SHORT:` flag when the log spans under 30 days. Its web-search price ($0.01/req) is from a code comment and labelled unverified.

**Measured 2026-11-23:** batch briefs $0.47 / $0.13 / $0.29 a day → ~$9/month, inside the storylines spec's $7–10.70 estimate. Input swings 92k–397k tokens: ~90k base prompt, the rest web-search and code-execution results. A real-time fallback replay (377k in, 8 searches) cost ~$0.89 at full price.

**Why:** a cost line that looks measured but carries a stale constant drove a wrong budget conclusion through three artifacts in one turn (probe-failures #5).

**How to apply:** for any brief-cost question, run the script (or price tokens with `comprehend.cost_usd`) and say which rates you used. A ~2k-token prompt change (e.g. phase 1's dated window, ~8.8k chars) is invisible against that day-to-day swing: measure the added block's size directly, never by comparing usage lines. Related: [[rethink-charter-2026-11-20]], [[newsbrief-comprehend-cost]], [[batch-pipeline-timing]].

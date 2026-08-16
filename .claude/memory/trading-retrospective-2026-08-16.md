---
name: trading-retrospective-2026-08-16
description: "First trading retrospective — performance layer was corrupt, signals are 50/50 directionally, the reversal rule was the real loss; momentum cap rejected on evidence"
metadata: 
  node_type: memory
  type: project
  originSessionId: 951b41de-741d-4e04-a8b4-945ab0025650
  modified: 2026-08-16T11:10:22.926Z
---

**2026-08-16, first retrospective on the trading half. Full write-up is in the repo
at `docs/2026-08-16-trading-retrospective.md` — read that, not this.** Commits
`9d766e1` (data bugs) and `a60ef2c` (reversal removed), 702 tests. This entry holds
what the doc cannot: the open action, and what to distrust.

**Host repair: RUN by the user on 2026-08-16** (`scripts/repair_unit_bug_rows.py`).
Not independently verified — confirm on the next book pull that the five corrupt
rows are gone and mean edge reads ≈−1.6% rather than −9.3%. The script is idempotent,
so re-running is safe and prints "Nothing to repair" if it already applied. Note it
is **dry-run unless `--apply`** is passed. If it ever needs re-running, never restore
the `from-server/` snapshot (2026-08-14, gitignored) over the live file — run the
script against the live book instead.

**Distrust every performance number produced before this date.** Three bugs fed
`performance_report`, `evaluate_gate` *and* `performance_prompt_block` — so the
model's own track-record block was reading fiction. Mean edge was reported as
−9.34%; it is −1.56%. All three are now guarded in `trading.py`
(`PRICE_SANITY_RATIO`, `BENCHMARK_SANITY_RETURN`, prediction-only −100% floor).

**The two findings that matter:**

1. **The signals carry no directional information.** 28 single-leg episodes, hit
   **50.0%**, sign-test **p=1.000**. There is nothing to invert — the "fade the
   event" hypothesis is dead, and the earlier leg-level p=0.033 was a correlation
   artifact (see [[analysis-stats-traps]]).
2. **The reversal rule was doing the damage.** Multi-leg chains −6.02% vs −0.28%
   for holding the first thesis; holding won 17 of 21 chains, p=0.0072. Fixed by
   declining contrary signals (`contrary_held` leakage key). **This creates no
   edge** — it takes the sleeve from −6% to roughly zero and cuts 126 legs to 43,
   which matters mainly because clean observations are a better instrument for
   detecting a future edge than thrash.

**Do not re-propose these — both were investigated and rejected on evidence:**

- **Capping/stopping momentum prediction plays.** Every simulated version is
  neutral or worse; the losers were already at −100% by their 1w checkpoint, and a
  −20% stop kills the biggest winners (two were double-digit down at 1w before
  +151.8% and +24.0%). The real signal is entry price (sub-0.10 longshots are 5-of-6
  total losses) but at n=6 that is unresolvable in either direction.
- **Running BTFDBot (btfdbot.com) as a parallel sleeve.** Its best-documented
  strategy ("Large Gaps Down") *is* the fade hypothesis as a price rule, so it buys
  correlation, not coverage. What news-brief has that a price scanner lacks is the
  *reason* for a move — which is exactly that site's stated failure mode ("stocks
  falling out of bubbles"). A complement to a gap trigger, never a parallel system.

Related: [[analysis-stats-traps]], [[live-state-on-deploy-host]],
[[self-improving-trading-roadmap]], [[polygram-live-trading-spec]],
[[newsbrief-deferred-findings]].

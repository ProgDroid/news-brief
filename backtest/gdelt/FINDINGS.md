# GDELT Signal-Validation Spike — Findings

**Window:** _<start>_ → _<end>_  ·  **GDELT region-event days:** _<n>_
**Engine:** `backtest.run.run_backtest` (temporal 50/50 discovery/held-out split, standardized, horizons 1/3/5/10d)

## Pre-registered decision rule (set BEFORE running)
- **Primary cell:** signal = `conflict_mentions`, mode = `level`, per instrument.
- **GO** (build the GDELT 2.0 real-time feeder) requires, on the **held-out** split:
  correct-sign IC with `|IC| > 0.03` on **both** `USO` and `GLD`, hit-rate > ~53%, n ≥ ~120.
  `SPY` correct sign (negative) is confirmatory, not required.
- **NO-GO / SKIP:** otherwise. Everything outside the primary cell (mean_tone,
  mean_goldstein, delta mode) is **exploratory robustness only** — do not let an
  incidental hit in a non-primary cell flip the decision (multiple-comparison risk:
  ~24 cells swept).

## Results
_Paste `SUMMARY.md` here._

## Decision
- [ ] GO  — proceed to 2.0 real-time puller (separate spec/plan)
- [ ] NO-GO / SKIP — close backlog item #2

**Rationale:** _<one paragraph: which cells fired, signs, n, and why it clears/fails the bar>_

---
name: backtest-nonstationarity-check
description: Backtest methodology — a single temporal holdout can be regime-contaminated; always check per-period sign stability before believing a signal
metadata: 
  node_type: memory
  type: project
  originSessionId: 4c2fff72-fde8-4474-928a-21842bee9965
---

When validating any news/sentiment/event signal with `backtest/` (run_backtest's discovery→held-out temporal split), a non-zero **held-out IC is NOT sufficient** evidence of a real signal. The held-out window is a single later slice and can be dominated by one regime.

**Why:** In the GDELT spike (2026-06-25, [[external-geo-dashboards-backlog]] #2) the held-out USO IC was a strong-looking **−0.2196 (~4·SE)**. A per-year split exploded it: IC = **+0.155 (2022) / −0.180 (2023) / −0.006 (2024)**, full-period ≈ 0. The "signal" was a sign-flipping regime artifact — the 50/50 temporal split simply put the +regime in discovery and the −regime in holdout, manufacturing a confident out-of-sample number. We nearly recorded it as a finding.

**How to apply:** Before believing any backtest signal, run a **per-period (e.g. per-year) sign-stability check** in addition to the held-out IC. If the sign flips across sub-periods, it's non-stationary → not tradeable, treat as null. Cheap to add: `align_dated` → group pairs by `date[:4]` → `spearman_rank_ic` per year. Also stress with (a) volume-normalization (raw level counts carry GDELT's secular volume trend) and (b) tighter entity/region scoping — both shrank the apparent edge here. Complements the engine's existing in-sample-bias fix noted in [[bigdata-next-steps]] and the confident-null discipline in [[sentiment-sizing-null-decided]].

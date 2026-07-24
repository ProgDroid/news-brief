---
name: self-improving-trading-roadmap
description: "Staged-autonomy plan to make paper trading self-improving; Stage A (measurement) BUILT+PUSHED 2026-06-25, Stage B is next"
metadata: 
  node_type: memory
  type: project
  originSessionId: 40635983-d539-4587-84a5-bfb7d59b46da
---

2026-06-25: decided to make the trading side self-improving **gradually**, increasing
autonomy only as accumulated paper-trade data validates the prior step. Roadmap doc:
`docs/superpowers/specs/2026-06-25-self-improving-trading-roadmap.md` (living).

**Stages A→B→C→D**, autonomy earned per stage (sample size is the gate — the project has
been burned by noise before, see [[backtest-nonstationarity-check]]):
- **A — measurement/attribution** (no autonomy). **BUILT + PUSHED 2026-06-25**
  (commits c774968..6a9fd5d → origin/main, Docker deploy triggered; 8 TDD tasks, 438 tests).
  Spec/plan: `docs/superpowers/specs|plans/2026-06-25-stage-a-trading-measurement*`. Ships:
  source `kind`+`perspective` attribution on closed trades (extractor names `source_id` from a
  closed source list → `annotate_signal_sources` resolves at save-time → `mode_paper` copies →
  `validation` dims), sample-aware calibration block w/ inversion flag, 7-day signal-leakage
  counts (`leakage-log.json`). All in `performance_report` (`/performance` + weekly); descriptive,
  fail-safe, zero autonomy. Activates on deploy; populates as trades close.
- **B — LLM self-recalibration** (feed track record into prompt; extends
  `performance_prompt_block`). Gated on A proving a stable signed effect. **Implementation note
  for B:** the firewall is structural — `performance_prompt_block` iterates a HARDCODED tuple
  `("asset_class","confidence","thesis_ref")`, NOT `_DIMENSIONS`. Stage A added `source_kind`/
  `source_perspective` to `_DIMENSIONS` (human report + gate) but they do NOT reach the prompt.
  To start B, edit that hardcoded tuple (+ honor `_PROMPT_MIN_N`), proving the dim first.
- **C — bounded autonomous knob-tuning**. **D — policy/source-weight learning.**

Key existing loop this builds on: `validation.py` already slices net/hit/edge by
asset_class/confidence/play_type/thesis_ref and feeds `performance_prompt_block` into the
daily prompt; `evaluate_gate` is informational and never auto-enables live.

Stage A decisions (Q1–Q5): source attribution at **kind+perspective**; resolve via
**upstream pick-from-list** (post-delivery signal extractor names the registry source it
cited from the day's tagged list, code derives tags — single source of truth); declined
signals = **lightweight leakage counts** now (full counterfactual scoring PARKED in roadmap);
**human-facing only** — `performance_prompt_block` firewall kept (prompt-feeding = Stage B).
Aligns with descriptive-only philosophy of [[sentiment-sizing-null-decided]].

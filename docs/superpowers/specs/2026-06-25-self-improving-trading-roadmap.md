# Self-Improving Trading — Staged-Autonomy Roadmap

**Date:** 2026-06-25
**Status:** Roadmap (living). Stage A in active brainstorming; B–D are placeholders to be
designed when their entry gate is met.

## Through-line

Autonomy is **earned, not granted**. Each stage stays descriptive / human-in-the-loop until
the measurement built in the *prior* stage shows a **stable, real, net-of-cost edge over a
sustained multi-evaluation window** — the same "sustained" philosophy already encoded in
`validation.evaluate_gate`.

**Sample size is the gate.** Paper trading is data-poor (a handful of closed positions per
week), and this project has already been burned by noise masquerading as signal:
- GDELT trading-signal IC of −0.22 flipped sign per-year (regime artifact, not edge).
- Sentiment-sizing was a confident null at n≈7k → enrichment is permanently descriptive-only.

We do not repeat that mistake by auto-tuning on tiny samples. Out-of-sample / temporal
validation, never in-sample fit.

## What already exists (the starting point)

`validation.py` already implements a soft feedback loop:
- `aggregate_performance` — net return, hit-rate, edge-over-benchmark by `asset_class`,
  `confidence`, `play_type`, `thesis_ref`.
- `performance_prompt_block` — injects the realized track record into the next daily prompt
  as *"context for self-correction, not a rule change."*
- `performance_report` — flags "chronically wrong" theses (n≥3, negative net).
- `evaluate_gate` — per-asset go-live readiness with a sustained positive-edge window;
  **never auto-enables live trading.**

So the system is already at "Stage A, partial / Stage B, partial." This roadmap deepens it
deliberately rather than reinventing it.

## Stages

### Stage A — Sharper measurement & attribution  *(BUILD FIRST)*
- **Goal:** the system reliably tells *you* what is working and what isn't, across more
  dimensions than today.
- **Autonomy:** none. Every change is a human decision.
- **Overfitting risk:** near zero (measurement only).
- **Exit gate → unlock B:** attribution is trusted (stable week-over-week) **and** at least
  one dimension shows a persistent signed effect across N sustained evals.

### Stage B — Deeper LLM self-recalibration
- **Goal:** feed richer, structured track record into the signal/brief prompt so the
  generator corrects itself. Extends `performance_prompt_block`.
- **Autonomy:** low — the LLM adjusts its own confidence/selection; code does not change
  knobs. Fail-safe: drop the feedback when samples are thin.
- **Exit gate → unlock C:** measured calibration improvement (stated confidence predicts
  realized edge) attributable to the feedback, sustained.

### Stage C — Autonomous parameter tuning  *(bounded)*
- **Goal:** code nudges a small, whitelisted set of knobs from realized P&L
  (candidates: the `medium/high` actionability filter, `PG_SIMILARITY_FLOOR`, the
  1w/2w/4w horizons).
- **Autonomy:** medium but bounded — bounded step sizes, hard min/max, only on dimensions
  with sufficient n, auto-revert on degradation, every change logged + reversible.
- **Exit gate → unlock D:** tuning demonstrably improves out-of-sample edge without
  increasing variance, sustained.

### Stage D — Policy / strategy learning
- **Goal:** standing policy that up/down-weights signal sources or topic types; possibly
  auto-enable live for an asset class that has cleared the gate.
- **Autonomy:** highest — but real-money go-live still sits behind the existing gate's
  human confirmation.

## Cross-cutting principles
- Descriptive-first; autonomy only after measurement proves a stable edge.
- Never auto-size on sentiment (already-decided null).
- Every autonomous action logged, bounded, and reversible.
- Sample-size gating everywhere; temporal/out-of-sample validation, not in-sample fit.
- Fail-safe: any feedback/tuning component degrades to "do nothing" on thin data or error.

## Progress log
- 2026-06-25 — Roadmap written. Stage A brainstorming begins.

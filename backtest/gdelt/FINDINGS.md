# GDELT Signal-Validation Spike — Findings

**Window:** 2022-01-01 → 2024-12-31  ·  **GDELT region-event days:** 2553
**Engine:** `backtest.run.run_backtest` (temporal 50/50 discovery/held-out split, standardized, horizons 1/3/5/10d)
**Held-out n:** ~371–376 per cell (well above the ~120 bar — a well-powered test, not a small-sample inconclusive). SE(IC) ≈ 1/√375 ≈ 0.052.
**Data note:** run in a simulated-2026 environment; 2022–2024 chosen deliberately as the unambiguously-real, axis-aligned window. GDELT files are correctly dated by **modal** SQLDATE (row-0 carries straggler/late-added events — do not date a file by row 0).

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

| instrument | field | mode | best h | held-out IC | hit rate | n | sign ok |
|---|---|---|---|---|---|---|---|
| USO | conflict_mentions | level | 10 | -0.2196 | 43.40% | 371 | no |
| USO | conflict_mentions | delta | 1 | -0.0216 | 48.40% | 375 | — |
| USO | mean_tone | level | 10 | 0.0951 | 50.13% | 371 | — |
| USO | mean_tone | delta | 1 | 0.0105 | 49.73% | 375 | — |
| USO | mean_goldstein | level | 10 | 0.1097 | 52.56% | 371 | — |
| USO | mean_goldstein | delta | 1 | 0.0517 | 52.14% | 375 | — |
| GLD | conflict_mentions | level | 5 | 0.0664 | 49.73% | 374 | yes |
| GLD | conflict_mentions | delta | 10 | -0.0050 | 50.13% | 371 | — |
| GLD | mean_tone | level | 5 | -0.0738 | 41.18% | 374 | — |
| GLD | mean_tone | delta | 1 | -0.1263 | 44.65% | 375 | — |
| GLD | mean_goldstein | level | 1 | -0.0486 | 47.20% | 376 | — |
| GLD | mean_goldstein | delta | 1 | -0.0272 | 49.73% | 375 | — |
| SPY | conflict_mentions | level | 3 | 0.0500 | 52.27% | 375 | no |
| SPY | conflict_mentions | delta | 1 | 0.0756 | 54.28% | 375 | — |
| SPY | mean_tone | level | 10 | -0.2556 | 32.08% | 371 | — |
| SPY | mean_tone | delta | 3 | 0.0416 | 51.87% | 374 | — |
| SPY | mean_goldstein | level | 10 | -0.2223 | 38.81% | 371 | — |
| SPY | mean_goldstein | delta | 1 | -0.0883 | 47.86% | 375 | — |
| ITA | conflict_mentions | level | 3 | 0.0930 | 53.21% | 375 | yes |
| ITA | conflict_mentions | delta | 3 | 0.0196 | 50.13% | 374 | — |
| ITA | mean_tone | level | 3 | -0.0600 | 41.18% | 375 | — |
| ITA | mean_tone | delta | 1 | -0.1114 | 46.65% | 375 | — |
| ITA | mean_goldstein | level | 5 | -0.1706 | 37.80% | 374 | — |
| ITA | mean_goldstein | delta | 1 | -0.0611 | 47.18% | 375 | — |

## Decision
- [ ] GO  — proceed to 2.0 real-time puller (separate spec/plan)
- [x] **NO-GO / SKIP — close backlog item #2**

**Rationale:** The pre-registered primary gate fails decisively. **Oil (USO)** — the cleanest
thesis target — shows a **wrong-signed, statistically strong** held-out IC of **−0.2196** (≈4·SE)
with a sub-coin-flip 43.4% hit-rate: more Middle-East conflict-mention *level* is associated with
oil *under*performance over the next ~10 days, the **opposite** of the "conflict → oil up" thesis
(plausibly mean-reversion after a coverage spike, or the level being a persistent/contrarian rather
than shock signal). **Gold (GLD)** is correct-signed but weak (+0.0664, ≈1.3·SE) and **misses the
hit-rate bar** at 49.7%. The GO rule requires both oil and gold to clear; neither truly does, and oil
is inverted. The only thesis-consistent result is **defense (ITA)**, conflict_mentions/level
+0.0930 / 53.2% — intuitive (conflict → defense stocks up) but it is **not a gate cell**, sits at
only ≈1.8·SE, and is one of two marginal "hits" expected by chance across 24 swept cells
(SE(IC)≈0.052, so |IC|≲0.10 is within ~2·SE of zero). The robustness columns (mean_tone,
mean_goldstein, delta mode) are mixed and mostly weak/wrong-signed — no coherent alternative signal.

**Conclusion:** A well-powered (n≈375) **confident null** — Middle-East GDELT conflict tone does not
carry a usable, correctly-directed predictive edge for oil or gold at a daily cadence. Building the
GDELT 2.0 real-time feeder is **not justified**. This is consistent with the project's prior confident
null on sentiment-sizing. Backlog item #2 is closed NO-GO.

**Possible future angle (not pursued now):** the defense-stock (ITA) direction and the *inverse* oil
relationship are the only non-noise leads; if ever revisited, frame as an event-study around discrete
escalation shocks (not a daily level), which this level-IC design is not built to capture.

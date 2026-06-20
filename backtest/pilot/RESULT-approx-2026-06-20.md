# MCP-approx sentiment backtest — pilot result (DIRECTIONAL ONLY)

Universe: MU, CVX, RGLD · 12 monthly windows (2025-04 → 2026-03). Sentiment = Claude tone of bigdata_search headlines (NOT RavenPack). See [README](./README.md). Faithful run is Task 9 (REST).

## Methodology + caveats (read before the tables)

- **Purpose: wiring confirmation.** This was the lean first pass (3 names, 12 monthly windows) to prove the capture→score→price→engine pipeline runs end-to-end and to measure cost — NOT to reach a go/no-go on sizing.
- **Approximation.** Claude scores the media tone of each window's `bigdata_search` headlines into [-1, 1]; this substitutes for RavenPack's numeric sentiment (the MCP path exposes none). Results test whether *a* sentiment signal predicts returns, not whether **Bigdata.com's** does.
- **Scoring deviation (this run).** No `ANTHROPIC_API_KEY` in the env, so tone was scored *inline by the operating model* (Claude Opus 4.8) rather than via `scorer_llm.score_window`'s haiku API call. Directionally equivalent; the headlines are saved under `raw/` so the API path is reproducible when a key is present. Scoring was done from headlines ONLY, before any price was fetched — no look-ahead.
- **Capture cost (measured):** 36 windowed searches, ~0.6–1.2 query units each ≈ **~31 units total** (~$0.23 at $0.0075/unit). Archive reached back to 2025-04 cleanly.
- **Sentiment dates** are month-end, snapped to the last trading day ≤ the date so they align to a real close. `standardize=True` z-scores tone within each ticker before pooling. q=4 quantile buckets.
- **Tiny-n caveat.** Held-out n≈17–18. Rank-IC standard error ≈ 1/√(n−1) ≈ 0.24, so any |IC| below ~0.5 here is statistically indistinguishable from zero — well short of the Bonferroni α/N=0.0125 the report flags. Treat every number below as directional, not significant.

## mode = level
## Bigdata.com sentiment backtest — discovery + held-out confirmation (https://bigdata.com)
> ⚠ The discovery sweep below is **IN-SAMPLE and selection-biased** (best horizon = arg-max |IC| over the swept horizons). Only the **held-out confirmation** row is out-of-sample. Treat the discovery table as exploratory; base any sizing decision on the held-out confirmation, not the discovery IC.

Mode: **level** · sentiment: **per-ticker standardized** · split: 50% discovery / 50% held-out (temporal: earlier→discovery, later→held-out)

### Discovery — in-sample horizon selection (selection-biased)
| Horizon (d) | n (discovery) | Rank IC (in-sample) |
|---|---|---|
| 1 | 18 | 0.3661 |
| 5 | 18 | 0.3909 |
| 21 | 18 | 0.0496 |
| 63 | 16 | 0.1724 |

Selected horizon (arg-max |IC| on discovery): **5d**

### Held-out confirmation — out-of-sample, at the selected horizon
| Horizon (d) | n (held-out) | Rank IC | Hit rate | Quantile fwd returns (low→high) |
|---|---|---|---|---|
| 5 | 18 | 0.2938 | 58.82% | -0.0741, 0.0276, -0.0109, 0.0086 |

> Discovery over 4 horizons; treat any single p<0.05 with a Bonferroni-adjusted threshold alpha/N=0.0125. Confirm the chosen horizon on held-out data before any sizing decision.

## mode = delta
## Bigdata.com sentiment backtest — discovery + held-out confirmation (https://bigdata.com)
> ⚠ The discovery sweep below is **IN-SAMPLE and selection-biased** (best horizon = arg-max |IC| over the swept horizons). Only the **held-out confirmation** row is out-of-sample. Treat the discovery table as exploratory; base any sizing decision on the held-out confirmation, not the discovery IC.

Mode: **delta** · sentiment: **per-ticker standardized** · split: 50% discovery / 50% held-out (temporal: earlier→discovery, later→held-out)

### Discovery — in-sample horizon selection (selection-biased)
| Horizon (d) | n (discovery) | Rank IC (in-sample) |
|---|---|---|
| 1 | 16 | 0.0236 |
| 5 | 16 | 0.2520 |
| 21 | 16 | 0.1621 |
| 63 | 15 | -0.0824 |

Selected horizon (arg-max |IC| on discovery): **5d**

### Held-out confirmation — out-of-sample, at the selected horizon
| Horizon (d) | n (held-out) | Rank IC | Hit rate | Quantile fwd returns (low→high) |
|---|---|---|---|---|
| 5 | 17 | 0.3250 | 47.06% | -0.0344, -0.0166, -0.0230, 0.0308 |

> Discovery over 4 horizons; treat any single p<0.05 with a Bonferroni-adjusted threshold alpha/N=0.0125. Confirm the chosen horizon on held-out data before any sizing decision.

## Read of this pilot (go/no-go)

- **Pipeline: CONFIRMED.** Capture → inline tone score → yfinance prices → temporal-split engine → report all ran clean for 3 names × 12 windows. The `backtest/` engine and `scorer_llm` work against live-shaped data.
- **Signal: directionally positive, statistically inconclusive.** Held-out 5d rank IC was positive in both level (+0.29) and delta (+0.33) modes, but at n≈18 that is ~1.2 SE from zero and the quantile buckets are non-monotone — no usable edge is established.
- **Confound.** All three names rose hard over the window (memory/AI supercycle, gold rally, oil spike). Low sentiment dispersion + strongly trending prices inflate spurious-IC risk; a flat/down regime is needed.
- **Recommendation.** Do **not** size off this, and do **not** fan the MCP-approx version out to 50–100 names — more approximate tone scoring buys cost without statistical power or vendor fidelity. The durable win is the de-risked pipeline. The decisive run is **Task 9** (faithful RavenPack REST sentiment, larger universe, multiple regimes), gated on the business-email key. Until then, enrichment stays read-only / never-sizing.

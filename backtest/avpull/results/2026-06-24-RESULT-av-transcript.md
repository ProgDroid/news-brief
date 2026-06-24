## Alpha Vantage sentiment backtest — discovery + held-out confirmation (https://www.alphavantage.co)
> ⚠ The discovery sweep below is **IN-SAMPLE and selection-biased** (best horizon = arg-max |IC| over the swept horizons). Only the **held-out confirmation** row is out-of-sample. Treat the discovery table as exploratory; base any sizing decision on the held-out confirmation, not the discovery IC.

Mode: **level** · sentiment: **per-ticker standardized** · split: 50% discovery / 50% held-out (temporal: earlier→discovery, later→held-out)

### Discovery — in-sample horizon selection (selection-biased)
| Horizon (d) | n (discovery) | Rank IC (in-sample) |
|---|---|---|
| 1 | 658 | 0.0531 |
| 5 | 658 | -0.0122 |
| 10 | 658 | 0.0078 |
| 21 | 658 | -0.0475 |
| 63 | 640 | 0.0126 |

Selected horizon (arg-max |IC| on discovery): **1d**

### Held-out confirmation — out-of-sample, at the selected horizon
| Horizon (d) | n (held-out) | Rank IC | Hit rate | Quantile fwd returns (low→high) |
|---|---|---|---|---|
| 1 | 659 | 0.0160 | 49.47% | -0.0027, -0.0025, -0.0034, -0.0019, -0.0037 |

> Discovery over 5 horizons; treat any single p<0.05 with a Bonferroni-adjusted threshold alpha/N=0.01. Confirm the chosen horizon on held-out data before any sizing decision.
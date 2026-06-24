## Alpha Vantage sentiment backtest — discovery + held-out confirmation (https://www.alphavantage.co)
> ⚠ The discovery sweep below is **IN-SAMPLE and selection-biased** (best horizon = arg-max |IC| over the swept horizons). Only the **held-out confirmation** row is out-of-sample. Treat the discovery table as exploratory; base any sizing decision on the held-out confirmation, not the discovery IC.

Mode: **level** · sentiment: **per-ticker standardized** · split: 50% discovery / 50% held-out (temporal: earlier→discovery, later→held-out)

### Discovery — in-sample horizon selection (selection-biased)
| Horizon (d) | n (discovery) | Rank IC (in-sample) |
|---|---|---|
| 1 | 7137 | -0.0061 |
| 5 | 7118 | -0.0221 |
| 10 | 7100 | -0.0265 |
| 21 | 7063 | -0.0281 |
| 63 | 6896 | -0.0176 |

Selected horizon (arg-max |IC| on discovery): **21d**

### Held-out confirmation — out-of-sample, at the selected horizon
| Horizon (d) | n (held-out) | Rank IC | Hit rate | Quantile fwd returns (low→high) |
|---|---|---|---|---|
| 21 | 7063 | -0.0082 | 51.66% | 0.0220, 0.0234, 0.0236, 0.0277, 0.0167 |

> Discovery over 5 horizons; treat any single p<0.05 with a Bonferroni-adjusted threshold alpha/N=0.01. Confirm the chosen horizon on held-out data before any sizing decision.
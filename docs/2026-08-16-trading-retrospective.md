# Trading retrospective — 2026-08-16

First retrospective on the trading half of news-brief. Sample: `book.json` pulled
from the deploy host, **245 positions, 2026-06-01 → 2026-08-13** (55 distinct entry
days). Analysis was throwaway (scratchpad, not committed); this file is the record.

The brief itself is not in scope here — it is working well as an information
product. This is only about the paper/live trading sleeves.

## Headline

**The performance layer was reporting fiction.** Three data-integrity bugs
corrupted `performance_report`, `evaluate_gate` *and* `performance_prompt_block`
— the last of which feeds the model its own track record. Every conclusion drawn
from those numbers before this date should be treated as unreliable.

Once cleaned, the honest result is: **the equity signals have no positive
directional edge, and the point estimate is negative in both directions.**

## The bugs

### B1 — GBX/GBP 100× on four UK positions

Entry price captured in pence, marks in pounds. Four rows, all opened
2026-06-01 → 06-04:

Booked vs. repaired `realized_return` (the repair recomputes from the stored
prices, which were always correct):

| Position | Booked | Actual |
| --- | --- | --- |
| `2026-06-02:RRl_EQ:bullish` | **−98.86%** | **+14.29%** |
| `2026-06-01:SGLNl_EQ:bullish` | −99.06% | −5.54% |
| `2026-06-04:SPOLl_EQ:bullish` | −99.01% | −1.08% |
| `2026-06-02:DXJGl_EQ:bearish` | +98.98% | −2.50% |

Rolls-Royce was the best equity call of the whole run and the book recorded it as
a near-total loss.

**The bug itself is already dead.** Every `.uk` price from 2026-06-15 onward is
sane — the fix arrived with the Stooq→Yahoo cutover (Stooq quoted LSE in pence,
Yahoo in the instrument's own denomination). Only the corrupt rows remain.

### B2 — one corrupt benchmark fetch

`2026-06-26:equity:EXV1:bullish` carries `benchmark_entry = 733.33`. Every other
equity row is ~7350–7760. Same 10× family. It produces `edge = −902.83%` on a
position whose net return was −1.08%, and **on its own drags mean edge from
−1.6% to −9.3%.**

The row is recoverable rather than a write-off: the close-time index level is
`entry × (1 + stored return)` = **7354.02**, which is *exactly* the
`benchmark_entry` independently recorded on the 2026-06-28 EXV1 row. Corrected
entry 7333.30, corrected edge +0.28%.

### B3 — haircut applied on top of a total loss

`_stamp_close_metrics` computes `net = gross − haircut`. When a prediction settles
worthless, `gross = −1.0` and net goes below −100%. **11 of 56 closed predictions**
are affected, down to **−116.29%**. You cannot lose more than the stake on a
long-only instrument.

### NOT a bug — wide PolyGram spreads

An `entry_spread` of `0.14285714285714288` initially looked like a bad orderbook
read. It is exactly `(0.40 − 0.30) / 2 / 0.35` — a real 10¢-wide book on an
illiquid market. `_fetch_pg_half_spread` is correct. It only causes visible damage
through B3.

## Status of the fixes (2026-08-16)

| ID | Fix | Where |
| --- | --- | --- |
| B1 | `_mark_is_plausible` — a mark >10× off the entry is treated as an unusable price; the row stays open and retries | `trading.py` `mark_to_market` |
| B2 | `BENCHMARK_SANITY_RETURN` — an implausible benchmark move leaves `benchmark_return`/`edge` unset and logs, rather than stamping fiction | `trading.py` `_stamp_close_metrics` |
| B3 | `net_return` floored at −100% for long-only (prediction); equity/crypto keep no floor because a bearish row is a short | `trading.py` `_stamp_close_metrics` |
| Legacy rows | Targeted, idempotent, dry-run-by-default repair of the 5 known rows | `scripts/repair_unit_bug_rows.py` |

Effect of the repair on the real book, no rows excluded:

| Metric | Before | After |
| --- | --- | --- |
| mean net | −2.81% | −1.25% |
| mean gross | −2.65% | −1.10% |
| **mean edge** | **−9.34%** | **−1.56%** |

**Still to do:** run the repair on the host book. The snapshot analysed here is
from 2026-08-14 and the host has moved on since, so the script must be run against
the live file — never restore this snapshot over it.

## Performance, cleaned

Corrupt rows excluded throughout (figures computed before the repair existed;
the repaired book gives materially the same picture, with the four recovered rows
now contributing).

### Equity / crypto — n=126 closed

| Cut | n | Hit | Mean net | Median net |
| --- | --- | --- | --- | --- |
| All | 126 | 39.7% | −1.33% | −0.59% |
| Bullish | 61 | 41.0% | −2.00% | −0.55% |
| Bearish | 65 | 38.5% | −0.69% | −0.62% |
| Confidence = high | 33 | 36.4% | −1.33% | −0.62% |
| Confidence = medium | 93 | 40.9% | −1.32% | −0.55% |

Gross of costs it is −1.17% / 40.2% hit, so **this is not a cost story — the
direction itself is wrong.** Note that stated confidence carries no information:
`high` performs no better than `medium`.

### Prediction — n=56 closed

Hit 64.3%, median +1.57%, mean +5.84% — but **trimmed mean −10.02%**. The entire
positive mean rests on a single +996% winner.

| Cut | n | Hit | Mean |
| --- | --- | --- | --- |
| `play_type=resolution` | 29 | 86.2% | −3.71% |
| `play_type=momentum` | 27 | 40.7% | +16.11% |
| `close_reason=horizon` | 6 | **0.0%** | **−95.59%** |

Momentum plays held to the 4-week horizon are a total loss, every time, in this
sample.

### Leakage — 560 signals over 44 days

`no_ticker` 29.6% · `neutral` 25.7% · `low_confidence` 23.8% · **`traded` 17.5%** ·
`no_instrument` 2.0% · `no_price` 1.2%.

Only about one signal in six reaches the book.

## The fade hypothesis

Inverting every directional call would have returned **55.1% hit, mean +1.17%**
gross. A sign test on 51/127 winners gives **p = 0.033** two-sided.

**That p-value should not be trusted**, for four reasons:

1. **The observations are not independent.** 104 of 127 closes are `reversal` —
   the same few tickers (MU, ESLT, DXJG, SGLN, FLRK) chaining back and forth as
   each new opposing signal closes the old position and opens its mirror. The
   binomial test assumes independence; the effective sample is well under 127.
2. **Multiple comparisons.** Several cuts were examined before this one; no
   correction applied.
3. **One regime, ten weeks.**
4. **The decay curve is survivorship-biased inside the book.** A 4w checkpoint only
   exists for positions that were *not* reversed early, and reversal is triggered
   by news, not by a rule independent of price. Paired curve (n=21): 1w −1.50%,
   2w −0.65%, 4w −2.78%.

**Supported conclusion:** there is no positive directional edge.
**Not yet supported:** that a systematically inverted edge exists and is tradeable.

## Step 2 — the fade hypothesis is wrong, and the real cause is the reversal rule

Re-tested on ticker-**episodes** (a maximal chain of positions on one instrument
where each close is immediately followed by the next entry), each episode's return
being the compound of its legs. Grouped by *instrument*, not ticker string, since
the book carries the same instrument under several spellings (`RRl_EQ`/`RRl`).
130 closed legs collapse to **49 episodes**.

### The direction carries no information

Single-leg episodes are the clean test — no churn, no compounding, one thesis held
to its exit:

| | n | Hit | Mean | Sign test |
| --- | --- | --- | --- | --- |
| Single-leg, as traded | 28 | **50.0%** | −0.66% | **p = 1.000** |
| Single-leg, inverted | 28 | 50.0% | +0.66% | p = 1.000 |

Exactly 50/50. **There is no edge to invert.** The earlier leg-level p=0.033 was
the correlation artifact it was flagged as.

### The reversal machinery is what loses the money

| | n | Hit | Mean | Sign test |
| --- | --- | --- | --- | --- |
| Multi-leg chains, as traded | 21 | 14.3% | **−6.02%** | p = 0.0015 |
| Chains, gross of costs | 21 | 19.0% | −5.56% | p = 0.0072 |

Cost drag is only 0.46%, so this is **whipsaw, not transaction costs**.

The decisive test holds the name, the window and the opening call fixed, and varies
only the machinery — hold the first thesis and ignore every later signal:

| | n | Hit | Mean |
| --- | --- | --- | --- |
| Reversal machinery | 21 | 14.3% | −6.02% |
| **Hold first thesis** | 21 | **57.1%** | **−0.28%** |

**Holding beat the machinery in 17 of 21 chains, mean +5.74%, p = 0.0072.** Robust:

| Subset | n | Holding won | Mean gain | p |
| --- | --- | --- | --- | --- |
| All chains | 21 | 17/21 | +5.74% | 0.0072 |
| Excl. the 2 worst | 19 | 15/19 | +3.85% | 0.0192 |
| Excl. all chains ≥10 legs | 17 | 13/17 | +3.85% | 0.0490 |
| Chains of 2–4 legs only | 15 | 12/15 | +4.75% | 0.0352 |

`corr(chain length, damage) = +0.399` — the longer the chain, the worse the damage,
which is what the mechanism predicts and what a "volatile names" explanation does
not. Because the comparison is *within* each chain, name-selection cannot produce
it.

### Conclusion

**The signals are directionally uninformative (harmless), and the reversal rule
converts that zero into a real −6% per chain.** Reversal closes are 105 of 130,
median hold **5 days**, and 59% of all closes are reversals inside 7 days — the
book is thrashing on news flow. MU alone ran 18 legs in under two months.

Note that "hold the first thesis" is a *counterfactual to isolate the mechanism*,
not a deployable strategy: it has no exit discipline and is itself flat, not
profitable.

## Step 3 — policy replay, and what shipped

Replayed the real signal stream per instrument against **real daily closes**,
varying only the reversal policy. 21 instruments with usable series (`nvda.us`,
`asml.us` dropped for thin history — excluded from *every* policy, so the
comparison stays fair).

| Policy | legs | mean | median | win% |
| --- | --- | --- | --- | --- |
| **as traded** | 126 | **−6.06%** | −7.77% | 19.0% |
| no reversal (hold to 4w) | 43 | −0.60% | +0.92% | 52.4% |
| min hold 7d | 74 | −3.34% | −2.55% | 33.3% |
| min hold 14d | 58 | +0.43% | −1.18% | 42.9% |
| min hold 21d | 45 | −1.20% | −0.21% | 47.6% |
| cooldown 7d | 56 | −1.82% | −1.23% | 38.1% |
| cooldown 14d | 41 | −1.34% | −1.39% | 42.9% |

Paired against as-traded, **ties dropped** (instruments with a single signal behave
identically under every policy; counting them as failures deflates the test):

| Policy | n | ties | better | mean gain | p |
| --- | --- | --- | --- | --- | --- |
| no reversal | 18 | 3 | 12/18 | +6.36% | 0.238 |
| min hold 7d | 15 | 6 | 11/15 | +3.81% | 0.119 |
| min hold 14d | 16 | 5 | 10/16 | +8.52% | 0.455 |
| min hold 21d | 17 | 4 | 10/17 | +6.00% | 0.629 |
| cooldown 7d | 19 | 2 | 14/19 | +4.69% | 0.064 |
| cooldown 14d | 19 | 2 | 16/19 | +5.22% | **0.0044** |

**Established:** every policy improves on the status quo, all six with a positive
mean gain, corroborated by step 2's independent paired test (p=0.0072).
**Not established:** which policy is best — at n=21 they are indistinguishable, and
selecting the largest point estimate would be fitting noise.

**Shipped: no reversal.** Chosen over cooldown-14 despite the weaker p-value —
it is a branch deletion rather than new per-instrument state, it is closest to the
counterfactual step 2 actually tested, and with no edge to protect, buying
statistical consistency with added complexity is a poor trade. A contrary signal is
now declined and counted under a new `contrary_held` leakage key. Legs drop 126 → 43,
which is the real win: 43 clean observations are a far better instrument for
detecting a future edge than 126 legs of thrash.

**No policy here creates an edge.** The best of them takes the sleeve from −6% to
roughly zero. This stops self-inflicted damage; it does not make money.

### The momentum-prediction cap was investigated and REJECTED

The plan was to cap momentum prediction plays before the 4w horizon, since all six
held to horizon were total losses. **The data does not support it** — the losers
were already at −100% by their 1w checkpoint:

| Simulated exit | hit | mean | trimmed |
| --- | --- | --- | --- |
| no stop (current) | 40.7% | +16.11% | −17.82% |
| stop at −20% | 37.0% | +10.85% | −23.50% |
| stop at −30% | 40.7% | +17.28% | −16.56% |
| close all at 1w | 44.4% | −16.53% | — |
| close all at 2w | 28.6% | −48.81% | — |

Every intervention is neutral or worse. A −20% stop is actively harmful because two
of the largest winners were double-digit *down* at 1w (−22% → +151.8%, −11% → +24.0%)
and a stop would have killed exactly those. Closing earlier is worse still.

The real signal is **entry price**, not exit timing:

| Entry price | n | hit | mean | median |
| --- | --- | --- | --- | --- |
| <0.10 longshot | 6 | 16.7% | +76.11% | **−105.33%** |
| 0.35–0.65 | 13 | 46.2% | −10.04% | −4.77% |
| 0.65–0.90 | 17 | 64.7% | +0.04% | +11.86% |
| ≥0.90 favourite | 18 | 94.4% | +2.52% | +1.93% |

Five of six sub-0.10 longshots are total losses; the sixth is the +996.7% that
carries the entire sleeve's headline mean. **At n=6 this cannot be resolved** — the
bucket's mean is +76% and its median is −105%. Not a basis for a rule in either
direction. Recorded, not acted on.

## Open questions for the next pass

- **Does the prediction haircut double-count?** `realized_return` is computed off
  the executed price, which already embeds the spread. Subtracting the spread
  again may be wrong for predictions generally, not just at the −100% floor. B3's
  clamp fixes the impossible values without settling this.
- **Test fade on a de-correlated sample** — one observation per ticker-episode
  rather than per reversal leg.
- **Pull pre-entry price action** to separate "we bought after the pop and it
  reverted" from "there was never a move to catch". Entry happens at signal-day
  price, so the burst is already partly inside the cost basis and the book alone
  cannot answer this.
- **Momentum predictions held to horizon** went 0-for-6 at −95.6%. Small, but the
  mechanism (a momentum thesis left to rot for four weeks) is worth a rule, not
  just a note.
- **Stated confidence is uninformative.** `high` and `medium` are indistinguishable.

## Related

External comparison that prompted this: BTFDBot (btfdbot.com), a price-based
mean-reversion swing scanner. Its best-documented strategy ("Large Gaps Down") is
the fade hypothesis expressed as a price rule — so a parallel BTFD sleeve would
likely buy correlation rather than coverage. What news-brief has that a price
scanner does not is *the reason for the move*, which is exactly the blind spot
behind that site's stated failure mode ("stocks falling out of bubbles").

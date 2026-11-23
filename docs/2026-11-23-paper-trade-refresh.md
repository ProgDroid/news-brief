# Paper-trade refresh — 2026-11-23 (news-brief-d7w)

Rethink charter §8 sub-project 0. It repeats `docs/2026-08-16-trading-retrospective.md`'s cut on
positions opened **after** the 2026-08-16 fixes. **Verdict: NOT USEFUL.** Under the operator
decision of 2026-11-20, the signals extraction call and the paper sim are removed
(`news-brief-cg3`). The brief's 📌 Position Signals section stays.

## Pre-registration

Recorded on bead `news-brief-d7w` before the book was opened. Two amendments were also made
before opening it.

- **Sample:** equity, crypto and index positions opened 2026-08-17 to 2026-10-26, closed at the
  4-week horizon. Prediction positions were retired and are excluded.
- **Measure:** market-adjusted 4-week return = sign × (r_instrument − r_benchmark).
  - The benchmark is the S&P 500 (^GSPC) for equity and BTC for crypto, recomputed over the
    same two dates.
  - Index positions use r_benchmark = 0, since the book has no index benchmark.
- **Both directions count.** The operator corrected the long-only premise: some shorts are
  available in the ISA, including leveraged inverse ETPs.
- **USEFUL only if all of these hold:**
  - n ≥ 30
  - hit ≥ 55% (the code's own `GATE_MIN_HIT_RATE`)
  - one-sided sign test p < 0.10, ties dropped
  - mean > 0
  - mean > 0 after dropping the largest entry week
  - amendment 2, for correlated positions (trap 1 of the stats-traps notes):
    - with each instrument counted once: hit ≥ 55% and p < 0.10
    - mean > 0 in at least 60% of entry weeks
- **Expected before looking:** a hit rate near 50%, a NOT USEFUL verdict, and n of roughly 50
  to 150.

## Why not the book's `edge` field

`trading.py` stamps `edge = net_return − benchmark_return` for **both** directions. For a bearish
call that subtracts the market move it should have added, so the market moves the result twice.
That field feeds the go-live gate and `performance_prompt_block`. The removal makes this moot, so
no separate bead was filed.

## Controls

- **Synthetic books with known answers:** 30 of 40 gives 75% and p ≈ 0.001; seven of seven
  instruments gives p = 1/128. A flat-priced equity row scores −(S&P move) when bullish and
  +(S&P move) when bearish.
- **The recomputed S&P start level** agrees with each real equity row's stored
  `benchmark_entry` within 1% on 39 of 39 rows.
- **Unit-bug check:** no row has a raw 4-week move above 80%.
- **Checkpoint dates:** 3 of 41 4-week checkpoints are `price_basis=current` (priced on a later
  run date). The benchmark is taken over the same dates, so the adjustment stays consistent.

## Result

| Check | Value | Pass |
| --- | --- | --- |
| n | 41 | ✅ |
| Hit | 51.2% (21/41) | ❌ |
| Sign test, one-sided | p = 0.50 | ❌ |
| Mean / median | +1.12% / +0.11% | ✅ |
| Mean without the largest week (12 positions) | +3.05% | ✅ |
| Instrument-collapsed hit (31 instruments) | 54.8%, p = 0.36 | ❌ ❌ |
| Weeks with mean > 0 | 4 of 9 | ❌ |

**Reported beside the verdict; these do not rule it:**

| Cut | n | Hit | Mean |
| --- | --- | --- | --- |
| Bullish | 19 | 47.4% | +1.94% |
| Bearish | 22 | 54.5% | +0.42% |
| Confidence high | 8 | 62.5% | +4.61% |
| Confidence medium | 33 | 48.5% | +0.28% |
| `thesis_ref` set | 22 | 40.9% | −0.86% |
| `thesis_ref` empty | 19 | 63.2% | +3.43% |
| 1-week horizon | 41 | 53.7% | +0.18% |
| 2-week horizon | 41 | 58.5% | +0.66% |

The same picture as 08-16: a hit rate at a coin flip, and a small positive mean carried by a
few large winners (the median is +0.11%). No cut is significant. These cuts were not
pre-registered, so none of them reopens the verdict:
- the 2-week horizon's 58.5% (p = 0.17)
- the 62.5% of 8 high-confidence calls
- the `thesis_ref` split

## Deviation from the expected numbers

I expected n of 50 to 150 and got 41. The filter was checked: every row has an `entry_date`, and
the window captures every position opened 08-17 to 10-26. The estimate was wrong, not the
system. It extrapolated the pre-fix opening rate (about 57 a month in June and July, inflated by
reversal churn). After 08-16 the book opened 17 to 25 a month, which is what declining contrary
signals was expected to do.

## Data

The book was copied off the host to `from-server/2026-11-23/book.json` on the dev machine. The
script and its full output are beside it. That folder is gitignored and holds real positions.

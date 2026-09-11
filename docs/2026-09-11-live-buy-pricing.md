# What a live buy actually costs: the venue rule the book never saw

2026-09-11. Beads: `news-brief-rhg` (closed 2026-09-11; repairs applied, then the whole feature retired — see 2026-09-11-prediction-trading-retired.md),
`news-brief-p3v` (measured — the question it asked is answered), `news-brief-9tq` (measured — six payouts; repair below).

## The measurement

Sources, in order of authority: the operator's reading of polygram.ink's **wallet history**
(the artifact), `/trade/history` via `pgdiag` (5 orders: 2 buys, 3 sells), and the
2026-08-29 book copy in `from-server/` (7 more buys, venue fields as recorded at open).

**Wallet.** Every $2 buy appears as **two debits: $2.00 and $0.06, separately.** Every
buy line reads exactly 2.00 — none reads 1.87 or 1.91. So `amount` is dollars, the fee
is charged on top, and the cost of a $2 position is **$2.06**.

**Shares.** On all nine $2 buys ever placed:

| market | fill | shares | 2/shares | (shares − 2) × fill |
|---|---|---|---|---|
| 3324624 | 0.943 | 2.021737 | 0.98925 | 0.020498 |
| 2910437 | 0.89175 | 2.022987 | 0.98864 | 0.020499 |
| 2774057 | 0.8815 | 2.023250 | 0.98851 | 0.020495 |
| 3348047 | 0.779 | 2.026312 | 0.98701 | 0.020497 |
| 3399428 | 0.9225 | 2.022220 | 0.98901 | 0.020498 |
| 2910438 | 0.8405 | 2.024389 | 0.98795 | 0.020499 |
| 3491476 | 0.80975 | 2.025315 | 0.98750 | 0.020499 |
| 3501950 | 0.9225 | 2.022221 | 0.98901 | 0.020499 |
| 2243896 | 0.943 | 2.021737 | 0.98925 | 0.020498 |

`shares = 2 + 0.0205 / fillPrice`, to share-rounding, nine of nine. **A dollar buys
~1.011 shares whatever the displayed price.** The price paid per share is 0.987–0.989
across a displayed range of 0.78–0.94. The venue's take per $2 is `2(1 − fill) − 0.0205`:
$0.11 at 0.92, **$0.42 at 0.78** — it grows as the displayed price falls, and it is
per-dollar, so stake size does not dilute it.

**Sells are honest.** `amount == shares × fillPrice` to the last digit on all three, and
`proceeds = amount − totalFee`. Control: the sell response for 3501950 reported
`balance 49.593235`; adding 2243896's net proceeds gives 51.3173; `pgdiag` read the
wallet at **$51.32**. The chain closes.

**What was rejected on the way.** The bead's notes had withdrawn "amount is not dollars"
on the argument that a share cannot cost more than its $1 payout. It can: all-in cost
was `2.06 / 2.022 = $1.019` per share. The competing reading — the venue treats
`amount` as a share count, debit `2 × fill + 0.0205` — fit the share table equally well
and was killed only by the wallet: the debits do not vary per trade. My first question to
the operator offered only "2.00" and "2.06", which presupposed the answer; the
discriminating question was *same on every buy, or different?*

## What it means for the book

| field | was | is | evidence |
|---|---|---|---|
| `cost_basis` | `amount` | `amount + total_fee` | two wallet debits |
| `entry_price` | venue `fillPrice` | `amount / shares` | share table |
| `fill_price` (new) | — | venue `fillPrice` | kept: the gap is the venue's take |
| `above_band` (new, Sleeve A) | — | `entry_price > PG_A_BAND_HI` | post-fill verdict, logged at error, rendered in the daily message |
| `realized_return` | `proceeds / 2.00 − 1` | `proceeds / 2.06 − 1` | wallet chain |

The three completed round trips, corrected:

| market | opened | held | old return | true return |
|---|---|---|---|---|
| 2774057 | 2026-08-11 | 30 d | −5.85% | **−8.59%** |
| 3501950 | 2026-09-01 | 9 d | −9.83% | **−12.46%** |
| 2243896 | 2026-09-01 | 9 d | −13.80% | **−16.31%** |

The Sleeve A stop (`entry_price − held ≥ PG_A_STOP`) now measures from the paid price.
With a 0.943 fill it fires at held ≤ 0.839 rather than ≤ 0.793. Decided 2026-09-11: a
stop measures the adverse move from what was paid.

## What it means for Sleeve A (`p3v`, answered)

`p3v` asked whether stake size or the band was the lever, on two observations. On three,
and with the venue rule: **neither.** The take-profit at 0.97 sits *below* the 0.989 paid
on every fill; the fixed $0.11 round-trip fee is 5.5% at $2 and would be 0.55% at $20, but
the venue's take is proportional and grows as the entry price falls, which is the
direction a fade wants to buy. At this venue, under these mechanics, a Sleeve A trade
cannot end positive by any path. The decision of what to do with the sleeve is the
operator's; the measurement is complete.

The one thing not measured: whether the buy rule is the same at a larger `amount`. Nine
of nine were $2.

## Repair runbook (host, after deploy)

```sh
# dry run first — prints every row's change, writes nothing
docker compose run --rm newsbrief-monitor \
  python scripts/repair_live_cost_basis.py /app/logs/paper/book.json

docker compose run --rm newsbrief-monitor \
  python scripts/repair_live_cost_basis.py /app/logs/paper/book.json --apply
```

Pre-registered: **9 rows re-based, 3 returns recomputed** to the figures in the table
above (2774057 only if `backfill_settled` has already stamped it; otherwise backfill
will, against the corrected cost). Anything else — a tenth live row, a stake that is not
$2, a recorded proceeds figure that disagrees with the venue's history — is a **refusal**
(exit 2), and a second run refuses because the rows now carry `fill_price`.

## The six resolved rows (`9tq`, measured)

The remaining legacy rows — 3324624, 2910437, 3348047, 3399428, 2910438, 3491476 —
resolved at the venue. `/trade/history` holds **5 orders in total** and a resolution is not
an order, so no field-name fix could ever have backfilled them. The operator read the
wallet history on 2026-09-11: **six payout credits of ~$2.02, one per market.** Every one
of them won.

Every one of them lost money. `shares × $1.00` (2.022–2.026) against the $2.06 debited is
**−1.6% to −1.9% on a winning trade** — the buy rule above, measured at resolution.

```sh
# after repair_live_cost_basis.py has been applied; refuses otherwise
docker compose run --rm newsbrief-monitor \
  python scripts/repair_settled_payouts.py /app/logs/paper/book.json
docker compose run --rm newsbrief-monitor \
  python scripts/repair_settled_payouts.py /app/logs/paper/book.json --apply
```

Pre-registered: **6 row(s) valued**, each `realized_return ≈ −0.018`, `last_mark.price 1.0`,
`proceeds = shares`. The complete live track record is then nine trades, nine losses:
six wins at −1.8%, three sales at −8.6 / −12.5 / −16.3%.

## Timestamps

`/trade/history` `createdAt` runs **one hour behind** the times in the container log for
the same event (3501950 sold: history 16:57:00, log 17:57:00). One of them is not UTC.
Anything joining the two by time must allow for it; nothing does yet.

# The live close the venue completed and the book threw away

2026-09-10. Beads: `news-brief-sb0` (fixed), `news-brief-8lb` / `news-brief-qiz` (open),
`news-brief-grc` (unrelated, fixed in the same pass).

## What happened

A `/close` on `2026-09-01:prediction:3501950:NO:live` at 17:57:00 UTC **sold, for real
money, at the venue**. `POST /trade/sell` answered:

```
{'success': True, 'sold': 2.022221, 'price': 0.9165,
 'proceeds': 1.8033655465, 'remainingShares': 0, 'balance': 49.593235}
```

`sell_position` logged that payload under `PolyGram sell not completed` and returned
`None`. It was looking for a nested object:

```python
sale = (data or {}).get("sale")
if not isinstance(sale, dict) or sale.get("status") != "completed":
```

with fields `sharesSold` / `salePrice` / `profit` / `fee` / `status`. **The venue sends no
`sale` key and not one of those five names.** The shape came from the published docs page
and had never been observed, because until 2026-09-10 no live close had ever got far
enough to produce a response at all (`news-brief-8fy`, the `position_key` lookup, was
fixed hours earlier).

The cascade:

| time | event |
|---|---|
| 17:57:00 | `Live close: selling ... shares=2.022221` |
| 17:57:01 | venue sells; parse returns `None`; `close_live_position` returns `False`; **row left open** |
| 17:57:20 | operator retries; position is gone from the venue (it was sold) → `not on venue; leaving to reconcile` |
| 18:00:24 | `LIVE RECONCILE settled ... (gone from venue)` → `close_reason="settled"`, `realized_return=None` |
| 18:00:25 | `Live sweep: 0 exited, 1 reconciled, 0 backfilled` |

Net effect: the first completed live exit in this book's history is recorded as a
settlement of unknown value. The sale price, the proceeds and the real exit reason
(`manual`) were all lost, and the retry at 17:57:20 would have **sold a second time** had
any shares remained.

## Root cause, stated generally

**Response shapes are per-endpoint. A sibling endpoint's shape is not evidence.**

On this one API:

- `/trade/place` requires `side`; `/trade/sell` does not.
- `/trade/positions` carries no id at all; the docs promise `positionId`.
- `/trade/place` nests its result under `order`; `/trade/sell` is flat.

Three asymmetries, each of which invited a symmetric "fix", each of which cost a separate
incident. The docs have now lost to the running API three times on this endpoint family.

**The tests were confirming the fiction.** `tests/test_polygram_live.py` hard-coded the
nested payload in two fixtures. A suite cannot falsify the assumption it was written from.
Every external-shape fixture in that file now carries the date it was measured
(`MEASURED_SELL_2026_09_10`) — that date is the only thing separating an observation from a
guess once both are sitting in the same file.

## What changed

`sell_position` parses the measured flat payload. `success` is the only completion signal
the venue offers, so it is what gets checked. `profit` and `fee` are **absent from the
return rather than synthesised** — inventing a field the venue does not send is the habit
that caused this; the one caller needs neither, since `realized_return` is computed from
proceeds against our own `cost_basis`.

Three outcomes are now distinguished where there was one:

| condition | level | meaning |
|---|---|---|
| `data is None` | warning | request never landed; no money moved |
| `success: false` | warning | explicit refusal, taken at its word |
| 2xx we cannot read | **error** | capital may have moved with no book row, and a retry would sell again |

A partial fill (`remainingShares > 0`) is returned — the money moved — and logged, because
the caller is about to stamp a close on a position the venue still holds.

Mutation-audited: reverting the completion predicate fails exactly 4 tests.

## Operator runbook: repairing the lost row

The parse bug is dead, but the row it left behind still reads as a settlement of unknown
value. Repair it on the deploy host **after** this change is deployed:

```sh
# dry run first — prints the change, writes nothing
docker compose run --rm newsbrief-monitor \
  python scripts/repair_lost_live_close.py /app/logs/paper/book.json

docker compose run --rm newsbrief-monitor \
  python scripts/repair_lost_live_close.py /app/logs/paper/book.json --apply
```

It is targeted by position id, never by a detector, and asserts every pre-registered fact
about the row before writing — `execution`, `status`, `close_reason`, `instrument`, a
positive `cost_basis`, an unset `realized_return`, and that the book's share count matches
the 2.022221 the venue actually sold. Any mismatch is a **refusal** (exit 2), not an edit
of unknown size. Running it twice refuses the second time. It keeps a timestamped backup.

## Still open

`backfill_settled` — the fallback that should have rescued this row — keys `/trade/history`
on a raw `(marketId, outcome)` tuple, bypassing the `venue_key()` normaliser every other
join uses. And `/trade/history`'s shape has never been observed either:
`data["history"] or data["trades"]`, and a `proceeds` field, are all guessed from the same
docs page that has now been wrong three times.

**Do not fix those field names blind.** `pgdiag` now probes `/trade/history` read-only and
**enumerates every key** of the first record rather than matching by name — a probe that
selects fields by name pattern presupposes what the answer is called, which is exactly how
`position_key` stayed invisible for a year. Run it, then fix the join against what it
prints:

```sh
docker compose run --rm newsbrief-monitor pgdiag
```

`news-brief-uvo` (the deliberate $2 round trip) is effectively answered by this incident: a
real close ran end to end at the venue. What had never been proven was the bookkeeping,
which is precisely what broke.

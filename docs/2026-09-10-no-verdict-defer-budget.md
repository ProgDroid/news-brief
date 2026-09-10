# A budget for failures that judged nothing (news-brief-h8p)

**Date:** 2026-09-10
**Status:** implemented, not yet deployed
**Touches:** `comprehend.py`, `migrations/0013_integrate_defers_{up,down}.sql`,
`common.KNOBS`, `docker-compose.yml`

## What was measured

The 2026-09-10 host logs, 00:00–08:00 (nine hourly comprehend passes):

| Pass  | `batch:ValueError` items | batches lost |
|-------|--------------------------|--------------|
| 00:00 | 0                        | 0 |
| 01:00 | 0                        | 0 |
| 02:00 | 10                       | 2 |
| 03:00 | 10                       | 2 |
| 04:00 | 5                        | 1 |
| 05:00 | 10                       | 2 |
| 06:00 | 0                        | 0 (15 items deferred on DNS instead) |
| 07:00 | 10                       | 2 |
| 08:00 | 0                        | 0 |

**45 items charged an `integrate_attempts` strike in five of nine passes**, every
one of them for the same fault:

```
ValueError: emit_extraction input missing 'items' list;
            input keys=['items'] items type=dict
```

`integrate_attempts >= 3` retires an item **permanently**, and the integration
SELECT is `ORDER BY i.id`, so the loss is not spread at random — it lands on the
oldest corpus first.

The denominator, ~121 integration calls in that window, is **hand-counted from
the log** and should be treated as approximate. The argument below is built so
it does not depend on that number being right.

## The rule

> Only a path that actually **judged** an item may spend that item's lifetime
> budget.

`_is_transient` already encoded this for network faults (news-brief-bqa.13): a
DNS outage says nothing about the news, so it defers instead of charging. A
response the parser cannot read is the same kind of event one layer up — the
model answered, and the answer was unreadable. Nothing in the batch was judged.

## Why the deferral is budgeted rather than free

The pre-existing test `test_a_response_the_parser_rejects_still_charges_the_item`
was not an oversight. Its docstring named the hole it was plugging:

> Without it a classifier answering 'transient' to everything passes both tests
> above, and a permanently-malformed batch re-pays an 8192-token generation
> every hour forever because nothing ever retires it.

That bound is real and has been **replaced, not removed**. A batch the model
mangles *because of what an item contains* will fail identically every pass, and
an unbudgeted defer would re-pay its generation hourly with nothing able to stop
it.

So: a no-verdict batch failure spends `item_triage.integrate_defers`. When that
counter reaches `COMPREHEND_MAX_DEFERS`, the failure converts back into an
`integrate_attempts` charge and the three-strike door closes as before.

Two columns rather than one, because they answer different questions —
*"how often did we fail to get an answer"* is a fact about the extractor,
*"how many times was this item judged and found wanting"* is a fact about the
item.

## Why the ceiling is 10

Ten consecutive misses for one item, at the measured rate, is about `5e-12`.
The point does not rest on the exact rate: even if the true per-batch failure
rate were **triple** the measured one, ten in a row is still ~`1e-6`. Any item
that reaches the ceiling is therefore **not** unlucky — its own content is
provoking the failure, which is precisely the case a ceiling should retire.

The cost of being wrong is bounded and small: at most ten wasted generations
(~600 output tokens each) before the normal three strikes resume.

It is a settings row, not a constant, so the host can retune it once the shape
faults are fixed and the rate moves.

## Why transport deliberately stays uncapped

Transport defers were left alone. A DNS outage is self-clearing, and putting it
on the same ceiling would let one long outage walk every item in the corpus up
to the cap — after which the next unrelated shape fault charges immediately.
That reintroduces news-brief-bqa.13 through the back door. `test_a_transport_
failure_does_NOT_spend_the_defer_budget` pins the two paths apart.

## The instrument that was missing (news-brief-19i)

The dict's shape was never logged, so **no amount of grepping the host could
recover it** — `items type=dict` is equally consistent with items keyed by
index, a single item emitted bare, and the array nested one level deeper. Those
are three different recoveries.

`_shape_of` now names keys and value types (never content, which would put
unbounded article text in the log). 19i should be built from the first real
`items shape=` line this produces, not from a guess.

## After deploy — what to watch

1. `deferred_response` in the pass tally becomes non-zero where
   `failed_integration` used to be. If `failed_integration` does not fall
   correspondingly, the charge did not move.
2. `defer_cap_hit` should stay at **0**. Anything else means an item is
   deterministically breaking the response, and its `items shape=` line is the
   thing to read.
3. `gave_up_integration` should stop growing from this cause.
4. Collect one `items shape=` line, then close 19i against it.

## Not addressed here

- `scripts/inspect_integration.py` reimplements run()'s integration SELECT and
  claims in its docstring to use "the SAME select". This change does not touch
  that SELECT, so no drift was introduced — but the reconstruction hazard is
  live and unticketed.
- Retired items are never un-retired. The 45 strikes already charged on the host
  stand; nothing here reverses them.

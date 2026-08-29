# Gold set: first run against the live reconcile prompt

**2026-08-29.** `news-brief-jx9.7`. Built the break-detection gold set and its scorer,
then ran it. The result changes what `news-brief-93u` should do.

## What exists now

- `tests/fixtures/gold_set_breaks.json` — 23 hand-labelled items seeded from the
  2026-08-29 replay, committed. Every row carries a `rationale` so a label can be
  argued with in a diff.
- `scripts/score_gold_set.py` — manual scorer. One Haiku call per item against
  `brief_memory._RECONCILE_TEMPLATE` as it currently stands. CI has no
  `ANTHROPIC_API_KEY`, so it never runs there.
- `tests/test_gold_set.py` — 26 offline tests covering the fixture's schema and the
  scorer's arithmetic, so CI still catches a harness that would have miscounted.

## Two findings that arrived before the first call

**The restatement guard was already shipped.** Fix #4 of spec §12.3 went in as part of
`jx9.8` (commit `22a9776`); `_RECONCILE_TEMPLATE` already carries *"A restatement,
escalation or confirmation is not a break"* with the BOJ 1.0% case as its worked
example. The ~61% baseline was measured against `replay.py`'s template, which had only
the absence guard. So this run measures a guard that was already live, and `93u`
narrows to the claim-admission half.

**The ~61% baseline is not reproducible.** It came from an in-session read of
`audit.py` output, which prints `claim[:105]` of a field `replay.py` had already
truncated to 150 chars (`replay.py:277,284`). Labelling the untruncated checkpoint text
gives **7 true breaks, 14 false, 2 unclear** — a baseline precision of **33.3%**, not
61%. The fixture is now the baseline of record.

## The run

| | this run | baseline |
|---|---|---|
| precision | **75.0%** | 33.3% |
| recall | **42.9%** | 100% |
| false positives converted | 13 | — |
| true breaks lost | **4** | — |

The seed detector called every row in the fixture broken — that is why they are in it —
so its precision is the gold positive rate and its recall is 1.0 by construction.

The guard works, and it over-corrected. Spec §12.3 pre-registered the failure in one
direction ("a run that improves recall while dropping precision below baseline is a
regression"). This is the mirror image, and it is the more dangerous one: a lost break
leaves a false claim standing in the ledger, which §6.1 says is a permanent integration
error, while a false break is a synthesis error that lasts a day.

Lost: `gs-08` (Portugal tops group / both tied on 4 pts), `gs-14`, `gs-15`, `gs-16`
(a conditional thesis naming its own falsifier, then the falsifier firing). The single
surviving false positive is `gs-21`, an ephemeral price divergence.

## The admission guard does most of the remaining work

Six rows are labelled `admissible: false` — claims that should never have entered the
ledger as durable facts. Scoring only the rows that *should* exist:

```
tp 3   fp 0   fn 2   tn 11      precision 100%   recall 60%
```

The one false positive and two of the four lost breaks live entirely on claims the
admission guard would reject upstream. That reorders `93u`: build the admission guard
first, then re-score, and only then touch the break wording.

## Caveats, all of them

- **n = 21 classifiable.** One relabel moves precision about five points. Read the
  per-item table, not the aggregate.
- **Isolated pairs, not end-to-end.** Each probe is one claim and one contradicting
  clause, with retention and crowding-out removed. The replay measured the system;
  this measures the judgment. A better judgment is still invisible end-to-end if the
  claim was evicted before the contradiction arrived.
- **Severity variance is echo.** The probe hands the model a ledger row already
  carrying the seed severity, and it came back unchanged on 21 of 23. Only `status`
  is judged from scratch. Live ledger rows remain the only place to read severity
  calibration — and there it is still `high` 25/25.
- **Single annotator, not independently reviewed.**
- **One row withheld.** The seed's `r-0808` carries Bigdata.com-derived numeric values
  and this repo is public, so it stays under `from-server/`. It is an ephemeral-price
  admission case, not a break case, so the break set loses no coverage.

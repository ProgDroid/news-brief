# Prediction-market trading, retired

Bead `news-brief-oh4`. 2026-09-11.

## Decision

The operator dropped polygram.ink as a venue, and with it every prediction-market
strategy news-brief traded — paper and live, both sleeves.

The proximate finding, from `docs/2026-09-11-live-buy-pricing.md`: on polygonscan
(where polygram.ink links its trades), the 2774057 buy reads "bought 2.02325 No
shares for $1.73" against a wallet debit of $2.00 + $0.06 in fees — a fill nowhere
near the price the site displayed. Nine of nine $2 buys returned
`2 + 0.0205/fillPrice` shares regardless of the displayed price (which ranged
0.78–0.94), i.e. the venue charges roughly a flat dollar amount of shares
independent of price, not the price it quotes. The venue's own fee page says
"~2%"; the measured effective cost ran far higher on lower-priced fills.

Polymarket's CLOB — the venue this was meant to reach — is not reachable from the
operator's jurisdiction, and a bare Polygon wallet cannot get CLOB fills because
matching happens off-chain. There is no adjacent path to a working execution
venue. The operator's own words: "I doubt this is a bug, just them getting their
money's worth ... let's drop it completely."

## What was removed

Tasks 1–4 of the retirement plan, committed as `2507cad`, `fa66e48`, `b2a7856`,
`5073dd9`, `7e13c64`. Test collection before the plan started: **1904** tests
(`1680` `def test_` lines); after: **1714** tests (`1492` `def test_` lines), per
`py -m pytest --collect-only -q | tail -1` run before and after.

Removed entirely:

- `polygram_live.py` (623 lines) and its 54 tests — the venue write layer.
- Sleeve A (systematic favorite-fade): `open_sleeve_a_live`, `_sleeve_a_entry_reason`,
  `_sleeve_a_exit_reason`, `sweep_live_exits`, `_fetch_pg_half_spread`.
- Sleeve B (discretionary conviction holds): the `/predict` wizard's `_predict_*`
  handlers, `_sleeve_b_open_ok`, `score_settled_theses`, the thesis log
  (`thesis_log.json` + `load/save/append_thesis`), `prune_scored_theses`.
- `pgdiag`, the live-seam probe command and its Dockerfile/README wiring.
- The paper prediction sleeve: `run_prediction_matcher`, `_gather_pg_candidates`,
  `_parse_matches`, `_signal_search_terms`, `_pg_match_pass`,
  `_open_prediction_positions`, `_mtm_prediction`, `_settle_prediction`,
  `polygram_login`/`_polygram_get`/`polygram_search`/`polygram_market`,
  `_parse_pg_market`, `_pg_outcome_label`, `fetch_pg_volume`, `_pg_market_volume`.
- Reporting: `pred_name`/`pred_title`/`_pred_handle`/`_pred_lines`,
  `live_performance`, `_sleeve_a_block`.
- Three repair scripts and their tests: `repair_live_cost_basis.py`,
  `repair_lost_live_close.py`, `repair_settled_payouts.py`.
- `tests/test_prediction.py` in full.
- 18 settings knobs and their compose anchors: 16 `PG_*` rows plus
  `HAIRCUT_BPS_PREDICTION` and `VOL_FLOOR_PREDICTION`.
- `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` credentials.
- `play_type` and `entry_spread` on newly-opened rows; `play_type` from
  `validation._DIMENSIONS`; `prediction` from `validation._ASSET_CLASSES` and
  `trading._VENUE_BY_ASSET`.

## What was kept, and why

- The `/thesis` annotation feature (`theses.json`, `thesis_ref`) — a different
  feature (thesis-per-position notes on any asset class), not part of the
  prediction sleeves.
- The `index` asset class — market pulse via Yahoo, unrelated to PolyGram.
- Every historical prediction row in `book.json` — nothing is deleted, only
  retired. Close reasons on prediction rows: `venue_retired` (new, from the
  retirement script), alongside the pre-existing `settled`/`manual`.
- `aggregate_performance` now excludes both retired populations from every
  aggregate: rows with `execution == "live"` (their returns measure the venue's
  pricing bug, not any strategy) and rows with `asset_class == "prediction"`
  (a class the model can no longer emit). Decided by the operator, 2026-09-11.
- A counted `mark_to_market` guard: prediction rows that are open but not yet
  retired are left untouched, and the run logs "N prediction row(s) still open —
  run scripts/retire_prediction_rows.py" rather than silently skipping them.
  `/close` and `/positions` report the same for such rows.
- `/positions` now iterates `sorted(by_class)` so `index` rows render (they were
  previously dropped by an asset-class list that enumerated only
  equity/crypto/prediction).

`scripts/retire_prediction_rows.py` closes each still-open paper prediction row
at its last recorded mark, with `close_reason = "venue_retired"`, and scores it
by reproducing the prediction branch of the now-deleted
`trading._stamp_close_metrics`: haircut is `entry_spread` if the row has one,
else the `HAIRCUT_BPS_PREDICTION` default of 0.02 (200 bps), +0.02 more for a
momentum play; net return is floored at −1.0 (a stake cannot lose more than
itself); benchmark is the naive 0.0 coin-flip baseline used throughout. A row
that was never marked retires with every score field `None` — unknown, not
zero. This is an operator decision, not a re-derivation: the branch it mirrors
no longer exists to call.

## Operator gate (measured before any code was removed)

```
PG_LIVE_ENABLED = 1
PG_A_ENABLED    = 0
PG_B_ENABLED    = 0
HAIRCUT_BPS_PREDICTION: no row (default 200)
0 open live rows
```

## Host runbook

```sh
# 0. Preconditions were MEASURED before the push (Task 6 Step 4): the three
#    PG_*_ENABLED rows read 0, HAIRCUT_BPS_PREDICTION is 200 or absent, and the
#    book had 0 open live rows. If any of that has changed since, stop.
# 1. Deploy the commits (whatever the usual pull/build/up is here).
# 2. Retire the open paper rows — dry run first; expect "N row(s) to retire, M never marked"
#    where N is what the host book holds (the 08-29 local copy said 25 / 2; the host has moved on):
docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json
docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json --apply
# 3. Optional tidy — nothing reads these any more:
#    remove PG_* and POLYGRAM_* lines from the host docker-compose.yml and .env
#    DELETE FROM settings WHERE key LIKE 'PG\_%' ESCAPE '\';
#    rm /app/logs/paper/polygram_token.json /app/logs/thesis_log.json
```

`REFUSED: ... live row(s) still open` means the plan's premise is false — stop
and look; it is not a prompt to add `--force`.

The first post-deploy weekly report's "Overall" line will drop the ~70
previously-scored prediction rows from its denominator. That is the exclusion
above taking effect, not a regression — expect a step change in the reported
hit-rate and return, not a gradual drift.

## Lessons

A fixture whose numbers were derived from the same formula under test
(`shares = amount/fill`) could not catch that formula being wrong — it agreed
with the bug by construction. A question put to the operator whose every
answer option ("2.00 / 2.06") presupposed the conclusion got a
non-discriminating reply; "same on every buy, or different?" was the question
that actually separated a pricing bug from a fee schedule. A venue's displayed
price is not evidence of its fill — only the wallet balance and the chain read
are, and both had to be checked before the fee page's "~2%" could be weighed
against them. And process, not just data: two fresh-context red-team reviews
of this retirement plan each found defects — six combined — that the plan's
own author had not caught in self-review, three of which would have shipped as
green commits that quietly did the wrong thing.

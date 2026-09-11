# Red-team: retire prediction-market trading (`2026-09-11-retire-prediction-trading.md`)

**Reviewer stance:** hostile. The brief was to find why executing this plan fails to
produce what the operator decided, not to polish it. Findings are ordered by how much of
the plan they invalidate. Every claim below was checked against the working tree at
`7f0a904` (plus the uncommitted `.beads`/`.claude/memory` changes, which I did not touch);
where I ran something, the command and the number are given.

**What I actually read/ran:** the plan; `docs/2026-09-11-live-buy-pricing.md`; the
inventory; `trading.py` (`fetch_price`/`fetch_quote`/`_parse_symbol` :151-165, :507-525,
:1035-1083; `_stamp_open_benchmark`..`_stamp_close_metrics` :647-739; `_migrate_position`
:1258; `mode_paper` :1891-2059; `mark_to_market` :2133-2183; `_watched_instruments`/
`run_volume_monitor` :1342-1401); `brief.py` (`_predict_start`/`_predict_commit`
:1007-1251, `_close_ticker`/`_close_picker_render` :1317-1394, `/watch` :1716, `/positions`
:1790-1825, `mode_collect` :3486-3506, `mode_weekly` :3512-3562, `mode_monitor`
:4390-4434, dispatch :4587-4632); `validation.py` :20-320, :460-521; `retention.py`
:110-165; `common.py` :105-140, :186-236, :357-376; `docker-compose.yml`; `Dockerfile`;
`.github/workflows/docker-publish.yml`; `tests/test_packaging.py` in full;
`tests/test_config.py` :190-295, :690-875; `tests/test_validation.py` :1-16, :227-270,
:306-330, :399-480; `tests/test_trading.py` :40-82; `tests/test_prediction.py` :594-600,
:1093-1120, :1313-1320; `tests/test_monitor.py` :8-16, :84-95; the 2026-08-29 book copy
in `from-server/paper/book.json` (258 rows); beads `oh4 rhg 9tq 7ch 8fy nyy.5`.
Counts: `py -m pytest --collect-only -q` -> **1904**; `grep -c '^def test_'` per file
matches the inventory exactly (79/54/11/11/8/35/38/35/95/27/62/16/27/7; total 1680).
I also applied the plan's Task 3 `_predict_commit` stub to a scratch copy of `brief.py`
and ran `ruff check` on it (result under a.2).

---

## The three strongest objections

| # | Axis | One line |
|---|---|---|
| 1 | (a) intermediate commits | **Task 3 cannot be green as written.** `tests/test_prediction.py:9` is a module-level `import polygram_live` (whole file fails collection, 46/79 tests reference Task-3 symbols); `brief.py:1013,1097` read `PG_LIVE_ENABLED`/`PG_LIVE_PER_TRADE_CAP` that Task 3 deletes from `KNOBS` (so `/predict` raises `AttributeError` and two Task-4 tests error in Task 3); and the plan's `_predict_commit` stub fails `ruff check` with F841 + 7x F821 (measured). Tasks 3 and 4 are one commit, or Task 3 must keep the three `PG_LIVE_*` knobs and delete all of `/predict`. |
| 2 | (b) false assumption + unverifiable step | **Task 1's "change `== 2.0` to `0.55`" instruction breaks two tests, and nothing in the plan would notice.** Neither `2.0` literal in `test_config.py` (:250, :750) asserts a default — both assert an env value that was *imported* as `"2.0"`. Every default assertion already goes through `common.KNOBS[...].default`. All 18 edited tests are DB-only (`conn` fixture): they SKIP under the plan's local gate and first execute in CI **after the Task 7 push**. |
| 3 | (a)/(c) host sequencing | **The runbook deploys the code that deletes the only sell path *before* checking that no live row is open, and never turns Sleeve A off.** `PG_LIVE_ENABLED`/`PG_A_ENABLED` are ON in the host `settings` rows (nine live fills prove it); the daily collect can open a new $2 live row any day before deploy; after deploy `polygram_live.close_live_position` is gone, `mark_to_market` skips the row silently, and the retire script *refuses* — the only alarm is one that fires only if someone runs it. Also: the runbook's `docker compose run --rm newsbrief-monitor python scripts/...` does not exist in the repo's compose (one service, `newsbrief`, `ENTRYPOINT ["python","brief.py"]` -> `brief.py python ...` -> usage error). |

---

## (a) Does executing the tasks produce what was decided?

### a.1 Task 3 is not a green commit — three independent reasons

**a.1.1 `tests/test_prediction.py` dies at collection, not test-by-test.** Line 9 is
`import polygram_live` at module scope. `git rm polygram_live.py` in Task 3 Step 2 makes
the whole file an `ImportError` during collection — 79 tests, not "only what breaks".
Measured (`scratchpad/count_pred.py`, regex over the plan's Task 3 symbol list):
**46 of 79** tests reference a Task-3 symbol (`polygram_live`, `open_sleeve_a_live`,
`_sleeve_a_*`, `sweep_live_exits`, `_fetch_pg_half_spread`, `pgdiag`, `score_settled_theses`
...). Task 3 therefore removes ~47 tests from that file plus the import, and Task 5's
pre-registration ("whole file 79") double-counts them. The plan says "count with grep
before deleting"; the grep it gives (`Step 7`) does not include the names above that
live only in this file (`_fetch_pg_half_spread`, `score_settled_theses`, `mode_pgdiag`).

**a.1.2 Task 3 deletes knobs that Task 4's code still reads.** Task 3 Step 6 deletes
`PG_LIVE_ENABLED`, `PG_LIVE_TOTAL_CAP`, `PG_LIVE_PER_TRADE_CAP` from `KNOBS`. But:

- `brief.py:1013` `_predict_start`: `if not (common.PG_LIVE_ENABLED and common.PG_B_ENABLED)` — survives to Task 4.
- `brief.py:1097` `_sleeve_b_stake_cap`: `min(common.PG_B_POS_CAP, common.PG_LIVE_PER_TRADE_CAP)` — survives to Task 4.
- `common.__getattr__` (`common.py:372-374`) raises `AttributeError` for a name not in `KNOBS`.

So in the Task 3 commit, `/predict` from Telegram raises `AttributeError` inside the
commands daemon. Green tests, broken bot. And `tests/test_commands.py:1047,1082` —
`test_predict_wizard_thesis_to_market`, `test_predict_disabled_says_so` — do
`monkeypatch.setattr(common, "PG_LIVE_ENABLED", ...)`; monkeypatch's `getattr` probe hits
the same `__getattr__` and the tests **error in Task 3**, not Task 4 where the plan
pre-registers them. `tests/test_commands.py:1258-1262` (`test_predict_stake_respects_effective_cap`)
patches `PG_LIVE_PER_TRADE_CAP` the same way — the plan lists it in Task 3 "only if they
fail"; it will.

**a.1.3 The `_predict_commit` stub fails `ruff check`.** I applied the plan's exact
replacement (Task 3 Step 4) to a scratch copy and removed the import. Result:

```
F841 Local variable `live_exposure` is assigned to but never used
F821 Undefined name `row`   (x7 — every reference after the early return)
```

Ruff runs with defaults here (no `pyproject.toml`/`ruff.toml`; `E4,E7,E9,F` enabled),
so F821/F841 are live. The worker cannot commit Task 3 without deleting most of
`_predict_commit`, at which point the split between Tasks 3 and 4 is fiction.

**Fix:** merge Tasks 3 and 4 into one commit ("remove the venue client, both sleeves,
`/predict`, pgdiag and the thesis log"), pre-registered together. If two commits are
wanted for review size, the honest split is: Task 3 = Sleeve B + thesis log + `/predict`
(no `polygram_live` deletion yet, keeps `PG_LIVE_*`); Task 4 = venue client + Sleeve A +
pgdiag + `PG_LIVE_*`. That order has no forward references.

### a.2 One more Task 3 test the grep misses

`tests/test_prediction.py:1093 test_mark_to_market_skips_live_rows` stubs
`_mtm_prediction`, feeds an open **live** prediction row and asserts the stub was not
called. Task 3 Step 3 deletes the `execution == "live"` skip in `mark_to_market`
(:2142-2143) — this test then fails, and it matches none of the plan's grep patterns.
(Moot if a.1 is fixed by deleting the file in the merged commit; listed because the plan
claims its grep is sufficient.)

### a.3 The retire script's output is invisible to every aggregate — the plan says the opposite

Plan Global Constraints: "Paper prediction history stays in the aggregates as a
historical asset class." `_stats` (`validation.py:35-52`) scores **`net_return`** only.
`scripts/retire_prediction_rows.py` writes `realized_return` and never `net_return`
(nor `haircut`/`edge`), so every `venue_retired` row is excluded from `aggregate_performance`,
the weekly report, the gate, and `performance_prompt_block`. In the 08-29 copy: 70
prediction rows carry `net_return` (all closed pre-retirement) and 25 open ones will be
retired without it. That may well be the *right* call (a last mark is not a close), but
it is the opposite of what the plan states and it is unstated in the script docstring.
Decide it and write it down; do not let the record say "in the aggregates" when 25 of 95
rows are not.

### a.4 `/close` on a not-yet-retired prediction row degrades to a misleading message

After Task 5, `price_position(p)` -> `fetch_price("prediction", "3324624")` ->
`fetch_quote("3324624")` -> `_parse_symbol` (`trading.py:154-163`) returns `None` because
a numeric market id has no `.market` suffix -> `_close_position_at_market` returns
`False` -> the operator sees `⚠️ Couldn't close 3324624 — left open.` (or the
partial-failure branch's ". Check the logs."). Safe (no network, no bad price — I
checked that every prediction `instrument` in the 08-29 book is a bare integer, none
contains a `.`), but the reason is an accident of `_parse_symbol`, the plan does not
argue it, and the message tells the operator to look in logs for something the logs
do not say. Add a one-line branch in `_close_ticker`: a prediction row -> "retired
venue; run scripts/retire_prediction_rows.py". Same for `/positions`, which will print
25 lines of `– 3324624: mark —` until the script runs.

### a.5 `_stamp_close_metrics`'s "no branch needed" claim holds — for the reason in a.4, not the one given

The plan says no prediction row can close again because the retire script writes status
directly. True, but only because `_parse_symbol` rejects the id (a.4) and because of the
new `mark_to_market` guard. Both are one `if` away from changing. Not a defect today;
noted so the record does not carry a false justification.

### a.6 Task 5 Step 9's acceptance grep contradicts the tolerance requirement, and its allow-list is incomplete

The gate "`grep -rni prediction` must print only: the retire script + test, the
`aggregate_performance` comment, the `mark_to_market` guard" is in direct tension with
"code that reads closed rows must tolerate them". Any test that proves tolerance must
name the class. The plan's own `test_mark_to_market_leaves_retired_prediction_rows_alone`
(Task 5 Step 4) contains the word and is **not** on the allow-list. Two existing
pure-data tests the plan deletes for the same reason are exactly the tolerance tests it
should keep:

- `tests/test_validation.py:306 test_aggregate_performance_excludes_live` — imports
  nothing deleted; it pins the very exclusion the plan says to KEEP. Deleting it leaves
  the retained filter unguarded (and the filter is already inert on the real book: no
  live row has ever been given `net_return` — `grep net_return polygram_live.py scripts/repair_*.py`
  finds nothing — so a future cleanup will remove it with no test failing).
- `tests/test_validation.py:466 test_daily_trade_message_still_empty_without_status_or_positions`
  — a two-arg call asserting `""`; it is the natural test for the NEW signature.

Other unlisted hits the final greps will print: `tests/test_common.py:153-157`
(`common.PG_A_ENABLD` typo test — still passes, now a typo of a deleted knob),
`tests/test_common.py:188` and `tests/test_job_interlock.py:352` (docstrings naming
`PG_LIVE_ENABLED` / `sweep_live_exits`), `from-server/replay-2026-08-29/agg.py:27`
(gitignored but on disk; the grep runs over `.`). Enumerate them or the worker is left
deciding what "anything else is a miss" means.

### a.7 Missed test edit: `tests/test_trading.py:71`

`test_stamp_open_benchmark_equity` asserts `p["entry_spread"] is None`. The Task 5
replacement `_stamp_open_benchmark` drops `p["entry_spread"] = None` entirely ->
`KeyError`. Not in any of the plan's edit lists.

### a.8 Retention: delete the tuple, not one key

Task 4 says "remove `"sleeve"` from `_SUMMARY_KEYS` only if that tuple is now unused".
`_SUMMARY_KEYS` (`retention.py:110-120`) has exactly one consumer, `prune_scored_theses`.
Delete the tuple. (`datetime`/`timedelta` imports may go unused too — ruff will say.)

### a.9 What the plan gets right on this axis (so it is not re-checked)

Verified against code: `file_lock`/`load_book`/`save_book`/`trading.BOOK_FILE` stay used
in `brief.py` (`:1177,1322,3554` etc.); `import html` stays used in `brief.py` (54 sites);
`import html` in `validation.py` becomes unused after Task 5 (its only survivors are in
deleted functions and `:515`) — plan handles it; `run_retention`'s `theses_pruned` key
is read by nothing outside its tests (`brief.py:3498-3502` reads `deleted`/`trimmed_lines`);
`docker-compose.yml` has ONE service using the anchor (`newsbrief`, :205) — no second
copy of the `PG_*` lines; `HAIRCUT_BPS_PREDICTION`/`VOL_FLOOR_PREDICTION` are indeed not
in the anchor; `test_packaging.py` derives everything from artifacts, names no script or
module the plan omits, and the new script passes `test_a_script_run_as_a_path...`
(argparse exits 2 with usage; no `ModuleNotFoundError`); `scripts/__init__.py` exists and
is empty; `conftest.py:87` is a comment only; `pgdiag` is not in `JOB_MODES`; no
prediction-specific file is enumerated by `backup.py`; the signals schema needs no edit
(confirmed `_EMIT_SIGNALS_TOOL` enum is equity/crypto); `_watched_instruments` will feed
open prediction rows and any host `watchlist.json` prediction entries to `fetch_volume`
hourly, which returns `None` via the same `_parse_symbol` path and `continue`s silently —
benign, no network.

---

## (b) What does the plan assume that is false or unverified?

### b.1 The `== 2.0 -> 0.55` instruction is wrong, and its blast radius is invisible locally

`grep -n "2\.0\b" tests/test_config.py` -> lines 248/250 and 742/750/751. Both `assert
... == 2.0` follow `monkeypatch.setenv("PG_A_STAKE", "2.0")` + `import_settings_from_env`:
they assert the **imported env value**, not the default. There is no test asserting the
default as a literal — `:234, :270, :703, :726, :794` all use
`common.KNOBS["PG_A_STAKE"].default`. Applying the plan's instruction to the two literals
that exist turns `2.0 == 0.55` red. The correct instruction is: **substitute names only;
touch no numeric literal.**

Why this is objection #2 and not a nit: every one of the 18 tests takes the `conn`
fixture, so under the plan's local gate (`py -m pytest -q`, no `DATABASE_URL`) they
SKIP, before and after. "Same passing count as Step 1" is satisfied by 0 == 0. The
first execution is CI, after the Task 7 push of all seven commits. Task 1 must run with
the test Postgres up (CLAUDE.md gives the command; memory says add
`--tmpfs /var/lib/postgresql/data`) and pre-register that `tests/test_config.py` reports
**runs, not skips**.

The substitutions themselves are sound: `BRIEF_MEMORY_ENABLED`/`CLAIM_VERIFY_ENABLED`
are `Knob(bool, False)` (`common.py:233-234`) and both are in the anchor (`compose:122-123`);
`GATE_MIN_HIT_RATE` is `Knob(float, 0.55)` (`:216`) and is NOT in the anchor — no
test_config test reads the anchor, so that is fine; `conftest.py` sets none of them.
`test_the_documented_upsert_command_works` is a string literal, not a README read.

### b.2 The pre-registered final count is wrong on the plan's own arithmetic

Plan: "Expected post-plan `def test_` count: **1476** (1680 - 204)". But the plan also
adds 6 (Task 2), relocates 2 and adds 1 (Task 5, "net -92"), so its own numbers give
1680 - 204 + 9 = **1485**. And the 204 counts `_pred_row` (a helper, `test_validation.py:357`)
as a test — the named-delete list for that file is 13 tests, not 14 — so 1486. If a.6 is
accepted and two tests are kept, 1488. The plan's escape hatch ("re-collect, do not
trust the estimate") is right, but a pre-registered number that is already wrong
invites "the prediction was wrong" reasoning when the system is what moved. Re-derive
it after fixing a.1's task boundaries.

### b.3 The runbook command does not work against the repo's compose

`docker compose run --rm newsbrief-monitor python scripts/retire_prediction_rows.py ...`
(Task 7 Step 1, copied from `docs/2026-09-11-live-buy-pricing.md:88`). Facts:

- The repo's `docker-compose.yml` defines services `newsbrief` and `postgres` only
  (:205, :237). No `newsbrief-monitor`.
- `Dockerfile:69-70`: `ENTRYPOINT ["python", "brief.py"]`, `CMD ["collect"]`. Any
  service inheriting the anchor turns `run ... python scripts/x.py` into
  `python brief.py python scripts/x.py` -> `brief.py:4587` takes mode `"python"` ->
  connects to Postgres, seeds, then prints Usage and exits 1.

Beads `rhg`/`9tq` say the operator ran the sibling repair commands successfully on
2026-09-11, so the HOST compose differs from the repo's (memory: "the repo's compose is
a stale template and the HOST's is ahead"). That is UNKNOWN from here, not PRESENT. The
form that works on both is `docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json`
(the compose header itself documents `--entrypoint bash newsbrief` at :26). Use it.

### b.4 Things the plan should verify by line before the worker starts (all checked; all hold)

`brief.py:70` import; `:1294 _BUTTON_NAME_CAP` sole use at `:1384`; `:1337-1340` live
fork; `:1380-1388` label; `:1719/:1728` `/watch`; `:1801/:1821` `/positions`;
`:3486-3491` collect call; `:4415-4433` monitor block; `:4477` MODES; `:4630` usage.
`trading.py:48`, `:553-562`, `:647-687`, `:690-739`, `:1069-1083`, `:1258-1266`,
`:1318-1339`, `:1515-1520`, `:2030-2052`, `:2142-2146`. `validation.py:24-32`, `:55-62`,
`:74-93`, `:273-279`, `:460-521`. `common.py:111`, `:126-129`, `:134`, `:188-206`,
`:208-212`, `:229`, `:313`, `:365`, `:483-499`. `retention.py:16`, `:110-120`, `:123-148`,
`:150-165`. `supervisor.py:558`. `enrichment/config.py:10`. `comprehend.py:690`.
`Dockerfile:39`. Workflow `:17,75,76`. Compose `:28-32`, `:60-61`, `:69-98`. README
`:78,159,165,170,225,234,265,334-339,401,406`. All as the plan states.

---

## (c) What breaks in six months?

### c.1 The daily prompt keeps citing a track record for an asset class the model cannot emit

`performance_prompt_block` (`validation.py:288-320`) hardcodes
`("asset_class", "confidence", "thesis_ref")` and feeds any dimension value with
`n >= 5` into the DAILY prompt as "YOUR TRACK RECORD". `asset_class=prediction` has
**n=70** with `net_return` in the 08-29 book. So every morning, indefinitely, the
model is told `asset_class=prediction: NN% hit-rate, net ...% (n=70)` for a class its
tool schema (`_EMIT_SIGNALS_TOOL`, equity/crypto) does not allow. It is pure prompt
noise, and it is the kind that never produces a visible error. The weekly
`performance_report`'s "Overall" and calibration block are likewise ~34% prediction
rows (70 of 206 scored) forever, with the share only decaying as equity/crypto closes
accumulate. The plan's "keep in aggregates" is the operator's call; the plan should at
minimum exclude `prediction` from `performance_prompt_block` (one clause, one test) and
say in the record that the weekly "Overall" is a blended figure.

### c.2 The `mark_to_market` guard is a silent fail-closed path

`if asset_class == "prediction": continue` with no log. If the runbook is never run
(the operator is human; the script is optional-looking in a code block), 25 rows stay
open forever with no signal anywhere but a `/positions` listing. This repo's own rule
(memory `fail-closed-needs-status-not-count`) is that a silent skip is unattributable.
Make the guard count and `log.warning` once per weekly run: `"{n} prediction rows still
open — run scripts/retire_prediction_rows.py"`. After the script runs it is dead code;
that is fine and the log line says why it exists.

### c.3 The `index` gap is copied forward into the rewrite

`mode_paper` opens `index` rows (`trading.py:1993-1996`, the branch the 08-09
commodity fix added). `/positions` renders `for ac in ("equity", "crypto", "prediction")`
— an open index row is silently omitted today, and the plan's replacement body
(`for ac in ("equity", "crypto")`) preserves the omission while rewriting the exact
line. `validation._ASSET_CLASSES` likewise gets no `index` row in the gate. The plan
is not making it worse, but it is the second time this tuple has been edited without
fixing it. `/positions` should iterate `sorted(by_class)`; whether `index` belongs in the
gate is a separate decision — file it.

### c.4 Memory and beads that will re-propose the feature

Task 7 updates `polygram-live-trading-spec.md` and the roadmap only. Still presenting
prediction trading as current in the auto-loaded `MEMORY.md` index: the
`polygram-candidate-search-fix` line ("CONFIRMED end-to-end: 2 prediction positions
opened post-deploy. Positions are PAPER (**live exec = future want**)"), `multi-asset-
trading-build.md` (in the history file), `commodity-signals-are-index-class` (names
`mode_paper`'s prediction branch). Bead `nyy.5` ("Build the web UI: inspection and
trading monitor") still scopes "PolyGram positions, exposure, caps" (its description,
line 8). The index is already over its size limit, so these should be *shortened* to a
RETIRED marker, not appended to.

### c.5 Old rows' `close_reason` values are safe

`aggregate_performance` is the only production reader of closed rows (`grep '"closed"'`
over `trading.py brief.py validation.py`: `validation.py:60` and `trading.py:1694`, the
latter deleted). Nothing groups by `close_reason`; `retention` prunes dated files and
the signals log, never book rows; `record_gate_history` `setdefault`s only the surviving
classes and leaves the host file's `prediction` key as a stale list. No six-month hazard
from `venue_retired`/`settled`/`settlement`/`manual`/`max_hold`/`target` values.

---

## Ordered fix list (what I would demand before execution)

1. Merge Tasks 3+4 (or re-split as in a.1 fix). Re-pre-register: `test_prediction.py`
   goes in that commit or the split one; add `test_mark_to_market_skips_live_rows`,
   `test_predict_wizard_thesis_to_market`, `test_predict_disabled_says_so`,
   `test_predict_stake_respects_effective_cap` to the correct commit's list.
2. Task 1: delete the "`== 2.0` -> `0.55`" sentence; require the test Postgres and a
   `runs, not skipped` line for `tests/test_config.py` in Steps 1 and 3.
3. Task 7 runbook: **step 0, before `git push`**: set `PG_LIVE_ENABLED=0` and
   `PG_A_ENABLED=0` in the host `settings` rows (the documented upsert), then confirm
   `0 open live rows` in the host book (a `jq` one-liner or `/positions` showing no 💵).
   Use `--entrypoint python newsbrief` in every command.
4. Keep `test_aggregate_performance_excludes_live` and
   `test_daily_trade_message_still_empty_without_status_or_positions`; add
   `test_stamp_open_benchmark_equity`'s `entry_spread` line to the Task 5 edit list;
   put every tolerance test on the Step 9 allow-list.
5. Decide and record a.3 (retired rows carry no `net_return`); add the log line in c.2;
   exclude `prediction` from `performance_prompt_block` (c.1) or record why not.
6. Re-derive the expected `def test_` count after 1 and 4; it is not 1476.

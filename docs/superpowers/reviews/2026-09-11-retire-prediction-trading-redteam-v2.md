# Red-team v2: retire prediction-market trading (plan v2, 2026-09-11)

**Stance:** hostile. The question is whether executing v2 as written produces six green
commits and a host that ends up where the operator decided — not whether v2 reads better
than v1. Every claim below was checked against the working tree at **`b136f75`** (two
commits past the `7f0a904` the v1 review was written against; both are live-book repairs
and change nothing the plan touches). Where I ran something the command and number are
given; where I could not, it says UNKNOWN.

**What I actually read/ran.** The plan; the v1 review; `docs/2026-09-11-live-buy-pricing.md`;
`trading.py` (`fetch_quote` :507, `fetch_volume` :553, `_stamp_open_benchmark`..
`_stamp_close_metrics` :647-739, `polygram_login`/`_polygram_get` :794-853, `price_position`
:1069, `_migrate_position` :1258, `resolve_watch_entry` :1318, `_watched_instruments` :1342,
`_floor_for` :1515, `_close_position_at_market` :1540, `mode_paper` :1891-2059,
`mark_to_market`..`_mtm_prediction` :2133-2232); `brief.py` (imports :60-115, `/predict`
wizard :948-1010, `_pos_ticker`/`_close_ticker`/`_close_picker_render` :1311-1394, `/watch`
:1716, `/positions` :1790, `mode_collect` :3489, `mode_pgdiag` :3932, `mode_monitor` :4390);
`validation.py` in full; `common.py` :63, :111, :128, :186-234, :484-496; `retention.py`
:16, :124-163; `tests/conftest.py` :144-171; `tests/test_validation.py` (`_closed` :4,
:306-330, :399-480); `tests/test_commands.py` :90-165, :949-1319; `tests/test_trading.py`
:40-112; `tests/test_prediction.py` :1-40, :594-630, :1315-1330 plus a regex split of all
79 tests; `tests/test_config.py` :215-255, :738-790; `tests/test_packaging.py` :222-262;
`docker-compose.yml`; `Dockerfile`; README lines the plan cites; the 08-29 book copy;
beads `oh4`, `rqu`. **Ran:** the plan's Task 2 script + tests in isolation (8 passed); a
differential of the script's `score()` against production `_stamp_close_metrics` on all
23 marked open paper rows of the 08-29 book plus five edge cases (0 divergences); the
plan's four Task 4 tests against the current tree (all four fail — two for the reason the
plan gives, two not); `ruff check`/`ruff format --check` on the Task 2 snippets; the
acceptance grep of Task 4 Step 8 as written and as `git grep`; `py -m pytest
--collect-only -q` → **1904**; `grep -c '^def test_'` → **1680**, per-file counts identical
to the plan's.

---

## The three strongest objections

| # | Axis | One line |
|---|---|---|
| 1 | (a)/(c) host sequencing | **v1's objection 3 is closed in prose only: the runbook now asserts the live knobs "are false (already true on 2026-09-11)" — bead `rqu`, filed the same morning and still open, says `PG_A_ENABLED is still 1 on the host with no open position, so the next signal opens a certain loss`.** The plan gives no command that reads the rows, and step 0 lives in a runbook the worker hands over AFTER Task 6 Step 4 has pushed (push = image build = deploy). A $2 row opened by any collect between now and the deploy has, after the deploy, no sell path in the code, a `mark_to_market` that only counts it, and a retire script that REFUSES on it. |
| 2 | (a) green commits | **Task 3 is still not green, and Task 4 is not either.** Task 3 deletes `_fetch_pg_half_spread` and the 💵 label but schedules `tests/test_trading.py::test_stamp_open_benchmark_prediction` (monkeypatches the deleted name, asserts `entry_spread == 0.02`) and `tests/test_commands.py::test_close_picker_labels_prediction_by_question` (asserts `"💵" in pred["text"]`) for Task 4; its `test_prediction.py` pattern list omits `pgdiag`, so five `test_pgdiag_*` tests survive to call a deleted `brief.mode_pgdiag`. Task 4's new prediction filter turns the KEPT `test_aggregate_performance_excludes_live` (both rows `asset_class: "prediction"`) into `None["n"]`, and its new test calls `_closed(asset_class=..., net_return=0.5)` against a helper whose signature is `_closed(asset_class, net, ...)` — `TypeError` before AND after the change. |
| 3 | (a) acceptance | **Task 4's "must print ONLY these lines — anything else is a miss, don't extend the list" grep prints 60+ lines it does not list**: `-i "PG_"` matches `pg_tables`/`pg_indexes`/`pg_sleep`/`pg_proc`/`pg_try_advisory_lock` (only `pg_advisory` is excluded — `pg_try_advisory_lock` does not contain it), `.` includes the gitignored `.playwright-mcp/*.yml` (47 PolyGram lines), and five tracked prose hits are unlisted (`tests/test_comprehend_matcher.py:3,4,47`, `tests/test_locking.py:145`, `tests/conftest.py:87`). Three of those are `polygram` in `tests/`, which bead `oh4`'s own acceptance criterion (`grep -ri polygram over *.py, tests/ ... returns nothing`) forbids — so following the plan cannot close the epic. |

---

## (a) Is each v1 objection actually closed, or only in prose?

| v1 fix | Status | Evidence |
|---|---|---|
| 1. Merge Tasks 3+4 / re-pre-register | **Partly.** The import coupling is fixed (v1's a.1.2/a.1.3 hold: `/predict`, `PG_LIVE_*`, the stub are all in Task 3 now). Two tests and five pgdiag tests are still on the wrong side of the boundary. | a.1 below |
| 2. Task 1: no `2.0 → 0.55`; Postgres required | **Closed.** Postgres is required in Global Constraints and Step 1/3 say "runs, not skipped". The "built-in default becomes 0.55" sentence survives as dead text — `grep -n "2\.0\b" tests/test_config.py` → :248/:250/:742/:750/:751, all env-set values, and the plan exempts :250/:750 by line. Substitution targets verified: `BRIEF_MEMORY_ENABLED`/`CLAIM_VERIFY_ENABLED` are `Knob(bool, False)` (common.py:233-234), read via `common.X` (brief_memory.py:126, claim_verify.py:33), in the anchor (compose :122-123); `_consumed_variables()` includes every `KNOBS` key (test_packaging.py:235). PG_ counts 50/4 confirmed; the three sed names cover every PG_ name in both files (`PG_A_ENABLED` 10+4, `PG_A_STAKE` 39, `PG_B_ENABLED` 4). | — |
| 3. Runbook step 0 before push; `--entrypoint` | **Half.** `--entrypoint python newsbrief` is in every command and works against the repo compose (one service, `/app/logs` volume, `DATA_DIR=/app/logs` → `/app/logs/paper/book.json`). Step 0 is present but (i) asserts the knobs are off with no command to read them, contradicted by `rqu`; (ii) is sequenced after the push in the plan's own execution order. | Objection 1 |
| 4. Keep two tests; `:71` edit; allow-list | **Two of three.** Both tests kept; `:71` edit listed. The kept `test_aggregate_performance_excludes_live` breaks under Task 4's own filter (unlisted edit). The allow-list is written and wrong. | a.3, objection 3 |
| 5. Decide a.3; log line c.2; `performance_prompt_block` c.1 | **Closed.** Scoring decided and mirrored (b.1 — verified numerically); the counted guard logs; excluding `prediction` in `aggregate_performance` reaches `performance_prompt_block`, `_calibration_block`, `record_gate_history`, `evaluate_gate` and `performance_report`, all of which take `agg` from that one function (validation.py :103, :117, :238, :295); `_leakage_block` reads the leakage log, not positions. No second path. | — |
| 6. Re-derive the count in Task 6 | **Closed in principle**, but the per-task pre-registrations it feeds are off (a.4). | a.4 |

### a.1 Task 3 as one commit — the orphan check the plan says it did

I simulated the merged deletion by listing every test that references a symbol Task 3
removes (`_fetch_pg_half_spread`, `_sleeve_a_*`, `_sleeve_b_open_ok`, `score_settled_theses`,
`open_sleeve_a_live`, `sweep_live_exits`, `live_performance`, `_sleeve_a_block`,
`load/save/append_thesis`, `THESIS_LOG_FILE`, `mode_pgdiag`, `polygram_live`, the 💵 label,
the `_stamp_open_benchmark` prediction branch) and checking which task deletes each test.

**Production side is clean.** Every `trading.`/`polygram_live.`/`common.append_thesis`
reference in `brief.py` sits inside `_predict_*`, `_close_ticker`, `mode_pgdiag` or
`mode_monitor` (mapped by enclosing function); `brief.py`'s `from trading import (...)`
block names nothing Task 3 deletes, and `pred_name`/`pred_title` go in Task 4 with their
last two callers. `polygram_live` is imported by `brief.py:70`, `trading.py:1742/:2250`
(inside the two deleted functions), `tests/test_prediction.py:9`, six tests in
`test_commands.py` and one in `test_monitor.py` — all named in Task 3. `retention.py`'s
`load_thesis_log, save_thesis_log` import is the only production reader of the thesis log
outside `brief.py`; Task 3 Step 2 handles it. `common.PG_*` readers in `trading.py` are all
inside `_sleeve_a_entry_reason`, `_sleeve_b_open_ok`, `open_sleeve_a_live`,
`_sleeve_a_exit_reason` (Task 3) — `_floor_for`'s `VOL_FLOOR_PREDICTION` and
`_haircut_fraction`'s `HAIRCUT_BPS_PREDICTION` are Task 4 knobs, deleted with their readers.
So no AttributeError-through-`common.__getattr__` in the Task 3 state. Good.

**Test side has three leaks, all measured:**

1. `tests/test_trading.py:77 test_stamp_open_benchmark_prediction` —
   `monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.02)` then
   `assert p["entry_spread"] == 0.02`. Task 3 deletes the function (monkeypatch raises
   `AttributeError` on a missing target) AND rewrites the branch to `entry_spread = None`.
   The plan lists this test under **Task 4** Step 1.
2. `tests/test_commands.py:1318 test_close_picker_labels_prediction_by_question` —
   `assert "💵" in pred["text"]` with an `execution: "live"` row. Task 3 Step 5 deletes the
   two 💵 lines in `_close_picker_render`. Listed under **Task 4** Step 1.
3. `tests/test_prediction.py`: the plan's pattern list (Task 3 Step 1) does not contain
   `pgdiag`. Regex split of the file: **42** of 79 tests match the plan's patterns; adding
   `pgdiag` gives **47** (five `test_pgdiag_*` tests, each calling `brief.mode_pgdiag()`,
   which Task 3 deletes). v1 a.1.1 named `mode_pgdiag` explicitly as a name "that lives
   only in this file"; v2 quotes v1's "46" and dropped the pattern. The 32 survivors import
   cleanly after `import polygram_live` is removed (module scope is `json`, `datetime`,
   `pytest`, `common`, `trading`; the one class `_Resp` is a stub) and none reads
   `entry_spread`, the sleeve, or a deleted knob — checked by regex over each survivor
   and over the three helpers (`_gated_sleeve_a`, `_pgdiag_env`, `_pgdiag_with_positions`
   are used only by deleted tests).

The full gate would catch all three, so the worker is not shipping red — but the plan
claims in its self-review that "no intermediate state imports a deleted module or reads a
deleted knob", and the pre-registered counts (a.4) are what the worker compares against.
A count that is wrong by design produces "the prediction was wrong" reasoning when a
genuine miss appears.

**One ordering nit while here:** Task 3 deletes `mark_to_market`'s
`execution == "live": continue` guard but leaves `_mtm_prediction` until Task 4. In the
Task 3 state an open live row (they carry `play_type` and `side_index` — checked on the
seven live rows of the 08-29 copy) would flow into `_mtm_prediction` → `polygram_market`
and could be `_settle_prediction`'d as paper. That state is never deployed alone, so this
is an ordering nit, not a hazard — move the guard deletion to Task 4 beside the dispatch
it protects.

### a.2 Task 4's four new tests — what is actually red, and why

Ran all four against `b136f75` (the Task 3 state differs only in the live guard, which
none of them reaches):

| test | red? | actual reason | plan's stated reason |
|---|---|---|---|
| `test_mode_paper_returns_a_summary_when_there_is_nothing_to_do` | yes | `{'opened': 0, 'sleeve_a': None} != {'opened': 0}` | same ✓ |
| `test_mark_to_market_leaves_a_retired_prediction_row_alone_and_says_so` | yes | the log line is absent; captured log shows `MtM kept open (no price): prediction 3324624` from `_mtm_prediction` | "`fetch_price` is called today via `price_position`" — **false**: `price_position` dispatches `asset_class == "prediction"` to `polygram_market` (trading.py:1075-1082), never to `fetch_price`. The `pytest.fail("priced")` control is inert before the change (wrong path) and after it (the guard `continue`s first). It only discriminates in the mutation check (guard removed → rewritten `price_position` → `fetch_price`), which is fine, but the RED-reason the worker is told to look for will not appear. |
| `test_aggregate_performance_excludes_the_retired_prediction_class` | yes | **`TypeError: _closed() got an unexpected keyword argument 'net_return'`** — the helper is `_closed(asset_class, net, edge=None, confidence=None, play_type=None, thesis_ref=None)` (test_validation.py:4). Stays red after the change. | "prediction is still aggregated" |
| `test_close_on_a_not_yet_retired_prediction_row_names_the_script` | yes | `⚠️ Couldn't close 3324624 — left open.` via `polygram_market` returning `None` uncredentialed (`_polygram_get` :822 returns before any HTTP; the conftest network block never fires) | same message ✓, mechanism unstated |

Fix for the third: `_closed("prediction", 0.5)` / `_closed("equity", 0.1)`. This is
`tdd-plan-fixtures-drift-from-contracts`, fourth instance in this feature.

The `/close` test does not patch `save_book`; today the path never reaches it (`closed_n`
stays 0) and after the change the retired branch returns first, so the real `BOOK_FILE`
is never written. Acceptable, but say so — a future edit that moves the branch below the
loop turns this test into a writer of `logs/paper/book.json`.

### a.3 A kept test that Task 4 silently breaks

`tests/test_validation.py:306 test_aggregate_performance_excludes_live` — the test v1 asked
to keep and v2 keeps — has both rows as `asset_class: "prediction"`. Under Task 4's new
filter `closed` is empty, `_stats([])` returns `None`, and `agg["overall"]["n"]` is
`TypeError: 'NoneType' object is not subscriptable`. Not in any Task 4 edit list. Fix:
paper row → `"asset_class": "equity"` (the live row can stay `prediction`; the test then
still proves the `execution` clause on its own, and the plan's mutation check "revert the
prediction clause → exactly 1 failure" holds).

### a.4 Pre-registration arithmetic

- Task 3 names `test_validation.py` **9** and lists **8** (`test_live_performance_reports_live_only`,
  `..._separates_live_from_paper`, four `test_sleeve_a_block_*`, two `test_a_live_row_*`).
  Regex over the file finds exactly those 8 touching `live|sleeve|_sleeve_a_block|above_band|fill_price`
  (plus `test_performance_report_renders`, whose only "live" is in "Go-live"). Named total is
  **23**, not 24; with the two mis-scheduled tests (a.1) the true Task 3 removal from the
  named files is **25**, and `test_prediction.py` contributes **47**, so the commit removes
  84 + 25 + 47 = **156** `def test_` lines, not 84 + 24 + 46 = 154.
- Task 4 says "+2 relocated, +4 new" (= +6 in Step 8) but specifies **five** tests: four in
  the snippet plus `test_collect_trading_failure_does_not_duplicate_brief`. The first snippet
  test is a rewrite of the existing `test_prediction.py:1315 test_mode_paper_returns_summary_when_nothing_to_do`
  (same body, new shape) — if that is the "second relocated" test it is being counted twice.
  Either name the sixth or make it +5.

### a.5 Task 2 — verified, works

Extracted the plan's script and tests verbatim into a scratch package and ran them:
**8 passed**. `ruff check` clean. `ruff format --check` fails on both files (three
over-88-column lines: the `{k: None for k in (...)}` comprehension, the `live_open`
comprehension, the `changes = {...}` literal; in the Task 4 snippets also the two long
`def`/`monkeypatch` lines) — cosmetic, `ruff format` fixes it, but the plan says "each
commit green" and hands the worker text that is not. `scripts/__init__.py` exists; the
`from scripts import retire_prediction_rows` form matches the sibling tests. The script
writes with `Path.write_text` and no `file_lock`, exactly like `repair_live_cost_basis.py:159`
— same convention, same (pre-existing) race with the serve container's `/close`; not new.

---

## (b) New assumptions v2 introduces

### b.1 `score()` reproduces the deleted branch — VERIFIED, with one dependency

Differential run against production at `b136f75` (`config._read_settings` stubbed to `{}`
so knobs resolve to defaults): for each of the **23 marked** open paper prediction rows in
the 08-29 book, `_stamp_close_metrics` on a copy with `realized_return = last_mark["return"]`
vs `score(row)` → **0 divergences** across `realized_return`, `haircut`, `net_return`,
`benchmark_return`, `edge`. Edge cases, production vs script:

| `entry_spread` | `play_type` | haircut | net (gross −0.5) |
|---|---|---|---|
| `None` | resolution | 0.02 / 0.02 | −0.52 / −0.52 |
| `None` | momentum | 0.04 / 0.04 | −0.54 / −0.54 |
| `0.0` | resolution | 0.0 / 0.0 (a captured zero is honoured, not defaulted — both sides) | −0.5 / −0.5 |
| `0.0` | momentum | 0.02 / 0.02 | −0.52 / −0.52 |
| key absent | key absent | 0.02 / 0.02 | — |

Sign, floor (`max(net, −1.0)`), momentum leg, benchmark `0.0`, `edge = net` all agree.
`last_mark["return"]` is what `_mtm_prediction` stamps as `_signal_return("bullish", entry, price)`,
which is exactly what a close would have written as `realized_return`. The only shape
difference: for an unmarked row production's early return leaves `haircut`/`net_return`/
`benchmark_return`/`edge` **absent**, the script writes them as explicit `None` — harmless
(`_stats` uses `.get`), and arguably better.

**The dependency:** production reads `common.HAIRCUT_BPS_PREDICTION`, a settings row; the
script hardcodes `0.02`. The knob is not in the compose anchor, so the row was seeded from
the default and is 200 unless someone upserted it — UNKNOWN from here. By the time the
script runs the knob no longer exists in code, so it cannot read the row. Add to runbook
step 2: `SELECT value FROM settings WHERE key='HAIRCUT_BPS_PREDICTION'` → expect `200` or
empty; anything else means `score()` no longer mirrors what production would have done.

### b.2 `sorted(by_class)` in `/positions`

No test in `tests/test_commands.py:98-160` (or elsewhere in the file) asserts section order;
`test_positions_lists_open_with_marks` checks membership only. Safe. The plan's snippet
matches the real block's structure (`by_class` dict → header lines → `telegram_send_long`).

### b.3 The `mark_to_market` guard placement

The real function (trading.py:2140-2147) is `today = ...` → `for p in book["positions"]:` →
`if p["status"] != "open": continue` → `if execution == "live": continue` → `if prediction:
_mtm_prediction; continue` → `price = price_position(p)`. Nothing sits between `today` and
the loop; the snippet drops nothing. After Task 3 removes the live guard the snippet is a
faithful replacement.

### b.4 The runbook's compose one-liner

`docker compose run --rm --entrypoint python newsbrief -c "..."`: the repo compose has one
app service (`newsbrief`, `command: [serve]`, anchor volume
`${APPDATA_DIR:-./appdata}/news-brief:/app/logs`); `common.DATA_DIR` defaults to
`/app/logs`, `PAPER_DIR = DATA_DIR / "paper"`, so `/app/logs/paper/book.json` is right.
`--entrypoint python` replaces `["python","brief.py"]`, and positional args after the service
name replace `command`, so the container runs `python -c "..."`. Works. (Whether the HOST
compose still matches is UNKNOWN — memory says it is ahead of the repo — but the form
survives any anchor-inheriting service.)

### b.5 The precondition that is asserted rather than measured (objection 1, evidence)

Plan Task 6 Step 1: `# 0. Preconditions (already true on 2026-09-11 — confirm, don't
assume): settings rows PG_LIVE_ENABLED / PG_A_ENABLED / PG_B_ENABLED are false`. Bead
`news-brief-rqu` (created 2026-09-11T09:57Z, **open**, the bead this plan closes in Task 6
Step 2): "*PG_A_ENABLED is still 1 on the host with no open position, so the next signal
opens a certain loss.*" Its acceptance criterion is "a settings-row decision is recorded
here with the observation behind it" — closing it with "polygram dropped entirely" records
no observation. Nothing in memory, beads or the working tree says the row was flipped
since. The v1 review said the same thing with the nine fills as evidence. v2 replaced the
instruction with an assertion and gave the operator a command for open live rows but none
for the rows that decide whether a NEW one opens.

Sequencing: Task 6 Step 4 is "Commit + push … Report the runbook to the operator". Push
triggers `.github/workflows/docker-publish.yml` (on push to `main`, builds and pushes the
image the deploy tracks). So step 0 of the runbook executes after the code that removes
`polygram_live.close_live_position` is already on its way. Today the daily collect at the
host's cron time runs `mode_paper` → `open_sleeve_a_live` with the flags as they are.

Consequence if a row opens in the window: after deploy, `/close` on it prints the new
"retire script" message; the retire script prints `REFUSED: 1 live row(s) still open`; the
weekly `mark_to_market` logs "1 prediction row(s) still open — run scripts/…", which is the
wrong instruction for that row; the only exit is the venue's website. Plus the record then
opens with a tenth live loss.

**Fix (mechanical, no new code):** make the following a numbered step of Task 6 that
precedes Step 4, executed by the OPERATOR with the worker waiting on the result:

```sh
# on the host, BEFORE the push — read, then flip, then re-read:
docker compose exec postgres psql -U newsbrief -d newsbrief -c \
  "SELECT key, value FROM settings WHERE key IN ('PG_LIVE_ENABLED','PG_A_ENABLED','PG_B_ENABLED')"
docker compose exec postgres psql -U newsbrief -d newsbrief -c \
  "UPDATE settings SET value='0' WHERE key IN ('PG_LIVE_ENABLED','PG_A_ENABLED','PG_B_ENABLED')"
# then the existing open-live-rows one-liner → must print "0 open live rows"
```

(The table/column names are the ones `tests/test_config.py:252` uses — `UPDATE settings SET
value = ... WHERE key = ...` — and the README's documented upsert at :234; adjust the psql
invocation to the host's service name.) Record the two SELECT outputs in `rqu`'s close
reason. Only then push.

---

## (c) Six months out — what v2's changes make worse, and what they don't

### c.1 Nothing reads positions around `aggregate_performance` — confirmed

`validation.py`: `record_gate_history` (:103), `evaluate_gate` (:117), `performance_report`
(:238), `performance_prompt_block` (:295) each call `aggregate_performance(book)` and read
`agg`; `_calibration_block(agg)` (:163) takes the dict; `_leakage_block` (:215) reads
`LEAKAGE_LOG_FILE`. `daily_trade_message` reads `open_now` directly but scores nothing. No
other production module reads closed rows (`grep '"closed"'` over `trading.py brief.py
validation.py`: validation.py:60 and trading.py:1694 inside `score_settled_theses`, deleted).
The exclusion is therefore total, as the operator decided. Say in the record that the weekly
"Overall" loses 70 scored rows (70 of the 08-29 copy's 206) on the first run after deploy —
the operator will otherwise read the drop as a bug.

### c.2 Enumerators of `_ASSET_CLASSES` — none outside validation

`grep -rn "_ASSET_CLASSES\|\"prediction\""` over `retention.py backup.py supervisor.py
common.py` → nothing. `backup.py` names no prediction-specific file; `retention` prunes dated
files and the signals log only. The host's `gate_history.json` keeps a `prediction` key that
`evaluate_gate` never reads (loops `_ASSET_CLASSES`). Inert, as v1 c.5 said.

### c.3 `["play_type"]` readers

Three: `trading.py:1617` (`_open_prediction_positions`), `trading.py:2221` (`_mtm_prediction`),
`validation.py:395` (`_pred_lines`) — all deleted in Task 4. After Task 4 the only reader is
`p.get(dim)` in `aggregate_performance`, and `play_type` leaves `_DIMENSIONS`. Dropping
`_migrate_position`'s `setdefault("play_type", None)` orphans nothing. No KeyError risk on new
rows. `entry_spread`: readers are `_haircut_fraction` (rewritten) and `_stamp_open_benchmark`
(rewritten) — clean.

### c.4 What still sees the class after Task 4, and is benign

`_watched_instruments` (trading.py:1342) feeds every OPEN row to `fetch_volume` hourly. Until
the retire script runs, that is 25 calls of `fetch_volume("prediction", "3324624")` →
`fetch_quote("3324624")` → `_parse_symbol` returns `None` (no `.market` suffix; every
prediction `instrument` in the book is a bare integer) → `None` → `continue`. No network,
no log line. After the script runs, zero. Fine — but it is the same "works by accident of
`_parse_symbol`" that v1 a.5 flagged; the record should say the retire script is what makes
these paths dead, not the parser.

### c.5 The pieces that will re-propose the feature

Task 6 Step 3 shortens the four `MEMORY.md` lines; good. It does not touch
`MEMORY-history.md`'s multi-asset-trading entry (not auto-loaded — acceptable) or the
`polygram-live-trading-spec.md` body's "NEXT = …" paragraphs (the appended RETIRED line
must come FIRST in that file, not last, or the next reader stops at "NEXT = user pushes and
enables on host").

### c.6 Acceptance grep (objection 3, exact)

As written the Step 8 grep over `.` prints, beyond the allow-list: 47 lines from the
gitignored `.playwright-mcp/*.yml`; `db.py:128 pg_try_advisory_lock`; `tests/test_claim_store.py:45
pg_indexes`; `tests/test_comprehension_schema.py:61,193`, `tests/test_db.py:34,199`,
`tests/test_kb_schema.py:77,744,745,902` (`pg_tables`/`pg_sleep`/`pg_proc`/`pg_namespace`/
`pg_constraint`/`pg_get_constraintdef`); `tests/conftest.py:87` (`PG_A_ENABLED` in a comment);
`tests/test_comprehend_matcher.py:3,4,47` and `tests/test_locking.py:145` (PolyGram prose);
`tests/test_gold_set.py:149` ("a positive prediction", English). Replace with:

```sh
git grep -n -i -E "polygram|prediction|play_type|entry_spread|pred_" -- '*.py' '*.yml' Dockerfile \
  | grep -v "^docs/\|claim_verify.py:180\|brief_memory.py\|thesis_claims"
git grep -n -E "PG_[A-Z]" -- '*.py' '*.yml' Dockerfile | grep -v "^docs/"
```

and pre-register the residual: the script + its test; the `mark_to_market` guard + test; the
`aggregate_performance` clause + both tests; the `_close_ticker`/`/positions` branches + the
`/close` test; `test_validation.py::_closed`'s call sites; `comprehend.py:690`;
`tests/test_gold_set.py:149`. Edit (not allow) `tests/test_comprehend_matcher.py:3,4,47`,
`tests/test_locking.py:145`, `tests/conftest.py:87` — the first two are `polygram` under
`tests/`, which `oh4`'s acceptance criterion forbids.

---

## Things v2 gets right that I checked and will not re-raise

`plan(book, *, today)` signature matches every call; `score()` matches production (b.1);
`Refused` on an open live row is tested and the 08-29 copy has 2 open live rows → the script
would refuse today, which is the intended behaviour and the reason objection 1 matters; the
`_stamp_close_metrics` de-indent in Task 4 is a correct transcription of :690-739 minus the
two prediction branches; `fetch_volume`/`_stamp_open_benchmark`/`_haircut_fraction`/
`price_position` replacements are correct against the current bodies; `_migrate_position`
keeps `setdefault("execution", "paper")`, which the `aggregate_performance` filter relies on;
`daily_trade_message`'s rewrite renders a not-yet-retired prediction row as
`3324624 (prediction) bullish: −x%` rather than crashing; README line references all hold
at `b136f75`; `docker-compose.yml` has one anchor consumer; the 16-knob / 16-line counts
hold; `enrichment/config.py:10` and `tests/test_enrichment_config.py:49-52` are docstring/typo
sites that pass in every intermediate state; the four new `bd` actions are coherent with the
beads as they stand (`rqu` aside — its close reason must carry the SELECT output, b.5).

---

## Ordered fix list

1. **Task 6:** add an operator step BEFORE Step 4 that reads and flips the three `PG_*_ENABLED`
   rows and re-runs the open-live-rows one-liner, with the outputs pasted into `rqu`'s close
   reason. The worker does not push until the operator has said "0 open live rows, flags 0".
   Drop "already true on 2026-09-11" from the runbook text.
2. **Task 3:** move `test_stamp_open_benchmark_prediction` and
   `test_close_picker_labels_prediction_by_question` into Task 3's delete list; add `pgdiag`
   to the `test_prediction.py` pattern list; correct the pre-registration to 23 named (+2
   moved = 25) and 47; move the `mark_to_market` live-guard deletion to Task 4.
3. **Task 4:** `_closed("prediction", 0.5)` / `_closed("equity", 0.1)`; add
   `test_aggregate_performance_excludes_live` (paper row → `equity`) to the edit list; fix the
   stated RED reason for the `mark_to_market` test (it is the log line, not `fetch_price`);
   make "+2 relocated, +4 new" name six tests or say five.
4. **Task 4 Step 8:** replace the acceptance grep with the `git grep` pair in c.6 and its
   pre-registered residual; add `tests/test_comprehend_matcher.py:3,4,47`,
   `tests/test_locking.py:145`, `tests/conftest.py:87` to Step 7's prose edits.
5. **Task 2 / Task 6:** runbook step 2 checks `HAIRCUT_BPS_PREDICTION`'s row is 200 or
   empty before `--apply`; note in the record that "Overall" drops ~70 scored rows on the
   first post-deploy weekly.
6. **Cosmetic:** run `ruff format` on the pasted snippets before committing (five lines
   exceed 88 columns); put the RETIRED line at the TOP of `polygram-live-trading-spec.md`.

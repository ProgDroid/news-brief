# Watch-file Price Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** add a price stage to the 06:30 `watch` job. It judges the watch file's `kind: "price"` rows on completed Yahoo adjusted closes, with the same rules as the portfolio repo's `watch_check.py`, and alerts on crossing (ringing), re-arming when a row stops firing.

**Architecture:** the pure parts go in `watch.py`: the fetcher (with an injected `get`), the completed-bar filter, `evaluate_price`, and price alert planning. `brief.mode_watch` gains a second, isolated stage. A parity script runs the new code against the portfolio repo's `watch_check.evaluate` on real data before deploy. The fetcher lives in `watch.py`, not `brief.py`, so that script can import it under the portfolio venv without news-brief's heavy dependencies.

**Tech Stack:** Python, requests, and the Yahoo v8 chart endpoint; pytest. The parity run uses the portfolio venv (yfinance 1.8.0).

**Spec:** `docs/superpowers/specs/2026-11-19-watch-price-triggers-design.md`. Bead: `news-brief-vap`.

## Global Constraints

- The gate is `ruff check .`, `ruff format --check .` and `py -m pytest -q`. DB-backed tests need `DATABASE_URL`; if Docker is down, CI runs them.
- Never commit, print or log a real row's name, reason or level. Tests use made-up rows; the parity script prints counts and row ids only. **The repo is public.**
- Values: completed bars are those dated strictly before `as_of`'s UTC date. Stale when the last completed bar is more than **5** days before `as_of`. MA window **200**. The stage fails when **more than 25%** of distinct symbols fail to fetch. State keys use the prefix `watch.price.`.
- Use adjusted closes in native units, with **no /100 conversion** for GBp.
- Only FIRED rings (`notify=True`). UNKNOWN goes into the existing silent problems report.
- Tests patch all three send channels, and never touch the network: a canned Yahoo payload goes through an injected `get`.

## Review Focus

1. **A symbol shared by two rows when one fetch fails:** both rows are UNKNOWN, and the symbol counts once toward the 25%. Test in Task 4.
2. **A row whose level is an int in JSON** (`"level": 400`): it compares numerically and prints as `400`. Covered by the loader, which already floats it; asserted in Task 1.
3. **Yahoo returns 200 with an empty `result`, or `timestamp` and closes of different lengths:** the fetcher raises, and the symbol counts as a fetch failure. Test in Task 3.
4. **The last completed bar is exactly 5 days old:** that's not stale. Six days is. Test in Task 1.
5. **Weekend or holiday runs** (the job runs daily): Friday's bar on a Monday is 3 days old, not stale, and a fired row that stays fired doesn't re-ring. Covered by Task 1 (age) and Task 2 (no re-ring).

---

### Task 1: Completed bars and `evaluate_price` (pure)

**Files:** Modify `watch.py`. Test `tests/test_watch_price.py`.

**Interfaces:**
- Consumes: `PriceRow(id, name, action, signal, condition, level)` from `watch.py`.
- Produces:
  - `MA_WINDOW = 200`, `STALE_DAYS = 5`
  - `@dataclass(frozen=True) class PriceVerdict: row_id: str; state: str; detail: str`, where `state` is `"fired" | "not" | "unknown"`
  - `def completed_bars(bars: list[tuple[date, float]], as_of: datetime) -> list[tuple[date, float]]`: sorted by date, keeping bars dated before `as_of`'s UTC date (a naive `as_of` is UTC)
  - `def evaluate_price(row: PriceRow, bars: list[tuple[date, float]] | None, as_of: datetime) -> PriceVerdict`: `None` bars mean the fetch failed

- [ ] **Step 1: Write the failing tests**
  - `test_below_fires_under_the_level_and_not_at_or_over_it`
  - `test_above_fires_over_the_level`
  - `test_above_ma200_uses_the_mean_of_the_last_200_completed_bars`
  - `test_above_ma200_with_199_completed_bars_is_unknown`
  - `test_a_bar_dated_on_the_run_day_is_ignored`: a run-day bar that would flip the verdict must not.
  - `test_last_bar_five_days_old_is_not_stale_six_is`
  - `test_fetch_failure_is_unknown`: `bars=None` gives `state == "unknown"` and "fetch" in the detail.
  - `test_no_completed_bars_is_unknown`
  - `test_gbp_values_compare_without_conversion`: level `120.0` with a close of `111.9` is `below` → fired.
  - `test_detail_reads_like_watch_check`: `"SPY 512.30 vs below 500 (+2.5%) @ 2026-11-18"` for `level=500.0` (prints `500`) and `close=512.30`.
- [ ] **Step 2: Run** `py -m pytest tests/test_watch_price.py -q`. Expected: it fails on import.
- [ ] **Step 3: Implement.** The detail format comes from `watch_check.py`:
  - level rows: `f"{signal} {last:.2f} vs {cond} {level:g} ({100*(last/level-1):+.1f}%) @ {asof}"`
  - ma rows: `f"{signal} {last:.2f} vs 200dma {ma:.2f} ({100*(last/ma-1):+.1f}%) @ {asof}"`
- [ ] **Step 4: Run.** Expected: pass.

### Task 2: Price alert planning, and keeping keys for skipped rows (both stages)

**Files:** Modify `watch.py`. Test `tests/test_watch_price_alerts.py`. Modify `tests/test_watch_alerts.py` (the `plan_alerts` signature).

**Interfaces:**
- Consumes: `PriceRow`, `PriceVerdict`, `STATE_PREFIX`.
- Produces:
  - `PRICE_STATE_PREFIX = "watch.price."`
  - `def price_hash(row: PriceRow) -> str`: sha256 of `f"{signal}|{condition}|{level}"`, first 16 hex characters.
  - `@dataclass(frozen=True) class PriceAlert: row: PriceRow; verdict: PriceVerdict; key: str; value: dict`
  - `def plan_price_alerts(rows: list[PriceRow], verdicts: list[PriceVerdict], state: dict, keep_ids: set[str]) -> tuple[list[PriceAlert], dict, list[str]]`. It returns (alerts to send, re-arm updates `{key: {"hash", "fired": False}}` for `not` rows whose stored `fired` is true or whose hash differs, stale `watch.price.*` keys whose id is not in `keep_ids`).
  - **Changed:** `plan_alerts(rows, verdicts, state, keep_ids: set[str])`, where stale means not in `keep_ids` (previously "not in rows").
  - `def keep_ids(wf: WatchFile) -> set[str]`: every event and price id, plus skipped ids that are not positional (`"#<n>"`).
  - `def format_price_alert(a: PriceAlert) -> str`: `📈 <b>Watch {id}: {name}</b>. FIRED`, then the escaped detail.
  - **Changed:** `format_problems(skipped, invalid, unknown: list[PriceVerdict] = ())`, with lines `• price unknown {id}: {detail}`.

- [ ] **Step 1: Write the failing tests**
  - `test_crossing_fires_once_then_silent_while_fired`
  - `test_not_rearms_and_a_recross_fires_again`
  - `test_edited_level_rearms`
  - `test_unknown_changes_nothing`: no alert, no update, key untouched.
  - `test_removed_row_key_cleared_but_skipped_row_key_kept`, for price.
  - For events, in `test_watch_alerts.py`: `test_skipped_event_row_keeps_its_key`. Update the existing callers to pass `keep_ids`.
  - `test_keep_ids_excludes_positional_skips`
  - `test_format_price_alert_escapes_and_says_fired`
  - `test_format_problems_lists_unknown_prices`
- [ ] **Step 2: Run** both test files. Expected: they fail.
- [ ] **Step 3: Implement**, then update `brief.mode_watch`'s two `plan_alerts` calls to pass `watch.keep_ids(wf)`.
- [ ] **Step 4: Run** both files plus `tests/test_watch_mode.py`. Expected: pass.

### Task 3: The fetcher (in `watch.py`, with an injected `get`)

**Files:** Modify `watch.py`. Test `tests/test_watch_price_fetch.py`.

**Interfaces:**
- Produces:
  - `YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}` (the same value as `trading._YAHOO_HEADERS`)
  - `def fetch_adj_closes(symbol: str, *, get=None) -> list[tuple[date, float]]`: `GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}` with params `{"interval": "1d", "range": "1y"}`, those headers, and `timeout=15`.
    - `get` defaults to `requests.get`, imported lazily.
    - It takes `indicators.adjclose[0].adjclose`, falling back to `indicators.quote[0].close` per bar where the adjclose value is missing.
    - It drops `None` and collapses duplicate dates (UTC date of the timestamp), keeping the last.
    - It applies no unit conversion.
    - It raises `ValueError` on an HTTP error, an empty `result`, or mismatched lengths.

- [ ] **Step 1: Write the failing tests**, all on a canned payload through a fake `get`:
  - `test_uses_adjclose`
  - `test_falls_back_to_close_where_adjclose_missing`
  - `test_drops_none_bars`
  - `test_duplicate_dates_keep_the_last`
  - `test_no_unit_conversion_for_gbp`: `currency=GBp`, close 111.9 → 111.9.
  - `test_empty_result_raises`
  - `test_mismatched_lengths_raise`
  - `test_http_error_raises`
  - `test_request_shape`: the URL, params `{"interval": "1d", "range": "1y"}`, the headers and the timeout.
- [ ] **Step 2: Run.** Expected: it fails on import.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: pass.

### Task 4: The price stage in `mode_watch`

**Files:** Modify `brief.py` (`mode_watch`). Test `tests/test_watch_mode.py`.

**Interfaces:**
- Consumes: Tasks 1–3, `mode_watch`'s existing `_send(text, who, *, notify)`, `load_state`/`save_state`, and `config.clear_runtime_state`.
- Produces:
  - `PRICE_FAIL_LIMIT = 0.25` in `brief.py`
  - `mode_watch(*, as_of=None, post=None, fetch=None)`, where `fetch` defaults to `watch.fetch_adj_closes`

The stage order is events, then prices. Each stage runs in its own `try`: an exception is recorded as a stage failure, and the other stage still runs. At the end, the job raises `RuntimeError` naming the failed stages and any failed sends.

The price stage:
1. Fetch each distinct symbol once. A failure gives that symbol `None`.
2. If more than 25% of symbols failed, raise the stage failure **after** sending FIRED alerts for the rows that did evaluate.
3. Evaluate, then `plan_price_alerts`.
4. Send each alert with `notify=True` and store its key only after a True send.
5. `save_state` the re-arm updates, and clear the stale keys.
6. Collect UNKNOWN verdicts for `format_problems`.

- [ ] **Step 1: Write the failing tests**, with a fake `fetch` mapping symbol to bars or raising:
  - `test_price_fired_rings_and_stores`
  - `test_price_stays_silent_while_fired_then_rearms`
  - `test_shared_symbol_fetched_once`
  - `test_price_fetch_failure_still_sends_event_alerts_and_exits_nonzero`: over 25%.
  - `test_quarter_or_fewer_failures_report_silently_and_exit_zero`: 1 of 4 symbols.
  - `test_shared_symbol_failure_counts_once_toward_the_limit`
  - `test_no_price_rows_makes_no_fetch`
  - `test_unknown_price_rows_appear_in_the_problems_report`
- [ ] **Step 2: Run.** Expected: it fails (no `fetch` parameter).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full gate.** Expected: green. Mutation: delete the re-arm `save_state`, and exactly `test_price_stays_silent_while_fired_then_rearms` should fail.

### Task 5: Pre-deploy parity run (real data, on the operator's PC)

**Files:** Create `scripts/watch_price_parity.py`. It's not shipped behaviour, and it lives in `scripts/` like the other probes. Test `tests/test_watch_price_parity.py` (the comparison logic only).

**Interfaces:**
- Produces `compare(ours: dict[str, str], theirs: dict[str, str], straddle: set[str]) -> dict`, which returns `{"agree": n, "disagree": [ids], "tolerated": [ids], "unknown": n}`.
  - States are mapped to `fired`/`not`/`unknown`.
  - `straddle` holds the ids of `above_ma200` rows within 0.5% of their MA.
  - Rows where either side is `unknown` count as `unknown`, never as agree.
- CLI: `<portfolio>/.venv/Scripts/python.exe scripts/watch_price_parity.py --portfolio <path>`.
  - It reads the portfolio repo's `data/watchlist.csv`, keeping open rows that are not events.
  - It runs `watch_check.evaluate(row)` and the new `fetch_adj_closes` + `evaluate_price` with `as_of` = now.
  - **It prints only the counts and the disagreeing or tolerated row ids.**
  - Exit 0 with no disagreements, 1 with any.

- [ ] **Step 1: Write the failing tests for `compare`**: agree, disagree, tolerated straddle, and unknown never counting as agree.
- [ ] **Step 2: Run.** Expected: fail. **Step 3: Implement. Step 4: Run.** Expected: pass.
- [ ] **Step 5: Run the parity script under the portfolio venv.** Expected: `disagree: []`. On any disagreement, stop and diagnose (systematic-debugging) before deploy; never widen the tolerance to pass.

### Task 6: Ship

- [ ] Final whole-branch review (a fresh reviewer, the most capable model), then one fix pass.
- [ ] Update the watch memory (price stage, no new job, parity result) and the spec status line.
- [ ] Commit, push and watch CI; then the operator redeploys. Expect a one-off burst of FIRED alerts on the first 06:30 run.
- [ ] Close `news-brief-vap` once the first morning's `/jobs` row shows `watch` exit 0 with the price stage run (the log line shows the price row count).

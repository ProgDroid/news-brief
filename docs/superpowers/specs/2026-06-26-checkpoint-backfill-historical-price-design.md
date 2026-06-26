# Spec: Historical crossing-date prices for paper checkpoints

**Date:** 2026-06-26
**Status:** Approved (design) — pending implementation plan
**Area:** trading.py (paper mark-to-market / performance layer)
**Backlog item:** "checkpoint-backfill price skew" — item #1 of the deferred-findings OPEN BACKLOG
(`docs/superpowers/...`, memory `newsbrief-deferred-findings`).

## Problem

Paper positions record `1w`/`2w`/`4w` horizon checkpoints in `mark_to_market`
(`trading.py`). A checkpoint is recorded the first time a mark observes
`days_open >= threshold` (`_record_checkpoints`), and it stores **the price at the
moment of that mark**, not the close on the true crossing date
(`entry_date + 7/14/28`).

`mark_to_market` is called **only** in the weekly job (`brief.py:2671`), while
positions open **daily** (`mode_paper`). Two consequences:

1. **Structural weekly lag.** Even with no missed run, a checkpoint is recorded
   at the next *weekly* mark — routinely 0–6 days after the true crossing date,
   at whatever price that mark caught.
2. **Missed-run skew.** A skipped weekly run widens the gap further; the original
   finding flagged this case ("uses today's price for past horizons").

Because the `4w` checkpoint also drives the position close
(`PAPER_CLOSE_HORIZON`, `trading.py:1563`) and sets `realized_return`, the skew
feeds directly into the paper performance stats that the self-improving-trading
roadmap's Stage A measurement consumes.

## Goal

Record each crossed checkpoint at the **close on its true crossing date**
(`entry_date + threshold`), for the asset classes that have a real historical
price source, regardless of when the mark actually runs. Fall back safely and
visibly when no historical price is available.

### Scope decisions (settled during brainstorming)

- **Accuracy target:** the *full fix* — true crossing-date close always, not just
  large/missed-run gaps.
- **Asset classes:** `equity`, `index`, `crypto` get historical prices.
  `prediction` keeps current-price behavior (no clean historical source).
- **Fallback:** when the historical price is genuinely unavailable, record the
  checkpoint at the **current mark price** and **flag it approximate**
  (`price_basis: "current"`). Never block a checkpoint; never stall the 4w close.

### Non-goals (YAGNI)

- No historical prediction-market prices.
- No new scheduled/daily mark cadence (rejected alternative; historical lookup
  makes the weekly cadence accurate enough).
- No reporting-side change in v1 — `performance_report` keeps consuming checkpoint
  returns as-is; the new `price_basis` flag is recorded for transparency and
  future use, not acted on yet.
- No book migration — the new field is additive; absent = legacy/current.
- No reuse of `backtest/prices_yf.py` (it imports `yfinance`, deliberately kept
  out of the live cron/CI image; uses different symbol identifiers; no crypto).

## Approach (A — chosen)

Per open equity/crypto/index position, when at least one checkpoint has **newly
crossed** this run, fetch that position's daily close **series once** over
`[entry_date, today]` via a dependency-free REST extension of the existing live
pricers, then derive each newly-crossed checkpoint's price by snapping its
crossing date to the last close on/before it.

This is one network call per position per run (only when something crossed),
reuses the existing endpoints / symbol mapping / GBp conversion, and stays
`requests`-only (no `yfinance` in the live path).

## Components

### New REST series-fetchers (requests-only, mirror existing pricers)

- `_yahoo_closes(yahoo_symbol: str, start: str, end: str) -> dict[str, float]`
  - Same `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}` endpoint as
    `_yahoo_fetch`, but params `interval=1d`, `period1`/`period2` (unix seconds for
    `start` 00:00 UTC and `end`+1d to make the range inclusive).
  - Parse `result[0].timestamp[]` zipped with `indicators.quote[0].close[]` into a
    `{ "YYYY-MM-DD": close }` map; date = `datetime.fromtimestamp(ts, tz=utc)`
    formatted. Skip null closes.
  - Apply the same `GBp` → `GBP` `/100` conversion via `meta.currency`.
  - Returns `{}` on any network/parse failure (logged at warning, like `_yahoo_fetch`).
- `_kraken_closes(pair: str, since: str) -> dict[str, float]`
  - `https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440&since={unix}`.
  - Kraken keys the result by canonical pair name → take the single non-`"last"`
    entry's candle list; each candle `[time, open, high, low, close, ...]` →
    `{date: float(close)}`.
  - Returns `{}` on error array / empty / parse failure.

### Dispatcher + pure snap helper

- `historical_closes(asset_class: str, instrument: str, start: str, end: str) -> dict[str, float]`
  - `equity` → `_yahoo_closes(_yahoo_format_symbol(*_parse_symbol(instrument)), …)`
    (returns `{}` if `_parse_symbol` is `None`).
  - `index` → `_yahoo_closes(instrument, …)` (instrument is already a raw Yahoo symbol).
  - `crypto` → `_kraken_closes(instrument, start)`.
  - anything else (incl. `prediction`) → `{}`.
- `_snap_close(closes: dict[str, float], target_date: str) -> float | None`
  - Pure. Returns the close for the latest date key `<= target_date`, or `None`
    when no key is `<= target_date` (target precedes available history) or the map
    is empty. Lexical date-string comparison is valid for `YYYY-MM-DD`.

### Rewrite of `_record_checkpoints`

Today it takes a single `(today_str, price, ret)` and writes that to every newly
crossed checkpoint. It is rewritten to compute **per-checkpoint** price/return:

- New inputs (final signature settled in the plan): the position `p`, `today_str`,
  the current mark `price`/`ret` (fallback values), `days_open`, and the prefetched
  `closes` map for the position (`{}` when none was fetched / available).
- For each `label, threshold` in `PAPER_HORIZONS` not yet recorded with
  `days_open >= threshold`:
  - `crossing_date = entry_date + threshold` (UTC date arithmetic).
  - `hist = _snap_close(closes, crossing_date)`.
  - if `hist is not None`:
    `cp = {date: crossing_date, price: hist, return: _signal_return(direction, entry_price, hist), price_basis: "historical"}`
  - else (fallback):
    `cp = {date: today_str, price: price, return: ret, price_basis: "current"}`
  - `p["checkpoints"][label] = cp`

### Caller wiring (`mark_to_market`, equity/crypto branch)

Before calling `_record_checkpoints`, determine whether any checkpoint is newly
crossed; if so, fetch `closes = historical_closes(asset_class, instrument, entry_date, today_str)`
once and pass it in. If nothing newly crossed, skip the fetch (pass `{}`). The 4w
close trigger and `realized_return` assignment are unchanged structurally — they
now read a more accurate `return` when it is historical.

### Prediction path (`_mtm_prediction`)

Its `_record_checkpoints` call passes `closes={}`, so every prediction checkpoint
records `price_basis: "current"` — uniform schema, no behavior change otherwise.

## Checkpoint schema (additive, back-compatible)

```
checkpoints[label] = {
  "date":   "YYYY-MM-DD",   # crossing date (historical) or mark date (current)
  "price":  float,
  "return": float,
  "price_basis": "historical" | "current",   # NEW
}
```

Existing live checkpoints lack `price_basis`; all readers treat **absent =
"current"** (legacy). No migration, no structural change to close/realized-return
logic.

## Error handling / fallback

- Any network failure, empty/garbled payload, unresolved symbol, or crossing date
  before available history → that checkpoint falls back to current price +
  `price_basis: "current"`. Consistent with the pricing layer's "None = skip,
  never guess" convention.
- One `historical_closes` call per position per run, only when a checkpoint newly
  crossed → negligible added latency in the already network-heavy weekly job.
- Fail-safe: the delivered weekly summary and the book's structural integrity are
  never affected by a historical-fetch failure.

## Testing

- `_snap_close` — pure unit tests: exact-date hit; weekend/holiday snap to prior
  close; target before all history → `None`; empty map → `None`.
- `_yahoo_closes` / `_kraken_closes` — parse tests with monkeypatched
  `requests.get` against captured fixture payloads: normal series, GBp conversion,
  null-close skipping, error/empty payload → `{}`.
- `mark_to_market` integration (monkeypatch `historical_closes`):
  - newly-crossed checkpoint records historical price/return + `price_basis: "historical"`;
  - fetch-miss path records current price + `price_basis: "current"`;
  - a 4w cross still closes the position, with the historical `realized_return`;
  - no fetch occurs when nothing newly crossed.
- Network fetchers are not exercised in CI (matches existing live pricers — only
  their parse/transform is covered).

## Affected files

- `trading.py` — new fetchers/dispatcher/snap helper; `_record_checkpoints`
  rewrite; `mark_to_market` + `_mtm_prediction` wiring.
- `tests/test_paper.py` / `tests/test_trading.py` (and/or a new
  `tests/test_checkpoint_backfill.py`) — the cases above.
- No Dockerfile/workflow changes (no new top-level module; all within `trading.py`).
```

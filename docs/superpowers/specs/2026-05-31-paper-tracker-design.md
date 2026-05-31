# Paper trade tracker — design

**Date:** 2026-05-31
**Scope:** A pure-paper (no money, no orders) trade tracker in `brief.py` that opens
notional positions from medium/high-confidence directional signals, marks them to market
weekly, and scores signal quality. Single file, no new dependencies.
**Depends on:** the signals feature (`signals-YYYY-MM-DD.json` snapshots) — already implemented.

## Goal

Answer, over time: *are my medium/high-confidence directional calls right?* — via hit-rate
and percentage returns measured at fixed horizons. "Both, pragmatically" was scoped down to
**returns + hit-rate only** (no notional currency figure), which is clean and FX-neutral.

## The crux: price resolution for unheld tickers

Trading212's public API has **no quote endpoint for instruments you don't hold**
(`currentPrice` appears only in `/equity/portfolio` and `/equity/portfolio/{ticker}`, and the
latter 404s with no open position). Paper positions are, by definition, in unheld tickers, so
prices must come from outside T212.

**Chosen source: Stooq light CSV quote** (`GET https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv`).
Free, no API key, uses `requests`. Verified live during design:
- `aapl.us` → `Close 312.06`; `shel.uk` → `Close 3110.5` (GBX pence).
- A bad/unknown symbol returns `N/D` in every field — a clean, parseable "not found" signal.

Rejected alternatives: Yahoo `query1` (increasingly blocks headless/no-crumb requests — unsafe
for an unattended container); Claude web-search at mark time (entry and mark would come from
different model calls → not internally consistent → P&L meaningless).

**Internal consistency is the hard requirement:** entry price and every mark for a position come
from Stooq, in the instrument's own currency. P&L is expressed as a **return ratio**
(`mark / entry − 1`), so units/currency cancel — no FX conversion needed, and mixed-currency
positions aggregate cleanly as percentages.

### Symbol mapping (T212 ticker → Stooq symbol)

T212 tickers look like `AAPL_US_EQ`, `SHEL_US_EQ`. Resolution order in
`resolve_stooq_symbol(ticker, cache, overrides)`:

1. **Manual override** — if `ticker` in `ticker_map.json` (same manual-annotation pattern as
   `theses.json`), use that Stooq symbol. Authoritative.
2. **Derived** — `base = ticker.split("_")[0].lower()`, then a market suffix from the cached
   instrument's `currencyCode` (`USD→us`, `GBX`/`GBP→uk`, `EUR`→ISIN country `DE→de`/`FR→fr`).
   Candidate = `f"{base}.{suffix}"`.
3. If neither yields a candidate → **unresolved**.

The instrument cache (`instruments-cache.json`, `{fetched_at, instruments: {ticker: {isin,
currencyCode}}}`) is populated from `GET /equity/metadata/instruments` (rate-limited 1 req/50s,
but one call returns the full list). It is refreshed in the weekly job, and **lazily refreshed
by `mode_paper`** when the cache is missing or older than 14 days (a single rate-limited call).
If `T212_API_KEY` is unset, the cache can't be built and mapping relies on `ticker_map.json`
overrides only (derivation needs `currencyCode` from the cache).

**Skip-and-log, never guess:** if the symbol is unresolved, or Stooq returns `N/D`, or the
signal's `ticker` is null (macro-level) — the position is **not opened** (or, at mark time, left
open and retried next week) and a one-line reason is logged. A plausible wrong price is worse
than a missing one.

## Lifecycle (hybrid, with manual close)

- **Open** (`mode_paper`, daily): from today's `signals-YYYY-MM-DD.json`, take signals with
  `direction ∈ {bullish, bearish}`, `confidence ∈ {medium, high}`, and non-null `ticker`.
  **Dedup:** skip a candidate if an *open* position already exists for the same
  `(ticker, direction)` — measures "did this call work", not "how often it was repeated".
  Resolve the Stooq symbol and fetch the entry price; on any failure, skip + log. Otherwise append
  an open position. `mode_paper` is **called at the end of `mode_collect`** (no new cron),
  remains runnable standalone, and is **added to the dispatch dict** (fixing its current
  unreachable state).
- **Mark to market** (weekly job): for each open position, fetch the current Stooq price, compute
  the return, update `last_mark`. Record any not-yet-recorded horizon checkpoints whose elapsed-day
  threshold has been crossed. **Auto-close at the 4w horizon** (`close_reason: "horizon"`).
- **Manual close** (`/close TICKER` Telegram command): close open position(s) for that ticker at
  the current Stooq mark (`close_reason: "manual"`). If Stooq returns `N/D`, report to Telegram and
  leave the position open.

**Horizons:** `{"1w": 7, "2w": 14, "4w": 28}` days, measured from `entry_date`. Because marking is
weekly, a checkpoint is recorded at the first weekly run where `days_open ≥ threshold` (so it may
land a few days late — acceptable for an indicative tracker). If a weekly run is missed, all
crossed-but-unrecorded checkpoints are recorded at that run using the current price.

**Return convention:** `sign = +1 if direction == "bullish" else −1`;
`return = sign × (price / entry_price − 1)`; a position is a "hit" when `return > 0`.

## Data model

`paper-book.json` = `{"positions": [ ... ]}`, rewritten on each update (positions mutate —
checkpoints and closes can't be expressed in the append-only `.jsonl` the WIP `mode_paper` used).
Each position:

```json
{
  "id": "2026-05-31:SHEL_US_EQ:bullish",
  "opened": "2026-05-31",
  "ticker": "SHEL_US_EQ",
  "stooq_symbol": "shel.us",
  "direction": "bullish",
  "confidence": "high",
  "topic": "hormuz-disruption",
  "thesis_ref": "oil supply tightness",
  "rationale": "…",
  "entry_price": 312.06,
  "entry_date": "2026-05-31",
  "status": "open",
  "close_reason": null,
  "closed_date": null,
  "checkpoints": {
    "1w": {"date": "…", "price": 0.0, "return": 0.0}
  },
  "last_mark": {"date": "…", "price": 0.0, "return": 0.0}
}
```

(The existing append-only `paper-book.jsonl` from the WIP is replaced by this; the old `mode_paper`
body is rewritten.)

## Scorecard (weekly Telegram section)

Computed from `paper-book.json` in the weekly job and appended to the weekly delivery:

- **Hit-rate** (return > 0) overall and split by `confidence` (medium vs high), computed over
  closed positions' realized (4w) returns; open positions reported separately on their latest mark.
- **Mean return** at each horizon (1w/2w/4w) across positions that reached it.
- A short list of recently closed positions with their realized return and close reason.
- Counts: open / closed / skipped-this-cycle.

No currency figure. All numbers are percentages.

## Modes / integration points

- `mode_paper()` — rewritten; called from end of `mode_collect`, added to dispatch dict.
- `mode_weekly()` — refresh instrument cache, mark open book to market + record checkpoints +
  auto-close at 4w, build the scorecard, append it to the weekly Telegram message.
- `process_telegram_commands()` — handle `/close TICKER`; add `/close` to `HELP_TEXT`.
- New helpers (next to the Trading212 section): `fetch_stooq_price(stooq_symbol) -> float | None`
  (returns None on `N/D`/error), `resolve_stooq_symbol(...)`, `load/refresh_instruments_cache()`,
  `load/save_paper_book()`, `mark_to_market()`, `paper_scorecard()`.

## Deliberately excluded (scope control)

- No real orders (`/equity/orders/*` is never called).
- No notional currency P&L (returns + hit-rate only).
- No event-based reversal close (manual `/close` covers the discretionary case).
- No intraday prices (Stooq EOD is sufficient for a weekly mark).

## Verification

Same stubbed-exec harness as the signals work (stub `feedparser`/`requests`, redirect `/app/logs`),
plus a stubbed `fetch_stooq_price` so no network is needed in tests:

- `resolve_stooq_symbol`: override wins; `USD→.us`, `GBX/GBP→.uk` derivations; unresolved → None.
- Stooq CSV parse: extracts `Close`; `N/D` row → None.
- `mode_paper` open: filters med/high directional non-null; dedups `(ticker, direction)`; skips +
  logs unresolved/`N/D`/null-ticker; appends well-formed positions.
- `mark_to_market`: correct return sign for bullish/bearish; records 1w/2w/4w at the right
  elapsed-day thresholds; closes at 4w; leaves position open on `N/D`.
- `/close`: closes matching open positions at current mark; no-ops with a message on `N/D`.
- `paper_scorecard`: hit-rate and mean-return math correct on a fixture book.
- `py_compile` + `ruff check` clean.

## Privacy boundary

Intact. Paper positions carry only tickers, Stooq symbols, public prices, and percentage returns —
no synthetic-or-real monetary amounts. Stooq data is public. T212 `/equity/metadata/instruments`
returns the platform's tradable-instrument catalogue (ISIN/currency/name), **not** the user's
holdings or any monetary value. No absolute amount from the account enters any paper file or prompt.
The portfolio-weights privacy logic (`fetch_portfolio_weights`) is untouched.

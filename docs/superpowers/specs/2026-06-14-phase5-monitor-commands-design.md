# Phase 5 — Volume Monitor + New Telegram Commands — Design

**Date:** 2026-06-14
**Status:** Approved for planning
**Parent spec:** `docs/superpowers/specs/2026-06-13-multi-asset-trading-polygram-design.md`
(this phase implements that spec's `monitor` cron and the four new `commands`-mode
Telegram commands; Phases 1–4 are complete).

## Goal

Close out the multi-asset trading build by adding the two remaining components from the
parent spec:

1. A new **`monitor`** cron mode (hourly): cross-asset volume-anomaly alerts over watched
   instruments and open positions, fully decoupled from the brief's critical path.
2. Four new **Telegram commands**: `/watch`, `/unwatch`, `/positions`, `/performance`.

Both extend existing seams (pricers, the unified book, `validation.py`'s reporting) rather
than introducing new subsystems.

## Verified context (current code)

- Pipeline is stateless one-shot container runs via host cron (`docker compose run --rm
  newsbrief-<mode>`): `submit`, `collect`, `weekly`, `commands`. There is no daemon. The
  entry dispatch in `brief.py` maps mode → `mode_*` and wraps every run in a last-resort
  `telegram_alert` on uncaught crash.
- **`trading.py`** owns the trading subsystem. `fetch_stooq_price` requests the Stooq CSV
  with format `sd2t2ohlcv` — the response already includes **Volume** as column 7
  (`Symbol,Date,Time,Open,High,Low,Close,Volume`; `cols[6]`=Close, `cols[7]`=Volume).
  `fetch_kraken_price` calls the public `Ticker` endpoint whose result entry exposes 24h
  volume at `v[1]`. So volume needs new *parsers over existing calls*, not new endpoints.
- `load_book` / `save_book` manage the unified `book.json`; positions carry `asset_class`,
  `venue`, `instrument`, `status`, `entry_price`, `direction`, etc. `price_position` marks a
  position; `_signal_return` computes percentage return.
- **`validation.py`** already exposes `performance_report(book) -> str` (full Telegram-HTML
  report: overall + dimensions + go-live gate), reused verbatim by `/performance`.
- Telegram commands are dispatched in `brief.py`'s `_handle_telegram_update` (existing:
  `/focus /mute /note /close /reset /status /help /thesis /dig`). `/close` is the precedent
  for a command that reads `book.json` directly. User text is `html.escape()`d before being
  echoed back (Telegram 400s on malformed HTML). `HELP_TEXT` lists the commands.
- Shared infra in **`common.py`**: `_write_json_atomic`, `_load_json_or`, `telegram_send`,
  config/paths. `DATA_DIR` is env-gated for import-safe local runs.
- The `Dockerfile` already `COPY`s `common.py trading.py` alongside `brief.py` — no build
  change is required for logic added to those modules.

## Design decisions (settled in brainstorming)

1. **Cross-asset watchlist.** One `watchlist.json` holds equity tickers, crypto symbols, and
   prediction markets — not a prediction-only file. **This supersedes the parent spec's
   `polygram_watchlist.json`**, which assumed prediction-only watching.
2. **Anomaly rule = ratio + floor + warm-up.** Alert when `current / trailing-mean ≥
   multiplier`, gated by an optional absolute floor and a minimum trailing-sample count.
   Chosen over z-score (noisier on thin/zero-volume instruments, less intuitive to hand-tune)
   and ratio-only (thin instruments would false-trigger).
3. **Per-instrument cooldown for dedup.** A stored `last_alert_ts` with a configurable
   cooldown window. Uniform across asset classes: daily-resolution equity volume naturally
   fires once; intraday crypto can re-fire after the cooldown. Chosen over new-bar dedup
   (needs a per-pricer "bar id" *and* still needs a cooldown for crypto/prediction) and
   per-asset cadence (couples the monitor to venue market calendars).
4. **`/watch` infers asset class, with an optional explicit override.** Inference order:
   crypto map → equity resolver → prediction. The confirmation echoes the resolved class so a
   wrong guess is visible; `/watch crypto BTC` / `/watch prediction <slug>` force it. Chosen
   over always-require-a-flag (rejected for friction) and never-allow-a-flag (no way to
   correct a misfire). Prediction markets cannot be inferred from a ticker-shaped token, so
   watching one requires the explicit `prediction` keyword or a slug.
5. **Prediction volume degrades gracefully.** If `/api/prices-history` does not expose a
   volume field, that instrument is logged and skipped — the phase is not blocked on
   confirming the field exists, and the monitor never crashes on its absence.

## Architecture

### Module placement

- **`trading.py`** — volume parsers (`fetch_stooq_volume`, `fetch_kraken_volume`,
  `fetch_pg_volume`), the watchlist accessors (`load_watchlist` / `save_watchlist` and the
  `/watch` resolution helper), and `run_volume_monitor()` (pure-ish: returns alert strings,
  reads/writes `volume-history.json`).
- **`brief.py`** — `mode_monitor()` wrapper; the four command branches inside
  `_handle_telegram_update`; `HELP_TEXT` update; `mode_monitor` added to the entry dispatch
  and usage string.
- **`validation.py`** — unchanged; `performance_report` is reused.

### Volume monitor

`mode_monitor()` calls `run_volume_monitor()`, which returns a list of alert strings; the
mode sends **one silent Telegram message** if the list is non-empty (omitted entirely if
empty). The mode is its own cron entry, structurally decoupled from the brief — a monitor
failure cannot duplicate or delay the brief. The entry dispatch's existing crash handler
provides the last-resort alert; additionally, **each instrument is wrapped in its own
try/except** so one bad fetch cannot abort the sweep.

**Watched set:** the union of (a) `watchlist.json` entries and (b) the `instrument`s of all
`status == "open"` positions in `book.json`, deduped by `(asset_class, instrument)`.

**Per-asset volume fetchers** (new, siblings of the pricers, same None-on-failure discipline):

| Asset | Fetcher | Source |
|---|---|---|
| equity | `fetch_stooq_volume` | column 7 of the existing Stooq CSV |
| crypto | `fetch_kraken_volume` | Kraken `Ticker` result `v[1]` (24h) |
| prediction | `fetch_pg_volume` | `/api/prices-history`; returns `None` (skip) if no volume field |

**Baseline — `volume-history.json`** under `DATA_DIR`, written via `_write_json_atomic`.
Shape:

```json
{
  "equity:shel.uk": { "samples": [123456, 130000, ...], "last_alert_ts": "2026-06-14T10:00:00Z" },
  "crypto:XBTUSD":  { "samples": [...], "last_alert_ts": null }
}
```

On each run, per instrument: fetch current volume, then **append it only if it differs from
the most recent stored sample** (consecutive-duplicate dedup — so daily-resolution equity
volume contributes one sample per day, not one per hour), capping `samples` at
`VOL_TRAILING_N`.

**Anomaly rule.** Let `prior = samples` *before* appending the current value and
`baseline = mean(prior)`. Emit an alert for the instrument when **all** hold:
- `len(prior) >= VOL_MIN_SAMPLES` (warm-up; cold start never alerts),
- `current >= floor` for that asset class (`VOL_FLOOR_<CLASS>`, default 0 = off),
- `baseline > 0` and `current / baseline >= VOL_SPIKE_MULT`.

**Cooldown.** Suppress the alert if `now - last_alert_ts < VOL_ALERT_COOLDOWN_HRS`. When an
alert fires, set `last_alert_ts = now`. (Sample appending happens regardless of cooldown so
the baseline keeps tracking.)

**Alert line** (per instrument): asset class, instrument, current vs baseline, and the ratio
— e.g. `📈 crypto XBTUSD: 4.1× avg volume (1.2M vs 0.3M)`.

### New Telegram commands (in `_handle_telegram_update`)

- **`/watch <symbol>`** — resolve asset class (inference order crypto → equity → prediction;
  optional explicit `/watch <class> <symbol>` override), resolve the venue instrument via the
  existing resolver for that class, append `{raw, asset_class, instrument, added}` to
  `watchlist.json` (no duplicates). Confirmation echoes the resolved class and instrument so a
  misfire is visible. Prediction requires the explicit `prediction` keyword or a slug.
- **`/unwatch <symbol>`** — remove the watchlist entry whose `raw` matches (case-insensitive);
  report if nothing matched.
- **`/positions`** — load the book, filter to `status == "open"`, group by `asset_class`, and
  for each show the instrument, current mark (via `price_position`) and unrealized return (via
  `_signal_return`). Unpriceable positions render the mark/return as `—`. "No open positions"
  when empty.
- **`/performance`** — `performance_report(load_book())`, sent via the existing
  `split_html_message` chunking if long.

`HELP_TEXT` is updated to list all four.

## State, config & wiring

**New persisted state under `DATA_DIR`** (atomic writes, `_load_json_or` defaults):
- `watchlist.json` — cross-asset watchlist.
- `volume-history.json` — trailing samples + `last_alert_ts` per instrument.

**New env knobs** (all optional, with defaults; documented in `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `VOL_SPIKE_MULT` | `2.5` | ratio threshold (current / trailing mean) |
| `VOL_TRAILING_N` | `20` | max trailing samples retained per instrument |
| `VOL_MIN_SAMPLES` | `5` | warm-up: min prior samples before any alert |
| `VOL_ALERT_COOLDOWN_HRS` | `12` | per-instrument re-alert suppression window |
| `VOL_FLOOR_EQUITY` | `0` | absolute volume floor, equities (0 = off) |
| `VOL_FLOOR_CRYPTO` | `0` | absolute volume floor, crypto (0 = off) |
| `VOL_FLOOR_PREDICTION` | `0` | absolute volume floor, prediction (0 = off) |

**Wiring:**
- Add `mode_monitor` to the `brief.py` entry dispatch and the usage string.
- Add a `newsbrief-monitor` service to `docker-compose.yml`, mirroring the sibling services,
  with `command: monitor`.
- Document the **hourly host cron** (`docker compose run --rm newsbrief-monitor`) in
  `README.md`, alongside the existing mode crons.
- No `Dockerfile` change (already copies `common.py`/`trading.py`).

## Testing approach

New `tests/test_monitor.py` plus additions to the command tests; mirrors existing patterns
(env-gated `DATA_DIR`, stubbed HTTP, `pytest -q`).

- **Volume parsers:** Stooq column-7 parse, Kraken `v[1]` parse, prediction parse, and the
  prediction **no-volume graceful-skip** path; None on network/garbled input (parity with the
  pricers).
- **Anomaly rule:** ratio at/over and under threshold; floor gating; warm-up (fewer than
  `VOL_MIN_SAMPLES` prior samples → no alert); consecutive-duplicate dedup keeps the baseline
  from filling with repeated daily equity values.
- **Cooldown:** a second run within the window is suppressed; after the window it re-fires;
  samples still append during the cooldown.
- **Watchlist:** add with inferred class, add with explicit override, duplicate is a no-op,
  `/unwatch` removes, `/unwatch` of a missing entry reports cleanly.
- **`/positions`:** open-only filter, multi-asset grouping, unpriceable position shows `—`.
- **`/performance`:** smoke test that it wraps `performance_report`.
- **Failure isolation:** one instrument's fetcher raising does not abort the monitor run; the
  monitor mode is independent of the brief delivery path.

## Out of scope (v1)

- Live order placement and SSE streaming (unchanged from the parent spec).
- Automated threshold tuning (the `VOL_*` knobs are hand-tuned config).
- Market-calendar awareness for the monitor (the cooldown handles cadence instead).
- Backfilling historical volume into the baseline — the trailing window warms up live.

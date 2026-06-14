# Phase 4 — Validation / Performance Layer (design)

**Date:** 2026-06-14
**Governing spec:** `docs/superpowers/specs/2026-06-13-multi-asset-trading-polygram-design.md`
(sections: *Validation + improvement loop*, *Delivery (Telegram)*, *Pipeline placement*).
**Prior phases:** `plans/2026-06-13-phase1-module-extraction.md`,
`plans/2026-06-13-phase2-unified-book-crypto.md`,
`plans/2026-06-13-phase3-prediction-polygram.md`.

Phase 4 turns the primitive weekly `paper_scorecard` into a real validation/performance
layer: dimensional aggregation, a per-trade benchmark and cost haircut, a per-asset-class
go-live readiness gate, a performance-feedback prompt block, and the unified daily trade
message. Paper-only throughout — no live order placement (the `LiveExecutor` stays unbuilt
behind the gate). Phase 5 (volume monitor cron + new Telegram commands) remains out of scope.

## Foundational decisions (resolved during brainstorming)

1. **Data store = `book.json` (single source of truth).** Closed positions already persist
   forever in `book.json` carrying `realized_return`, `confidence`, `asset_class`,
   `play_type`, `thesis_ref`, `checkpoints`, `closed_date`. No `performance.json` is
   introduced (the governing spec's `performance.json` is **superseded** — it would
   duplicate the book and risk drift). Benchmark/haircut results are **stamped onto the
   closed position** at close time.
2. **Benchmark = market index per asset class.** Equity → `^SPX` (Stooq); crypto → BTC/XBT
   (Kraken); prediction → naive coin-flip baseline (`benchmark_return = 0`). Implemented by
   **stamping the benchmark entry level at open** and fetching the current level at close
   (same pattern as `entry_price`) — avoids historical price lookups. beta=1 simplification.
3. **Cost haircut = config bps per asset class + real prediction orderbook.** Static
   round-trip bps for equity/crypto; for predictions, capture the real
   `/api/orderbook/:tokenId` half-spread at **open** (market is live, `token_id` already
   stored) and stamp it as that trade's entry cost, falling back to config bps if
   unavailable. Resolution trades settle at 1/0 (no exit spread); momentum exits use config
   bps.
4. **Go-live gate = readiness indicator (moderate default).** Nothing auto-enables live
   trading (the `LiveExecutor` is unbuilt); the gate is reported in the weekly job. Per asset
   class: **≥30 closed trades AND mean net-of-haircut edge over benchmark > 0 AND net
   hit-rate ≥ 55%, with edge positive in the two most recent weekly evaluations.** All
   thresholds configurable.
5. **Analysis code = new `validation.py` module.** Pure functions over a `book` dict
   (`common ← validation`, `brief ← validation`). The existing `paper_scorecard` moves here
   and is superseded by the enhanced report. Benchmark/haircut **stamping** stays in
   `trading.py` (lifecycle).

## Module boundaries

| File | Phase-4 responsibility | Imports |
|------|------------------------|---------|
| `common.py` | New config constants (haircut bps, gate knobs) | — |
| `trading.py` | Open-time stamping (`benchmark_entry`, `entry_spread`); close-finalizer `_stamp_close_metrics`; benchmark/orderbook fetch helpers | `common` |
| `validation.py` (new) | `aggregate_performance`, `evaluate_gate`, enhanced report (`paper_scorecard` relocated + superseded), `performance_prompt_block` | `common` |
| `brief.py` | Wire enhanced report into `mode_weekly`; inject perf block into `build_daily_prompt`; unified daily trade message in `mode_collect` | `common`, `trading`, `validation` |

The one-way dependency chain is preserved: `common ← trading`, `common ← validation`,
`{trading, validation} ← brief`. `trading.py` does **not** import `validation.py`
(stamping is self-contained in `trading.py`, so no cycle).

## Position model additions (new nullable fields)

All additive and nullable; legacy positions degrade gracefully. Field shapes stay consistent
across creation (`mode_paper` / `_open_prediction_positions`), close
(`_stamp_close_metrics`), and read (`validation.py`).

**Stamped at open** (best-effort; `null` on fetch failure, never blocks an open):

| Field | Equity | Crypto | Prediction |
|-------|--------|--------|------------|
| `benchmark_entry` | `^SPX` level | BTC/XBT price | `null` |
| `entry_spread` | `null` | `null` | `/api/orderbook/:tokenId` half-spread (else `null`) |

**Stamped at close** by `_stamp_close_metrics(p, day)` (called from every close path):

| Field | Definition |
|-------|------------|
| `haircut` | Round-trip cost fraction. Config bps by `asset_class`; prediction uses `entry_spread` when present, else `HAIRCUT_BPS_PREDICTION`. |
| `net_return` | `realized_return − haircut`. |
| `benchmark_return` | Long-sense move of the index/coin over `[entry, close]` (current level fetched at close vs `benchmark_entry`). `0` for prediction. `null` if `benchmark_entry` missing (legacy) or fetch fails. |
| `edge` | `net_return − benchmark_return`; `null` when `benchmark_return` is `null`. |

**Benchmark sense:** the benchmark is always *buy-and-hold the index/coin* (long):
`benchmark_return = (level_close − benchmark_entry) / benchmark_entry`. `edge = net_return −
benchmark_return` (beta=1). A bearish trade in a falling market therefore earns positive edge
above market beta, which is the intended "skill vs beta" separation.

## Close-path hooks (3 sites, all in `trading.py`)

A single finalizer `_stamp_close_metrics(p, day)` is invoked **after** `status` is set to
`"closed"` in each of:

1. `_close_position_at_market` — reversal closes + `/close` command (equity/crypto).
2. `mark_to_market` — horizon (4w) close (equity/crypto).
3. `_settle_prediction` — momentum target/horizon + resolution settlement (prediction).

The finalizer is best-effort on the benchmark fetch: a fetch failure leaves
`benchmark_return`/`edge` = `null` but still stamps `haircut`/`net_return` and lets the close
proceed. It must never raise out of a close path.

## Config (env, defined in `common.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `HAIRCUT_BPS_EQUITY` | `10` | Equity round-trip cost (bps). |
| `HAIRCUT_BPS_CRYPTO` | `26` | Crypto round-trip cost (Kraken taker, bps). |
| `HAIRCUT_BPS_PREDICTION` | `200` | Prediction fallback cost when no orderbook spread (bps). |
| `GATE_MIN_TRADES` | `30` | Min closed trades per asset class. |
| `GATE_MIN_HIT_RATE` | `0.55` | Min net hit-rate. |
| `GATE_SUSTAINED_EVALS` | `2` | Consecutive weekly evals edge must stay positive. |

Constants follow the existing env-override pattern in `common.py`. bps → fraction:
`bps / 10_000`.

## The five deliverables

### (a) Dimensional aggregation — `validation.py`

`aggregate_performance(book) -> dict`. Over closed positions, computes for **overall** and
each dimension value (`asset_class`, `confidence`, `play_type`, `thesis_ref`):
`n`, net hit-rate (`net_return > 0`), mean & median `net_return`, and mean `edge`
(over the subset where `edge is not None`). Pure; tolerant of missing/null fields.

### (b) Go-live gate — `validation.py` + `gate_history.json`

`evaluate_gate(book) -> dict` per asset class against the moderate defaults. "Sustained over
the last N evals" requires history: each **weekly** run appends that week's per-asset mean
edge to a small **`gate_history.json`** under `DATA_DIR` (`_write_json_atomic` /
`_load_json_or`); the gate checks the last `GATE_SUSTAINED_EVALS` entries are all positive.
Returns per asset class: `READY` / `NOT_READY` plus the first failing criterion (trade count,
hit-rate, mean edge, or sustained-window), for display in the report.

### (c) Enhanced weekly report — `validation.py`, wired into `mode_weekly`

Supersedes `paper_scorecard` (relocated to `validation.py`; name may be kept or renamed —
brief's import repoints accordingly). Telegram-HTML containing: overall stats, per-dimension
breakdowns (asset_class / confidence / play_type / thesis_ref), best/worst dimensions,
chronically-wrong theses flagged for manual `/mute` / `/thesis`, and the go-live gate status
per asset class. Replaces the current `telegram_send(paper_scorecard(book))` call in
`mode_weekly`, which also appends the weekly `gate_history.json` entry.

### (d) Performance-feedback prompt block — `validation.py`, injected into `build_daily_prompt`

`performance_prompt_block(book) -> str`: a compact track-record summary by
thesis / asset_class / confidence, surfacing the model's own realized hit-rate and edge so it
can recalibrate (soft, continuous; **no hard rule changes**). Only dimensions with **n ≥ 5**
are included (no noise from tiny samples); returns `""` when nothing qualifies. Injected into
`build_daily_prompt` via a **new parameter** that slots beside the existing `feedback_block`
(focus/mute/notes). `mode_collect` passes it (built from the loaded book). Empty string adds
nothing to the prompt.

### (e) Unified daily trade message — `mode_collect`

Replaces the **current silent open** with one Telegram message (silent, brief style), sent
**only if non-empty**:
- **Opened today** — equity + crypto positions opened from signals.
- **Prediction suggestions** — matched markets with side · `play_type` · similarity.
- **Open positions** — multi-asset summary with current marks.

Volume alerts are Phase 5. The message is built/sent **inside** the existing
`try/except`-wrapped trading stage that runs **after** `clear_batch_state`, so a
trading/PolyGram/Claude/Telegram failure can never duplicate or affect the brief (the Phase-3
ordering invariant is preserved).

## Pipeline / wiring summary

- **`collect`**: brief delivery → `save_signals` → `clear_batch_state` →
  `try/except`(open positions [stamps `benchmark_entry`/`entry_spread`] → **unified daily
  trade message**). `build_daily_prompt` now also receives the performance-feedback block.
- **`weekly`**: `mark_to_market` (close paths now stamp `haircut`/`net_return`/
  `benchmark_return`/`edge`) → append per-asset mean edge to `gate_history.json` →
  **enhanced report** (incl. gate status) via Telegram.
- **`commands`**: `/close` close path now also stamps close metrics (via the shared
  finalizer). No new commands in Phase 4 (`/performance` etc. are Phase 5).

## State files

- **`book.json`** — unchanged shape except the new nullable position fields above.
- **`gate_history.json`** (new, under `DATA_DIR`) — small append of per-week per-asset mean
  edge for the sustained-window check. `_write_json_atomic` / `_load_json_or`.

## Network behaviour

Benchmark and orderbook fetches add calls to `collect` (open), `weekly` (close), and `/close`
(close). All are **best-effort**: failure → `null` field, never blocks an open or close.
Both `collect` and `weekly` already hit the network, so no new fragility enters the critical
path. Unit tests stub these fetches (or rely on creds-gating) to stay hermetic.

## Testing approach

`tests/test_validation.py` (new) + additions to existing trading tests. Mirrors current
`tests/` patterns (env-gated `DATA_DIR`, `pytest -q`, monkeypatch on the module under test per
the split-module testing convention).

- **Aggregation:** dimensional breakdown math on a fixture book (hit-rate, mean/median net
  return, mean edge; null-tolerant).
- **Haircut/benchmark/edge math:** per asset class, incl. prediction `entry_spread` path,
  config-bps fallback, and the long-sense benchmark for bullish vs bearish trades.
- **Gate:** per-asset evaluation incl. `gate_history.json` sustained-window logic and each
  failing-criterion branch.
- **Legacy graceful path:** position without `benchmark_entry` closes with `net_return` set
  and `benchmark_return`/`edge` = `null`; counted in hit-rate, excluded from edge stats.
- **Stamping hooks:** `_stamp_close_metrics` fires on all three close paths; never raises on
  benchmark fetch failure.
- **Prompt block:** included only when n ≥ 5; `""` otherwise.
- **Daily trade message:** omitted when nothing opened / no open positions; failure isolation
  (trading stage raising must not duplicate the brief) preserved.

## Out of scope (Phase 5 / later)

- Volume-anomaly monitor cron and new Telegram commands (`/watch`, `/unwatch`, `/positions`,
  `/performance`).
- Live order placement (`LiveExecutor` unimplemented).
- Automated threshold tuning (explicitly decoupled from the live milestone).
- Historical benchmark backfill for positions opened before Phase 4 (they degrade to
  `null` edge).

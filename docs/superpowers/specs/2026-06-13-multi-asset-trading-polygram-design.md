# Multi-Asset Paper Trading + PolyGram Integration — Design

**Date:** 2026-06-13
**Status:** Approved for planning
**Supersedes:** the original "PolyGram Integration" handoff spec (which assumed a ~700-line
`brief.py`, paper positions in `signals-log.jsonl`, an `asset_class` signal field, and
TBD PolyGram endpoints — all corrected below).

## Goal

Evolve the existing news-brief pipeline's equity-only paper-trading layer into a
**unified multi-asset trading subsystem** covering equities, crypto, and prediction
markets (PolyGram), built deliberately toward an eventual **paper → live** transition.
The live transition is gated on a rigorous, data-driven validation layer rather than a
calendar date.

This is not a bolt-on of one venue; it is a re-shaping of the paper layer around pluggable
seams so a third asset class slots in cleanly and a fourth (or a live executor) is a small,
bounded addition rather than a rewrite.

## Verified context (current code, brief.py @ 1982 lines)

- Pipeline is **four stateless one-shot container runs** via host cron (`docker compose run
  --rm newsbrief-<mode>`): `submit` (~20:00 UTC), `collect` (~06:00 UTC), `weekly`
  (Sun ~21:00), `commands` (every 30 min). There is no daemon.
- `mode_collect` orchestration: `poll_batch → split_brief_and_signals → normalize_signals
  → deliver (brief) → save_signals → mode_paper → clear_batch_state`. The brief is sent
  **before** `mode_paper`, so any trading-stage failure cannot affect the brief — except
  that throwing before `clear_batch_state` would re-collect and **duplicate** the brief.
- Paper positions live in **`paper-book.json`** (under `PAPER_DIR`), not `signals-log.jsonl`.
  `signals-log.jsonl` is a rolling append of the fixed 7-field signal dicts for feedback
  review; `mode_paper` reads the dated `signals-<date>.json` snapshot.
- Signal schema is a fixed 7 fields (`ticker, topic, direction, confidence, thesis_ref,
  rationale, provenance`); `normalize_signals` strips unknown keys. **No `asset_class`
  field exists yet.**
- `mark_to_market` (weekly) marks open positions, records horizon checkpoints
  (`PAPER_HORIZONS` 1w/2w/4w), closes at 4w (`PAPER_CLOSE_HORIZON`). Equity-specific:
  Stooq pricing + `_signal_return` percentage math.
- Telegram commands dispatched in `_handle_telegram_update`: `/focus /mute /note /close
  /reset /status /help /thesis /dig`. `/close` is the precedent for a paper command (reads
  `paper-book.json` directly). `feedback.json` only stores `{focus, mute, notes}`.
- `run_dig` (brief.py:504) is the reusable **synchronous** Messages-API pattern
  (`ANTHROPIC_HEADERS`, `MODEL`) — the Claude matcher follows this shape.
- `build_daily_prompt` already injects prior brief, weekly summary, `feedback.json`,
  `theses.json`, and portfolio back into signal generation — the hook for the
  performance-feedback block.
- Existing reusable helpers: `_write_json_atomic`, `_load_json_or`, `load_paper_book` /
  `save_paper_book`. `requests` is already a dependency.

## Verified PolyGram API (rendered from https://polygram.ink/docs, 2026-06-13)

- **Base:** `https://polygram.ink/api` · **Auth:** JWT Bearer.
- **Auth:** `POST /api/auth/register` (manual, one-time), `POST /api/auth/login` → JWT;
  `2fa/setup`, `2fa/confirm` exist. Header `Authorization: Bearer <token>`; refresh on 401.
- **Events & Markets (read — all paper needs):** `GET /api/events?limit=N`,
  `/api/events/slug/:slug`, `/api/markets/:id`, `/api/orderbook/:tokenId`, `/api/price`,
  `/api/prices-history`, `/api/search`.
- **Trading:** an 8-endpoint section exists (order placement). Exact paths to be pinned at
  live-executor implementation time (deferred — not built in this scope).
- **Streaming:** `/api/stream/*` SSE — **out of scope**, we poll `/api/price` and
  `/api/prices-history`.
- **Nature:** real money — PGUSD on Polygon, Polymarket-mirrored, UMA-resolved, "18+ /
  licensed." This is why the live executor stays unbuilt behind the go-live gate.

`/api/search` (pre-filter candidates for the matcher) and `/api/orderbook/:tokenId` (real
bid/ask spread for the cost-haircut) were not in the original spec and materially improve
the matcher and validation realism.

## Architecture

### Module split (extract trading layer out of brief.py)

- **`common.py`** — shared infra: config/paths, `_write_json_atomic` / `_load_json_or`,
  `telegram_send`, Anthropic client (`MODEL`, `ANTHROPIC_HEADERS`, synchronous-message
  helper). Imported by both other modules; the dependency direction is one-way so there is
  no circular import.
- **`trading.py`** — the whole subsystem: unified book, per-asset resolvers / pricers /
  executors, Claude matcher, lifecycle/MTM, volume monitor, performance/validation.
- **`brief.py`** — sources, prompts, delivery, Telegram, orchestration (`mode_*`).
- **Build/test:** Dockerfile must `COPY common.py trading.py` alongside `brief.py`; test
  imports updated. Local-run import-safety (env-gated `DATA_DIR`) preserved in all modules.

### Unified position model

One **`book.json`** (replacing the equity-only `paper-book.json` concept) of polymorphic
positions. Every position carries:

- `asset_class`: `equity | crypto | prediction`
- `venue`: `t212 | kraken | polygram` (informational; drives resolver/pricer/executor)
- `execution`: `paper | live` (only `paper` is reachable in this scope)
- `instrument`: resolved venue symbol/id (Stooq symbol, Kraken pair, PolyGram tokenId)
- `play_type` (prediction only): `resolution | momentum`
- plus the existing lifecycle fields (`entry_price`, `entry_date`, `direction`,
  `confidence`, `topic`, `thesis_ref`, `rationale`, `checkpoints`, `last_mark`, `status`,
  `close_reason`, `closed_date`, `realized_return`).

**Unifying principle:** every position is *"long an instrument at entry price, marked to
current price, closed on a trigger."* Return math is identical across asset classes; only
the close trigger differs. For prediction markets the "instrument price" is the price of
the held side (YES = `p`, NO = `1−p`); settlement is simply that price going to 1.0 or 0.0.

### Four pluggable seams (dispatch by `asset_class`)

| Seam | equity | crypto | prediction |
|---|---|---|---|
| **Resolver** (signal → instrument) | `resolve_stooq_symbol` (existing) | Kraken pair (e.g. `XBTUSD`) | Claude matcher via `/api/search` |
| **Pricer** (instrument → mark) | `fetch_stooq_price` (existing) | Kraken Ticker | `/api/price` (held side) |
| **Executor** | Paper (live = T212, later) | Paper (live = Kraken, later) | Paper (live = PolyGram, later) |
| **Return / close trigger** | % return / 4w horizon | % return / horizon | held-side price; **momentum** → horizon/target, **resolution** → settlement (→1/0) |

`mode_paper` (open loop) and `mark_to_market` (MTM loop) become asset-class-aware via this
dispatch, extending — not rewriting — the existing logic.

## Signal sourcing & prompt change

- **Equity + crypto: direct.** The model emits directional signals naming the instrument.
  Add `asset_class: equity | crypto` to the emitted signal JSON schema; update
  `SYSTEM_PROMPT` to permit crypto directional calls (BTC, ETH, …). `normalize_signals`
  validates/defaults the new field and keeps it in the 7→8-field schema.
- **Prediction: matched (indirect).** The model cannot name live markets it has not seen.
  After signals exist, the **Claude matcher** runs: fetch candidate markets via
  `/api/search` on signal topics/theses → one synchronous Claude call (run_dig pattern,
  no web search) passing today's signals + candidates → Claude returns matched
  `(market, side, play_type, similarity)` tuples parsed as JSON (same discipline as
  signal parsing). `play_type` is classified by the matcher: `resolution` when the signal
  speaks to the eventual outcome, `momentum` when it is a near-term catalyst likely to move
  the odds regardless of settlement.

## Pipeline placement (cron modes)

- **`collect`** (daily): unchanged brief delivery → multi-asset open (equity + crypto from
  signals; prediction via matcher) → **unified daily trade message** → `clear_batch_state`.
  The PolyGram/trading stage is `try/except`-wrapped and ordered **after**
  `clear_batch_state`, so it can never affect or duplicate the brief.
- **`weekly`**: multi-asset MTM + **enhanced performance report** + go-live gate status.
- **`monitor`** (NEW hourly cron): cross-asset volume-anomaly alerts — per-asset volume
  (Stooq / Kraken / `/api/prices-history`) vs a stored trailing average, over watched
  instruments + open positions. Decoupled from the critical paths.
- **`commands`** (30 min): adds `/watch <market>`, `/unwatch <market>`, `/positions`
  (multi-asset summary), `/performance`.

**Cadence decision:** trade-idea generation stays **daily**. Increasing brief frequency is
the wrong lever — the brief is news-bound, so intraday reruns produce *correlated* signals
that corrupt the validation statistics (one news event counted as N independent bets). More
*independent* sample volume comes from **breadth** (the three asset classes), not frequency.
The one frequency-hungry component (volume monitoring) already has its own hourly cron.
Cadence is a tunable cron knob; revisit only on demonstrated need.

## Delivery (Telegram)

One **unified daily trade message** sent after the main brief (silent, same style), only if
there is something to say:
- **Opened today** — equity + crypto positions opened from signals.
- **Prediction-market suggestions** — matched markets with side + `play_type` + similarity.
- **Open positions** — multi-asset summary with current marks.
- **Volume alerts** — emitted by the `monitor` cron when spikes occur (omitted if none).

## Validation + improvement loop

### Validation (full)
- Persist every closed-trade outcome to **`performance.json`**.
- Report **hit-rate, avg/median return, win/loss** broken down by `asset_class`,
  `confidence`, `play_type`, `thesis_ref`.
- **Benchmark** each trade vs its asset's baseline (e.g. the underlying's same-window move /
  index / hold-the-coin) so skill is separated from beta.
- **Cost haircut** (configurable) modelling live friction — uses the real
  `/api/orderbook/:tokenId` spread for prediction markets. (Addresses the deferred-haircut
  note in the paper-sim-no-fees decision.)
- **Go-live gate:** written per-asset criteria (minimum N closed trades + edge over
  benchmark sustained over a window) that must pass before any `LiveExecutor` for that asset
  class may be enabled. The gate, not a date, is what authorizes real money.

### Improvement loop (informational in v1)
- **Performance-feedback prompt block** injected into `build_daily_prompt`: the model sees
  its own track record by thesis/asset_class/confidence and recalibrates — a *soft,
  continuous* adaptation with no hard rule changes.
- **Dimensional surfacing** in the weekly report: best/worst dimensions and
  chronically-wrong theses flagged for **manual** `/mute` / `/thesis` action.
- Selection thresholds (e.g. the actionable `confidence in (medium, high)` filter) remain
  **manual config**, tunable by hand from the report.

### Explicitly deferred: automated threshold tuning
Not in v1, and **decoupled from the live milestone**. Auto-tuning is riskier with real money
(chases noise → real losses), and the sample volume is far too low to tune on without
overfitting. The correct trigger is *"manual tuning has become impractical because volume is
high"* plus a proven manual baseline — not *"we are going live."* It should land after live
is stable, never as its prerequisite.

## Credentials / new state

- **Env (via docker-compose, like T212):** `POLYGRAM_EMAIL`, `POLYGRAM_PASSWORD` → JWT
  persisted to `polygram_token.json` (refresh on 401). Registration is manual/one-time —
  **not** in the cron path. Kraken needs no key for paper prices; Kraken/T212 live keys are
  future.
- **Persisted state under `DATA_DIR`:** `book.json`, `polygram_watchlist.json`,
  `polygram_token.json`, `performance.json`, and a volume-history file for the monitor
  baseline. Use `_write_json_atomic` / `_load_json_or` throughout.

## Out of scope (v1)

- Live order placement (Executor seam defined; `LiveExecutor` unimplemented — no code path
  places a real order).
- SSE streaming (poll instead).
- Auto-register in the cron path (login + refresh only).
- Automated threshold tuning.

## Testing approach

- Unit tests mirror existing `tests/` patterns (env-gated `DATA_DIR`, `pytest -q`).
- Per-seam tests with stubbed HTTP: resolver mapping (incl. Kraken `XBT` quirk), pricer
  parsing, return math for each close trigger (equity %, prediction momentum, prediction
  settlement → 1/0).
- Matcher: stub the Claude call; assert robust JSON parsing and `play_type` handling,
  including malformed/empty responses (reuse the signals-parsing resilience posture).
- Lifecycle: multi-asset MTM dispatch; resolution-vs-momentum close triggers; dedup/reversal
  parity with current equity behaviour.
- Validation: dimensional aggregation, benchmark + haircut math, go-live gate evaluation.
- Failure isolation: trading stage raising must not duplicate the brief (ordering after
  `clear_batch_state` + try/except).

## Key decisions (with rationale)

1. **Direct equity+crypto, Claude-matched prediction** — the model can't name live markets;
   crypto directional calls are natural like equities.
2. **Model-tagged `asset_class`** — explicit and self-describing vs a brittle symbol allowlist.
3. **One unified book** — trivial cross-asset scorecard; one lifecycle with dispatch.
4. **Kraken for crypto pricing** — the user's actual venue; aligns paper with future live, no
   source switch later.
5. **`play_type` matcher-classified; resolution-vs-momentum close triggers** — captures
   short-term odds plays without discarding hold-to-settlement, and unifies into the
   price-based return model.
6. **`common.py` + `trading.py` + `brief.py`** — focused files, clean one-way dependency.
7. **Seam + paper-only; live deferred behind a go-live gate** — zero real-money risk now, no
   refactor later.
8. **Full validation + informational improvement loop; auto-tuning deferred and decoupled
   from live** — human-in-the-loop regulator while samples are small.
9. **Volume monitor generalized across asset classes on its own hourly cron.**
10. **Daily cadence retained** — frequency would corrupt validation via correlated samples;
    breadth, not frequency, yields independent volume.

# Phase 3: PolyGram Read Client + Claude Matcher + Prediction Lifecycle — Design

**Date:** 2026-06-13
**Status:** Approved for planning
**Governing spec:** `2026-06-13-multi-asset-trading-polygram-design.md` (this is the Phase 3
addendum; where they differ, the deltas in §1 below supersede the governing spec).
**Builds on:** Phases 1–2 (module split `common.py`/`trading.py`/`brief.py`; unified
polymorphic `book.json`; `fetch_price(asset_class, instrument)` / `price_position(p)`
dispatch; crypto/Kraken seam).

## Goal

Add **prediction markets (PolyGram)** as the third paper-traded asset class: a read-only
PolyGram client, a Claude **matcher** that maps the day's signals to live markets, and a
prediction **lifecycle** (open → mark-to-market → close) that reuses the shared
equity/crypto return math and forks only on the close trigger by `play_type`. Wired into
the live `collect` cron **silently** (the rich unified trade message is Phase 4). No live
order placement — paper only, behind the unbuilt go-live gate.

## Real-API grounding (verified read-only against https://polygram.ink/api, 2026-06-13)

PolyGram mirrors Polymarket's Gamma data. Confirmed shapes (probe, not docs):

- **Auth:** `POST /api/auth/login` `{email, password}` → `{user, token, twoFactorVerified}`.
  JWT (~212 chars) under `token`. Header `Authorization: Bearer <token>`; 401 → re-login.
- **`GET /api/search?q=<term>`** → array of **events**, each with nested `markets[]`.
  Event: `{id, ticker, slug, title, description, endDate, active, closed, volume,
  liquidity, markets[]}`. Each market is binary.
- **`GET /api/markets/:id`** → full market. Relevant fields:
  - `outcomes` — JSON-**string** `'["Yes", "No"]'`
  - `outcomePrices` — JSON-**string** `'["0.0045", "0.9955"]'`, **index-aligned** to
    `outcomes`. Live on open markets; **the search/events list copy is stale** (a closed
    market showed `'["0.5050","0.99"]'` in the list but `'["0","1"]'` in detail).
  - `clobTokenIds` — JSON-**string** of two token ids, index-aligned to `outcomes`.
  - `closed` (bool), `umaResolutionStatus` (`"resolved"` when settled, else `None`),
    `bestBid`/`bestAsk`/`spread`, `lastTradePrice`.
  - Settled example: `closed=true`, `umaResolutionStatus="resolved"`,
    `outcomePrices='["0","1"]'`.
- **`GET /api/price?tokenId=<id>`** → `{"price":"0.004"}` (param is `tokenId`, **not**
  `token_id`; 400 otherwise). Single token only. **Deferred to Phase 4.**
- **`GET /api/orderbook/:tokenId`** → `{bids, asks, spread, midpoint}` (real spread for the
  cost haircut). Only on open markets. **Deferred to Phase 4.**

### §1 — Delta from the governing spec

The governing spec's *Pricer → `/api/price` (held side)* is **superseded**. The prediction
pricer is **`GET /api/markets/:id` → `json.loads(outcomePrices)[side_index]`**, because that
one call yields both the held-side mark **and** the resolution status
(`closed`/`umaResolutionStatus`), so MTM and settlement detection share a single fetch.
`/api/price` + `/api/orderbook` are used only for the Phase 4 cost-haircut.

## Components (all in `trading.py` unless noted)

### Read client
- **Env:** `POLYGRAM_EMAIL`, `POLYGRAM_PASSWORD` (added to `.env.example` +
  `docker-compose.yml`). Registration is manual/one-time, never in the cron path.
- **`polygram_login()`** → JWT, persisted to `polygram_token.json`
  (`_write_json_atomic`/`_load_json_or`).
- **`_polygram_get(path, params)`** — attaches `Authorization: Bearer <token>`; on **401,
  re-login once and retry**; returns parsed JSON or `None`. Same None-on-failure posture as
  `fetch_stooq_price`/`fetch_kraken_price` — a PolyGram outage never throws into cron.
- **`polygram_search(query)`**, **`polygram_market(market_id)`** — thin reads.
- **`_parse_pg_market(m)`** → `{market_id, question, yes_price, end_date, closed,
  uma_status, token_ids}`; does the defensive `json.loads` of the stringified
  `outcomes`/`outcomePrices`/`clobTokenIds` arrays once.

### Claude matcher
- **Inputs:** ALL of today's normalized signals (not pre-filtered to actionable — prediction
  markets capture event outcomes that need not be a clean bullish/bearish equity call) + a
  deduped, capped candidate set.
- **Candidates:** flatten `polygram_search(q)` over each distinct signal `topic` +
  `thesis_ref`; keep **open** binary markets (`closed == false`); dedup by `market_id`; cap
  at `PG_CANDIDATE_CAP` (default 25). Each passed as `{market_id, question, yes_price,
  end_date}`.
- **Call:** one synchronous Messages-API call (`run_dig` pattern — `MODEL`,
  `ANTHROPIC_HEADERS`, `requests.post`), **no tools/web search**. Returns a JSON array of
  `{market_id, side: "YES"|"NO", play_type: "resolution"|"momentum", similarity: 0..1,
  target: float|null}`, parsed with the **signals-parsing resilience posture** (tolerant of
  fences/garbage, empty-on-failure, structure-based).
- **Gating:** open only matches with `similarity >= PG_SIMILARITY_FLOOR` (default 0.60) AND a
  still-open market. `target` meaningful only for `momentum`.

### Prediction position (polymorphic; equity/crypto leave new fields null)
- `asset_class="prediction"`, `venue="polygram"`, `execution="paper"`,
  `instrument=market_id`, `play_type ∈ {resolution, momentum}`.
- New nullable fields: `outcome` (`"Yes"`/`"No"`), `side_index` (0/1), `token_id` (Phase 4
  haircut), `target` (momentum only). Equity/crypto positions carry these as `null`, exactly
  as they already carry `play_type=null`.

### Lifecycle (extends, does not rewrite)
- **Open** (`mode_paper`, new `asset_class=="prediction"` branch): entry price = held-side
  `outcomePrices[side_index]`. Dedup/reversal reuses the existing 3-tuple
  `(asset_class, ticker, direction)` with `ticker = market_id`.
- **MTM** (`mark_to_market` + `price_position` new `prediction` branch): mark =
  `outcomePrices[side_index]`. Return = **long** held-side `(mark − entry)/entry` (you are
  always long the token you hold), reusing `_signal_return` in the long sense.
- **Close triggers (the only fork):**
  - **`momentum`:** 1w/2w/4w checkpoints; close early if `target` is set and the held-side
    mark **reaches or exceeds** it (a prediction position is always long the held side, so
    the target is an upside profit-take on `outcomePrices[side_index]`); else
    **force-close at 4w** (equity backstop).
  - **`resolution`:** **ignores the 4w horizon**; records checkpoints; closes when
    `closed == true && uma_status == "resolved"` at settlement
    (`outcomePrices[side_index]` → 1/0); `PG_MAX_HOLD` (default 26w) backstops
    never-resolving markets.

## Pipeline placement (cron)

- **`mode_collect` ordering fix (required):** today `mode_paper()` runs **before**
  `clear_batch_state()` and is **unguarded**. Reorder to
  `deliver → save_signals → clear_batch_state → try/except(trading stage)` so a
  matcher/PolyGram/Claude failure can never re-collect and **duplicate the brief**. The
  trading stage = `mode_paper()` (now also opens prediction positions via the matcher). No
  Telegram message in this phase.

## New state under `DATA_DIR`
- `polygram_token.json` (JWT). `book.json` gains prediction positions.
- `polygram_watchlist.json` / `performance.json` are Phase 4/5.

## Config constants (tunable)
- `PG_CANDIDATE_CAP = 25`, `PG_SIMILARITY_FLOOR = 0.60`, `PG_MAX_HOLD` ≈ 26w.

## Testing approach (`tests/test_prediction.py`, mirrors the Kraken stubbed-HTTP style)
- Client: login parse; `_polygram_get` **401 → re-login → retry**; None-on-failure.
- `_parse_pg_market`: stringified-array parsing; stale-list-vs-detail guard (pricer reads
  detail).
- Matcher: JSON-parse resilience (fences/garbage/empty); similarity gate; play_type/target
  handling; candidate dedup + cap.
- Pricer dispatch: `price_position` routes `prediction` → market-detail held-side price.
- Close triggers: momentum target-cross; momentum 4w backstop; resolution settlement → 1/0;
  resolution ignores 4w; max-hold cap.
- Failure isolation: trading stage raising must NOT duplicate the brief (ordering after
  `clear_batch_state` + try/except).

## Out of scope (Phase 4/5)
- Unified multi-asset Telegram trade message; `/api/price` + orderbook cost-haircut;
  validation/performance breakdown + go-live gate + performance-feedback prompt block;
  volume monitor; new Telegram commands; any live executor / real order placement.

## Key decisions (with rationale)
1. **Pricer = market detail, not `/api/price`** — one fetch gives both mark and settlement
   status; the list-cache price is stale. (§1)
2. **Optional momentum target + 4w backstop** — captures front-loaded momentum spikes when
   the matcher has a view, never depends on a target, and the horizon caps holding period
   (keeps Phase 4 benchmark windows bounded). Superset of horizon-only.
3. **Resolution holds to settlement (ignores 4w), max-hold cap** — a hold-to-settle bet must
   not be force-closed mid-flight; the cap backstops dead markets.
4. **All signals → capped candidates → similarity floor** — prediction markets key off
   neutral/macro signals too; cap bounds prompt cost; floor keeps validation precision.
5. **Wired into `collect` silently + ordering fix** — prediction opens live like
   equity/crypto already do; the reorder guarantees a trading failure can never duplicate
   the brief.
6. **Read-only client, None-on-failure, JWT refresh on 401** — mirrors the existing pricer
   posture; zero real-money risk; live executor stays unbuilt behind the go-live gate.

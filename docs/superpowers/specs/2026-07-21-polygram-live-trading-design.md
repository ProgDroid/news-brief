# PolyGram Live Trading — Design

**Date:** 2026-07-21
**Status:** Approved (brainstorm complete) → ready for implementation plan(s)
**Scope note:** One design, expected to decompose into ≥3 implementation plans (shared foundation → Sleeve A → Sleeve B). Equity & crypto stay **paper**; only prediction (polygram.ink) goes **live**.

## One-line

Take **real-money** positions on polygram.ink prediction markets via a two-sleeve book — a disciplined **systematic favorite-fade** engine (Sleeve A) and a **discretionary conviction-hold** override (Sleeve B) — on shared live-execution/funding/safety/attribution rails, where live positions **close on their trade trigger** instead of being held to fill the paper checkpoint dataset.

## Motivation & framing

Today prediction "trading" is **pure simulation with no orders at all**: the PolyGram client (`trading.py`) only reads (`polygram_login`, `_polygram_get`, `polygram_search`, `polygram_market`); `_open_prediction_positions` appends a `book.json` row tagged `execution:"paper"` priced at the live mark; `_mtm_prediction` marks + closes on the **weekly** cadence. Going live means building a **write** path (place/close/settle real orders, reconcile fills) that does not exist yet.

Two user asks, cleanly separable:

1. **"How do we go about it"** → the shared live-execution/funding/safety foundation + the two strategy sleeves.
2. **"Close on time rather than extending to measure performance"** → today's exit triggers are deliberately *measurement-shaped*: a momentum position with no target-hit is held to the **4-week** checkpoint (`PAPER_CLOSE_HORIZON = "4w"`); a resolution market to settlement or a **182-day** backstop (`PG_MAX_HOLD_DAYS`). Those holds exist to fill the 1w/2w/4w calibration dataset. **Live positions must not inherit them** — they close on the real trade trigger, checked frequently.

**Edge thesis (the user's, interrogated during brainstorm):** a *differentiated-sourcing informational edge* — access to out-of-consensus, still-unpriced high-probability threads via non-Western + Nitter analyst feeds. Ambition is *consistent alpha, not massive profit*. This edge only pays at the triple intersection `(our view ≠ price) ∩ (market on the other side) ∩ (we're right)`; good sourcing buys only the first. It is therefore **unproven** and must be *measured*, which shapes every risk decision below.

## Evidence that shaped this (paper `book.json`, ~2026-06-25 → 07-21)

Analysed all ~50 prediction rows (~21 closed). Findings (directional; n small, one hot Hormuz/Iran window, simulated-2026 clock — hypotheses, not proof):

- **9/9 closed `resolution` bets were net-positive** — but all the *same trade*: NO on already-improbable, high-priced events (entry 0.76–0.98) that resolved No, harvesting the small `(1−entry)` premium. This is the documented **favorite-longshot bias** (crowds overpay for longshots, underpay favorites), not forecasting.
- **The disasters are the mirror image:** cheap YES longshots (entry <0.10) → mostly −100% (2475212, 2569222 closed −1.02/−1.16 *after cost*; 2176262 open 0.645→0.016 = **−97%**). "NO good / YES bad" is illusory — buying NO@0.95 and YES@0.05 are the *same claim*; the winners were the **high-priced favorite side**.
- **`play_type` is not the axis; side + entry price + spread is.** Momentum's headline is a single outlier (2851414: 0.0195→0.224 = **+997%**); strip it and the other 11 momentum closes net negative.
- **Cost is the biggest killer, and it's in the data:** toxic longshots carry `entry_spread` up to **0.50** (2851414: 0.4999; 2851427: 0.333); tight favorites ≈ 0.00–0.006. **A hard spread gate would have excluded nearly every disaster and kept nearly every consistent winner** — higher leverage than sizing and exit logic combined.
- **Repricing is real, fast, capturable** — open favorites climb toward 1.0 within 1–2 weeks (2774056: 0.655→0.9995). Capturing the reprice banks the gain in days and frees capital; **holding is what turned 2176262 into −97%**.
- **Alpha magnitude of fading favorites is thin:** median closed fade ≈ **+3.2%**, and returns are mechanically capped by room above entry (0.98 entry ⇒ +2% max vs −98% tail). The asymmetric upside lives in the *conviction* sleeve, not the fade — hence the **barbell**.
- **Current prediction P&L is NOT evidence of the sourcing edge** — `source_kind:"unknown"` on nearly every prediction row. Fixing prediction-side source attribution is a precondition for ever testing the real thesis.

## Scope decisions (locked during brainstorm)

| Fork | Decision | Why |
|---|---|---|
| What is "real"? | polygram.ink is **real money**, order API known to user | User-confirmed; verifying the exact order/funding contract is plan task #1 |
| Which assets go live? | **Prediction only**; equity/crypto stay paper | Staged risk; prediction is the requested venue |
| Edge → strategy shape | **Barbell:** Sleeve A (systematic fade, consistent base) + Sleeve B (conviction holds, convex upside) | Fade alone caps at a few-% grind; conviction holds are where asymmetry lives |
| Sleeve A entry | **Favorite side** (high-priced, band ~0.75–0.92) of tight-spread binaries | Favorite-longshot bias; where tradeable spreads + consistent wins are |
| Spread gate | **Hard `entry_spread ≤ threshold`**, both sleeves | Single highest-leverage rule; structurally bans toxic longshots |
| Sleeve A exit | **Repricing-target OR stop-loss OR time-stop**, frequently checked; settlement-hold only near-dated | Answers "close on time"; recycles capital; cuts the −97% tail; kills the 4w/182d measurement-holds |
| Sizing (launch) | **Minimal fixed stake + hard per-trade & total caps** | Edge unproven; Kelly/confidence need a calibrated `p_hat` we haven't earned |
| Sleeve B entry | **User-authored** thesis via conversational Telegram flow (v0) | The only place the sourcing edge can be expressed; human owns each conviction bet |
| Sleeve B exit | **Hold to settlement / hold-until-`/close`**, exempt from Sleeve A auto-exits | A hold is governed by thesis, not daily churn |
| Sleeve B containment | **Per-position "money-I-can-zero" cap + total sleeve cap + NO DCA + separate P&L** | Contains the indistinguishable "early vs wrong" tail without a stop that would kill slow-burn theses |
| Volume monitor role | **Liquidity/settlement watcher, not entry signal** | In-data volume spikes mostly *coincide with settlement* (lagging); use `entry_spread`/depth as the gate |
| Calibration approach | Score **vs market-implied** (Brier-skill), **discrimination-first**, **shadow-log every proposal** | Beating the market-as-forecast is the only bar; discrimination is what trading needs; scoring only confirmed theses measures the human filter, not the machine |
| Calibration pace | **Real-time only, be patient** (no shorter-horizon padding, no historical bootstrap) | Cleanest signal; verdict ~a year out — acceptable |
| Clock confound | **Fully contemporaneous** (user-confirmed) | Feeds and live polygram prices share the same "now" → measured edge is genuine, not look-ahead |
| Machine-proposed theses | **Deferred to spec #2** (propose-then-confirm push) + earned auto-open autonomy | Auto-opening conviction holds on an uncalibrated engine is the ruin trap; earn it with the scorecard |
| Retention of thesis logs | **Resolution-aware**, integrated into `retention.py` | A flat short TTL deletes unresolved theses before scoring; keep payload until `resolve_by`+grace+scored, then prune to a compact scored summary |

## Architecture & data flow

```
SHARED FOUNDATION (real money)
  polygram_live.py  ── place/close/settle real orders + fill reconciliation
                       (verified order/funding contract; None-on-failure posture like the pricers)
  book.json         ── positions gain execution:"live"; sleeve:"A"|"B"; live vs paper split in all reporting
  safety            ── PG_LIVE_ENABLED master kill-switch, per-trade cap, total live-exposure cap, minimal stake
  attribution       ── per-sleeve realized P&L (fills+costs, not marks); prediction source_kind fixed

SLEEVE A — systematic favorite-fade (rides the daily signal pipeline)
  mode_paper/live: signals ─► matcher ─► favorite-side, band+spread gated ─► polygram_live.place ─► live row
  monitor cron   : each open Sleeve-A live row ─► reprice/stop/time-stop/near-settlement ─► polygram_live.close
                   (frequent; NOT the weekly MtM. Measurement-holds removed for live.)

SLEEVE B — discretionary conviction hold (Telegram override)
  /thesis-style wizard (callback_query, like /addsource):
     free-text thesis ─► polygram_search ─► pick market/side/stake/hold-mode ─► confirm ─► live row (sleeve B)
  thesis record {market_id, side, p_hat?, resolve_by, source_ids, rationale} ─► thesis_log
  exit: settlement (monitor cron) or /close. No auto reprice/stop. No DCA.

CALIBRATION (starts day one; machine generation deferred)
  every thesis ─► thesis_log ; on resolution ─► score p_hat vs market-implied-at-entry (Brier-skill) ─► scored summary
  retention.py: keep payload until resolve_by+grace+scored → prune to compact scored summary (kept long-term)
```

## Component detail

### 1. `polygram_live.py` (new top-level module) — the write layer

**Verified contract** (polygram.ink, base `https://polygram.ink/api`, JWT Bearer — same token as the read client; USD custodial balance funded by Polygon USDC/USDT deposit; min order $1; 300 req/min authed, 429 → backoff-with-jitter; `403` = geo-blocked/frozen; errors `{error, message}`):

| Need | Endpoint | Shape |
|---|---|---|
| Market buy | `POST /trade/place` | body `{eventId, marketId, tokenId, outcome:"Yes"\|"No", amount (USD)}`; **synchronous** resp `order:{id, fillPrice, shares, spreadFee, tradeFee, totalFee, status:"filled"}` |
| Sell / exit | `POST /trade/sell` | body `{positionId, shares?}`; resp `sale:{sharesSold, salePrice, proceeds, profit, fee, status}` |
| Positions (reconcile + get `positionId`) | `GET /trade/positions` | `positions:[{id:"pos_…", marketId, outcome, shares, avgPrice, currentPrice, costBasis, currentValue, unrealizedPnl}]` |
| Cash balance (cap check) | `GET /wallet` | `{balance, currency:"USD", pending…}` |
| Live spread/depth gate | `GET /orderbook/:tokenId` | `{bids, asks, spread, midpoint}` |

- **Market orders at launch** (decided): `place_order(...)` calls `POST /trade/place` and returns the synchronous fill (`fillPrice`, `shares`, `spreadFee`, `tradeFee`) or `None`. The **hard spread/depth gate reads `/orderbook/:tokenId` pre-trade**, so a market order only ever crosses a small, bounded spread; at $1–5 stakes, slippage ≈ top-of-book. **Every fill's real fees are recorded** so the fee-drag can be measured — limit/marketable-limit orders are a *later, data-justified* optimization for Sleeve A only (not built in v0). Exits are **always market** (`/trade/sell`).
- **Two plumbing facts:** (1) `place` needs `eventId` **and** `marketId` **and** `tokenId` — the book stores market_id + token_id but not `eventId`, so the open path captures `eventId` from event/market detail at entry. (2) Selling needs a `positionId` (`pos_…`) that appears only in `GET /trade/positions`, not in the place response (`ord_…`) — so `close_order` is always "`GET /trade/positions` → match by `tokenId`/`outcome` → `POST /trade/sell {positionId}`," which is also the reconcile read. **Settlement has no redeem call** — a resolved position simply leaves `/trade/positions` and lands in realized P&L; reconcile detects "gone + market resolved → settled."
- `reconcile(book) -> book` — align live rows against `GET /trade/positions`; real orders **partial-fill, fail** — the book reflects *fills*, not intents; drift logged loudly, **venue wins**.
- `account_balance() -> float | None` — `GET /wallet` balance for the pre-trade total-exposure cap.

`dockerfile-copy-allowlist` chore: new top-level module ⇒ Dockerfile `COPY` + workflow path lists + workflow ruff file lists all need updating, or runtime `ModuleNotFound` that escapes CI lint.

### 2. Book & position schema (extend the polymorphic `book.json`)
New fields on prediction rows: `execution: "live"|"paper"`, `sleeve: "A"|"B"`, and for live rows the realized `fill` record + `fees`. Live and paper coexist; **all reporting splits by `execution` first** so paper stats never contaminate the live scorecard. `load_book`/`save_book` unchanged in shape; the book lock (`BOOK_LOCK_TIMEOUT`) already guards concurrent `/close` vs collect — live writes join the same lock discipline.

### 3. Sleeve A — systematic favorite-fade
- **Entry gate (all must pass):** matcher similarity ≥ floor; **favorite side** (buy the >0.5 side); **price band** `PG_A_BAND_LO ≤ price ≤ PG_A_BAND_HI` (default ~0.75–0.92 — excludes crumb zone >~0.93 where upside < costs, and longshots <0.5); **spread gate** `entry_spread ≤ PG_SPREAD_GATE` (default ~0.03–0.05); dedup per market; total-exposure + per-trade caps.
- **Sizing:** `PG_A_STAKE` fixed, capped; no Kelly/confidence in launch.
- **Exit (evaluated on the monitor cron, frequently — not weekly):**
  - `target` — held price ≥ repricing target → close.
  - `stop` — adverse move ≥ `PG_A_STOP` (price-space) → close.
  - `time_stop` — neither hit after `PG_A_TIME_STOP_DAYS` and not near settlement → close, recycle capital (this **replaces** the 4w measurement backstop with a *trading* time-stop).
  - `settlement` — allowed only when the market resolves within `PG_A_NEAR_DAYS` of entry (near-dated special case where reprice ≈ settle).
- The live path reuses `_signal_return`/mark logic but routes closes through `polygram_live.close_order`; `_mtm_prediction`'s measurement-hold branches (`PAPER_CLOSE_HORIZON`, `PG_MAX_HOLD_DAYS`) are **paper-only**.

### 4. Sleeve B — discretionary conviction hold (v0: user-authored)
- **Entry UX:** a conversational button/text wizard in the command daemon, built on the exact `_wizard_*` + `_handle_callback_query` pattern that powers `/addsource`. Steps: free-text **thesis** → `polygram_search(terms)` → pick **market** from candidates → pick **side** → **stake** (≤ `PG_B_POS_CAP`) → **hold-mode** (to-settlement / until-`/close`) → optional **`p_hat`** → **confirm**. (Naming: a **new dedicated command** — decided *not* to overload the existing `/thesis` remediation command. Working name `/predict` — final name chosen in the plan; register it via `setMyCommands` alongside the others.)
- **Exit:** hold to settlement (monitor cron detects resolved+settled) or manual `/close` (already exists). **No** auto reprice/stop. **No DCA** — v0 refuses a second Sleeve-B open on a market already held.
- **Containment:** `PG_B_POS_CAP` per position (sized as money you can watch go to zero), `PG_B_TOTAL_CAP` aggregate, separate P&L line in the report.

### 5. Calibration corpus + resolution-aware retention (starts day one)
- **Thesis record** (`thesis_log`, one file or per-day append): `{id, created, market_id, side, entry_price (market-implied), p_hat?, resolve_by, source_ids, rationale, traded: bool, sleeve}`. **Every** thesis is logged — including (in spec #2) machine proposals the user declines — because scoring only the confirmed subset measures `machine ∘ human filter`, not the machine.
- **Scoring (on resolution):** Brier-skill of `p_hat` vs market-implied-at-entry; **discrimination-first** (do higher-conviction theses win more?) before numeric calibration (Platt/isotonic-correctable later). Tag each with `source_ids` so edge can eventually be attributed per feed.
- **Retention (the user's explicit ask):** integrate into `retention.py` but **not** the flat `NEWSBRIEF_RETENTION_DAYS` sweep — keep the verbose payload until `resolve_by + PG_THESIS_GRACE_DAYS` **and** scored, then prune to a tiny scored summary (`p_hat`, market-implied, outcome, source_ids) retained long-term for the reliability curve. Mirrors the additive/lifecycle pattern of `severity-weighted-retention`, not the date-keyed artifact sweep.

### 6. Volume monitor repurpose
Wire polygram market volume into the existing monitor as a **liquidity/settlement** input, not an entry trigger: (a) confirm depth so `entry_spread` gate is meaningful, (b) flag settlement-coincident spikes for the exit/settlement check. No entry decision keys off a volume spike (in-data spikes lag the reprice).

## Sizing & risk limits (all env-configurable; dollar values set at go-live)

| Knob | Purpose |
|---|---|
| `PG_LIVE_ENABLED` | master kill-switch; off ⇒ everything stays paper (default off) |
| `PG_A_ENABLED` / `PG_B_ENABLED` | per-sleeve enables |
| `PG_A_STAKE` / `PG_B_POS_CAP` | per-trade stake / conviction per-position cap |
| `PG_LIVE_TOTAL_CAP` / `PG_B_TOTAL_CAP` | total live exposure / Sleeve-B sub-cap |
| `PG_SPREAD_GATE` | max `entry_spread` to open (both sleeves) |
| `PG_A_BAND_LO/HI`, `PG_A_STOP`, `PG_A_TIME_STOP_DAYS`, `PG_A_NEAR_DAYS` | Sleeve A entry band + exit triggers |
| `PG_THESIS_GRACE_DAYS` | retention grace after `resolve_by` before pruning payload |

The go-live gate (`evaluate_gate`, `gate_history.json`) is extended to *surface* prediction live-readiness but stays **informational** — the real guards are the kill-switch + caps + minimal stake, deliberately launching before the gate "passes" because the whole point is to measure with real money at trivial size.

## Error handling / fail-safe ladder

Real money ⇒ **fail-closed** (opposite of the brief's fail-open): when in doubt, do **not** trade, and never leave the book disagreeing with the venue.

| Failure | Behavior |
|---|---|
| `PG_LIVE_ENABLED` off / creds absent | No live orders; existing paper path unchanged |
| `place_order` errors/times out | Log loud; **no** book row written (no phantom position); retry next cycle |
| Partial fill | Book records the *actual* filled size/price; remainder cancelled |
| `close_order` fails | Position stays open + flagged; retry next monitor tick; alert |
| `reconcile` finds drift (book ≠ venue) | Trust the **venue**, correct the book, alert loudly — never silently overwrite |
| Cap would be breached | Reject the open pre-trade; log |
| Account balance unreadable | Treat as cap-breach ⇒ reject opens (fail-closed) |
| Telegram wizard interrupted | In-memory wizard state dropped (like `/addsource`); no partial position |
| Thesis scoring/model error | Log; leave record unscored for retry; never prune an unscored record |

All live-order touch-points wrapped so a failure **never** crashes the collect/monitor run or the brief.

## Pre-registered calibration gate (governs the deferred spec-#2 auto-open)

Logging starts now; the gate is fixed **before** results are viewed and reviewed only once enough theses have resolved (realistically ~a year, given long-horizon holds — accepted).

- **Gate 0 — instrument validity:** hand-adjudicate a sample of scored theses; is `p_hat`/market-implied being captured correctly and are outcomes right? Bad measurement ⇒ INCONCLUSIVE, fix before any promotion.
- **Discrimination gate:** do higher-conviction theses realize higher win rates / returns than lower-conviction ones (rank-order), at n large enough to matter?
- **Beats-market gate:** does acting on `p_hat` beat market-implied-at-entry (positive Brier-skill / positive net-of-cost return vs the price)?
- **Decision:** KILL/confident-null (no edge → conviction sleeve stays fully manual, or closes), KEEP (manual-only, keep logging), PROMOTE (machine-proposed → earned auto-open, its own spec-#2 gate). Thresholds rough but fixed up front. Mirrors the `claim-verification-grounding-pilot` and Stage-A discipline.

## Non-goals / deferred (spec #2)

- **Machine-generated theses** from analyst feeds (propose-then-confirm push via callback) and **earned auto-open autonomy**.
- Kelly/confidence-scaled sizing (unlocks only after calibration earns it; still hard-capped).
- Sleeve-B DCA (the last capability, gated on demonstrated drawdown-recovery).
- Any change to equity/crypto (stay paper) or to the brief itself.

## Testing (TDD, offline/deterministic — inject the order layer like `call=`)

- `polygram_live` place/close/reconcile with an **injected** HTTP/order stub: happy path, partial fill, failure→no-row, drift→venue-wins. No network in tests.
- Sleeve A gates: band, spread gate, favorite-side selection, dedup, cap rejection.
- Sleeve A exits: target, stop, time-stop, near-dated settlement — and that paper measurement-holds do **not** fire on live rows.
- Sleeve B: wizard state machine (mirror `/addsource` tests), per-position/total cap rejection, no-DCA guard, hold exempt from auto-exits.
- Calibration: thesis record assembly, Brier-skill scoring vs market-implied, resolution-aware retention (unresolved payload survives the sweep; resolved+grace prunes to summary; unscored never pruned).
- Fail-closed ladder: every failure path leaves book==venue and raises no exception into collect/monitor.

CI has no pandas ⇒ guard any pandas-touching test with `importorskip`. Full gate per `brief-local-run`: `ruff check` + `ruff format --check` + `pytest` (stage all reformatted files).

## Rollout

1. Contract **verified** (2026-07-21, docs pasted into the design thread) — endpoints/shapes captured in Component §1. Remaining live-only checks (auth token works for `/trade/*`, account funded, not geo-blocked) fold into rollout step 4.
2. Build foundation → Sleeve A → Sleeve B as separate plans; tests green locally; push to `main` (solo repo).
3. Deploy (Docker). Keep `PG_LIVE_ENABLED` **off**; confirm paper path unaffected.
4. Fund the account; set caps + minimal stake; flip `PG_LIVE_ENABLED=1` with `PG_A_ENABLED=1` first (systematic fade), Sleeve B once the wizard is validated.
5. Thesis logging accrues from day one; calibration gate reviewed when enough resolve.

## References

- Supersedes/extends: `2026-06-13-phase3-prediction-polygram-design.md`, `2026-06-14-phase4-validation-performance-design.md`, `2026-06-14-phase5-monitor-commands-design.md`.
- Memories: `multi-asset-trading-build` (paper→live was always the arc; "live exec = future want"), `polygram-candidate-search-fix` (matcher entity-token search; positions are PAPER), `self-improving-trading-roadmap` (autonomy earned as data validates — the auto-open gate), `sentiment-sizing-null-decided` (sizing on our conviction signal was a confident null → why launch fixed/minimal).
- Discipline precedent: `2026-06-26-claim-verification-grounding-pilot-design.md` (pre-registered log-only pilot → kill/keep/promote), `2026-06-25-gdelt-signal-validation-spike-design.md` (validate-the-premise), `2026-06-26-severity-weighted-retention-design.md` (lifecycle/additive retention pattern reused for thesis logs).
- Lessons applied: `dockerfile-copy-allowlist` (new top-level module = 3 updates), `telegram-send-long-convention` (growth-prone sends), `signals-parse-error-is-truncation` (post-gen call max_tokens/timeout), `live-state-on-deploy-host` (book/thesis logs live on the host volume, not the repo).

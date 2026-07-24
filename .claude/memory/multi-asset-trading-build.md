---
name: multi-asset-trading-build
description: Status + conventions for the multi-asset (equity/crypto/prediction) paper→live trading build; ALL phases 1–5 DONE + pushed (2026-06-14)
metadata: 
  node_type: memory
  type: project
  originSessionId: 3138e02c-0d04-4ac8-812a-7bbe4a0da34e
---

Multi-session build turning the equity-only paper layer into a unified multi-asset
(equity / crypto-Kraken / prediction-PolyGram) trading subsystem with a paper→live path
gated on validation. **Design spec:** `docs/superpowers/specs/2026-06-13-multi-asset-trading-polygram-design.md`.
**Plans:** `docs/superpowers/plans/` (one file per phase).

**Phasing (planned just-in-time, NOT all up front):** later phases reference interfaces earlier
phases create, so each phase is brainstormed/planned against the *real* code only once the
prior phase lands. Sequence: (1) module extraction → (2) unified polymorphic book + crypto/Kraken
→ (3) PolyGram read client + Claude matcher + prediction lifecycle (resolution|momentum play_type)
→ (4) validation/performance layer + go-live gate + performance-feedback prompt block →
(5) volume-anomaly monitor cron + new Telegram commands.

**Status (2026-06-14):** Phases 1, 2 AND 3 DONE and pushed to origin/main (CI-green locally;
87 tests). Phase 1 split `brief.py` into `common.py` + `trading.py` + `brief.py`
(one-way imports `common ← trading ← brief`). Phase 2 (`plans/2026-06-13-phase2-unified-book-crypto.md`)
delivered the **unified polymorphic book + crypto/Kraken**: `paper-book.json` → one `book.json`
(polymorphic positions carry `asset_class`/`venue`/`execution`/`instrument`/`play_type` + lifecycle
fields), with a one-time non-destructive legacy migration in `load_book` (old `paper-book.json` kept
as backup; field `stooq_symbol`→`instrument`). Asset-class dispatch via `fetch_price(asset_class,
instrument)` + `price_position(p)` (equity→Stooq, crypto→Kraken). Crypto seam: `resolve_kraken_pair`
(static `_KRAKEN_BASE` majors map, BTC→XBT/DOGE→XDG, USD quote, `crypto_ticker_map.json` override —
NO catalogue fetch) + `fetch_kraken_price` (public Ticker, `next(iter(result.values()))` for the
canonical-key alias). `normalize_signals` now emits an 8th field `asset_class` (validate `equity|crypto`,
default `equity`); `SYSTEM_PROMPT` + the emitted schema permit crypto calls. `mode_paper` dedup/reversal
keys are 3-tuples `(asset_class, ticker, direction)`. Phase 3
(`plans/2026-06-13-phase3-prediction-polygram.md`, design `specs/2026-06-13-phase3-prediction-polygram-design.md`)
delivered **prediction markets via PolyGram**: read-only client (`polygram_login`/`_polygram_get` JWT +
401-refresh → `polygram_token.json`; `polygram_search`/`polygram_market`; `_parse_pg_market` parses
PolyGram's JSON-**STRING** `outcomes`/`outcomePrices`/`clobTokenIds` arrays, YES=idx0/NO=idx1). Claude
matcher (`_gather_pg_candidates` dedup+cap `PG_CANDIDATE_CAP`=25 → ONE synchronous no-web-search Messages
call → `_parse_matches` resilient JSON-array parse → `PG_SIMILARITY_FLOOR`=0.60 gate; fed ALL signals, not
just actionable). **Pricer reads `GET /markets/:id` → `outcomePrices[side_index]`, NOT `/api/price`** — one
fetch yields mark AND settlement status; the search/events-list price is STALE (this SUPERSEDED the spec's
`/api/price` plan; `/api/price`+`/api/orderbook` reserved for the Phase-4 cost-haircut, `token_id`
captured-but-unused). Prediction positions are polymorphic with new nullable fields
`outcome`/`side_index`/`token_id`/`target`; `direction="bullish"` always (long the held side; return reuses
`_signal_return` long-sense). Close triggers fork by `play_type`: **momentum** = optional target-cross
(mark≥target) else 4w horizon backstop; **resolution** = hold-to-settlement (`closed AND
uma_status=="resolved"`), IGNORES the 4w horizon, `PG_MAX_HOLD_DAYS`=182 backstop. Opened **silently** in
`mode_paper` (matcher creds-gated on `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` so the suite stays hermetic — no
network in tests); `mode_collect` REORDERED to `deliver→save_signals→clear_batch_state→try/except(mode_paper)`
so a matcher/PolyGram/Claude failure can never re-collect and duplicate the brief. `_record_checkpoints`
extracted (shared equity+prediction MTM). Phase 4
(`specs/2026-06-14-phase4-validation-performance-design.md`, `plans/2026-06-14-phase4-validation-performance.md`)
DONE + pushed (33daef2; 111 tests green): NEW pure module **`validation.py`** (chain `common ← validation`,
`brief ← validation`; does NOT import trading → no cycle) holds `aggregate_performance` (overall + per-dim
`asset_class`/`confidence`/`play_type`/`thesis_ref`; stat dict keys `n,hit_rate(0-100),mean_net,median_net,
mean_edge,n_edge`; excludes positions with `net_return is None`), `evaluate_gate`+`record_gate_history`
(per-asset go-live readiness — moderate defaults ≥30 closed, mean edge>0, hit≥55%, edge positive in last
`GATE_SUSTAINED_EVALS`=2 weekly evals via NEW `paper/gate_history.json`), `performance_report`
(SUPERSEDES+DELETED `paper_scorecard` from trading.py), `performance_prompt_block` (n≥5 floor; injected into
`build_daily_prompt` via new `perf_block=""` param, built in `mode_submit` from `load_book()`),
`daily_trade_message` (pure, last-known marks, no re-pricing; sent in `mode_collect` INSIDE the
post-clear_batch_state try/except so it can't duplicate the brief). **DECISION: book.json stays single source
of truth — NO performance.json** (spec's performance.json superseded; closed positions persist forever in
book). **Benchmark = market index per asset class** stamped at OPEN (`benchmark_entry`: equity `^spx` via
Stooq, crypto BTC/XBT via Kraken, prediction None→baseline 0) and compared at close; legacy pre-Phase-4 opens
get `benchmark_return`/`edge`=null but still count in hit-rate. **Haircut = config bps** (`HAIRCUT_BPS_EQUITY`
10/`_CRYPTO` 26/`_PREDICTION` 200) + real PolyGram `/orderbook/:tokenId` half-spread captured at open
(`entry_spread`, fallback to bps; resolution=entry leg only, momentum=entry+exit bps). Close finalizer
`_stamp_close_metrics` (best-effort, never raises out of a close) stamps `haircut`/`net_return`/
`benchmark_return`/`edge` on ALL 3 close paths (`_close_position_at_market`, `mark_to_market` horizon,
`_settle_prediction`); helpers `fetch_benchmark_level`+`_fetch_pg_half_spread`+`_stamp_open_benchmark` live in
trading.py (lifecycle). **Phase 5 DONE + pushed (d404020; 155 tests green)** — the build is COMPLETE.
Phase 5 (`specs/2026-06-14-phase5-monitor-commands-design.md`, `plans/2026-06-14-phase5-monitor-commands.md`)
added the hourly **`monitor` cron mode** (`mode_monitor`→`run_volume_monitor` in trading.py): sweeps a
cross-asset watched set (NEW `paper/watchlist.json` ∪ open-position instruments, deduped) fetching volume
via NEW parsers reusing existing calls (`fetch_stooq_volume` CSV col 7 / `fetch_kraken_volume` Ticker `v[1]`
24h / `fetch_pg_volume` reads the market-detail `volume24hr`/`volume` field — SUPERSEDED the spec's
`/api/prices-history`, degrades to None if absent) vs a trailing-mean baseline in NEW `paper/volume-history.json`;
anomaly = ratio≥`VOL_SPIKE_MULT`(2.5) gated by per-asset floor + `VOL_MIN_SAMPLES`(5) warm-up + positive
baseline; per-instrument `VOL_ALERT_COOLDOWN_HRS`(12) dedup; `_append_sample` consecutive-duplicate dedup so
daily-resolution equity volume contributes 1 sample/day not 24 (the hourly-cron-vs-daily-data fix). One silent
Telegram msg if any alerts. Decoupled as its own dispatch mode → a monitor failure can't touch the brief.
Four NEW Telegram cmds in `_handle_telegram_update`: `/watch <sym>` (cross-asset; class inferred crypto→equity,
prediction needs explicit `prediction <market_id>`; optional explicit-class override; `watchlist.json` SUPERSEDES
the spec's prediction-only `polygram_watchlist.json`), `/unwatch`, `/positions` (open-only, grouped by asset_class,
live marks via `price_position`+`_signal_return`, `—` if unpriceable), `/performance` (thin wrapper over
`validation.performance_report`). VOL_* knobs in common.py; `newsbrief-monitor` compose service + README + .env.example
wired. Tests in NEW `tests/test_monitor.py` + `tests/test_commands.py`. Deferred Phase-4 polish: `daily_trade_message` does not `html.escape`
the prediction `rationale` (safe today — fixed-format string; revisit if rationale becomes free-form model
text); report omits the per-horizon means + "recently closed" tail the old scorecard had (intentional
redesign).
Two early Phase-2 commits (a31a7b4, 5e8693c) carry a cosmetic U+FEFF BOM in the subject (PowerShell 5.1
artifact) — left as-is, already pushed.

**Testing convention for the split modules (carry into phases 2–5):** when a test
**monkeypatches** behaviour, patch it on the module whose function is *under test* — e.g.
`brief.telegram_send`, `brief.TELEGRAM_CHAT_ID` — because brief's own functions resolve their
module-level names (bound from common at import); patching `common.*` would NOT affect brief's
already-bound value-imports. When a test just **calls a pure function**, either namespace works
(same object). Repointing the wrong way silently breaks tests.

**SDD operational note (carry into Phase 5):** Phases were executed with
superpowers:subagent-driven-development (implementer → spec review → code-quality review per task,
fresh subagent each). Subagents do NOT inherit this session's context, so every dispatch must
explicitly inject the repo env conventions or workers trip: run Python/pytest/ruff via the
**PowerShell** tool ([[python-via-powershell]] — Bash errors "stdin is not a tty") but make the git
**commit via the Bash** tool (PowerShell prepends a U+FEFF BOM to commit subjects). Also: when a phased
task imports a constant before any code consumes it, import only what THAT task uses (later tasks add
their own) rather than pre-importing — keeps `ruff` F401 green without a temporary `# noqa`.
Phase-5 review-loop learning: background-dispatched **review** subagents proved unreliable here — a
review agent's completion `<task-notification>` sometimes echoed the *implementer's* transcript instead
of the reviewer's verdict, and `TaskStop` on a finished reviewer returns "No task found" (they
self-terminate). For small/mechanical tasks the controller should just **verify the diff inline**
(`git show <sha>` + a read of the changed symbols) instead of spawning a third background reviewer —
faster and avoids the echo confusion. Reserve a fresh review subagent for genuinely large/risky tasks
(e.g. the final holistic Opus review across the whole phase, which WAS worth dispatching). Implementer
subagents (foreground or background) were reliable; the flakiness was specifically the extra review hops.

**Why:** this build is the spine the user wants to eventually trade real money on, so it is
built toward the end state deliberately, not phased as MVP-then-rework.
**How to apply:** before planning a new phase, read the spec + the prior phase's plan; verify
against current code (memories may be stale). Follow [[brief-local-run]] for the full pre-push
gate. The [[brief-sources-and-edge-latency-thread]] work is explicitly deferred until AFTER this
build ships.

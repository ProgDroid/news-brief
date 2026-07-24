---
name: stooq-dead-provider-replacement
description: Stooq free CSV pricing is permanently gone (not an outage); replacing with Yahoo+Alpaca market-routed failover
metadata: 
  node_type: memory
  type: project
  originSessionId: 6d8a8a66-1d86-4dd5-a90e-6c64c1ebbf86
---

**Root cause (verified live 2026-06-15, supersedes the "Stooq blocked, outage" note in [[newsbrief-deferred-findings]]):** Stooq did NOT have a transient outage. They (1) **removed** the free light-quote CSV endpoint `/q/l/?s=...&e=csv` — it now returns HTTP **404** "page has been moved" for every symbol incl. `^spx`, with or without a browser UA; and (2) put the remaining CSV endpoints (`/q/d/l/`) behind a **JavaScript proof-of-work bot wall** a plain `requests.get` can never pass. Homepage is HTTP 200, so it's not our IP. All three Stooq calls in `trading.py` (`fetch_stooq_price`, `fetch_stooq_volume`, `_stooq_daily_move`) + the `^spx` benchmark are dead. The code degraded silently because each catches the 404 → logs → returns None → callers skip.

**Provider research conclusion (5 free APIs compared 2026-06-15):** Among free tiers, **only Yahoo** covers UK/DE/FR equities AND real indices. Alpaca/Tiingo/Finnhub are US-only with NO index symbols (proxy S&P 500 via SPY ETF). Twelve Data gates international behind paid Grow (~$79/mo). **T212's API has no quote endpoint** for instruments you don't hold (only `/positions` carries `currentPrice`, holdings-only; metadata is static + throttled 1 req/50s) — reusing the T212 key for prices is a dead end. See [[stooq-ticker-resolution]].

**Decision (user, "Two live providers now" + "Free: Yahoo+Alpaca"):** market-routed failover, $0/mo.
- US equity → Alpaca primary (official, stable, `/v2/stocks/{sym}/snapshot`), Yahoo fallback
- UK/DE/FR → Yahoo only (`.L`/`.DE`/`.PA`); no free backup, degrades to None as today
- S&P 500 → Yahoo `^GSPC`, Alpaca `SPY` ETF fallback
- Yahoo transport: **raw `v8/chart` via requests + browser UA** (NOT yfinance) — verified v8/chart needs no crumb/cookie; matches the Kraken/Stooq code style, avoids library churn. GBp gotcha: Yahoo prices LSE in pence — divide by 100 when `meta.currency=="GBp"`.

**Key de-risk:** persisted `position["instrument"]` (`aapl.us`/`rr.uk`/`exv1.de`) is ALREADY a neutral `base.market` symbol — adapters just retranslate the suffix, so **book.json needs no migration**. Adapters stay IN trading.py (no new module → no Dockerfile COPY/CI-paths change per [[dockerfile-copy-allowlist]]).

**Status:** IMPLEMENTED + on main 2026-06-15 (commits `c730567`..`b6ca2d7`, 8 commits via subagent-driven TDD; plan `docs/superpowers/plans/2026-06-15-equity-price-provider-failover.md`). New seam in trading.py: `Quote` NamedTuple, `_parse_symbol`, `_yahoo_quote`/`_yahoo_fetch` (v8/chart, GBp/100), `_alpaca_quote` (snapshot/IEX), `fetch_quote(base_or_symbol, market=None)` router (US: Alpaca→Yahoo; UK/DE/FR: Yahoo), `fetch_benchmark()` (^GSPC→SPY). `resolve_stooq_symbol`→`resolve_symbol`. Alpaca creds `ALPACA_*` in common.py from `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` env (free signup, paper account, no funding; equities still price via Yahoo without keys). Live smoke verified: AAPL/SHEL.L(GBp✓)/SAP.DE/^GSPC all price.

**Keys configured + Alpaca leg live (2026-06-15):** user added real Alpaca keys; first real `mode_paper` run confirmed the leg authenticates — a bad symbol returned **HTTP 404, not 401/403**, so reaching-the-API ≠ auth failure. Diagnostic rule: an Alpaca `404` in the logs = symbol-not-found (falls through to Yahoo), NOT a credential problem; only 401/403 means the keys are wrong.

**Marker-strip bug found+fixed in the first real run (commit `a63a04b`):** plain marker-laden LSE signals (`SGLNl`/`ARMGl`/`DXJGl`) priced 404 because the marker `l` leaked into the symbol; fixed in `resolve_symbol` (derive base from matched T212 ticker). Full detail + remaining override cases (incl. `KSTRl` US-collision) in [[stooq-ticker-resolution]].

**Market pulse revived (DONE, commit `3552e3e`):** the pulse instruments in `MARKET_SPINE`/`PIN_INSTRUMENTS` are indices/commodities/FX, NOT base.market equities, so they went dark when Stooq died and `fetch_quote` returns None for them. Fix: retagged them as a new `"index"` asset class carrying **raw Yahoo symbols** (`^GSPC`, `DX-Y.NYB`, `GC=F`, `BZ=F`, `USDJPY=X`, `^N225`, `^HSI`) and `fetch_daily_move` prices the `index` class via `_yahoo_fetch(instrument)` directly, bypassing `_parse_symbol`/`fetch_quote`. Verified live: all 7 return real daily moves. Note the working Yahoo forms are `=F` futures / `=X` FX and `DX-Y.NYB` — `XAUUSD=X` and `DX=F` 404. See [[brief-sources-and-edge-latency-thread]].

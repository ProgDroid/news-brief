# Bigdata.com Integration + Sentiment Backtest — Design

- **Date:** 2026-06-19
- **Status:** Design (approved in shape; pending written-spec review)
- **Branding:** "Bigdata.com" (RavenPack). Cite sources, link https://bigdata.com.

## Context & decision

Bigdata.com is RavenPack's AI finance platform: entity-resolved **sentiment scores**, **event detection**, premium sources (FT, transcripts, filings), 20-year archive. A two-round trial (n=2, then full 11-name watchlist sweep, 2026-06-19) validated it on this project's real signals: it resolved single stocks cleanly, surfaced material idiosyncratic drivers the pipeline missed (e.g. an **AVAV securities class action**, CVX Tengiz/legal charge), confirmed/quantified others (MU memory upcycle + 6/24 earnings), and flagged an orthogonal contradiction (RGLD). See memory `bigdata-evaluation-and-trading-split`.

Decision: **integrate the enrichment now** (qualitative value — blind-spot detection, catalysts, premium reading — already justifies it). A **backtest gates the narrower question**: may sentiment/events drive position *sizing*.

This work also marks the start of separating the **digest** (informational) from the **trading** system. We do **not** perform the physical split now; we introduce enrichment as a cleanly-bounded module whose interface **is the future digest↔trading seam**. Hard constraint: this work must make the eventual split *easier*, never harder.

## Goals

- Ship a bounded, flag-gated enrichment module that feeds Bigdata.com structured data into the brief + signals **as read-only context** (no auto-sizing).
- Define the digest↔trading interface via that module (pure inputs→outputs, no new coupling).
- Design a backtest that decides whether sentiment/events earn the right to drive sizing, starting with a scoped feasibility pilot.

## Non-goals (YAGNI)

- No physical split of brief/trading into separate services/repos yet.
- No auto-population of any signal field that drives position sizing (gated on the backtest).
- No adoption of Bigdata.com's `bigdata-briefs-v2` service (FastAPI + SQLite + OpenAI + fixed universes) — we borrow its *patterns*, keep Claude, keep our pipeline.
- No crypto/ETF company-sentiment (not supported; handled via thematic search + existing Chroma/RSS).

## Locked decisions (from brainstorming)

| Topic | Decision |
|---|---|
| Backtest role | Gates *quantitative sizing* only; enrichment ships regardless |
| Backtest hypothesis | Sentiment→forward-returns (IC/quantile) **with** event-conditioned slice — one framework, two readouts |
| Backtest universe/horizon | Broad liquid universe; **1d–3mo horizon sweep** (discovery) |
| System split | Define the seam via bounded module now; full split later; must not hamper split |
| API access | REST Search API + `BIGDATA_API_KEY`; **flag-gated OFF**, fixture-backed; flip on when business-email REST creds land |
| Enrichment scope | **Targeted**: per active watchlist single-stock → sentiment tearsheet + events (earnings & conference); per active ETF theme/Top-Story → one thematic search |
| LLM | Keep Claude (Sonnet 4.6 batch) for synthesis |

## Architecture

```
        ┌─────────────────────────────────────────────┐
        │  Bigdata access layer (shared)               │
        │  REST client + BIGDATA_API_KEY               │
        │  feature flag (default OFF) · fixtures · TTL  │
        │  graceful-degrade to RSS+Chroma+web_search    │
        └───────────────┬──────────────────────────────┘
            ┌───────────┴────────────┐
            ▼                         ▼
   Feature A: enrichment/     Feature B: backtest/
   (ship first, v1)           (R&D; gates sizing)
```

### Shared access layer
Thin REST client wrapping the calls the trial proved: `find_securities`, `bigdata_search` (smart, timestamp-filterable), `sentiment_tearsheet`, `events_calendar`. Auth via `BIGDATA_API_KEY`. A module-level **feature flag** (env/config) defaults OFF; when off (or on any client error/timeout), callers receive empty bundles and the pipeline runs exactly as today. All units logged. **Interim (no REST creds yet):** the same query shapes are exercised via the MCP connector in-session for the pilot and prototype; the REST client is the production path.

### Feature A — Enrichment v1
Module `enrichment/` (new first-party module → **requires Dockerfile COPY allowlist + CI workflow path updates**, per memory `dockerfile-copy-allowlist`).

- **Inputs (explicit universe per run):** single-stocks = dedup union of open positions (`book.json`) ∪ `/watch` watchlist ∪ tickers in the latest signals snapshot. Themes = `feedback.json` pins (e.g. ukraine, iran) ∪ ETF-watchlist underlying themes (Japan/yen, European banks, China STAR, defence, gold) ∪ the brief's Top Stories. Reads existing artifacts only — no new coupling.
- **Ticker resolution:** strip `l`/`d` LSE/Xetra markers to base, resolve via `find_securities`, **cache `rp_entity_id`** (don't re-resolve; see `stooq-ticker-resolution`). ETFs resolve as funds → no company sentiment; BTC → no entity (thematic search only).
- **Per-symbol bundle** (trading-facing): `{ticker, rp_entity_id, sentiment{current,baseline,zscore_1mo,zscore_1qt,regime}, events[earnings+conference], top_evidence_docs}`.
- **Thematic bundle** (digest-facing): per active theme, a `bigdata_search` result **passed through a novelty filter** (cookbook pattern: embedding-dedup + claim check against recent briefs) so it doesn't repeat prior coverage.
- **Output:** both bundles serialized into a clearly-delimited `prompt_user` context section in `submit`. Claude reads it; **it never auto-writes a sizing field.** A descriptive `bigdata_sentiment` annotation on signals is permitted (informational), explicitly distinct from any sizing input.
- **Interface (the seam):** the two bundles are the digest↔trading contract — plain dataclasses/dicts, independently consumable by a future standalone digest or trading system.
- **Degradation:** flag OFF / Bigdata unreachable → bundles empty → today's pipeline unchanged.

### Feature B — Backtest
- **Hypothesis:** rank IC + quantile forward returns + hit rate of sentiment level/Δ vs forward returns; **event-conditioned slice** (same data, filtered to windows with a detected event).
- **Universe/horizon:** broad liquid universe (e.g. their Top US/EU 100 or S&P 500); **1d–3mo horizon sweep**.
- **Discovery vs confirmation discipline:** the sweep is discovery and invites false positives → split sample (or pre-register horizons) and confirm the best horizon on **held-out data**. Report effect sizes with multiple-comparison caveat, not just p-values.
- **Data path (the one real feasibility risk):** tested tools return *current* sentiment only → historical series must be **reconstructed from time-windowed `bigdata_search`** (per-doc sentiment aggregated per window). Over universe × years × horizons this is many query units with unknown depth/rate-limit on a personal account. **Mitigation: scoped feasibility pilot first** (~50–100 names, ~2–3 years) to confirm reconstruction is affordable and yields signal, *before* a full sweep. In parallel, check whether the REST API exposes a purpose-built historical-sentiment dataset (RavenPack's core product) — far cheaper if reachable.
- **Deliverable:** a notebook/script producing the IC/quantile/hit-rate tables + a go/no-go on sizing.

## Error handling & degradation
- All Bigdata calls wrapped; any failure → empty bundle + warning log, never breaks the brief (mirrors `_dump_raw_batch_result`'s swallow-and-continue).
- Per-run query-unit usage logged (extends the cost-logging shipped in `e6b98df`).
- Rate-limit/budget guard: bounded fan-out (watchlist-sized); a max-units-per-run ceiling.

## Testing
- Unit tests for `enrichment/` against **recorded fixtures** (captured from the live trial) — **no live Bigdata calls in CI**.
- Resolution/marker-stripping, bundle assembly, novelty filter, and graceful-degradation (flag OFF, client error) all covered.
- Full pre-push gate unchanged: `ruff check` + `ruff format --check` + `pytest` (see `brief-local-run`).

## Sequencing
1. Shared access layer (flag-gated, fixtures).
2. Feature A enrichment v1 (build first — committed, low-risk, immediate value).
3. Feature B backtest — scoped pilot, then (if pilot clears) full sweep.

## Open risks
- **Business-email REST access** unresolved (personal email denied). Interim: MCP-assisted for pilot/prototype; resolve via custom-domain email or sales for production. Does not block build/validate.
- **Historical data reconstruction** cost/feasibility — the pilot exists to retire this risk early.
- **Novelty filter in v1 vs defer** — include if cheap; otherwise fast-follow.

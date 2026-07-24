---
name: bigdata-enrichment-shipped
description: "Feature A — Bigdata.com enrichment access layer + brief integration — SHIPPED 2026-06-20, flag-gated OFF (dark); how it works, how to enable, known v1 gaps"
metadata: 
  node_type: memory
  type: project
  originSessionId: b60250e5-457e-471f-ae28-1f43f2dc9400
---

**SHIPPED 2026-06-20 (pushed, commits 37b847e..0f110eb on main).** The Bigdata.com enrichment "shared access layer + Feature A" from the design spec is built, reviewed (8 subagent-driven tasks + final whole-branch review), 272 tests pass. See decision/eval in [[bigdata-evaluation-and-trading-split]]; plan at `docs/superpowers/plans/2026-06-20-bigdata-enrichment-access-layer.md`; spec at `docs/superpowers/specs/2026-06-19-bigdata-integration-and-backtest-design.md`.

**What it is:** new first-party `enrichment/` package feeding Bigdata.com sentiment/events/evidence into the daily brief as READ-ONLY context, behind a vendor-neutral provider seam. Ships **dark** — with the flag off (default) the brief + saved signals are byte-for-byte unchanged.

**Package layout:** `enrichment/` = `config.py` (env flags), `models.py` (frozen dataclasses `SentimentScore/Event/EvidenceDoc/SymbolBundle/ThematicBundle/EnrichmentBundles` + `to_dict`/`*_from_dict`), `providers.py` (`Provider` protocol + `NullProvider`/`FixtureProvider` + `get_provider()`), `providers_bigdata.py` (`BigdataProvider` REST client), `universe.py` (`build_universe`, `normalize_ticker`, `latest_signal_tickers`), `build.py` (`build_enrichment`), `render.py` (`render_prompt_block`, `annotate_signals`).

**Env flags (all optional; default = dark/no-op):** `ENRICHMENT_ENABLED` (default "0"; master switch), `ENRICHMENT_PROVIDER` (`null|fixture|bigdata`; empty=auto → bigdata if a key is set else null), `BIGDATA_API_KEY`, `BIGDATA_BASE_URL` (default `https://api.bigdata.com`), `ENRICHMENT_MAX_SYMBOLS` (20), `ENRICHMENT_MAX_THEMES` (8), `ENRICHMENT_HTTP_TIMEOUT` (20), `ENRICHMENT_FIXTURE_DIR`.

**Wiring (brief.py):** `mode_submit` builds the universe (open equity positions ∪ `/watch` equity items ∪ latest signals-snapshot tickers ∪ feedback pins; known ETFs route to *themes* not symbols), calls `build_enrichment`, persists a snapshot to `DATA_DIR/enrichment/enrichment-<date>.json` (only when non-empty), and injects `render_prompt_block` as a delimited read-only section of the daily prompt. `mode_collect` reloads that snapshot and `annotate_signals` adds a DESCRIPTIVE `bigdata_sentiment` dict to saved signals by base-ticker match. Both wiring points are try/except-isolated; provider methods degrade-not-crash. **Invariant: enrichment NEVER writes a sizing field** — sentiment is media-tone overlay only (gated by the future backtest). trading.py reads only direction/confidence/ticker/asset_class/entry_price — never `bigdata_sentiment`.

**Vendor seam is real:** swapping to an alternative (Alpha Vantage etc., see landscape in [[bigdata-evaluation-and-trading-split]]) = one new file implementing `Provider`. The two bundle types are the digest↔trading contract.

**HOW TO ENABLE / VALIDATE:**
- Validate now without REST creds: `ENRICHMENT_ENABLED=1 ENRICHMENT_PROVIDER=fixture ENRICHMENT_FIXTURE_DIR=<dir>` with model-level JSON fixtures (capture real ones in-session via the connected Bigdata.com MCP tools — `find_securities`/`bigdata_sentiment_tearsheet`/`bigdata_events_calendar`; cached entity ids CVX=D54E62 MU=49BBBC RGLD=263216 ESLT=0401A0 AVAV=F1EB39).
- Production: set `BIGDATA_API_KEY` (needs business-email REST key — still outstanding) + `ENRICHMENT_ENABLED=1`.

**KNOWN v1 GAPS / verify-at-enable:**
1. **REST endpoint paths + field names are UNVERIFIED vs live `docs.bigdata.com`** — `BigdataProvider` was built to MCP-trial-shaped fixtures. FIRST thing to do at live enable: log one raw `_post` response and confirm the JSON paths in `providers_bigdata.py` match (else parsing silently yields empty/error bundles, which degrade quietly).
2. **`BigdataProvider` does NOT populate `SymbolBundle.evidence` in v1** (defaults `[]`; documented in its docstring). Thematic evidence comes via `thematic_bundle`. Add a per-symbol evidence search when wanted.
3. **"Top Stories" theme source deferred** — they don't exist at submit time (they're the batch output); v1 themes = pins ∪ ETF-watchlist themes. A collect-time re-enrich pass would be the fast-follow.
4. **Feature B backtest = separate future plan** (sentiment→forward-returns IC/quantile, gates whether sentiment may drive sizing). Not built.

New package required Dockerfile `COPY enrichment/` + CI `paths`/lint updates — done (see [[dockerfile-copy-allowlist]]).

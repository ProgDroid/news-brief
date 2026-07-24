---
name: bigdata-enrichment-validated-live
description: Feature A enrichment flag-on path validated end-to-end with REAL Bigdata.com MCP data on 2026-06-20 (Step 2 of bigdata-next-steps DONE)
metadata: 
  node_type: memory
  type: project
  originSessionId: 4356315d-665d-43bb-a1c0-dba9009690e0
---

**DONE 2026-06-20 — Step 2 of [[bigdata-next-steps]].** Captured live Bigdata.com MCP fixtures and exercised the [[bigdata-enrichment-shipped]] flag-on path (`ENRICHMENT_ENABLED=1 ENRICHMENT_PROVIDER=fixture`) end-to-end with the user's REAL live book/signals (provided in-session; the live state lives on the remote deploy host's `${APPDATA_DIR}/news-brief` volume, NOT on the dev machine).

**Universe derived via real `build_universe`:** tickers = ESLT, NEE, RGLD, RR, MU, CVX, VEUA, SPOL, AVAV, AVAV_ (10); themes = ukraine, iran, defence, European banks, Japan equities and yen, gold (6). 7 single stocks resolved cleanly; VEUA/SPOL/AVAV_ are bugs (see [[enrichment-universe-bugs]]).

**Captured (real, 2026-06-20):** sentiment tearsheets + events calendars for the 7 single stocks, thematic searches for 5/6 themes. Entity ids: CVX=D54E62 MU=49BBBC RGLD=263216 ESLT=0401A0 AVAV=F1EB39 (cached, all confirmed) + **NEW: NEE=2CB4C9, RR=947B28** (RR=Rolls-Royce LSE, not Richtech Robotics XNAS:RR). Normalized fixtures saved durably (uncommitted) to `enrichment/fixtures/live_2026-06-20/` (7 symbol_*.json + 5 theme_*.json).

**KEY RESULT — gap #1 (unverified REST field names) DE-RISKED for MCP shapes.** Both tearsheet and events calendar returned STRUCTURED JSON (not the markdown the tool docstrings imply). `bigdata_sentiment_tearsheet` → `signals.sentiment.{current,baseline,momentum,zscore_1mo,zscore_1qt}` — these EXACTLY match the `SentimentScore` model fields. Evidence under `evidence.docs[]` = {headline, source, timestamp, sentiment, url, relevance}. Events under `events[]` = {rp_entity_id, ticker, category, title, event_datetime, fiscal_year, fiscal_period, source_url}. NB: this is the MCP backend shape; the REST Search API (`providers_bigdata.py`) may still differ — confirm one raw `_post` at live REST enable. (BigdataProvider also still doesn't populate per-symbol evidence in v1; fixtures DO, so the evidence render path is now exercised.)

**Validation outcomes (all pass):** render_prompt_block → 5492-char read-only block (caveat + per-symbol sentiment/events/evidence + thematic docs); snapshot persisted + reloaded via `bundles_from_dict` (SentimentScore/EvidenceDoc dataclasses reconstruct correctly); `annotate_signals` attached `bigdata_sentiment` to 4/8 live signals (ESLT/CVX/RGLD/MU). The 4 misses: AVAV (the AVAV_ quirk), SGLN/SPOL/DXJG (ETF themes, no symbol bundle — expected). "Japan equities and yen" theme rendered empty (live smart-search returned 0 docs — over-constrained the query).

**Still remaining:** Step 1 (Feature B sentiment backtest plan + scoped pilot) and the business-email REST key. Fix [[enrichment-universe-bugs]] before flipping to live `bigdata`.

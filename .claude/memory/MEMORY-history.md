# Memory History - news-brief (shipped milestones)

Archived from `MEMORY.md` to keep the always-loaded index lean. These are "work shipped" markers. **Not auto-loaded each session.** Read on demand when you need context on past work; the full detail lives in each linked topic file and in git history.

## Bigdata enrichment and sources redesign (shipped)

- [Sources & edge-latency thread (2026-06-14)](brief-sources-and-edge-latency-thread.md) — SHIPPED 2026-06-14: pinned-hybrid brief (/pin /unpin) + region-native sources (kind tags) + market pulse + forward tilt; revisit edge ~2026-06-28
- [Bigdata enrichment SHIPPED (Feature A) (2026-06-20)](bigdata-enrichment-shipped.md) — 2026-06-20: enrichment/ package + brief integration, flag-gated DARK (ENRICHMENT_ENABLED off); provider seam (Null/Fixture/Bigdata); read-only/never-sizing; how to enable; v1 gaps (UNVERIFIED REST field names, no evidence in v1, Top-Stories deferred, backtest=Feature B pending)
- [Bigdata enrichment validated live (2026-06-20)](bigdata-enrichment-validated-live.md) — 2026-06-20 Step 2 DONE: flag-on path exercised end-to-end with REAL MCP data; gap #1 (REST field names) de-risked for MCP shapes; fixtures saved to enrichment/fixtures/live_2026-06-20/; NEE=2CB4C9 RR=947B28 new entity ids
- [Bigdata MCP enrichment brainstorm (2026-06-24)](bigdata-mcp-enrichment-brainstorm.md) — 2026-06-24: drive bigdata MCP connector as headless live-enrichment provider; self-serve DEAD-END (business-email gate); MCP-primary+AV-fallback behind existing seam; sizing track now CLOSED null → key (if it lands) = DESCRIPTIVE brief depth only, see [[sentiment-sizing-null-decided]]

## Shipped milestones

- [Multi-asset trading build (2026-06-14)](multi-asset-trading-build.md) — equity/crypto/prediction paper→live; ALL phases 1–5 DONE + pushed 2026-06-14 (modules + unified book.json + crypto/Kraken + PolyGram prediction + validation.py/go-live gate + volume monitor cron + /watch /unwatch /positions /performance cmds); build COMPLETE → [[brief-sources-and-edge-latency-thread]] is next

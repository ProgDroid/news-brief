---
name: bigdata-enable-spec-plan
description: "2026-07-19 — Bigdata enrichment ENABLE: spec + 9-task TDD plan written & committed (not yet implemented). Decisions: option-b native-field SentimentScore + smoothed trend, themes toggle, ToS derived-only snapshot. Builds on [[bigdata-rest-api-verified]]."
metadata: 
  node_type: memory
  type: project
  originSessionId: c950c1f3-0f76-4fd8-a64a-850bb83ada91
  modified: 2026-07-20T07:22:40.777Z
---

**2026-07-19: after the PAYG key landed and shapes were live-verified ([[bigdata-rest-api-verified]]), ran brainstorming → writing-plans for ENABLING the dark enrichment.** Spec + plan committed to main (NOT yet implemented):
- spec: `docs/superpowers/specs/2026-07-19-bigdata-enrichment-enable-design.md` (commit 438179e)
- plan: `docs/superpowers/plans/2026-07-19-bigdata-enrichment-enable.md` (commit e8231ea; 9 TDD tasks)

**Decisions banked in the design:**
1. **SentimentScore reshape = option b** (vendor native fields, NOT re-derived z/regime): `SentimentScore(as_of, daily_sentiment, sentiment_pressure, abnormal_media_attention, trend_mean, trend_delta, n_points)`. `sentiment_pressure`/`abnormal_media_attention` already encode "abnormal vs baseline" so we don't recompute z-scores. Plus a smoothed trend (mean + delta) from the same single call.
2. **Themes toggleable** — new `ENRICHMENT_THEMES_ENABLED` (default "1"); user "kind of wants" thematic search but wants to switch it off if low-value (it overlaps existing RSS/Chroma coverage; per-STOCK sentiment+events is the non-replicable value). When off, `build_enrichment` skips search fan-out.
3. **ToS derived-only snapshot** — `EnrichmentBundles.to_persisted_dict()` persists only `{as_of, provider, symbols:[{ticker, rp_entity_id, sentiment}]}`; drops all Content (headlines/search text/thematic docs/event titles). Safe because collect's `annotate_signals` only reads symbol sentiment; the content-bearing prompt block is transient (submit-only).
4. **No SDK** (`bigdata-client` being sunset by 2026-12-31) → hand-rolled `requests`, no new dep/Dockerfile/CI change.
5. Scope STILL descriptive-only (sizing null stands, [[sentiment-sizing-null-decided]]).

**COST anchor (from user):** PAYG `balance:1000` = **$10.00 (cents)**; the whole multi-endpoint probe cost ~$0.03. Est: symbols-only ~$2.7/mo, +8 themes ~$8.3/mo; $10 trial covers months. Cost is NOT a constraint → scope decisions are simplicity/value, not budget. Search ≈528 units vs 50 sentiment/events, 1 resolve.

**Plan tasks (TDD, per-task commit):** T1 themes flag; T2 SentimentScore + to_persisted_dict; T3 render/annotate; T4 provider HTTP rewrite (X-API-KEY + real paths/bodies + window helpers); T5 symbol parse (resolve exact-ticker `_pick_entity` + latest+trend + events flat shape); T6 thematic parse (/v1/search per-chunk); T7 fixture update; T8 brief.py derived-only snapshot + usage logging; T9 full gate. Raw fixtures use REAL probe-captured bodies (AAPL/D8442A). Window consts: SENTIMENT_LOOKBACK_DAYS=60, EVENTS_FORWARD_DAYS=90, SEARCH_MAX_CHUNKS=2.

**IMPLEMENTED + PUSHED 2026-07-19 (subagent-driven-development, all 9 tasks): commits de75bde..d4c8ca2 on main; pushed fa4ece6..d4c8ca2 -> origin/main, Docker deploy triggered.** 583 tests pass, ruff clean. Final whole-branch review (Opus) = READY TO MERGE, no Critical/Important; all 5 invariants verified in source (descriptive-only — trading.py never reads bigdata_sentiment; ToS whitelist snapshot; degrade-never-crash; X-API-KEY; requests-only). Per-task reviews: T4/T5/T8 + final used SUBAGENT reviewers (they WORKED — no stall this run; user said try them again); T1/T3/T6/T7 reviewed inline. T7 (fixture update) was pulled EARLY (before T3) to clear a fixture-load-crash landmine. Cleanup commit d4c8ca2 restored 3 render/annotate guards a mid-task dropped, deleted 4 orphaned fixtures, reconciled spec resolve text to safer exact-match-only. BOM clean on all 9 commits (Bash commits held). Deferred non-blocking minors: partial-null sentiment series untested; sequential sentiment→events fetch (bad events response degrades whole bundle — revisit only if events flaky at enable).

**NEXT (user's call):** PUSH = Docker deploy. After deploy, ENABLE on host: `ENRICHMENT_ENABLED=1 ENRICHMENT_PROVIDER=bigdata ENRICHMENT_THEMES_ENABLED=1` (BIGDATA_API_KEY already set). First live run: confirm `Enrichment built ... errors=0` + `Enrichment usage: ... units=N`, eyeball derived-only enrichment-{date}.json (NO headlines/titles), cross-check units vs $10 balance at app.bigdata.com/usage. Provider `usage` block is top-level for resolve but under `metadata.usage` for sentiment/events/search (accumulator checks both). If themes low-value → ENRICHMENT_THEMES_ENABLED=0.

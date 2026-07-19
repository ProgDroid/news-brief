# Bigdata.com enrichment — enable on live REST (design)

**Date:** 2026-07-19
**Status:** approved (brainstorming), pending implementation plan
**Predecessors:** `2026-06-19-bigdata-integration-and-backtest-design.md` (Feature A shipped dark);
`sentiment-sizing-null-decided` (sizing closed null → enrichment is descriptive-only, forever)

## Context & premise

The Bigdata.com PAYG REST key has landed (Developer Platform key, `X-API-KEY` header
against `api.bigdata.com`). The `enrichment/` package shipped dark in June 2026 with a
`BigdataProvider` REST client fitted to MCP-trial-shaped *guesses*. A live probe on
2026-07-19 (throwaway `scratchpad/bigdata_probe.py`, all endpoints HTTP 200) confirmed the
real API shapes and found the existing provider **wrong in nearly every particular**: wrong
auth header, wrong methods, wrong paths, wrong payloads, wrong response field paths.

Enabling is therefore a **provider rewrite + data-model reshape + ToS-compliance fix**, not a
flag flip. Scope stays **descriptive-only** — sentiment never influences position sizing (the
sizing backtest returned a confident null, n≈7k). Value = brief depth / research-offload
(entity-resolved sentiment + upcoming events on held positions), which the June trial evidenced
(AVAV securities-class-action catch, CVX Tengiz/$360M-legal blind spots, MU earnings-into-print).

## Verified live API shapes (probe, 2026-07-19)

Auth: header **`X-API-KEY: <key>`** (NOT `Authorization: Bearer`). Base `https://api.bigdata.com`.

| Purpose | Verb + path | Request (confirmed) | Response (fields used) | Cost (usage units) |
|---|---|---|---|---|
| Resolve | `POST /v1/knowledge-graph/companies` | `{"query":"AVAV"}` | `{"results":[{id,name,ticker,type,country,sector,...} × ~20]}` — id is **`id`** (6-char), **not** `rp_entity_id`; ~20 fuzzy rows | `knowledge_graph_tokens: 1` |
| Sentiment | `POST /v1/entity-sentiment/` | `{"identifier":{"type":"rp_entity_id","value":ID},"timestamp":{"start":YYYY-MM-DD,"end":YYYY-MM-DD}}` | `{"results":[{name,rp_entity_id,values:[{date,daily_sentiment,sentiment_pressure,abnormal_media_attention}]}],"errors":[]}` — daily series | `company_sentiment_tokens: 50` |
| Events | `POST /v1/events-calendar/query` | **FLAT** `{"rp_entity_id":[ID],"start_date":…,"end_date":…,"categories":["earnings-call","conference-call"],"limit":100}` | `{"results":{ID:[{category,event_datetime,title,fiscal_year,fiscal_period,rp_collection_id,...}]},"pagination":{…}}` — keyed by entity id; event fields **`event_datetime`** / **`title`**, **no url** | `corporate_calendar_tokens: 50` |
| Search | `POST /v1/search` | `{"search_mode":"fast","query":{"text":…,"filters":{"entity":{"any_of":[ID]}},"max_chunks":2}}` | `{"results":[{id,headline,timestamp,source:{id,name,rank,tier},url,chunks:[{cnum,text,relevance,sentiment}]}]}` — sentiment is **per-chunk**; `source.name` nested; key is `results` not `documents` | `premium_news_tokens + web_tokens ≈ 528` |

Guessed shapes that FAILED silently in the probe (why we verify): the nested `identifier`/`timestamp`
events body returned HTTP 200 with a **global 41-entity calendar** (target entity absent, date window
ignored). The flat `rp_entity_id` body filters correctly to the requested entity.

**Pricing anchor:** PAYG `balance` is denominated in **cents** (`1000` = $10 free trial); the entire
multi-endpoint probe cost ~$0.03. Content tiers price tokens differently, so treat unit→$ as
order-of-magnitude. Estimated daily fan-out cost: symbols-only ~$2.7/mo, symbols+8 themes ~$8.3/mo.

## Decisions (from brainstorming)

1. **Scope:** symbols always on; thematic search **toggleable** via new `ENRICHMENT_THEMES_ENABLED`
   (default `"1"`), so it can be switched off without touching the rest if it proves low-value.
2. **SentimentScore reshape (option b + smoothed trend):** adopt the vendor's native fields plus a
   cheap smoothed trend from the same single call. The native `sentiment_pressure` /
   `abnormal_media_attention` already encode "abnormal vs baseline", so we do NOT re-derive z-scores.
3. **ToS-compliant snapshot (option A):** persist only derived sentiment + entity id; drop all Content
   (headlines, search text, thematic docs, event titles) from disk. Collect only reads sentiment.
4. **No SDK** — hand-rolled `requests`; the `bigdata-client` SDK is being sunset (migrate-to-REST by
   2026-12-31). No new dependency, no Dockerfile/CI change.

## Component design

### `enrichment/config.py`
Add `ENRICHMENT_THEMES_ENABLED = os.environ.get("ENRICHMENT_THEMES_ENABLED", "1") == "1"`.
(Existing flags unchanged: `ENRICHMENT_ENABLED`, `ENRICHMENT_PROVIDER`, `BIGDATA_API_KEY`,
`BIGDATA_BASE_URL`, `ENRICHMENT_MAX_SYMBOLS=20`, `ENRICHMENT_MAX_THEMES=8`, `ENRICHMENT_HTTP_TIMEOUT`,
`FIXTURE_DIR`.)

### `enrichment/models.py` — `SentimentScore`
```python
@dataclass(frozen=True)
class SentimentScore:
    as_of: str | None                       # latest point's date
    daily_sentiment: float | None           # latest, -1..1
    sentiment_pressure: float | None        # latest native abnormality signal
    abnormal_media_attention: float | None  # latest native signal
    trend_mean: float | None                # mean daily_sentiment over the returned window
    trend_delta: float | None               # latest daily_sentiment - trend_mean
    n_points: int = 0                        # series length backing the stats
```
- `symbol_from_dict` unchanged mechanically (`SentimentScore(**s)`), but the persisted/fixture dict
  now carries the new keys.
- `Event`, `EvidenceDoc`, `SymbolBundle`, `ThematicBundle`, `EnrichmentBundles` unchanged in shape.
- **New** `EnrichmentBundles.to_persisted_dict()` → `{as_of, provider, symbols:[{ticker, rp_entity_id,
  sentiment}]}` (sentiment as its numeric dict or null; **no** events/evidence/themes). Used by the
  submit-time snapshot write. `to_dict()` retained for in-memory/full use and tests.

### `enrichment/providers_bigdata.py` — rewrite
- Header `X-API-KEY`. Keep `requests.Session`, `ENRICHMENT_HTTP_TIMEOUT`, per-run entity cache,
  degrade-never-crash try/except returning error-tagged bundles.
- **Resolve:** `POST /v1/knowledge-graph/companies` `{"query": ticker}`; from `results`, pick the row
  whose `ticker` matches (case-insensitive) AND `type == "PUBLIC"`; fall back to the first
  exact-ticker row; return `None` if no row's ticker matches (→ `error="no entity match"`). Entity
  id = `id`.
- **Sentiment:** `POST /v1/entity-sentiment/` with the nested identifier/timestamp body; window =
  trailing `SENTIMENT_LOOKBACK_DAYS` (module const, default 60) ending today (UTC). Parse
  `results[0].values`; sort by date; latest point → `daily_sentiment`/`sentiment_pressure`/
  `abnormal_media_attention`/`as_of`; `trend_mean` = mean of `daily_sentiment` over the returned
  points; `trend_delta` = latest − mean; `n_points` = len. Empty series → `sentiment=None`.
- **Events:** `POST /v1/events-calendar/query` flat body; window = today → +`EVENTS_FORWARD_DAYS`
  (const, default 90); parse `results.get(id, [])` → `Event(category, title, event_datetime[:10],
  url=None)`.
- **Search (thematic):** `POST /v1/search` fast mode, `max_chunks` (const, default 2); parse
  `results[]` → `EvidenceDoc(headline, source.get("name",""), timestamp[:10], url,
  sentiment=chunks[0].sentiment if chunks else None)`.
- `symbol_bundle` still leaves `evidence=[]` in v1 (per-symbol evidence search deferred);
  `thematic_bundle` returns the search docs.

### `enrichment/build.py`
Gate the theme fan-out on `config.ENRICHMENT_THEMES_ENABLED`: when off, `themes = []` and no
`thematic_bundle` calls. Log line adds the `themes_enabled` flag. Symbol fan-out unchanged.

### `enrichment/render.py`
- `_fmt_sentiment` rewritten for the new fields, e.g.
  `sent={daily_sentiment} pressure={sentiment_pressure} attn={abnormal_media_attention}
  trend={trend_delta:+} (mean {trend_mean} over {n_points}d)`, None-safe.
- `annotate_signals`' `bigdata_sentiment` dict → `{daily_sentiment, sentiment_pressure,
  abnormal_media_attention, trend_delta, rp_entity_id}`.
- `_CAVEAT` unchanged (media-tone, never a trigger).

### `brief.py` wiring
- `mode_submit`: persist `bundles.to_persisted_dict()` (not `to_dict()`) to
  `DATA_DIR/enrichment/enrichment-{today}.json`. `render_prompt_block` still uses the full in-memory
  bundles for the prompt (transient). Concrete cost-visibility requirement: the provider accumulates
  the per-call `usage` tokens and `build_enrichment` logs the run total at INFO. **Gotcha:** the
  `usage` block sits at **top level** for resolve but under **`metadata.usage`** for
  sentiment/events/search — the accumulator must check both locations. (Ad-hoc raw-response
  inspection at enable time is an operator step under Rollout, not a code requirement.)
- `mode_collect`: unchanged — reload snapshot → `annotate_signals` (only needs sentiment).

## Error handling
Unchanged philosophy: every provider method degrades to an error-tagged empty bundle rather than
raising; both brief.py wiring points remain `try/except`-isolated so the brief/signals never break on
an enrichment failure. Silent-empty degradation is acceptable by design (descriptive overlay only).

## Testing (TDD)
Capture real fixtures from the probe outputs into `enrichment/fixtures/live_2026-07-19/` (model-level
JSON, Content-stripped where persisted). Unit tests:
- resolve: exact-ticker + PUBLIC selection from a ~20-row fuzzy result; no-match → error bundle.
- sentiment: latest-point extraction, `trend_mean`/`trend_delta`/`n_points` math, empty-series → None.
- events: flat-body parse, `results[id]` extraction, `event_datetime[:10]` date, url None.
- search: per-chunk sentiment mapping, `source.name`, empty chunks → None sentiment.
- `to_persisted_dict` drops events/evidence/themes and round-trips through `bundles_from_dict` into a
  valid annotate input.
- `build_enrichment` skips themes when `ENRICHMENT_THEMES_ENABLED` is off.
- degrade paths: HTTP error / bad JSON → error-tagged bundle, no raise.
No pandas involved. Gate: `ruff check` + `ruff format --check` + `pytest` (full suite).

## Rollout / verify-at-enable
1. Land code (dark; defaults unchanged). Dockerfile already `COPY enrichment/`; no new dep.
2. On host: `ENRICHMENT_ENABLED=1 ENRICHMENT_PROVIDER=bigdata` (key already present).
   `ENRICHMENT_THEMES_ENABLED=1` initially to evaluate themes.
3. First live run: confirm no error-tagged bundles in the log, eyeball the rendered prompt block +
   the derived-only `enrichment-{today}.json`, and check the run's logged `usage` tokens against the
   $10 balance at `app.bigdata.com/usage`.
4. If themes prove low-value, set `ENRICHMENT_THEMES_ENABLED=0`.

## Out of scope (v1)
Per-symbol evidence search; analyst-estimates / earnings-surprises / search-volume / co-mentions
endpoints; collect-time re-enrich for Top-Stories themes; entity-cache persistence across runs.

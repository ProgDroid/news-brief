# Alpha Vantage Sentiment Backtest — Design Spec

**Date:** 2026-06-24
**Status:** Approved (brainstorm) → pending implementation plan
**Author:** Fernando Ferreira (with Claude)
**Related:** `docs/superpowers/specs/2026-06-19-bigdata-integration-and-backtest-design.md` (original Feature-B spec), memory `av-sentiment-backtest-validated`, `bigdata-mcp-enrichment-brainstorm`

---

## 1. Context & Goal

Feature B asks one question: **does sentiment predict forward returns — i.e. may sentiment ever drive position _sizing_?** This is the go/no-go gate before any sentiment signal is allowed to affect trades. Enrichment (Feature A) stays descriptive-only regardless; this backtest is the separate gate for autonomous sizing.

The interim MCP-approx pilot (`backtest/pilot/`, commit `cf29a4b`) confirmed the pipeline end-to-end but was **statistically inconclusive at n≈18** (3 names, all trending up — a power + confound problem). The faithful path through bigdata was retired: bigdata's MCP exposes no historical-sentiment time-series, the REST archive is gated behind a business-email key, **and** bigdata's ToS bars "creat[ing] a database of the Bigdata Content" — so a bigdata-historical backtest is both impractical and ToS-barred.

**Alpha Vantage** (live-tested 2026-06-24, memory `av-sentiment-backtest-validated`) is the self-serve replacement:
- `NEWS_SENTIMENT`: numeric, entity-resolved per-ticker sentiment, history empirically back to **Jan 2010** (URL-verified), free tier 25 req/day.
- `EARNINGS_CALL_TRANSCRIPT`: **15+ yr** of per-segment ("turn-by-turn") sentiment, coverage ~2010→now (occasional single-quarter gaps).
- No gate, no bigdata dependency, ~$0 (free tier) or $49.99/mo if we ever need a faster bulk pull.

**Goal of this build:** run the existing `backtest/` engine on a properly-powered universe (~35 names) for **two** AV-derived sentiment factors, producing held-out IC / quantile / hit-rate reports that give a real go/no-go where the pilot could not.

## 2. Scope & Non-Goals

**In scope:**
- A resumable Alpha Vantage data puller (offline, operator-run).
- Two pure aggregation transforms (news-tone, transcript) emitting the engine's sentiment-series format.
- Running the existing engine on both factors over the universe; two markdown reports.

**Non-goals (explicit):**
- **No live AV enrichment provider.** AV gives a sentiment number but no event/catalyst detection (the AVAV-lawsuit / MU-earnings-into-print wins were bigdata events). A thin sentiment-only overlay is not worth shipping; live enrichment waits for the bigdata key.
- **No new CI dependencies and no Dockerfile / cron-image changes.** Like the rest of `backtest/`, this is offline-only research code. (The pure-stdlib transform tests run under the _existing_ CI pytest; the network puller and any pandas-dependent path stay out of CI.)
- **No position sizing.** This backtest _informs_ whether sizing-on-sentiment is viable; wiring it into `trading.py` is a separate future decision gated on a positive result.
- **No bigdata content.** Uses Alpha Vantage exclusively.

## 3. Architecture

Reuse the existing `backtest/` engine **unchanged**. The data contract is already minimal:

```
SentimentSeries(ticker, points: tuple[SentimentPoint(date: 'YYYY-MM-DD', value: float)])
```

read by `FixtureSentimentSource` from `sentiment_<TICKER>.json`. The whole build is therefore: **produce those JSON files from AV data**, then drive the existing engine.

```
            ┌──────────────────────────────────────────────────────────┐
            │  NEW (offline, operator-run, not in CI / not in image)    │
            │                                                           │
 AV API ──► │  avpull/  ──►  raw JSON cache  ──►  transforms  ──►  ┐    │
 (free)     │  (resumable,                       (pure, TDD'd)    │    │
            │   checkpointed)                                     ▼    │
            └────────────────────────────────────────────  sentiment_<T>.json
                                                                   │
            ┌──────────────────────────────────────────────────────────┐
            │  EXISTING engine (unchanged)                          ▼   │
            │  FixtureSentimentSource ─► run_backtest ─► report_markdown │
            │  (+ prices_yf for closes; temporal split; IC/quantile/    │
            │   hit-rate pooled across tickers per horizon)             │
            └──────────────────────────────────────────────────────────┘
```

Two factor file-sets are produced (news-tone, transcript) and each is run through the engine independently → two reports.

## 4. Components

### 4.1 `backtest/avpull/` — resumable Alpha Vantage puller

Operator-run script (local import only; **not** imported by the cron app, **not** in CI). Responsibilities:

- **Inputs:** universe list (§6), `ALPHAVANTAGE_API_KEY` from env, an output cache dir.
- **News:** `GET …function=NEWS_SENTIMENT&tickers=<T>&time_from=<YYYYMMDDTHHMM>&sort=EARLIEST&limit=1000` paged forward from 2018-01-01 (news factor window) until "now", advancing `time_from` past the last `time_published` of each page. Cache each raw page.
- **Transcripts:** `GET …function=EARNINGS_CALL_TRANSCRIPT&symbol=<T>&quarter=<YYYYQM>` for each fiscal quarter from `2010Q1`→current. Cache each raw response; an empty `transcript` array is a recorded "no data for this quarter" (the gaps are isolated — see memory).
- **Rate-limit discipline:** the universe pull is ~2,550 calls (news paging + ~2,400 transcript quarters) — **infeasible on free tier (~95 days at 25/day)**, so it runs on **one month of Alpha Vantage premium** ($49.99, 75 req/min; cancel after). The puller paces at ≤75 calls/min (≈0.8s between calls) and completes the full pull in ~35 min in a single session. It still **persists a manifest** (`manifest.json`) of completed units (`("news", ticker)` and `("transcript", ticker, quarter)`) so an interrupted run resumes exactly where it stopped. It detects AV's throttle response (a JSON body with an `Information`/`Note` key) and stops cleanly, recording remaining work.
- **No transform here** — raw cache only, so transforms can be re-run/retuned without re-pulling.

### 4.2 Transforms (pure, TDD'd) → `sentiment_<TICKER>.json`

Pure functions over cached raw JSON → `SentimentSeries`-shaped dict. No network. Unit-tested against small synthetic AV-shaped fixtures.

**News-tone transform:**
- Filter each article's `ticker_sentiment[]` to the target ticker; take `ticker_sentiment_score` weighted by `relevance_score`.
- Bucket by **ISO week**, dating each point to that week's **Friday** (the typical week close, for clean alignment with the price/return calendar); value = relevance-weighted mean of that week's article scores.
- **Window: 2018-01-01 onward** (pre-2018 trimmed — too sparse, ~26 articles/yr, noisy few-article means).
- Empty weeks are simply absent points (the engine aligns on available dates).

**Transcript transform:**
- Per call (`symbol`,`quarter`): value = **mean of segment `sentiment` over non-boilerplate segments** (exclude `speaker == "Operator"` and Investor-Relations intro turns; rule documented in code and tested).
- Date the point to the call's date snapped to the nearest **trading day** (reuse the pilot's snap helper if present, else a small calendar helper). Quarterly cadence, **2010→now**.

Both transforms write `{"ticker": T, "points": [{"date","value"}, ...]}` — exactly what `load_sentiment_series` consumes.

### 4.3 Run + report (existing engine, unchanged)

For each factor file-set, point `FixtureSentimentSource` at its dir and call `run_backtest` over the universe with the engine's existing horizon sweep; `report_markdown` emits the held-out IC / quantile / hit-rate (discovery vs held-out sections already implemented). Two reports: `RESULT-av-news.md`, `RESULT-av-transcript.md` under `backtest/avpull/` (or a results dir).

Prices come from the existing `prices_yf` (yfinance adjusted closes; local-only, pandas — already guarded out of CI).

## 5. Data flow

1. Operator runs `avpull` daily (≤25 calls) until `manifest.json` shows the universe complete (news 2018→now + transcripts 2010→now).
2. Operator runs the two transforms → `sentiment_<T>.json` per factor.
3. Operator runs the engine per factor → two markdown reports.
4. Human reads the held-out IC: positive + monotone quantiles + adequate n ⇒ evidence a sentiment factor is tradeable; flat/insignificant ⇒ sentiment-for-sizing is not supported (for that factor).

## 6. Universe (~37 names — review/adjust here)

Watchlist single-stocks (5): **CVX, MU, RGLD, ESLT, AVAV**
Tech (6): AAPL, MSFT, NVDA, GOOGL, AMD, CRM
Financials (4): JPM, BAC, GS, V
Healthcare (4): JNJ, UNH, PFE, LLY
Energy (2): XOM, COP
Consumer (6): AMZN, WMT, KO, PG, MCD, NKE
Industrials (5): CAT, BA, HON, GE, LMT
Comms (3): META, DIS, NFLX
Materials (2): NEM, FCX

Sector spread breaks the pilot's "all names trending up" confound. **Known caveat — survivorship bias:** this is today's large-cap set, not point-in-time index membership, so the universe is winner-biased. Mitigants: the engine uses cross-sectional **rank IC** (relative, not absolute, sentiment→return) and a temporal held-out split; we are testing whether _relative_ sentiment ranks predict _relative_ forward returns, which is less sensitive to the common upward drift. The bias is disclosed in the report, not eliminated. Thin-coverage risk: ESLT/RGLD/AVAV may have lighter AV news/transcript coverage; the puller's manifest will surface this and those names can be dropped if coverage is inadequate.

## 7. Error handling & degradation

- **Puller:** any AV throttle/error → record progress, exit cleanly, resume next run. Never partially corrupt the cache (write-then-rename). Missing transcript quarter = recorded empty, not an error.
- **Transforms:** a ticker with no usable data → empty `points` (engine already tolerates empty series). Malformed cached record → skip + log, never crash the batch.
- **Engine:** unchanged; already degrades NaN/undefined hit-rate correctly (commit `af845b2`).

## 8. Testing strategy

- **TDD the pure transforms** against small synthetic AV-shaped fixtures (news feed with `ticker_sentiment`/`relevance_score`; transcript with `speaker`/`sentiment`), asserting: relevance weighting, weekly bucketing, 2018 trim, non-boilerplate exclusion, trading-day snap. Pure stdlib — runs in CI.
- **Puller is operator-run, not CI** (network + key), mirroring `prices_yf`/`scorer_llm`. Any test needing pandas uses `pytest.importorskip("pandas")` (CI has no pandas).
- Full pre-push gate per memory `brief-local-run`: `ruff check` + `ruff format --check` + `pytest`; stage all reformatted files.

## 9. Alpha Vantage ToS note

Unlike bigdata, Alpha Vantage's terms permit personal/programmatic use of the data; caching responses locally for a personal research backtest is within normal use. (Confirm no per-endpoint redistribution clause before any future sharing of raw AV data — not relevant to this offline personal backtest.)

## 10. Open questions / risks

1. **Coverage of the thinner names** (ESLT/RGLD/AVAV) — resolved empirically by the first puller pass; drop if inadequate.
2. **AV news floor vs window** — news factor uses 2018+, well inside AV's 2010 floor; no risk.
3. **Transcript dating (approximation + limitation):** AV's `EARNINGS_CALL_TRANSCRIPT` response carries **no call date** — only a fiscal `quarter` label (`YYYYQM`). The transform therefore anchors each call to **its calendar-quarter-end + ~50 days** (a conservative post-report date), snapped to the latest trading day ≤ anchor. This is **lookahead-safe** (the anchor sits at/after the actual call, so the sentiment was already public) but **mis-dates off-calendar-fiscal names** (e.g. MU, whose fiscal Q1 call is ~Dec): those points anchor late, adding alignment noise that **attenuates IC toward zero — it does not bias the sign**. Disclosed in the report; a future refinement could source exact call dates from an earnings-calendar feed.
4. **Pull cost/duration (DECIDED):** the transcript endpoint is one call per `(ticker, quarter)` with no batching → ~37 × ~64 quarters ≈ 2,400 calls, plus ~110–150 news calls. At free-tier 25/day that is ~95 days (infeasible). **Decision: one month of AV premium** ($49.99, 75 req/min) bulk-pulls the full universe + history in ~35 min; cancel/downgrade after. The resumable manifest still guards against an interrupted session.
5. **Transcript sentiment range/sign (real, resolve from first pull):** the probe only saw non-negative segment values (MU 2024Q1: 0.0/0.3/0.8, a strong quarter). If AV transcript sentiment is a **[0,1] positivity magnitude** rather than a signed score, the transcript factor measures _call positivity_, not bull/bear — cross-sectional **rank IC still works** (rank names by positivity vs forward return), but interpret accordingly and disclose it in the report. Confirm the empirical min/max from the first pull before finalizing the transform's interpretation.

## 11. Deliverables

- `backtest/avpull/` package: resumable puller + manifest + two pure transforms.
- `sentiment_<T>.json` factor file-sets (gitignored data, not committed).
- Two held-out reports (news-tone, transcript) + a short written go/no-go read.
- Tests for the transforms (CI-safe).

---
name: newsbrief-deferred-findings
description: Deferred findings from the 2026-06-09 deep-dive review — known gaps deliberately left open after the hardening pass
metadata: 
  node_type: memory
  type: project
  originSessionId: a7e75cd2-d78e-4709-907b-03d6d0429c84
  modified: 2026-07-20T07:24:21.278Z
---

**HARDEN-AND-OBSERVE PROGRESS (updated 2026-06-14 — followups session).**
Both big feature arcs are done (multi-asset trading COMPLETE; sources/edge SHIPPED). A followups
pass landed 5 commits on main (`5627293`..`7a61823`, 192 tests green):
- **DONE — cross-process locking.** New dependency-free `file_lock` (O_EXCL + timeout +
  stale-break) in common.py wraps every read-merge-write span: `save_state`/`clear_batch_state`
  (batch_state.json), `mode_paper` (whole load→open→save, `BOOK_LOCK_TIMEOUT=120s` — covers the
  creds-gated Claude prediction matcher), `/close`, and weekly mark-to-market. A coincident writer
  that can't acquire degrades to a graceful retry, never a clobbered book. Tests: tests/test_locking.py.
- **DONE — stray `<`/`>`/`&` in model prose.** `sanitise_html` now does a whitelist-preserving
  escape (allowed tags + existing entities pass through; everything else's `<>&` escaped per the
  Bot API HTML spec). "oil <$60" / "AT&T" no longer 400 a chunk.
- **DONE — cheap hygiene (log + trivy).** newsbrief.log → `RotatingFileHandler` (5 MB × 5,
  ~30 MB ceiling); CI gained a SHA-pinned trivy image scan after build-and-push (fails on fixable
  CRITICAL/HIGH, ignore-unfixed).
- **DONE — dead `_settle_prediction` price param** dropped (signature + 4 call sites).

**STILL OPEN after this pass:**
- **Stooq — RESOLVED 2026-06-15.** It was never an outage: Stooq permanently removed its free CSV
  API (404 + JS proof-of-work wall). Replaced the whole equity/index price layer with Yahoo+Alpaca
  and migrated the market pulse to a Yahoo "index" symbol class. Full story: [[stooq-dead-provider-replacement]].
- **signals/book retention — RESOLVED+PUSHED 2026-06-26** (commits 46e814d..71d7ff6; pushed in batch
  6a9fd5d..76d20f8 → origin/main w/ #4/#5a/#5b/#6, Docker deploy triggered). New `retention.py` module + 14 tests: at the tail of mode_collect, deletes
  dated artifact files strictly older than a global window (`NEWSBRIEF_RETENTION_DAYS` env, default 90; `days<=0`
  disables) across 6 families (briefs/, source_index-, claim_evidence-, verification-, enrichment-, signals-*.json)
  + 90-day date-based LINE-trim of signals-log.jsonl (keep-on-doubt, atomic rewrite). SAFETY: targets ONLY
  date-bearing filenames → undateable names skipped, so book.json/brief_memory.json/feedback.json/batch_state
  are STRUCTURALLY untouched (book correctly stays excluded, per the original finding). weekly/ excluded by design.
  Fail-safe (never affects the delivered brief). Spec/plan: docs/superpowers/{specs,plans}/2026-06-26-dated-artifact-retention*.
- **`<a href>` scheme — FIXED 2026-06-20** (commit `b6d1adf`). The earlier "needs a real parse"
  framing was wrong: instead of removing the `<a>` pair (which risks orphaning `</a>`), DEFANG the
  href — rewrite any non-allowlisted scheme (allowlist: http/https/tg/mailto) to `#`, leaving the
  tag pair intact. Runs in `sanitise_html` AFTER the whitelist-escape (every surviving `<a>` is then
  well-formed). Defense-in-depth on a paper-only surface; won't catch `java\tscript:` obfuscation.
- **Cosmetic/low-value:** duplication merges — `submit_batch`/`submit_batch_no_search` +
  `query_chroma`/`query_chroma_latest` MERGED 2026-06-20 (commit `8ed957f`, characterization-tested,
  tests/test_batch_and_chroma.py). **LSE base-match gap — FIXED 2026-06-21 (commit `8bb76c6`, TDD'd):**
  `_match_instrument_by_base` now accepts the marker-stripped base as an additive match key (new
  `_instrument_base_keys`), so a plain signal omitting the marker ('RR', 'EXV1') resolves the
  LSE/Xetra-only listing ('RRl_EQ'->rr.uk, 'EXV1d_EQ'->exv1.de) without needing an override; raw base
  still accepted + prefer-US ranking unchanged; suffix derivation factored into `_stooq_suffix_for`.
  **`mode_collect`/`mode_run` merge — RESOLVED 2026-06-26 by DELETION (commit `9c76f64`, PUSHED in batch 6a9fd5d..76d20f8)**, NOT a
  parity merge: investigation found mode_run was UNUSED drifted dead code (the no-arg default entrypoint, but host
  uses explicit modes, no test/CI ref, user never ran it; it lagged mode_collect ~5 steps: enrichment/reconcile/
  verify/retention/trade-update + opposite clear_batch_state order). A parity merge would have refactored the
  production mode_collect for a path nobody runs → wrong risk/reward. Deleted mode_run + its dispatch entry; bare
  `python brief.py` now prints usage (no-arg default `else ""`); usage string drops `run`, adds `paper`; mode_collect
  UNTOUCHED. 515 tests unchanged (nothing depended on it). Spec/plan: docs/superpowers/{specs,plans}/2026-06-26-delete-mode-run*.
  **fetch_web_source BCA noise — INVESTIGATED + CLOSED as a NULL 2026-06-26** (systematic-debugging, live host data).
  Premise ("JS-heavy BCA page falls back to raw resp.text[:800] noise") DID NOT REPRODUCE. Ran a boundary-instrumented
  diagnostic on the host against the real sources.json: BOTH configured page sources (bcaresearch.com midterm + iran-
  conflict dashboards) return status=200, full HTML (~200-258KB), and `name="description"` RX1 MATCHES with rich,
  specific content (BCA SEO-populates meta + og:description with the dashboard summary). path = META description, never
  the [:800] fallback. No fix — existing regex extraction works correctly; no noise reaches the prompt. LATENT (never-fired,
  not built): the `else resp.text[:800]` fallback WOULD dump raw HTML for any future page source lacking a name=description
  tag — but zero current sources trigger it (YAGNI). Diagnostic kept in session scratchpad.

### OPEN BACKLOG (deferred-for-cause; none currently worth the effort — recorded 2026-06-26 as first-class items)

The 2026-06-09 deferred-findings list has been worked down to these. Each was deliberately NOT done after weighing effort vs value; listed here so they're not re-derived as "new" findings. Verify against current code before acting.

1. **checkpoint-backfill price skew** — RESOLVED+PUSHED 2026-06-26 (commits 3282d12..470112a + plan-doc fix 565544c; pushed 76d20f8..565544c → origin/main, Docker deploy triggered). Spec/plan: docs/superpowers/{specs,plans}/2026-06-26-checkpoint-backfill-historical-price*. Brainstorm reframed the scope: the skew is NOT just missed-runs — mark_to_market runs WEEKLY but positions open DAILY, so every checkpoint was 0–6d late even on schedule. Full fix chosen: record each 1w/2w/4w checkpoint at the close on its TRUE crossing date (entry_date+threshold). KEY CHEAPENER: the live Yahoo quote path already hits v8/chart, which takes period1/period2 → historical = an endpoint-param extension, NOT a new provider (kept requests-only, NO yfinance in trading.py — backtest/prices_yf is yfinance+CI-excluded, deliberately not reused). New in trading.py: `_snap_close` (last close ≤ target, snaps weekend/holiday), `_yahoo_closes`/`_kraken_closes` (REST series, {} on any fail), `historical_closes` dispatcher (equity→Yahoo-resolved, index→Yahoo-raw, crypto→Kraken OHLC, prediction/other→{}), `_has_new_crossing` gate (1 fetch/position/run, only when something crossed), `_record_checkpoints` rewritten per-checkpoint. Checkpoint schema +`price_basis: "historical"|"current"` (ADDITIVE, absent=legacy/current, no migration). Fail-safe: any miss → current price + `price_basis:"current"`, never blocks the 4w close. Prediction stays current-price (flagged). NO reporting change in v1 (performance_report untouched; flag stored not acted-on). 25 new tests in tests/test_checkpoint_backfill.py (parse/transform only; network never hit in CI); full suite 542 passed, ruff+format clean. Subagent-driven (haiku impl T1–4, sonnet T5–6; reviews INLINE per [[subagent-review-stalls]]); all 6 commits BOM-free. PUSH = Docker deploy = user's call. Touches [[multi-asset-trading-build]], [[self-improving-trading-roadmap]] Stage A measurement.

2. **split_html_message open/close-tag split** — DEFERRED (low-prob, disproportionate). The Telegram-message splitter can cut between an open and close tag, yielding two invalid-HTML chunks → a 400 on that chunk. Low probability given the section-heading (`<b>…</b>`-per-line) format the brief uses; a robust fix needs HTML-aware splitting (track open tags across the split boundary, re-open on the next chunk), which is disproportionate for a paper-only/personal surface. Effort: medium (HTML-aware splitter + tests). Lives in `sanitise_html`/`split_html_message` in brief.py.

3. **LATENT (never-fired) — fetch_web_source `[:800]` raw-HTML fallback foot-gun** — NOT a current bug; recorded so it isn't rediscovered. `fetch_web_source` falls back to `resp.text[:800]` (raw HTML) when a page has no `name="description"` meta tag. Zero current page sources trigger it (both BCA dashboards have rich meta tags — see the BCA null above), so building for it is YAGNI. IF a future page source without a meta tag is added and injects HTML noise into the prompt, the cheap fix is a fallback chain `name=description → og:description → <title> → "" (header-only)` instead of dumping raw HTML. Effort: small. Only act when a real source needs it.
4. **enrichment `symbol_bundle` — one bad events response discards a good sentiment result** — DEFERRED 2026-07-19 (accepted at final review). In `enrichment/providers_bigdata.py`, the resolve→sentiment→events fetches share ONE try/except, so a malformed *events* payload degrades the whole symbol bundle to `error=` even though sentiment already parsed fine. Acceptable under the "silent-empty degradation is OK" design for a descriptive overlay. Cheap fix if events prove flaky once live: wrap the events fetch in its own try so sentiment survives. Only act on real evidence from the enable rollout. See [[bigdata-enable-spec-plan]].

5. **enrichment `_score_from_values` — partial-null sentiment series untested** — DEFERRED 2026-07-19. `n_points` counts every point in the series while `trend_mean` averages only non-null `daily_sentiment`, so the two diverge silently on a mixed series; and if the latest point's `daily_sentiment` is None, both `daily_sentiment` and `trend_delta` are None while `n_points > 0`. No crash (`_fmt_sentiment` gates the trend line on both trend fields being non-null). Bigdata is not known to emit partial-null series — add a guard test only if one is ever observed.

- **Non-code milestone:** let paper trades accumulate and evaluate the go-live gate — the
  ~2026-06-28 revisit in [[brief-sources-and-edge-latency-thread]]; graduation is data-driven.

---

A full deep-dive review + 8-thread hardening pass landed 2026-06-09 (Telegram failure alerts, poison-message isolation, tg_offset race fix, atomic JSON writes + quarantining loads, fetch timeouts, zero-price guard, token redaction, requests CVE bump + Dependabot, 39-test suite gating the Docker publish, non-root Dockerfile, SHA-pinned actions). These findings were **reviewed and deliberately deferred** — don't re-derive them from scratch, and don't treat them as new discoveries:

- **[FIXED 2026-06-14] No cross-process locking.** `file_lock` now serialises save_state/book
  read-merge-write (see top block). Note the getUpdates *double-processing* race is NOT closed — I
  deliberately did not serialise all of `process_telegram_commands` (it holds network/Claude calls);
  double-processing is low-harm (handlers are ~idempotent). Only the lost-update writes are locked.
- **[FIXED 2026-06-14] Model prose with stray `<`/`>`/`&`** — `sanitise_html` whitelist-preserving
  escape (see top block).
- **split_html_message can split between an open and close tag**, producing two invalid-HTML chunks. Low probability given the section-heading format.
- **[FIXED 2026-06-20] `<a href>` schemes unrestricted** in sanitise_html — defang non-allowlisted schemes to `#` (commit `b6d1adf`); see the dated entry above.
- **Duplication merge candidates:** submit_batch / submit_batch_no_search; query_chroma / query_chroma_latest; the collect body copy-pasted in mode_collect / mode_run.
- **[FIXED 2026-06-21] LSE base-match gap** (commit `8bb76c6`): a plain signal symbol for an LSE-only listing never base-matched because the marker letter is inside the base segment (`RRl_EQ` → base `RRL`, signal `RR`). Closed by stripping the known market-marker during comparison (additive match key in `_instrument_base_keys`), TDD'd in tests/test_paper.py. See [[stooq-ticker-resolution]].
- **fetch_web_source** regexes a JS-heavy BCA page and falls back to raw `resp.text[:800]` — probably mostly noise tokens in the prompt; check a debug dump before investing.
- **[PARTLY FIXED 2026-06-14] Unbounded growth:** newsbrief.log now rotates (RotatingFileHandler).
  paper-book.json / per-day signal files retention still open (deferred — not cheap/delete-risky).
- **[FIXED 2026-06-14] CI image scan:** SHA-pinned trivy step added to docker-publish.yml.
- **Checkpoint backfill uses today's price** for past horizons after a missed weekly run (documented in code; skews 1w/2w stats slightly).
- **[FIXED 2026-06-14] `_settle_prediction` dead `price` param** dropped (signature + 4 call sites, `7a61823`).

- **(2026-06-14) Market-pulse Stooq symbols — RESOLVED 2026-06-15.** The Stooq symbols
  (`dx.f`, `xauusd`, `cb.f`, `usdjpy`, `^nkx`, `^hsi`) were never verifiable because Stooq's
  free API was permanently gone, not down. Migrated `MARKET_SPINE`/`PIN_INSTRUMENTS` to a Yahoo
  `index` asset class with raw Yahoo symbols (`^GSPC`, `DX-Y.NYB`, `GC=F`, `BZ=F`, `USDJPY=X`,
  `^N225`, `^HSI`); all 7 verified live returning real moves. See [[stooq-dead-provider-replacement]].

**Why:** the review was exhaustive; re-running it would resurface these as "findings". This list is the authoritative backlog.

**How to apply:** if the user asks for "next threads" or another review pass, start here. Verify each against current code first — some may have been fixed since.

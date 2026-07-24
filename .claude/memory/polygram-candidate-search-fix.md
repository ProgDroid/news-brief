---
name: polygram-candidate-search-fix
description: Why PolyGram prediction trades never opened, and the token-search fix — plus the non-obvious fact that PolyGram /search is a case-insensitive SUBSTRING match
metadata:
  node_type: memory
  type: project
  originSessionId: 530d1847-5f77-4ecd-b291-0650d54ce513
---

**2026-06-25: diagnosed + FIXED (commit 4b526dc) why no PolyGram prediction trades ever opened.** Gate chain in `_open_prediction_positions` (trading.py): creds → candidates → matcher ≥ `PG_SIMILARITY_FLOOR` (0.60) → market priceable. Host log showed `No PolyGram candidates today` = **gate 2** (`_gather_pg_candidates` returned empty). Creds WERE set (gate 1 fine).

**Root cause:** the gather searched PolyGram with the raw kebab-case signal `topic` slug (e.g. `hormuz-fees-dispute`), which never appears verbatim in a market title → 0 results every day → matcher never ran.

**KEY NON-OBVIOUS FACT (verified live):** PolyGram `/search?q=` (polygram.ink/api, mirrors Polymarket) is a **case-insensitive SUBSTRING match** over market title/description. Consequences: (a) multi-word phrases AND hyphenated slugs return nothing; only short entity keywords hit (`Bitcoin` → 17 events). (b) SHORT tokens are toxic — the 2-char ticker `MU` substring-matched `Musk`/`Hor`mu`z` → 322 junk markets that filled the whole 25-cap with Elon-Musk-tweet markets and starved good tokens. (c) Response shape IS `[{...,"markets":[{...}]}]` and market fields ARE Polymarket-style (`outcomePrices`/`clobTokenIds` as JSON-encoded strings) — `_parse_pg_market` is correct, was never the problem.

**Fix (`_gather_pg_candidates` + new `_signal_search_terms`/`_pg_market_volume`, trading.py):** search distinct entity tokens from the topic (≥`PG_MIN_TOKEN_LEN`=4 chars, ticker EXCLUDED), rank each token's open markets by 24h volume, take ≤`PG_PER_QUERY_CAP`=5 per token so none monopolises, dedup + `PG_CANDIDATE_CAP`=25. Live-validated: surfaces real Strait-of-Hormuz / China-Taiwan / Iran markets. Residual substring junk (`Gold`→"Golden Boot" World Cup, `Europe`→Dota2) is HARMLESS — the matcher + 0.60 floor reject it; gather's only job is recall, matcher does precision. Deterministic chosen over LLM-keyword: an LLM can't conjure markets PolyGram doesn't list (semiconductor/defence/AI topics return 0 — venue is politics/crypto/sports/geopolitics only).

**Gate 3 CONFIRMED working 2026-06-25:** after redeploy, **2 PolyGram positions opened in book.json** — the matcher cleared the 0.60 floor and the full chain (gather → matcher → open) works end-to-end. Whole fix validated.

**Note on real vs paper:** the user expected the PolyGram positions might be REAL trades; they are PAPER like everything else in the system (paper-only by design, [[paper-sim-no-fees-decision]]). Live execution for prediction markets remains a latent future want, gated on the existing data-driven go-live mechanism ([[multi-asset-trading-build]] paper→live). For host diagnostics technique see [[live-state-on-deploy-host]] (in-container probe).

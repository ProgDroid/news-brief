---
name: enrichment-universe-bugs
description: Two latent bugs in enrichment/universe.py surfaced by the 2026-06-20 live flag-on validation; low severity (dark + read-only) but fix before enabling
metadata: 
  node_type: memory
  type: project
  originSessionId: 4356315d-665d-43bb-a1c0-dba9009690e0
---

Surfaced 2026-06-20 by exercising the enrichment flag-on path end-to-end with the live book/signals (see [[bigdata-enrichment-validated-live]]). Both are in `enrichment/universe.py`. Neither breaks the brief (enrichment is dark + try/except-isolated + read-only), but both cause **silent under-coverage** and wasted query budget once `ENRICHMENT_PROVIDER=bigdata` is live — so fix BEFORE flipping the flag.

**1. Double-underscore normalization quirk → `AVAV_`.** `normalize_ticker("AVAV__US_EQ")` returns `"AVAV_"` (trailing underscore), not `"AVAV"`. The book holds a position with ticker `AVAV__US_EQ` (double `_`) AND the latest signals snapshot carries `AVAV__US_EQ`. `_VENUE_SUFFIX_RE = _(?:[A-Z]{2}_)?EQ$` strips only `_US_EQ`, leaving the stray `_`. Downstream impact, both confirmed live: (a) a junk symbol `AVAV_` is queried (→ error/empty bundle, wasted query), and (b) the live AVAV signal **misses its `bigdata_sentiment` annotation** in collect because `annotate_signals` matches `normalize_ticker("AVAV__US_EQ")="AVAV_"` against bundle ticker `"AVAV"` → no match. So an open AVAV position + live AVAV signal get NO enrichment even though AVAV resolves cleanly (F1EB39). Fix: collapse repeated underscores / strip trailing `_` after venue-suffix removal in `normalize_ticker`.

**2. ETF-leak: `VEUA`, `SPOL` treated as company symbols.** `_ETF_THEME_MAP` only knows DXJG/DXJ/EXV1/KSTR/ARMG/SGLN. The live book has open positions in `VEUAl_EQ` (Vanguard FTSE All-World ex-US) and `SPOLl_EQ` (a Poland ETF), which aren't in the map → they fall through to `tickers` and get queried as companies. ETFs resolve as funds → company sentiment is meaningless/empty (the known ETF caveat in [[bigdata-evaluation-and-trading-split]]). Under the live `bigdata` provider that's wasted query units for empty bundles. Fix options: extend `_ETF_THEME_MAP` (brittle), or detect ETF via `find_securities` `security_type==ETF` and route to a theme (robust but adds a resolve call). SPOL also base-matches the SPOL signal, so it leaks on the signals path too.

Both were invisible until real data ran through the path — a vindication of the live-fixture validation step itself.

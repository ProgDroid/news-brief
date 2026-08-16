---
name: commodity-signals-are-index-class
description: "\"Paper skip: no instrument for BRENT (equity)\" is a wrong asset CLASS, not a missing symbol — commodities route through the index class"
metadata: 
  node_type: memory
  type: project
  originSessionId: 14a602eb-8f61-49a4-8556-33d4bfaf804e
  modified: 2026-08-09T14:43:50.585Z
---

`Paper skip: no instrument for <TICKER> (equity)` for a commodity (BRENT, GOLD, WTI…)
does NOT mean the symbol is unknown. It means the **asset class is wrong**, and the
skip message actively misleads by naming the gate that fired rather than the real gap.

**Why:** the signal-extraction JSON schema in `brief.py` offers the model an
`asset_class` enum of **`["equity", "crypto"]` only**, so a commodity call can *only*
arrive tagged `equity` — it is not a model error. `resolve_symbol` then hunts it in
the **T212 equity universe**, where it was never going to be. Meanwhile the pricing
layer had routed `"index"` to raw Yahoo symbols all along (`fetch_price`,
`_closes_for`) and `MARKET_SPINE` already priced Brent as `BZ=F` in the same file.
**Only the OPENING path in `mode_paper` lacked the branch**, so every commodity signal
the model produced was silently dropped.

**How to apply:** fixed 2026-08-09 (dfff949, pushed). `trading.INDEX_TICKER_SYMBOLS`
+ `resolve_index_symbol()` map ~10 commodity/macro tickers to Yahoo symbols;
`brief._classify_asset()` reclassifies them to `"index"` **at normalize time, before
the signal is saved** — NOT at open time, because `mode_paper` builds its dedup key
`(asset_class, ticker, direction)` *before* resolving the symbol, so a late flip would
store a position under a class it wasn't deduped under and tomorrow's identical signal
would open a duplicate. Crypto is never overridden (own Kraken resolver).

The map is deliberately narrow — an unlisted ticker still skips and logs, which is the
right failure for a guess. Widen it from what the logs show the model actually emits.
Two known rough edges left: `fetch_benchmark_level` returns None for `index` (so
`edge`/`benchmark_return` stay None on commodity rows — no false S&P comparison), and
the schema enum was NOT extended, so the reclassification is the only path in.

Related: [[stooq-ticker-resolution]], [[stooq-dead-provider-replacement]] (where the
`index` class and raw Yahoo symbols were introduced for market pulse).

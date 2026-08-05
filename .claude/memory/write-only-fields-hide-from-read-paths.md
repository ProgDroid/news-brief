---
name: write-only-fields-hide-from-read-paths
description: "When every read path works and only the write path fails, look for a field the reads never needed — and treat a docstring naming a field the code never reads as the marker"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0020b8b4-ded9-4def-919b-be8744df2379
  modified: 2026-08-05T22:29:56.917Z
---

A bug can be structurally unreachable until the first real write. If the read paths key off one representation and the write path needs another, no amount of read traffic exercises the second one.

**The instance:** PolyGram prediction markets. Every read path — prices, token ids, marks, settlement, dedup — keys off `side_index` (0/1), so none of them ever needed the outcome's *label*. `POST /trade/place` validates `outcome` against the market's `outcomes` array, and all three open paths built it as `"Yes" if side == "YES" else "No"`. Correct for a Yes/No binary, a 400 for any binary labelled Up/Down, Above/Below or with two candidate names. Paper trading had run for weeks over the same markets without touching it; it surfaced on the first real order (fixed ef45c45, 2026-08-05).

**The marker that would have caught it:** `_parse_pg_market`'s own docstring said "PolyGram mirrors Polymarket: `outcomes`, `outcomePrices`, and `clobTokenIds` are JSON-encoded strings" — and the function parsed the last two and silently ignored the first. A docstring that names a field the body never reads is either a stale comment or an unfinished implementation, and it is cheap to grep for (`grep -rn '"outcomes"'` returned only the docstring and a test fixture).

**How to apply:** when a write/POST fails while every GET works, do not start with the transport or the ids. List the fields the write path needs that no read path consumes, and check each is derived from real venue data rather than assumed. Then grep the parser's docstring against the fields it actually assigns. Also worth checking: labels used as dictionary/dedup keys must be the venue's own, or the key you write won't match the key the venue answers with. Relates to [[polygram-live-trading-spec]] and [[bigdata-rest-api-verified]] (the other case where assumed field shapes were wrong in nearly every particular).

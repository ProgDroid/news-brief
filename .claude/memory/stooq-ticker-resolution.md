---
name: stooq-ticker-resolution
description: How signal tickers map to Stooq symbols; T212 ticker format quirks and the base-symbol resolver
metadata: 
  node_type: memory
  type: project
  originSessionId: 81f25441-0adf-482c-92a8-44aba18f742a
---

Signals from the model carry **plain exchange symbols** (`SHEL`, `BP`, `EQNR`, `TSM`), NOT T212 instrument tickers. `resolve_stooq_symbol` bridges them to Stooq symbols (`shel.us`).

**T212 ticker format** (the instrument cache is keyed by these):
- US listings: `<SYMBOL>_<COUNTRY>_EQ` → `SHEL_US_EQ`, `BP_US_EQ`, `AAPL_US_EQ` (3 segments).
- LSE listings: `<SYMBOLl>_EQ` → `BPl_EQ`, `RRl_EQ`, `VODl_EQ` (trailing lowercase `l`, 2 segments, no country).
- Xetra listings: `<SYMBOLd>_EQ` → `EXV1d_EQ` (trailing lowercase `d`, 2 segments). The 2-segment form always carries a one-letter **exchange marker** appended to the symbol; the 3-segment US form does not.

**Marker-letter strip (2026-06-01 fix):** Stooq wants the bare symbol, so `RRl_EQ`→`rr.uk`, `EXV1d_EQ`→`exv1.de`. Before the fix the base derivation kept the marker (`rrl.uk`, `exv1d.de`) and Stooq returned `N/D`. `_STOOQ_MARKET_MARKER = {"uk":"l","de":"d"}` strips a trailing marker, but ONLY for the 2-segment form (`len(ticker.split("_"))==2`) so a clean 3-part or plain symbol that happens to end in `l`/`d` (e.g. `SHEL`→`shel.us`) is never mangled. Add markets to the map as observed (FR/Paris not yet confirmed).

**Resolver order** (`resolve_stooq_symbol`): override file → exact ticker match → base-symbol match (`ticker.split("_")[0]`) preferring the US listing via `_COUNTRY_PREFERENCE`, then derive suffix from the matched instrument's real currency (`_STOOQ_SUFFIX`) or ISIN country for EUR (`_STOOQ_EUR_BY_ISIN`). Unknown → `None` → skip+log (never guess a suffix).

**Why the `SHEL` skip happened (2026-06-01):** it was a ticker-**format mismatch**, NOT ownership — `refresh_instruments_cache` pulls T212's *entire* catalogue, held or not. Plain `SHEL` just never matched cache key `SHEL_US_EQ`. The base-symbol match fixed it. The model emits US-style symbols, so US-preference is the right tiebreak; LSE's trailing-`l` means `BP` only ever matches the US `BP_US_EQ`, keeping ambiguity low.

**Null tickers are by-design** for macro/thematic signals (Hormuz-generally, BoJ, NATO spend) with no single tradable instrument — `mode_paper` correctly skips them. Don't push the model to fabricate tickers for macro themes.

**Stooq coverage gaps (live with it):** some names just aren't in Stooq's free feed. `TOU_CA_EQ` (Tourmaline, Canadian) has no `.ca`/`.us` quote (both N/D) and CAD isn't in `_STOOQ_SUFFIX` — it permanently skips. Not a code bug; accepted (would need a second price source like Yahoo/Tiingo to cover).

To force a specific listing (e.g. BP's LSE quote), add `paper/ticker_map.json` override (authoritative). See [[t212-401-diagnosis]], [[paper-sim-no-fees-decision]].

**Since 2026-06-09:** the resolver contract is locked in by tests (`tests/test_paper.py` — override priority, US preference, marker strip, EUR-by-ISIN, unknown→None); change behavior there first.

**2026-06-15 — `resolve_stooq_symbol` renamed to `resolve_symbol`** (output is now a neutral `base.market` symbol consumed by the Yahoo+Alpaca pricer, see [[stooq-dead-provider-replacement]]) AND the marker-strip was fixed: it now derives base + marker from the **matched T212 ticker**, not the input. This CLOSED the case where a plain signal carries the marker baked in (`SGLNl`, `ARMGl`, `DXJGl` — no underscore) and base-matches the 2-segment LSE listing `SGLNl_EQ`: previously it kept the marker (`sglnl.uk` → Yahoo `SGLNL.L` → 404), now it strips it (`sgln.uk` → `SGLN.L`). Verified live. `_match_instrument_by_base` now returns `(matched_ticker, meta)`.

**Still override-only (two narrow cases):** (1) a plain symbol WITHOUT the marker for an **LSE-only** listing (`SHEL` vs key `SHELl_EQ`, base `SHELL` ≠ `SHEL`) never base-matches. (2) **US-namespace collision**: a marker-laden LSE symbol whose base also exists as a US listing resolves to the US one via US-preference (e.g. `KSTRl` → matched a USD `KSTRL` → `kstrl.us` → 404 instead of LSE `KSTR.L`). Both need a `paper/ticker_map.json` override (e.g. `"KSTRl": "kstr.uk"`). See [[newsbrief-deferred-findings]].

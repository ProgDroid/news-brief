---
name: brief-sources-and-edge-latency-thread
description: SHIPPED 2026-06-14 — the sources & edge-latency redesign (pinned-hybrid brief + region-native sources + market pulse + forward tilt); revisit edge after ~2 weeks
metadata: 
  node_type: memory
  type: project
  originSessionId: 3138e02c-0d04-4ac8-812a-7bbe4a0da34e
---

The "brief is same-y + news priced-in" thread the user flagged 2026-06-13. **SHIPPED
2026-06-14** as a single COMBINED redesign (user chose combined over sequenced, overriding the
recommendation). Spec: `docs/superpowers/specs/2026-06-14-brief-sources-edge-latency-redesign-design.md`;
plan: `docs/superpowers/plans/2026-06-14-brief-sources-edge-latency-redesign.md`. 10 commits
on main (`4710a0f`..`6dbf116`), 178 tests green.

**What shipped (4 pillars):**
1. **Pinned-hybrid structure** — `build_daily_prompt` rewritten from the fixed 5-country
   template to a fixed spine (TOP STORIES → MARKET PULSE → dynamic middle → MACRO → POSITION
   SIGNALS → WATCH/FORWARD → `@@@SIGNALS@@@`) + a user-controlled pin set. New `DEFAULT_PINS`
   (= the old 5 countries) + `resolved_pins(fb)` (absent `pin` key → defaults; explicit `[]`
   respected; `/reset` drops the key → restores defaults). New `/pin` `/unpin` Telegram cmds
   (symmetric to `/watch`), pins shown in `/status` via `feedback_summary`. `@@@SIGNALS@@@`
   marker + JSON schema kept byte-identical (signal parsing untouched).
2. **Sources** — every `RSS_FEEDS`/`WEB_SOURCES` entry tagged `kind` (wire|analyst|regional|
   primary); `fetch_rss` renders `[KIND]`. Added 8 region-native/forward feeds: Al Jazeera +
   SCMP (native RSS), Kyiv Independent, ISW, Yonhap, 38 North, NHK World, BOJ (Google News
   `site:` proxy — native dead for Kyiv & 38 North). Generic extra wires deliberately NOT
   added (user rates them poorly).
3. **Market pulse** ("what moved & why") — `trading.py`: `fetch_daily_move` (open→last %, one
   fetch; Stooq Open=col3/Close=col6, Kraken o vs c[0]); `MARKET_SPINE` (^spx, dx.f, xauusd,
   XBTUSD) + `PIN_INSTRUMENTS` map (iran→Brent cb.f, japan→usdjpy+^nkx, china→^hsi);
   `build_market_pulse(pins)` = spine + pin-derived + open-position moves + RECENT volume
   anomalies (2-day cutoff on `last_alert_ts`). Best-effort, never raises; failed fetch → `—`.
   Reads `volume-history.json` (NO network sweep). Wired into `mode_submit` via new
   `market_block` param. NOTE: watchlist price-moves intentionally omitted (user picked
   "macro set + positions + anomalies"); watchlist = volume alerting only.
4. **Edge tilt** — `SYSTEM_PROMPT` reframed: Reuters anchors facts, interpretation LEADS with
   forward-looking (analysts/primary/podcasts/market action); "don't echo priced-in headlines."

**Revisit trigger (the agreed substitute for combined-redesign's lack of attribution):**
after ~2 weeks of briefs (≈2026-06-28), check `validation` hit_rate/mean_edge + the user's
read experience; if still priced-in, that's the deeper edge pass (more primary indicators,
intraday context). See [[newsbrief-deferred-findings]] for the open Stooq-symbol verification.

**Why:** signal quality upstream caps trade quality downstream.
**How to apply:** if the user revisits brief quality, this is DONE — start from the revisit
trigger, not a fresh redesign.

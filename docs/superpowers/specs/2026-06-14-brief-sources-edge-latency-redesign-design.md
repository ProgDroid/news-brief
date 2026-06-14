# Brief Sources & Edge-Latency Redesign — Design

**Date:** 2026-06-14
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** Single combined redesign of the daily brief's sources, structure, and market grounding.

## Problem

The daily brief feels "same-y" and its news is "priced-in" by the time it reaches the
reader. Both symptoms share one root cause: the brief is anchored on **backward-looking,
single-outlet (Reuters) wire recap** delivered in a **rigid daily template**. An audit of
the current code confirms:

- **Single-outlet framing.** Both hard-news feeds in `RSS_FEEDS` ("Reuters Markets",
  "Reuters World") are Google News `site:reuters.com` proxies. `SYSTEM_PROMPT` and the
  prompt body instruct "Prioritise Reuters" three times. Every hard headline arrives
  through one outlet's lens.
- **Rigid structure.** `build_daily_prompt` hardcodes the same five country sections
  (Ukraine / Iran / Korea / Japan / China) every day, each with a "No significant change"
  fallback. `TOPICS` is a fixed list of five. This *structural* sameness persists
  regardless of source quality.
- **No market grounding.** The paper/monitor layer already fetches Stooq/Kraken prices and
  volume anomalies, but none of that "what moved & why" data feeds `build_daily_prompt`.

The user chose a **single combined redesign** (over a sequenced approach), accepting that a
big-bang change has no per-lever attribution, because the root cause is shared.

## Goals

1. Break structural sameness without losing the topics the reader cares about.
2. Diversify framing beyond a single wire's lens — preferring region-native and
   forward-looking sources over generic additional wires.
3. Ground the brief in actual market action ("what moved & why") to attack edge-latency.
4. Tilt the brief's interpretation forward (anticipatory) while keeping facts anchored.

## Non-Goals (YAGNI)

- No change to the `@@@SIGNALS@@@` JSON schema, the weekly summary, or Telegram delivery.
- No change to the paper/monitor **trading** logic. The redesign only **reads**
  `book.json` and `volume-history.json` for the market block.
- No paywall-bypass dependency (removepaywalls / 12ft / archive proxies) — fragile, ToS-gray,
  unreliable for an automated daily pipeline.
- No new MCP server and no new measurement infrastructure.
- No generic additional wire outlets (AP, etc.) — the reader rates most generalist wires
  poorly; framing diversity comes from region-native + analyst sources instead.

## Design

### 1. Structure — pinned-hybrid template

A short fixed spine plus a user-controlled dynamic middle. The "always shown" set is a
**pinned set** the reader controls, replacing the hardcoded five-country template.

- **New `feedback.json` field `pin`** — a list of topic labels. When the key is absent it
  defaults to `["ukraine", "iran", "korea", "japan", "china"]`, so day-one behaviour
  matches today exactly (nothing the reader cares about drops).
- **New Telegram commands `/pin <topic>` and `/unpin <topic>`**, implemented in
  `_handle_telegram_update`, symmetric to the existing `/watch` / `/unwatch`. Both persist
  via the existing atomic `save_feedback`.
- **`/status` lists pins.** `feedback_summary` gains a `Pinned: …` line (HTML-escaped, same
  as focus/mute), surfaced by the existing `/status` command. No separate `/pins` command.
- **`HELP_TEXT` + README** document `/pin`, `/unpin`, and the new `/status` line.
- **`build_daily_prompt` output template** changes from five hardcoded country sections to:
  - **Fixed spine** (always present, in order): `TOP STORIES` → `MARKET PULSE / WHAT MOVED`
    → *[dynamic topic sections]* → `MACRO SIGNAL` → `POSITION SIGNALS` → `WATCH / FORWARD`
    → `@@@SIGNALS@@@`.
  - **Dynamic middle:** the model renders a section for **every pinned topic** (always, at
    least a one-line pulse — a quiet pin collapses to one line, never vanishes) **plus** a
    section for **any unpinned topic that is materially significant today**, ranked by
    significance.
  - **READER OVERRIDES** block gains a `PINNED — always include at least a one-line pulse
    for: …` line built from the pin list.

**Pins vs `TOPICS`.** `TOPICS` keeps its current role: it drives the web-search queries and
the Chroma podcast queries. `pin` is a decoupled *display guarantee*. The default pin set
aligns with the `TOPICS` labels, so retrieval and display match on day one. A pin outside
`TOPICS` (e.g. `/pin taiwan`) is display-guaranteed but has no dedicated search/Chroma query;
the model draws on general material for it. This decoupling is intentional and acceptable.

### 2. Sources — region-native + free-forward

- **Keep:** Reuters Markets / Reuters World (wire backbone); all existing analyst/podcast
  feeds (Sinica, Un-Diplomatic, Observing Japan, Pinecone, Intersubjectively Transmissible,
  Marko Papic, Jacob Shapiro); the BCA Iran dashboard `WEB_SOURCES` entry.
- **Add** (free RSS or Google News `site:` proxy — no paywall-bypass):

  | Pin | Added source(s) | Role |
  |-----|-----------------|------|
  | 🇺🇦 Ukraine | Kyiv Independent; ISW daily assessment | regional; **forward-looking** |
  | 🇮🇷 Iran/Hormuz | Al Jazeera | regional/generalist (the one generalist the reader rates) |
  | 🇰🇷 Korea | Yonhap (English); 38 North | regional; **forward-looking** |
  | 🇯🇵 Japan | NHK World; BOJ statement feed | regional; **primary** |
  | 🇨🇳 China | SCMP | regional |

- **New feed field `kind`** on each `RSS_FEEDS` / `WEB_SOURCES` entry, one of
  `wire | analyst | regional | primary`. Existing feeds are tagged
  (Reuters → `wire`; substacks/Nitter → `analyst`; BCA → `regional`). The prompt uses
  `kind` to weight sources: wires anchor facts; analyst/regional/primary lead
  interpretation.
- **Graceful degradation.** Each new feed degrades silently to `""` if its RSS is
  unavailable (`fetch_rss` already handles this). During planning, each source's exact RSS
  URL is verified; any source lacking a usable feed falls back to the `site:` proxy, or is
  dropped from the slate with a note.

### 3. Market injection — "what moved & why"

The market pulse is **pin-derived**, not a fixed instrument list, so it follows whatever the
reader has pinned (consistent with the pinned-hybrid structure — a fixed list would keep
showing e.g. Hang Seng after `/unpin china`). It can't be fully config-free — the model can't
fetch prices (that's why we inject them), macro instruments aren't positions, and mapping a
pin to an instrument needs a lookup — but the predefined part is reduced to two principled,
pin-responsive pieces. Three tiers:

1. **Macro spine** (small, always fetched): `^spx`, `DXY`, `gold`, `BTC` — the universal
   risk-on/off pulse (~4 symbols).
2. **Pin-derived** (dynamic): a `topic → [instruments]` map (e.g. iran → Brent;
   japan → USD/JPY, Nikkei; china → Hang Seng). Only instruments for **currently pinned**
   topics are fetched, so the pulse tracks the pin set automatically. A pinned topic with no
   mapped instrument simply contributes no market line (the map is allowed gaps — e.g.
   Ukraine has no clean single instrument).
3. **Positions + watchlist** (fully dynamic, zero config): open-position daily moves from
   `book.json`, plus the watchlist, plus their volume anomalies.

- **New config:** `MARKET_SPINE` (tier 1, list of `(label, asset_class, instrument)`) and
  `PIN_INSTRUMENTS` (tier 2, `dict[topic_label, list[(label, asset_class, instrument)]]`).
  Exact Stooq/Kraken symbols are verified during planning; any symbol that does not resolve
  is dropped (logged), not fatal.
- **New `build_market_pulse(pins)` in `trading.py`.** It lives in `trading.py` because it
  consumes price/volume plumbing that already lives there (`fetch_price`, Stooq/Kraken
  fetchers, `load_book`, `volume-history.json`) and the one-way import chain
  (`common ← trading ← brief`) forbids `trading` importing `brief`. It takes the current pin
  list, fetches the spine + the pin-derived instruments + positions/watchlist, dedupes, and
  returns a formatted text block containing, best-effort:
  - each instrument's latest daily move (% vs prior close);
  - open-position daily moves from `book.json`;
  - current volume anomalies from the monitor's `volume-history.json`.
  Any individual fetch that fails renders as `—` and never blocks the brief.
- **Injection.** `build_daily_prompt` gains a `market_block: str = ""` parameter (same
  pattern as the existing `perf_block`). `mode_submit` builds the block from
  `build_market_pulse()` and passes it in. The prompt instructs: *"MARKET PULSE shows what
  moved; supply the likely why, and flag moves not explained by today's news as potential
  early signals."*

### 4. Edge tilt — prompt reweighting (balanced)

`SYSTEM_PROMPT` and the prompt body are reframed from "Prioritise Reuters" to a balanced
tilt: **wires anchor what happened (facts stay reliable), but interpretation leads with
forward-looking sources (regional analysts, primary statements, podcast context) and market
action; recap is compressed, not eliminated; do not echo headlines the market has already
priced.** The new `kind` field is referenced so the model knows which sources are wires
versus forward-looking.

## Data Flow

`mode_submit` (unchanged shape, two additions):

```
fetch RSS (expanded RSS_FEEDS, tagged by kind)
  + fetch web sources
  + build Chroma podcast context
  + build_market_pulse(pins)        # NEW — macro spine + pin-derived + positions/anomalies
  + fetch portfolio weights
  + performance_prompt_block
  + pin list (from feedback.json)   # NEW — drives PINNED override + dynamic template
  -> build_daily_prompt(..., market_block=..., perf_block=...)
  -> submit_batch(SYSTEM_PROMPT, prompt)
```

`_handle_telegram_update` gains `/pin`, `/unpin`; `/status` (`feedback_summary`) gains the
`Pinned:` line.

## Error Handling

- New feeds: silent degradation to `""` (existing `fetch_rss` behaviour).
- `build_market_pulse`: best-effort per instrument/position; failures render `—`; the
  function never raises into `mode_submit`.
- `/pin` / `/unpin`: validate/normalise input like the existing feedback commands; persist
  via atomic `save_feedback`; echo the resulting pin set.
- Default-pin seeding: absent `pin` key resolves to the five-country default at read time;
  it is not written eagerly (keeps `feedback.json` minimal and the default in one place).

## Testing

New tests (follow the module-patch convention: patch on the module whose function is under
test):

- `/pin` / `/unpin` add and remove topics; default seeding when `pin` key is absent;
  persisted via `save_feedback`.
- `/status` (`feedback_summary`) renders the `Pinned:` line, HTML-escaped.
- `build_daily_prompt` emits the fixed spine, the `PINNED` override line, and the dynamic
  middle instruction; pinned topics are named.
- `build_market_pulse` formats moves/positions/anomalies and degrades gracefully when a
  fetch returns nothing (no exception escapes).
- `kind`-based weighting appears in the assembled prompt.

The existing suite (155 tests) stays green; new tests are additive.

## Evaluation

A combined redesign has no per-lever attribution by construction. Evaluation leans on:

1. the existing validation layer (`validation.aggregate_performance` — `hit_rate`,
   `mean_edge`, benchmark `edge`) accumulating over ~2 weeks of signals;
2. the reader's own read experience of the new brief.

**Revisit trigger:** after ~2 weeks of briefs, review the validation numbers and the read
experience; if the brief still feels priced-in, the next step is a deeper edge-latency pass
(e.g. more primary indicators, intraday market context).

## Open Items Deferred to Planning

- Exact RSS URLs for Kyiv Independent, ISW, Yonhap, 38 North, NHK World, BOJ, SCMP, Al
  Jazeera; fall back to `site:` proxy or drop per source.
- Exact Stooq/Kraken symbols for the `MARKET_SPINE` (S&P, DXY, gold, BTC) and the
  `PIN_INSTRUMENTS` map (Brent, USD/JPY, Nikkei, Hang Seng, …); confirm the per-pin
  instrument mapping and accept gaps for pins with no clean instrument.
- Whether `MARKET PULSE` is its own brief section or folds visually into `MACRO SIGNAL` —
  current design gives it its own spine slot; planning confirms against output length budget
  (`MAX_TOKENS` = 16384 whole-turn).

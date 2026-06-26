# Source mining — complete the perspective matrix + energy starter

**Date:** 2026-06-26
**Status:** Design — approved, pending spec review
**Backlog item:** External geo-dashboards borrow-backlog #4 (source-list mining)

## Problem

The brief shipped a perspective-tagging feature (borrow-backlog #1) with a 10-value
vantage enum:

```
WESTERN, CHINESE, RUSSIAN, IRANIAN, ISRAELI, ARAB, UKRAINIAN, JAPANESE, KOREAN, INDIAN
```

Only **5 of those values are actually sourced today** (Arab→Al Jazeera, Ukrainian→Kyiv
Independent, Japanese→NHK, Korean→Yonhap, Chinese→SCMP). The other five — **Russian,
Iranian, Israeli, Indian, and explicit Western** — are defined infrastructure with no
feed behind them. The SYSTEM_PROMPT instructs the model to "triangulate agreement /
divergence across vantages," but for half the matrix there is nothing to triangulate.

Separately, the brief has **zero energy/commodities coverage**, a genuine gap given the
reader's portfolio exposure.

## Goal

1. **Complete the perspective matrix** for the four genuine gaps: **Russian, Iranian,
   Israeli, Indian.** (Explicit `WESTERN` is deliberately left unused — see Decisions.)
2. **Add a small energy/commodities starter** (2–3 thematic feeds, no vantage tag).

All new feeds are **permanent baseline additions baked into `RSS_FEEDS`**, validated
live before baking. They ride the next deploy (batched with the unpushed #5a
"why it matters" lens, so the pipeline is restarted only once).

## Non-goals

- No new code paths, functions, or schema. This is a data addition to `RSS_FEEDS` plus
  regression tests.
- No energy coverage forced through RADAR (RADAR is country-organized; energy trade
  press is thematic and not in it — see RADAR's role).
- Not filling `WESTERN`, Latin America, Africa, or SE Asia this pass.
- No full ingest of RADAR's 190-country list — that would flood the curated brief.

## Source model (existing, unchanged)

A feed is a dict in `RSS_FEEDS` (brief.py): `{name, url, category, kind, perspective?,
state_funded?}`.

- `kind ∈ {wire, analyst, regional, primary}`
- `perspective ∈ VALID_PERSPECTIVES` (the 10-value enum), **optional and sparse** —
  absent means "no vantage claim made," NOT "neutral."
- `state_funded: bool` (default False)
- `category` is a **free-form label**, surfaced to the model only as `(CATEGORY)` in the
  per-source header `### Name [KIND · PERSPECTIVE · STATE-FUNDED] (CATEGORY)`. It does
  **not** drive brief structure — the model renders sections dynamically by significance —
  so adding new categories carries no fragmentation risk.

## Candidate feeds

Each candidate is validated live before baking; the listed alternate is used on failure.

| Vantage / theme | Primary feed | Alternate | kind | category | perspective | state_funded |
|---|---|---|---|---|---|---|
| Russia — official | TASS (English) | RT | regional | russia | RUSSIAN | ✅ |
| Russia — independent | Meduza (English) | The Moscow Times | regional | russia | RUSSIAN | ❌ |
| Iran — official | Press TV | Tehran Times | regional | mideast | IRANIAN | ✅ |
| Iran — independent | IranWire | Radio Farda | regional | mideast | IRANIAN | ❌ |
| Israel (free press → 1) | Times of Israel | Haaretz / Jerusalem Post | regional | mideast | ISRAELI | ❌ |
| India (free press → 1) | The Hindu | Indian Express | regional | india | INDIAN | ❌ |
| Energy | OilPrice.com | — | wire | energy | — | — |
| Energy | Reuters Commodities (site: proxy) | — | wire | energy | — | — |
| Energy | EIA | — | primary | energy | — | — |

≈10 new feeds → ~27 total in `RSS_FEEDS`. The LLM curates by significance (only the 5
newest items per feed are used via `fetch_rss max_items`), so volume is not a structural
risk — but it is a deliberate, monitored increase.

New categories introduced: `russia`, `mideast`, `india`, `energy`.

## Validation gate (the core of the work)

For each candidate, in order:

1. Fetch the **native RSS** URL. Require **≥3 entries** (`feedparser`).
2. If native returns 0/dead, fall back to the **Google-News `site:` proxy** via
   `build_google_news_url(domain)` (`when:2d`, or `when:7d` for low-frequency sources
   like EIA). Re-test the ≥3-entries gate on the proxy.
3. If both fail, drop the primary and try the **alternate** the same way.
4. If everything fails for a slot, skip that slot and record it in the spec/commit
   message rather than baking a dead URL.

Each baked feed gets a `# verified N entries 2026-06-26` comment, matching the existing
region-native block (lines ~189–253 of brief.py). Energy count: bake **3 if all
validate cleanly, else 2.**

The gate is run as a **one-off live script** (e.g. under `scratchpad/`), not shipped code.
This matters because RADAR feeds are notoriously stale and a baked feed that silently
returns 0 is invisible rot — just a logged warning at submit time. The live gate + the
native→proxy fallback is what separates "added sources" from "added dead URLs."

## RADAR's role — honest scoping

The canonical outlets above are well-known; RADAR (`server/src/news_feeds.json`, MIT) is
**not load-bearing** for finding them. URLs and facts are not copyrightable regardless of
license. RADAR is used only as a **discovery cross-check** for any non-obvious regional
feed worth adding — its country structure is well-suited to that and useless for the
energy slice. This satisfies backlog #4 while the real deliverable is matrix completion.

## Testing

Pure data change — no new functions, so tests assert the *outcome*:

1. **Matrix-completion regression test:** assert that each of `RUSSIAN`, `IRANIAN`,
   `ISRAELI`, `INDIAN` now has ≥1 source in `RSS_FEEDS`. Locks the goal so a future edit
   can't silently drop a vantage.
2. **Structural test:** every `RSS_FEEDS` entry has a `kind ∈ VALID_KINDS`, any
   `perspective ∈ VALID_PERSPECTIVES`, and a non-empty `category`.

Existing `ruff check` + `ruff format --check` + `pytest` gate applies (see
[[brief-local-run]]).

## Decisions (locked with user)

- **Energy belongs to its own later backlog item, not forced through RADAR** — but a
  2–3 feed starter ships now because of portfolio exposure.
- **State-controlled-media vantages (Russia, Iran) get BOTH** an official (state_funded)
  and an independent outlet — official reads "the Kremlin/Tehran line" (what the
  perspective feature is for); independent supplies better facts.
- **Free-press vantages (Israel, India) get ONE** feed each — the "both" rule is only for
  state-controlled media; applying it to free press would be noise.
- **`WESTERN` stays unused** — wires/analysts are already Western, and per the design an
  absent perspective lets the model fall back on (Western-ish) priors. An explicit tag
  adds little.
- **Validate-then-bake into `RSS_FEEDS`** (not temp sources) — these are permanent
  baseline, not situational; temp sources would be a semantic mismatch.

## Related

[[external-geo-dashboards-backlog]] (#4), [[brief-py-sibling-prompt-strings]],
[[google-news-rss-recipe]], [[direct-page-temp-sources]], the #1 perspective-tagging
feature (`2026-06-25-perspective-state-funded-tagging-design.md`).

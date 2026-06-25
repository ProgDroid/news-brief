# Perspective / state-funded source tagging — design

**Date:** 2026-06-25
**Status:** approved (pending spec review)
**Backlog:** item #1 of `external-geo-dashboards-backlog` (RECOMMENDED FIRST). Borrowed *idea* (not code) from Pharos + WorldMonitor, both of which schema-bake source perspective/state-funding. Both are AGPL-3.0 → idea only, reimplemented from concept.

## Goal

Sharpen the brief's "full picture, not one side" framing by telling the model, per source, (a) whether it is state-funded and (b) what national/bloc vantage it speaks from — where that vantage actually changes the read. The model already has strong priors on most outlets; the tag earns its keep only on the sparse exceptions, so tagging is **opt-in per source**, not blanket.

## Scope decision (chosen: lean)

Considered three scopes:
- **Full** — required `perspective` enum + `state_funded` on *every* source. Rejected: labels neutral wires with a slant they don't carry; adds a required wizard step; sparse signal drowned in noise.
- **Lean (CHOSEN)** — binary `state_funded` everywhere (defaults False), `perspective` optional and set only where it changes the read.
- **stateFunded-only** — drop perspective entirely. Rejected: perspective is the higher-value half for the "whose narrative is this" reading on regional sources.

### Why no NEUTRAL enum value
A NEUTRAL tag is a *positive editorial claim* ("this source has no vantage") and is as contestable as picking a side. The lean plan's **absent** perspective encodes something weaker and more honest: *"no vantage claim made — model, fall back on your own priors."* Absence ≠ a neutrality verdict. This distinction is documented in code and taught in the prompt so a missing tag is never misread as an endorsement of neutrality.

## Design

### 1. Schema + semantics

Two new optional keys on a source dict, alongside `kind`:

- `state_funded: bool` — defaults `False`. Binary, unambiguous.
- `perspective: str | None` — defaults absent. Validated against `VALID_PERSPECTIVES`; an unknown value degrades to absent (same graceful-fallback pattern `kind` uses: invalid → safe default).

`VALID_PERSPECTIVES` (extensible tuple), naming national/bloc vantage rather than country of incorporation:

```
("WESTERN", "CHINESE", "RUSSIAN", "IRANIAN", "ISRAELI",
 "ARAB", "UKRAINIAN", "JAPANESE", "KOREAN", "INDIAN")
```

**Documented semantics:** absent `perspective` = "no vantage claim made", NOT "neutral". A comment at the constant definition states this so future edits don't introduce a NEUTRAL value.

### 2. Header rendering (what the LLM sees)

The `kind` tag is stamped into the section header that goes to the model (`### {name} [{KIND}] ({CATEGORY})`). New fields append to that bracket; absent fields are omitted, so untagged sources render byte-identical to today:

- `### Reuters World [WIRE] (GEO)` — unchanged
- `### Al Jazeera [REGIONAL · ARAB · STATE-FUNDED] (GEO)`
- `### SCMP [REGIONAL · CHINESE] (CHINA)`

Today `fetch_rss` and `fetch_web_source` each format this bracket inline (brief.py ~1207 and ~1242). Factor the bracket into **one helper** (`_source_header(name, kind, category, perspective, state_funded)` or similar) used by both, so the two sites cannot drift. The `·` separator is a literal middle dot inside the existing `[...]`.

### 3. Prompt line (attribute + triangulate)

Add to `SYSTEM_PROMPT` (brief.py ~1459), wording approximately:

> Some sources carry a perspective tag (the vantage they speak from) and/or a STATE-FUNDED flag. When a tagged source makes a claim, attribute its framing to that vantage rather than stating it as neutral fact (e.g. "Beijing's read, via SCMP, is…"). Treat agreement across opposing perspectives — or a state-funded outlet corroborating an independent wire — as a stronger signal; treat divergence as a flag worth surfacing. An untagged source carries no vantage claim; weigh it on its merits.

### 4. Wizard (`/addsource`)

After the existing `kind` step, add two steps mirroring the `as:kind:` pattern:
1. `state_funded?` — yes/no tap (`as:sf:0` / `as:sf:1`).
2. `perspective` — buttons from `VALID_PERSPECTIVES` (`as:persp:<VALUE>`) **plus a prominent "Skip" button** (`as:persp:_skip`) as the common path. Skip → no perspective key on the entry.

Thread both into the persisted entry dict and the confirmation line. Defaults if a step is somehow bypassed: `state_funded=False`, no perspective.

### 5. Baked-in source assignments

Only regional/primary sources carrying a national vantage get tagged. All wires + analyst substacks + US think-tanks (Reuters, the substacks, ISW, 38 North, Pinecone, BOJ, etc.) stay **untagged, `state_funded=False`** — tagging a US think-tank "WESTERN" is exactly the over-labeling the lean plan avoids.

| Source | perspective | state_funded | rationale |
|---|---|---|---|
| Al Jazeera | ARAB | True | Qatar government–funded |
| NHK World | JAPANESE | True | Japan public broadcaster |
| Yonhap (English) | KOREAN | True | SK state news agency, govt-subsidised |
| SCMP | CHINESE | False | Alibaba-owned (not state), pro-Beijing editorial vantage |
| Kyiv Independent | UKRAINIAN | False | reader-funded, independent |

### 6. Tests

- **Validation round-trip** (`load_temp_sources`): valid `state_funded`/`perspective` kept; unknown perspective dropped to absent; missing → `state_funded=False`, no perspective.
- **Header helper**: renders all four combinations (none / perspective-only / state_funded-only / both) correctly; untagged source byte-identical to current output (regression guard).
- **Wizard**: "Skip" path yields no perspective; a chosen perspective + `state_funded=True` persist into the entry.
- **Existing suite** stays green.

## Out of scope

- Perspective on wires/analysts (intentionally untagged).
- A NEUTRAL value (see rationale above).
- Cross-feed corroboration *counting* — that's backlog item #3 (claim ledger), separate.
- Any UI/globe/map borrowing — explicitly SKIP per backlog.

## Files touched

- `brief.py` — `VALID_PERSPECTIVES` constant; `load_temp_sources` validation; `_source_header` helper + call sites in `fetch_rss`/`fetch_web_source`; `SYSTEM_PROMPT` line; wizard steps + callbacks; 5 baked-in `RSS_FEEDS` entries.
- `tests/` — new cases per §6.

## Verification gate

`ruff check` + `ruff format --check` + `pytest` all green before commit (per project pre-push gate). Commit straight to main (solo repo). Update `external-geo-dashboards-backlog` memory: mark item #1 STATUS done.

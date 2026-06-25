# Corroboration confidence tag for the claim ledger

**Date:** 2026-06-25
**Status:** Design — approved, pending plan
**Backlog item:** External geo-dashboards backlog #3 (multi-source corroboration → claim ledger)

## Summary

Give each durable claim in the standing-claim ledger (`brief_memory.json`) a notion
of **how broadly it was corroborated across independent outlets**, and render that
back into the daily brief as a coarse confidence cue (`widely corroborated` /
`single-source`). Descriptive only — like the rest of the ledger it never affects
trading, is gated by the existing `BRIEF_MEMORY_ENABLED` flag, and is fail-safe
(any error leaves the brief and the prior ledger intact).

This steals WorldMonitor's corroboration idea (a claim seen across N independent
feeds is more trustworthy) but applies it to our descriptive ledger rather than to
a ranking/importance score — we deliberately do **not** build a ranking pipeline at
our ~190-item scale. The output is a reader-facing trust cue that feeds the
"confidence calibration" direction flagged as the next genuine value for the brief.

## Goals

- Each durable claim carries a best-effort count of distinct corroborating outlets.
- The brief can express confidence in established claims grounded in real source
  breadth, not hallucinated from synthesized prose.
- Zero new API round-trips: the corroboration assessment folds into the existing
  Haiku reconcile call.
- Fully backward-compatible and fail-safe: missing/old data degrades to today's
  behaviour (no cue), never an error.

## Non-goals (explicitly out of scope)

- **Retention / TTL weighting.** Corroboration does NOT influence which claims
  survive the `MAX_CLAIMS` cap or the 7-day retirement. The cap + recency TTL +
  the reconcile model's "keep the most important durable facts" instruction already
  do soft corroboration-weighting implicitly; hard-coding it is internal complexity
  for negligible observable benefit. (Trivial future add-on if ever wanted.)
- Any trading / sizing use. Descriptive only, consistent with the rest of the ledger.
- A story-ranking / importance score (WorldMonitor's `min(sourceCount,6)×12`).
  Premature at ~190 items; we offload ranking to Claude.
- Re-fetching feeds at collect time, or a third post-gen LLM call.

## Background — the data-flow constraint that shapes the design

The brief runs as **two separate processes hours apart**:

- `mode_submit` (`brief.py`) fetches the source-labelled feeds (`feed_content`,
  `web_content` — each feed's items grouped under a `_source_header(...)` block),
  builds the prompt, and fires a Claude **batch**.
- `mode_collect` polls the batch result up to ~12h later and reconciles the ledger.
  At reconcile time (`brief.py` ~2354) the only inputs are the finished **brief
  text** + `today`. The raw feeds are gone — fetched in a different process, never
  persisted.

So a *grounded* corroboration count cannot be read from an existing field; the
source breadth that exists at submit must be **persisted across the gap** and
joined to the durable claims at reconcile. This is the core of the design.

## Approach (chosen)

**Persist a compact source index at submit; let the existing reconcile LLM bucket it.**

Rejected alternatives:
- *Reconcile guesses from brief text alone* — the brief is synthesized prose that
  rarely enumerates its sources, so the tag would be largely hallucinated. A false
  "widely corroborated" tag is worse than no tag (manufactures false confidence).
- *Deterministic count in code at submit* — claim↔headline matching is fuzzy and
  durable claims don't exist yet at submit (the brief isn't generated until the
  batch returns), so the join is brittle. The LLM already reading both the brief
  and the index does this join far more robustly.

## Data model

One new field per claim in `brief_memory.json`:

- `source_count: int` — best-effort number of **distinct outlets** that carried the
  claim, kept at its **peak** (monotonic `max` across days).

The coarse bucket is **derived at render time**, not stored:

| bucket   | source_count | rendered cue        |
|----------|--------------|---------------------|
| `wide`   | ≥ 4          | `widely corroborated` |
| `some`   | 2–3          | `corroborated`      |
| `single` | 1            | `single-source`     |
| (none)   | 0 / missing  | no cue              |

Deriving the bucket at render time (rather than storing it) means thresholds can be
retuned later without rewriting history, and we never persist false precision — the
integer is best-effort, the reader only ever sees the coarse bucket.

**Judgment call — peak vs. today (decided: peak).** `merge_ledger` keeps
`source_count = max(prior_count, today_observed)`. "Established" is about whether a
fact was *ever* broadly confirmed; a claim confirmed by 8 outlets last week is still
well-established even if nobody re-runs it today, so the tag must not decay as the
story ages out of the cycle.

**Judgment call — thresholds (decided: 1 / 2–3 / ≥4).** Caps display at "4+";
matches WorldMonitor's spirit without their 6-cap, which existed for a ranking score
we are not building.

## Flow

### 1. Submit — build & persist the source index

A new helper builds a compact, **titles-only**, source-labelled index from the
feeds fetched this run (drop the 400-char summaries — only outlet name + headline
are needed to count breadth). Shape (text, ~190 lines, ~2–3k tokens):

```
SOURCE: Reuters
- <headline>
- <headline>
SOURCE: Al Jazeera
- <headline>
...
```

Persisted to `source_index-{today}.json` on the data volume (`DATA_DIR`). If the
write fails, submit is unaffected (the index is best-effort context, not load-bearing
for the brief).

### 2. Collect / reconcile — join breadth to claims

The existing Haiku reconcile call (`reconcile_ledger`) additionally receives the
day's source index (loaded from `source_index-{today}.json`). The reconcile prompt
and output schema gain one field per claim:

- `source_count: int` — count of distinct `SOURCE:` blocks in the provided index
  whose headlines support this claim; `0` if none/unknown.

`merge_ledger` carries the peak: for a reaffirmed claim, `max(prior, observed)`;
for a new claim, the observed value; for a claim not returned by the model this run
(carried over untouched), the prior value is preserved unchanged.

### 3. Render — surface the cue

`render_established_block` prefixes each claim with its derived bucket cue and
extends its instruction paragraph: reference single-source claims more tentatively,
lean on widely-corroborated ones with confidence. Example line:

```
  • [oil] (widely corroborated) OPEC+ extended its production cut through Q3.
  • [tech] (single-source) Startup X claims a 10x training-cost reduction.
```

## Error handling / backward compatibility

- **Missing index file** (submit ran on an older build, or the write failed):
  reconcile runs exactly as today — the prompt omits the index section, the model
  returns claims with no `source_count`, and render omits the cue. No error.
- **Old claims already in the live ledger** lack `source_count` → render untagged
  until next reaffirmed with an index present.
- **Malformed/extra fields** in the reconcile response are tolerated by the parser
  (consistent with current lenient parsing).
- **No new feature flag.** Rides under the existing `BRIEF_MEMORY_ENABLED` (already
  on in prod) — it is an extension of the same descriptive ledger. Because every new
  path degrades cleanly to current behaviour, activating on deploy is safe.

## Blast radius

- `brief_memory.py` — reconcile template (inject index + new output field), parser
  (accept `source_count`), `merge_ledger` (carry peak), `render_established_block`
  (derive + render bucket), plus the threshold/bucket helper.
- `brief.py` — `build_source_index` seam (derive titles-only index from the fetched
  feeds), persist at submit, load + pass into `reconcile_ledger` at collect.

## Testing (TDD)

- `merge_ledger` keeps the peak `source_count` (reaffirmed > prior, new = observed,
  carried-over untouched, never decays).
- Bucket derivation: 0→none, 1→single, 3→some, 4→wide, large→wide.
- `parse_reconcile_response` tolerates missing `source_count`, extra fields, and
  non-int values (coerce/skip safely).
- `render_established_block` emits the right cue per bucket and omits it at 0/missing.
- `build_source_index` produces source-labelled titles-only text from feed content
  and is robust to empty / malformed feed blocks.
- End-to-end fail-safe: reconcile with no index file behaves identically to today.

## Connections

See `[[brief-claim-memory-build]]`, `[[sentiment-sizing-null-decided]]`
(confidence-calibration as next value), `[[external-geo-dashboards-backlog]]` item #3.

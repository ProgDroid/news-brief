# Comprehension pipeline cost — measured baseline and what to explore next

**Date:** 2026-09-10
**Status:** `COMPREHEND_ENABLED=false` on the host. Nothing is spending.
**Purpose:** hand the cost exploration to the next session with the numbers
already established, so it does not re-derive them or re-propose the ideas that
measurement has already killed.

## What the bill actually was

Anthropic console, 7–10 Sep 2026:

| | tokens in | tokens out |
|---|---|---|
| Sonnet 5 | 7,046,666 | 1,488,239 |
| Haiku 4.5 | 1,245,266 | 107,082 |

At $2/$10 per MTok (Sonnet 5) and $1/$5 (Haiku 4.5): **≈ $31 consumed.** The user
also holds ~$19 unspent credit, which is why the card charge looked like ~£45 —
that gap is accounted for and is not missing spend.

Daily, against a pre-comprehend baseline of ~100k Sonnet + ~10.5k Haiku per day
(figures are in+out combined, and they reconcile exactly against the totals):

| day | Sonnet | × baseline | ≈ cost |
|---|---|---|---|
| 7 Sep | 801,382 | 8× | $2.71 |
| **8 Sep** | **5,142,748** | **51×** | **$17.45** |
| 9 Sep | 1,632,825 | 16× | $5.54 |
| 10 Sep | 957,950 | 10× | $3.25 |

**One day is 57% of the bill, and its cause is already fixed.** 8 Sep is the
`items`-as-JSON-string failure (`68688a0`) — "35 items per pass, 12% of the
corpus, whole batches at a time, and re-paid every hour" — compounded by the
retry multiplication recorded in `shared-helper-carries-first-callers-tuning`
(3 item-level × 2 HTTP = 6 charged 8192-token generations per bad item).

The trend since is **5.14M → 1.63M → 0.96M**, before today's `h8p` (no-verdict
failures stop charging) and `19i` (the double-wrap recovery) are reflected.

The per-day in/out split above applies the 4-day aggregate ratio to each day. It
is an approximation; 8 Sep was probably more output-heavy, which would make it
more dominant, not less.

## Ideas that measurement has already killed

Do not re-propose these without new evidence.

**Truncating item bodies.** Measured on production: `avg 154 chars, p95 502,
max 4205` across 4,353 items. Five items per call is ~770 chars ≈ 210 tokens.
Bead `uqy` was right that no outlet's RSS body carries article text. There is
nothing to truncate.

**Prompt-caching the static prefix.** `system` + `tools` = 2,796 chars ≈ **777
tokens**. Claude Sonnet 5's minimum cacheable prefix is **1024 tokens**, so a
`cache_control` marker is accepted and then silently does nothing —
`cache_creation_input_tokens: 0`, no error. Worth ~$0.50/day if it worked.
Recorded in `build_integration_request`'s comment. Revisit only if the prefix
crosses 1024 tokens or the model changes (Claude Opus 5's minimum is 512, and
the minimums are **not** monotonic across generations).

## The instrument that now exists

`_timed_post` logged `out=` and nothing else, so the number that sizes the bill
was never recorded — which is why this took a billing console and three days to
diagnose. It now logs:

```
in=<input_tokens> out=<output_tokens> cache_read=<n> cache_write=<n>
```

**One day of production logs with this in place answers every open question
below.** Get that before changing anything else.

## Open options, ranked by what the numbers currently support

### 1. Batch size — the obvious lever, and it is NOT obviously a win

`COMPREHEND_INTEGRATE_BATCH` is 5, giving ~320 integration calls/day.

- Raising it to 15 cuts calls to ~107, saving ~213 repeated 777-token prefixes
  ≈ 165k tokens/day ≈ **$0.33/day**.
- **But candidate entities and events are gathered per batch**, so a batch of 15
  carries a larger candidate list. That growth may cancel the prefix saving
  entirely. Nobody knows by how much, because per-call input has never been
  measured.
- It also roughly triples expected output per call (today: `out=` 450–1,000 for
  5 items) against `INTEGRATE_MAX_TOKENS` 8192. Headroom looks adequate, but
  truncation is this repo's most expensive recurring failure
  (`signals-parse-error-is-truncation`, recurred 4×).

**Do this only after reading one day of `in=` numbers.** If input per call is
dominated by the prefix, raise the batch; if it is dominated by candidates,
raising the batch makes things worse.

Note it is a **settings row**: editing the default in `common.py` changes nothing
on an established host.

### 2. Frequency — the largest lever nobody has costed

Comprehend runs hourly, capped by `COMPREHEND_MAX_ITEMS` (300/day). Halving the
frequency roughly halves the bill, and the question is entirely about what the
KB loses. `b42.5` already established that capture's cadence can be measured
rather than guessed; the same method applies here.

### 3. Candidate list size

`candidate_events` and the entity candidates are the per-call variable cost, and
`candidate_cap_hit` already fires 10–22 times per pass in production. That
counter is evidence the caps bind regularly — worth reading before assuming the
lists are small.

### 4. Doing work locally instead of in the model

Triage is already rules-first (`triaged_by_rules` 41–109 vs `triaged_by_model`
0–7 per pass), so the cheap half is already local. The integration stage is
extraction, which is the part that genuinely needs a model. Any local
preprocessing should target *reducing what reaches integration*, not replacing
it.

### 5. Model choice / OpenRouter

Deliberately last. At ~$3/day and falling, a provider migration is a large change
against a small and shrinking number, and it forfeits the prompt-cache namespace
if caching ever becomes viable. Revisit only if 1–4 leave the bill unacceptable.

## Immediate state

- `COMPREHEND_ENABLED=false` — nothing spending.
- Re-enable with the settings row, then read one day of `in=`/`cache_read=`.
- The daily brief batch is a separate line item worth its own look: today it was
  `in=289313 out=10337 cache_read=0`, `est_batch_cost=$0.5615` — roughly a third
  of the day's Sonnet usage **in a single call**, and it caches nothing. Its
  prefix is likely well over 1024 tokens, so unlike comprehend it is a real
  caching candidate.

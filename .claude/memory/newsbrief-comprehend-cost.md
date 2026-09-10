---
name: newsbrief-comprehend-cost
description: What the comprehension pipeline actually costs, the baseline it is measured against, and the two savings ideas measurement already killed
metadata:
  type: project
---

**$31 consumed 7-10 Sep 2026, and 57% of it was ONE day.** Full write-up with the
per-day table: `docs/2026-09-10-comprehend-cost-handover.md`. Read that before
proposing any cost work; this is the index.

Baseline before comprehend: ~100k Sonnet + ~10.5k Haiku tokens **per day**. The
spike day (8 Sep) hit 5.14M Sonnet = 51x, and its cause was already fixed by
`68688a0` -- the `items`-as-JSON-string failure re-paid hourly, compounded by the
retry multiplication in [[shared-helper-carries-first-callers-tuning]]. Trend
since: **5.14M -> 1.63M -> 0.96M**, before `h8p` and `19i` landed.

**A card charge is not a burn.** The user reported ~£45; ~$19 of that was unspent
credit. Reconcile purchased against consumed before treating a gap as a mystery.

**TWO IDEAS ARE DEAD -- do not re-propose without new evidence:**

- **Truncating item bodies.** Measured: avg **154 chars**, p95 502, max 4205
  across 4,353 rows. RSS carries leads, not articles (bead `uqy` was right).
  There is nothing to truncate.
- **Prompt-caching the integration prefix.** `system` + `tools` = ~**777
  tokens**; Claude Sonnet 5's minimum cacheable prefix is **1024**. The marker is
  accepted and silently does nothing. See [[a-config-can-be-accepted-and-inert]].

**A THIRD IS UNPROVEN, and I withdrew my own recommendation for it.** Raising
`COMPREHEND_INTEGRATE_BATCH` 5->15 cuts ~320 calls/day to ~107 and saves ~$0.33/day
of repeated prefix -- but **candidates are gathered PER BATCH**, so the per-call
candidate list grows and may cancel it entirely. I proposed it before knowing the
prefix was 777 tokens and bodies 154 chars. **A recommendation made before
measurement has to be re-checked after it, not inherited.**

`_timed_post` now logs `in=`, `out=`, `cache_read=`, `cache_write=` -- that number
never existed before, which is why this took a billing console and three days.
**One day of production logs settles the batch and frequency questions.**

Separate and unexamined: the daily brief batch runs `in=289313` in a SINGLE call
with `cache_read=0` -- about a third of a day's Sonnet spend, and unlike
comprehend its prefix is almost certainly over 1024 tokens, so it is a real
caching candidate.

---
name: newsbrief-comprehension-pipeline
description: "bqa.4b BUILT 2026-09-05 but UNPUSHED and disabled — comprehend.py plus its pre-registered gate, and the five defects that nearly shipped silently"
metadata: 
  node_type: memory
  type: project
  originSessionId: 19b68250-ca61-4936-834b-18d6a595831c
  modified: 2026-09-05T19:23:03.674Z
---

**`bqa.4b` CLOSED 2026-09-05.** 26 commits on local `main` (`25d911f..41d7747`), suite **1554
passed / 0 failed**, ruff clean. **NOT PUSHED, NOT DEPLOYED.** Behind `COMPREHEND_ENABLED`, which
defaults off; the compose anchor line exists.

`comprehend.py` is an hourly job child building the entity/event/assertion layer over captured
items: triage in two halves (a free `SurfaceIndex` lookup, then a model call on the remainder),
a daily-budgeted `sampled` control arm, candidate retrieval, extraction parsing with validation,
and a savepoint-per-item write path. Migrations **0009** (`item_triage`) and **0010**
(`sampled_at`). `scripts/score_comprehension.py` is the pre-registered gate.

**Five defects that would have shipped silently** — every one found by running or mutating code,
none by reading:

- `SurfaceIndex.build`'s claims query omitted `retired_on IS NULL`, so retired topics stayed
  trackable forever. It was the only live-claim query in the repo missing it, and **migration 0007
  contains a written warning about exactly this** — a partial index restricts what is INDEXED, it
  does not filter a query.
- The sampled daily cap counted `created_at` (triage time) while the budget is spent at
  *promotion*, and `ON CONFLICT` never rewrites it. Across any UTC boundary the cap unbinds:
  20/hour becomes **480/day** in the expensive tier.
- `events.occurred_at` was written nowhere while `candidate_events` filtered on it — would have
  pinned `events_matched` at 0 and failed the corroboration gate *by construction*. It survived an
  earlier draft because **the fixtures set the column and production did not**.
- `"candidate_id": true` passed the hallucination guard, because `True == 1` and `True in {1}`.
  Fixed by `_is_id` at four sites (`isinstance(x, int) and not isinstance(x, bool)`; `1.0 == 1`
  too).
- The gate queried for `events_matched`/`events_created`, which live only in an in-memory `Tally`
  and are persisted to no table. Now derived exactly: `matches = count(assertions) - count(events)`.

**Recurring shape, three instances in one build: a row a predicate keeps re-selecting that nothing
advances.** Whenever a row is selected by a predicate, ask what advances it and check every path
that can decline to — each case was an early exit that skipped the advancement step.

**Open follow-ups:** `news-brief-3wb` (re-integration accumulates rather than supersedes — measured
1→2 events after a version bump), `news-brief-wvt` (integration reuses `_post_messages`'s 90s
timeout, sized for signals, at `max_tokens=8192`), `news-brief-uer` (an empty extraction is retried
3× as a failure), `news-brief-ya4` (`_TRIAGE_SYSTEM` not derived from `brief.SYSTEM_PROMPT` —
**deliberately deferred so the pre-registered gate measures the system it was registered against**).

**Operational gap:** `scripts/` is not in the Dockerfile COPY (consistent with the three older
scripts), so the gate cannot run inside the container despite measuring production data.

Method that produced this: [[mutation-diagnostic-demands-a-count]]. Schema context in
[[newsbrief-kb-schema-0006]]; capture upstream in [[newsbrief-capture-feature]].

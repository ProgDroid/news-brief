---
name: newsbrief-comprehension-pipeline
description: "bqa.4b — comprehend.py and its pre-registered gate: PUSHED, still disabled; the five defects that nearly shipped, and why two 'follow-ups' were really gate-validity bugs"
metadata: 
  node_type: memory
  type: project
  originSessionId: 19b68250-ca61-4936-834b-18d6a595831c
  modified: 2026-09-07T17:08:11.783Z
---

**`bqa.4b` CLOSED 2026-09-05.** 26 commits (`25d911f..41d7747`), ruff clean.
**CORRECTED 2026-09-07: it IS pushed** — this file said "NOT PUSHED, NOT DEPLOYED" for two days
after it stopped being true, and that stale line sent me looking for unpushed commits at the top
of a session. `git rev-list --left-right --count origin/main...HEAD` after a `git fetch` is the
probe; an empty `origin/main..HEAD` against a stale remote-tracking ref proves nothing.
Still **DISABLED and NOT DEPLOYED**: `COMPREHEND_ENABLED` defaults off and the compose anchor
exists.

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

## Sequence the flag-flip: gate-validity bugs first (2026-09-07)

**A pre-registered gate is a ONE-SHOT instrument, so audit the backlog for bugs that distort what
it READS before flipping the flag.** The script says it outright — "Do not tune these to make a run
pass; a threshold moved after seeing the data measures nothing" — which means a FAIL you cannot
attribute is close to unrecoverable: you cannot honestly re-run a pre-registration after looking.
Two of the four bqa.4b "follow-ups" turned out to be gate-validity bugs, not deferrable polish, and
nothing in their titles said so. **Read each open bug asking "does this change a number the gate
reads?", not "is it P2 or P3".** He chose fix-first over flip-now on exactly this argument.

- **`news-brief-wvt` FIXED 2026-09-07.** A too-short timeout dumps items into `failed_integration`,
  which the gate then reads as a weak extractor — thin data misattributed as a bad model.
- **`news-brief-uer` FIXED 2026-09-07.** Its own description said it concentrates on the `sampled`
  arm *by construction*, and `sampled` is the gate's unconfounded control. A P3 whose blast radius
  is the control arm outranks its priority number.
- **`news-brief-3wb` still open, correctly deferred** — unreachable while `INTEGRATE_PROMPT_VERSION`
  is 1, so it cannot touch the first gate run.
- **`news-brief-ya4` still open, deliberately deferred** so the gate measures the system it was
  registered against.

**NEXT = `news-brief-bqa.11`** (filed 2026-09-07, tops `bd ready`): enable `COMPREHEND_ENABLED` on
the host, choose an accumulation window, decide whether triage runs on a cheaper model, then run the
gate. The gate needs an outlet with 10+ assertions and multi-outlet corroboration between 10% and
60%, so it needs both volume and elapsed time. `news-brief-115` and `news-brief-ya4` are now
correctly BLOCKED on it. **The one input only he can supply: how many items a day capture is
actually producing** — it sets both the cost exposure and the window, and cannot be answered from
the dev repo. See [[shared-helper-carries-first-callers-tuning]] for what the two fixes taught.

**bd work state is LOCAL-ONLY on this machine** (verified 2026-09-07): `git ls-remote origin
'refs/dolt/*'` returns nothing, so the documented Dolt sync has never run, and `bd sync` is not a
command in this version. The only cross-machine record is the git-tracked passive export, which had
gone **a week stale** (45 lines vs 93 real issues) until `bd export -o .beads/issues.jsonl`
regenerated it. **Re-export before any session where the work state needs to travel** — a cloud
session reads the JSONL, never the Dolt DB.

**Operational gap:** `scripts/` is not in the Dockerfile COPY (consistent with the three older
scripts), so the gate cannot run inside the container despite measuring production data.

Method that produced this: [[mutation-diagnostic-demands-a-count]]. Schema context in
[[newsbrief-kb-schema-0006]]; capture upstream in [[newsbrief-capture-feature]].

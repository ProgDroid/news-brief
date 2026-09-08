---
name: newsbrief-comprehension-pipeline
description: "bqa.4b — comprehend.py and its pre-registered gate: LIVE in production since 2026-09-08 and filling the KB; the five defects that nearly shipped, the cold-start deadlock, and the ratchet that was retiring items on network faults"
metadata: 
  node_type: memory
  type: project
  originSessionId: 19b68250-ca61-4936-834b-18d6a595831c
  modified: 2026-09-08T06:18:07.509Z
---

**`bqa.4b` CLOSED 2026-09-05.** 26 commits (`25d911f..41d7747`), ruff clean.
**CORRECTED 2026-09-07: it IS pushed** — this file said "NOT PUSHED, NOT DEPLOYED" for two days
after it stopped being true, and that stale line sent me looking for unpushed commits at the top
of a session. `git rev-list --left-right --count origin/main...HEAD` after a `git fetch` is the
probe; an empty `origin/main..HEAD` against a stale remote-tracking ref proves nothing.
**SUPERSEDED 2026-09-08: it is now ENABLED, DEPLOYED and writing to the KB** — this line said
"DISABLED and NOT DEPLOYED" and would have been read as current by anyone stopping at the top of
the file. Read the 2026-09-08 section at the foot for live state. (Leaving a fixed fact standing
uncorrected in the same document is `the-correction-didn't-propagate`; on any factual correction,
grep EVERY artifact including the one you just appended to.)

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

**`news-brief-bqa.11` IS IN PROGRESS as of 2026-09-07 — read the section at the foot of this file
for what the first live run actually did.** Original framing: enable `COMPREHEND_ENABLED` on the
host, choose an accumulation window, decide whether triage runs on a cheaper model, then run the
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

**CORRECTED 2026-09-07 — this previously said `scripts/` is NOT in the Dockerfile COPY. Wrong**,
and it was `metadata-is-not-state`: it read `news-brief-115` (which exists to REMOVE the line) as
evidence the line was absent. `scripts/` IS in the allowlist at `Dockerfile:55`, added 2026-09-05
so the gate can run in-container:

```
docker compose run --rm --entrypoint python newsbrief scripts/score_comprehension.py
```

`--entrypoint python` is required (ENTRYPOINT is `["python", "brief.py"]`, which takes a MODE).
Exit codes: 0 PASS, 1 FAIL, **2 = no database**, which is not a FAIL. Redirect to a file; a pipe
to `tee` returns tee's status. The checkout route does not merely waste effort, it cannot work:
`docker-compose.yml` has no `ports:` key anywhere, so Postgres is reachable only on the compose
network, and **there is no git checkout on the deploy host at all** (confirmed by him 2026-09-07).

## 2026-09-07 — the first live run, and the cold-start deadlock it exposed

**Rehearse a one-shot gate against an EMPTY dataset before the measurement window.** Running
`score_comprehension.py` before the flag flip returned clean negatives on every check (`no rows`,
`no events`, `not yet measurable`) and cost five seconds. SQL name resolution happens at plan
time, so an empty KB still proves every query executes — which is exactly what an F6-style
`UndefinedColumn` would have burned a real window to discover. Looking early cannot corrupt a
pre-registration: the hazard is *moving a threshold*, and `n=0` everywhere tempts nobody.

**Measured volume, off the host (only he can supply this):** steady state is **700–900 new items
per day across 24 outlets**; `2026-09-04`'s 1,926 is a first-fill artifact, never use it in a
rate. Comprehend runs hourly, so ~30 items/pass and `COMPREHEND_MAX_ITEMS=300` never binds once
the ~4,100-item backlog drains. **Only ~25% of items carry 150+ chars of body**, which is what
makes the spec-12.3 depth-tier split load-bearing rather than decorative.

**MODEL DECISION: `TRIAGE_MODEL` and `INTEGRATE_MODEL` stay EMPTY** (both follow
`MODEL=claude-sonnet-5`) for the whole measurement window. Triage costs ~$0.20/day and Haiku 4.5
would save about half — the Haiku/Sonnet gap is 2x now, not 10x. The real argument is not cost:
`item_triage.triage_model` is stored **per row**, so a mid-window swap blends two populations in a
gate that has no arm to separate them. Revisit as a pure cost question AFTER the gate.

**THE FIRST LIVE PASS WROTE NOTHING: 179 of 179 eligible items failed.**
`Tally(items_seen=300, material=159, sampled=20, failed_integration=172, empty_extraction=7,
assertions_written=0)` — and 172+7 = 179 = 159+20, i.e. everything. Every call returned
`stop_reason=tool_use` with healthy output tokens, so it was neither truncation nor timeout.

Root cause: the model emitted `candidate_id` on **every** entity and event while the offered
candidate lists were empty, using it as its own local sequence number — it even reused id 4 for
the same actor across two items, reinventing coreference that `_resolve_entity` already provides
via `ON CONFLICT (lower(name), type)`. `_validate_item` rejected each item on `cid not in
entity_ids` against an empty set. **The prompt said "otherwise propose a new entity" but never
"omit the id", and the schema field had no `description`.**

That is a **cold-start deadlock**, not just a bug: empty KB → no candidate matches → no entity is
created → there are never any candidates. An empty KB is the state at the start of every
measurement window, and no test caught it because every fixture seeded candidates first.

FIXED in `338d2bb` with opaque labels — see [[never-give-a-model-raw-database-ids]] for the
design rule, which generalises well beyond this project. `INTEGRATE_PROMPT_VERSION` 1 → 2.

**Two durable facts about this code that cost real time to establish:**

- **`integrate_attempts >= 3` is a ONE-WAY DOOR and is NOT scoped to the prompt version.** Once an
  item hits 3, no `INTEGRATE_PROMPT_VERSION` bump brings it back. Both failure paths increment it,
  and because the integration SELECT is `ORDER BY i.id LIMIT 300` the SAME low-id items sit at the
  front of every pass — so a systematic failure retires the front of the queue at one attempt per
  hour. Recoverable only by hand, only if noticed: `UPDATE item_triage SET integrate_attempts = 0`.
  There is **no alerting path**. Pre-register a stop-loss before flipping any flag that feeds it.
- **`Tally.failures` is declared and written by NOTHING** (grep `failures[` and `.failures`: zero
  hits). It always prints `{}`, which reads like "no failure details" rather than "field nobody
  populates", and `failed_integration` is a bare count. That is why the 172 was unattributable and
  why `scripts/inspect_integration.py` (`news-brief-bqa.12`, shipped `d48b036`) had to exist:
  it rebuilds one integration call exactly as `run()` does, dumps the raw `emit_extraction`
  payload, and prints `_validate_item`'s verdict per row, read-only.

**An entity COUNT cannot detect a failed candidate resolution.** `_resolve_entity` upserts
`ON CONFLICT (lower(name), type)` and falls back to a SELECT, so a fallback that mints a "new"
entity from a name that already exists lands back on the same row. Only the `event_entities`
linkage discriminates. I wrote a test asserting the count, mutation-checked it, and it passed
under the mutation — the assertion was removed.

That rollout is DONE — the next section is what the fixed pipeline actually did.

## 2026-09-08 — the fix worked, and the failure it exposed underneath

**`338d2bb` deployed, attempts reset, flag on. The cold start is broken open.** The 06:00 pass:
`items_seen=300, material=259, assertions_written=216, entities_created=88, entities_resolved=390,
events_created=160, events_matched=56, unmapped_candidate=0`. Cumulative KB: **1768 assertions,
1089 entities, 1502 events, 3832 event_entities links.**

- **`unmapped_candidate=0` is the compliance meter for the opaque-label fix, and it is a perfect
  score.** No prompt tweak is needed; do not re-open that question.
- `entities_resolved` 390 vs `entities_created` 88 — 82% of references land on existing rows, so
  the KB accumulates rather than duplicates.
- Corroboration is 56/(56+160) = **26%, inside the gate's 10–60% band** rather than at a boundary.
- **The sampled control arm is healthy: exactly 20/day on 09-07 and 09-08**, matching
  `COMPREHEND_SAMPLE_PER_DAY`. A `sampled=0` in a single pass after ~05:00 UTC is the daily cap
  already spent, not a fault — `select_sampled` counts promotions since `date_trunc('day', now())`.

**The real finding: 72 of 259 material items failed integration — 28% — and `failures={}` meant
not one was attributable.** Only ~21 could be recovered by hand from stack traces (15 to a DNS
fault, 6 to `no entity survived resolution`), leaving ~51 unexplained. Fixed in `a452bd3`
(`bqa.13` + `bqa.14`): transport failures no longer charge the ratchet and count in
`deferred_transport`; `failures` is populated by `_validate_item` (its six branches),
`write_extraction` (savepoint cause, `NoEntitySurvived` now a named `ValueError` subclass) and the
batch path. A `dropped` key counts every absent item, so **`dropped` minus the `validate:*` keys
is "the model never returned this item at all"** — a defect no single counter could name. Design
rule generalised in [[retry-budget-needs-a-verdict]].

**A slow gate-validity hazard worth watching: `candidate_cap_hit=59`**, i.e. 20% of items already
saturate `CANDIDATE_ENTITY_CAP` at 1,089 entities. The cap truncates the candidate list that
feeds `events_matched`, so **the corroboration rate the gate reads may drift downward purely as a
function of KB size, independent of model quality.** Accumulating longer is therefore NOT a free
way to strengthen the measurement — it trades volume against a drifting denominator.

**Durable contract fact, cost a red test to learn: assertions are written PER EVENT.** An
extraction carrying entities but no events is a legitimate `assertions_written=0`, not a failure,
so any fixture meant to prove a successful write must carry an event
([[tdd-plan-fixtures-drift-from-contracts]]).

**NEXT:** he deploys `a452bd3` himself — he handles the server, I handle the repo. Read the first
tally after it: `failed_integration` should drop by roughly the transport share, and `failures`
should finally name the ~51. **Let that dict decide whether the accumulation window starts or
whether one more defect needs clearing** — picking a window before reading it is choosing blind.
`news-brief-bqa.15` tracks the missing stop-loss; `115` and `ya4` stay blocked on the gate run.

Method that produced this: [[mutation-diagnostic-demands-a-count]]. Schema context in
[[newsbrief-kb-schema-0006]]; capture upstream in [[newsbrief-capture-feature]].

---
name: newsbrief-kb-architecture-2026-08-29
description: 2026-08-29 project redirection — brief becomes a render over an accumulated knowledge base. Spec + replay measured; Epic 1 CLOSED 15/15 and PUSHED (corrected 2026-09-01), admission guard built and measured. Read before touching the ledger.
metadata: 
  node_type: memory
  type: project
  originSessionId: 1cb4d5f7-fc65-441c-9cbd-14128d45122f
  modified: 2026-09-01T10:53:31.548Z
---

**2026-08-29. Design + measurement (session 1), then Epic 1 IMPLEMENTED (sessions 2-3): **12 of 15** issues closed across **9 commits** on `main` — PUSHED as of 2026-09-01; the UNPUSHED notes below are historical. Test suite 751 -> **884**.**

Spec: `docs/superpowers/specs/2026-08-29-knowledge-base-architecture-design.md` (755 lines,
tracked in git, and amended 2026-08-29 where the first gold-set run superseded it).
Read it before touching any of this. Work state is in **bd** — `bd ready`.

## The redirection

The daily brief stops being a search-and-print job and becomes a **render over accumulated
knowledge**. User's own framing: "build the brief from the accumulated knowledge rather than go
look it up each day from scratch." The brief stays; it becomes a consumer, not the pipeline.

Root cause of every complaint: **the system is engineered for novelty and structurally hostile to
continuity.** All three memory channels are suppressive — `yesterday_brief` ("never re-explain"),
`weekly_summary` ("do not repeat it"), the claim ledger ("do NOT re-explain or restate as news").
Meanwhile MARKET PULSE asks the model to explain moves and flag unexplained ones. Yesterday's
driver is by construction not today's news.

## Measured findings (not inferred — these were run)

- **Citation-newline corruption:** `brief.py:2948` joins `web_search` content blocks with `"\n"`.
  The API splits prose at citation boundaries, so every citation inserts a mid-sentence newline,
  often right before a comma. **All 90 archived briefs affected.** One-character fix.
- **Capture blindness:** `fetch_rss(max_items=5)`, one call site, one poll/day = 130 headlines.
  Items that appear and roll off are never seen.
- **Crowding-out, confirmed:** chip/semiconductor names appear in **58 of 90 briefs (64%)**; the
  live ledger holds **zero** chip claims (25 claims: Ukraine ×11, Iran ×8).
- **Severity classifier is degenerate:** `high` on **25/25** live claims. Populated, passes every
  completeness check, carries no information.
- **Replay experiment (the gate) — RAN, 90 briefs, Haiku 4.5.** 70 resolutions (24 `broken`,
  46 `challenged`), ~0.27 breaks/brief. (Its "~61% precision" is SUPERSEDED — see jx9.7 below: 33.3%.) Positive control passed
  (Patriot claim broken by id, 2 days). **Verdict: build propagation rule 1.**
- **The mechanism is the CAP, not the TTL** — this reversed both the spec's and the red team's
  assumption. 56/70 resolutions arrive *inside* the claim's TTL window; 54 of 68 were lost
  anyway. Claims are crowded out, not aged out. Duplication feeds it: **816 new rows in 90 days**.
- **False positives have one signature:** restatement or confirmation misread as contradiction.

## Decisions made

- **gbrain REJECTED as substrate** — no numeric series table anywhere in ~66 tables; `raw_data`
  is `UNIQUE(page_id, source)`, a snapshot not a log; schema packs emit zero DDL so the gap can't
  be filled; invalidation is inert (cascade fires only on hard delete, which resolve/supersede
  never do). **Steal its `takes` resolution vocabulary** (`correct/incorrect/partial/unresolvable`
  + `superseded_by`). Note: the CLAUDE.md gbrain block is about gbrain as *Claude's* memory —
  a different question, deliberately not edited.
- **Hermes NOT adopted.** `NousResearch/hermes-agent` is an agent runtime, not a KB. Cron
  adoption buys retries/incidents for the price of s6-overlay and 135 env vars; gateway adoption
  is invasive (sole bot-token consumer → the existing command surface must migrate). Independent
  of gbrain — "gbrain" appears 0 times in the Hermes tree.
- **Chroma STAYS.** It's a separately deployed Modal app (`CHROMA_MCP_URL`, `brief.py:122`), not a
  stack service. Augmentation, not substrate. An earlier draft wrongly retired it.
- **Storage DEFERRED** — ~~and it is downstream of an undecided process-architecture question:
  several containers → Postgres; one consolidated service → SQLite suffices~~. **RESOLVED
  2026-08-31: both questions decided together — supervisor + Postgres, two containers.** The
  framing above was right that storage was downstream of process architecture; it was wrong that
  consolidating implied SQLite. See [[newsbrief-runtime-foundation-phase-1]]. **The KB spec's
  coupling to a container-per-service shape is VOID** — do not build Epic 3 against it.
- **Renderer is NOT read-only** — that rule was withdrawn. 29.5% of brief statements (5.69/brief)
  are unsourced analytical framing, and that *is* the product. Replaced by
  `origin: extracted | authored`: authored interpretation is persisted and scorable but can never
  support a claim or be rendered back as established fact.
- **Equity paper book kept, re-founded on theses** (opens on conviction, holds to horizon,
  resolves with the thesis; no daily reversal, no DCA) — but **deferred**, since it can't grade a
  thesis engine that doesn't exist yet.
- **No numeric confidence scoring. Stories use tags, not trees.**


## Epic 1 IMPLEMENTED — 2026-08-29 sessions 2-3 (12/15, 9 commits on main; UNPUSHED then, PUSHED since — see DEPLOY STATE at the end)

Closed: `j07` `pon` `jx9.8` `jx9.5` `ba9` `jx9.1` `jx9.4` `jx9.3` `jx9.2` `47q` `bsp`.
Suite 751 -> 792, ruff clean throughout. Commits `22a9776`..`4579910`.

**`jx9.8` did not exist and had to be filed.** Spec 12.3 lists `status`/`broke_on`/`broken_by`
as fix #3, one of the three the replay showed load-bearing, and both `jx9.5` and `jx9.6`
presupposed a field nothing had built. If you map a spec table onto bd, check for gaps.

### The load-bearing decision: WRITE-THEN-QUARANTINE

**`status` and `origin` are written on every row and read by NOTHING.** Rendering, TTL
retirement and working-set ordering all ignore them, deliberately, pinned by tests
(`test_render_output_is_unaffected_by_status`, `test_broken_claim_still_retires_on_ttl`,
`test_render_output_is_unaffected_by_origin`). Brief output is byte-identical to before.

**Why:** the replay measured contradiction detection at low precision (**33.3%** once relabelled
— see `jx9.7`), and its fix (`93u`) was gated on the gold set. Spec 6.1 — synthesis errors
last one day, integration errors are permanent — so a wrong verdict written to the ledger is
read back as established fact every subsequent morning. `47q` proves this exact failure mode
already happened in this exact prompt (severity came back `high` 25/25). Detections accumulate
as evidence meanwhile, which also makes `jx9.7` cheaper: live labelled samples instead of only
the 24 historical ones.

**How to apply:** the quarantine lifts only when the gold set shows the field has real
VARIANCE, not just presence. When you lift it, invert those tests consciously — do not delete
them. **The first gold-set run does NOT license lifting it:** `status` varies (3 values) but
its recall collapsed (`jx9.9`), and severity's apparent variance is echo, not judgment.

### Two issues that show as `bd ready` but are NOT

- **`jx9.6`** (TTL/cap exemption for non-standing) IS the quarantine — it makes `status` act on
  retention. Blocked by decision, not dependency. Reason recorded in its bd notes.
- **`93u`** depended on `jx9.7`, which is now CLOSED — so 93u is genuinely ready, with its scope narrowed to the admission guard (see below).

### `jx9.7` — DONE and RUN 2026-08-29 (commit `74c8f24`, UNPUSHED then, PUSHED since). Epic 1 now 12/15.

Built: `tests/fixtures/gold_set_breaks.json` (23 labelled items, **committed**),
`scripts/score_gold_set.py` (manual scorer, live Haiku, one call/item),
`tests/test_gold_set.py` (26 offline tests). Suite 792 → **884**. Seed stays at
`from-server/replay-2026-08-29/`. Write-up: `docs/2026-08-29-gold-set-first-run.md`.

**The open fixture question was decided: COMMIT the derived pairs**, minus one row
(`r-0808`) carrying Bigdata.com-derived numeric values, since the repo is public and the
enrichment integration is scoped derived-only. The `from-server/` policy targets operational
state; a hand-reviewed label file of news assertions is not that. **The gate is the
line-by-line review, not the file's location — nothing else inherits the carve-out.**
The "CI needs committable fixtures" tension was half false: CI has no `ANTHROPIC_API_KEY`
(`docker-publish.yml:56` is bare `pytest -q`), so the scoring run is manual regardless.
Committing buys durability and a fresh clone, not CI. See [[live-state-on-deploy-host]].

**Two findings that arrived before the first API call:**

- **The restatement guard was ALREADY SHIPPED** inside `jx9.8` (`22a9776`) — the live
  `_RECONCILE_TEMPLATE` carries it with the BOJ 1.0% worked example. `93u` therefore
  narrows to the **claim-admission** half. Its bd notes now say so.
- **The ~61% baseline is NOT reproducible.** It was eyeballed off `audit.py`, which prints
  `claim[:105]` of a field `replay.py:277,284` had already cut to 150 chars. Labelling the
  untruncated checkpoint gives 7 true / 14 false / 2 unclear = **33.3%**. The fixture is
  the baseline of record. (Classic [[analysis-stats-traps]] shape: the probe was reading
  truncated inputs and nobody checked.)

**First run: precision 75.0% vs 33.3%, recall 42.9% vs 100%, 4 of 7 true breaks LOST**
(`jx9.9`). Spec 12.3 pre-registered the opposite direction; this one is worse, because a
lost break leaves a false claim standing permanently (spec 6.1) while a false break lasts a
day. **Admissible rows only: precision 100%, recall 60%, zero false positives** — so the
admission guard does most of the remaining work and must land BEFORE the break wording is
touched again.

**Two harness caveats, both measured not assumed.** Severity variance is **echo**: the probe
hands in a ledger row already carrying the seed severity and it came back unchanged 21/23,
so only `status` is judged from scratch — live rows remain the only read on severity
calibration (still `high` 25/25). And each probe is an **isolated pair**, not end-to-end: a
better judgment is still invisible in production if the claim was evicted first.

### Conventions established this session

- **`PROMPT_VERSION`** in `brief_memory.py` — bump on ANY material `_RECONCILE_TEMPLATE` change,
  or a prompt change becomes indistinguishable from a change in the world. Already at v3.
  `extractor_model` + `prompt_version` are stamped on every row `merge_ledger` writes.
- **Re-measure `RECONCILE_MAX_TOKENS` whenever the reply schema grows.** The four new fields took
  worst-case output from ~2970 to ~3708 tokens against a 4096 budget; raised to 8192 with a
  regression test that trips on the next field. Truncation fails safe but silently loses a day of
  memory. See [[signals-parse-error-is-truncation]].
- **Epic 1's real theme:** `merge_ledger` treated the model's reply as authoritative over stored
  state. `j07` stopped the cap deleting rows, `pon` stopped duplicate rows, `jx9.5` stopped
  rewriting frozen rows, `jx9.4` records who wrote each. Most of Epic 1 is that walk-back.

**Why:** the spec's measured sections are settled; its deferred ones are genuinely open. Start
from `bd ready` AND this file, not a fresh redesign — two of the "ready" items are not ready.
See [[brief-claim-memory-build]] for the ledger's original design.

## `93u` CLOSED 2026-08-31 — admission guard built + measured. Epic 1 now 13/15.

`kind: claim|observation` on reconcile replies (rubric written to the `status` standard),
enforced as a hard drop in `merge_ledger` on **NEW rows only** — an echoed id and a dedup
match are reaffirmation, exempt, or the guard would fight `jx9.5`'s immutability rule.
**Fails OPEN** on a missing label (a model that stops emitting the field would otherwise
empty the ledger silently); absence is caught loudly instead as a scorer variance field.
`kind` is deliberately **NOT stored on the row** — everything surviving the guard is a
claim, so the column would be uniform on every read, which spec 12.2 rates worse than
missing. `PROMPT_VERSION` 3 → 4. Suite 884 → **920**. Write-up:
`docs/2026-08-31-admission-guard-run.md`. New scorer mode: `--mode admission`.

**The finding that matters: the model SPLITS rather than drops.** Given a price-anchored
claim it emits the level as an `observation` (dropped at merge) AND the durable half as a
`claim` (kept). So precision/recall on this fixture are a weak instrument — the labels
describe a **seeded row** the live prompt no longer emits. Result: **0 of 17 admissible
claims lost, 0 price prints entered the ledger across all 23 items.** On `gs-03` the
surviving half converges on almost exactly `gs-16`, the row independently hand-labelled
admissible — the rubric reached the annotator's boundary from the other side.

**Break re-scoring was run THREE times, not once** — the first v4 run moved the headline by
two items on a denominator of seven, and one run cannot separate a regression from sampling
noise. Precision is now **100% and stable** (v3: 75%; its one false positive `gs-21` is
rejected upstream now). Recall's *composition* changed rather than simply falling: `gs-16`
(the row `jx9.9` names as must-recover) went from LOST at v3 to caught 2/3 at v4, while
`gs-09`/`gs-10` went the other way. Per-row over 3 runs: `gs-04` 3/3, `gs-16` 2/3, and
`gs-08`/`gs-09`/`gs-10`/`gs-14`/`gs-15` all **0/3**.

**How to apply to `jx9.9`:** its denominator changed — `gs-14`/`gs-15` are inadmissible and
now disappear upstream, so they can no longer be scored as lost breaks. What remains is
`gs-08`/`gs-09`/`gs-10`, missed outright (not near the boundary), all sharing one shape:
**numeric and standings negations** ("tops group with 4 pts" vs "both tied on 4 pts"), which
the restatement guard never touched. That is a different fix from rewording that guard.

**Method note worth reusing:** the first admission run reported recall 0% and looked like a
dead guard. It was a lossy probe — it kept only `rows[0]` and asked "did anything enter the
ledger", which cannot see a split. The `kind` variance line ({claim:17, observation:6},
matching the 6 hand labels exactly) was what proved the judgment was working before the
aggregate did. Instrument the probe to distinguish outcomes BEFORE believing a zero.
See [[analysis-stats-traps]].

## `jx9.9` RE-SCOPED then CLOSED 2026-08-31 — Epic 1 now 14/15, only `jx9.6` left.

**The filed premise was WRONG and is superseded.** `jx9.9` said the restatement guard
over-corrected and suppressed breaks, so reword it. Three gold-set runs showed the
model is not failing to detect contradictions at all — it **absorbs them into the
claim text**, echoing the id with `status: standing`. Of true breaks scored standing:
**6 of 6 in run A, 3 of 3 in runs B and C had been rewritten** ("Colombia holds 6 pts"
came back as "both teams hold 4 pts", standing).

**This was a hole in `jx9.5`**, and it is the reusable lesson: **jx9.5 froze claim text
on `status != standing`, but that field is set by the MODEL.** Any guard conditioned on
a model-supplied field can be walked around by the model choosing the other value. When
enforcement matters, condition it on something the code can see for itself.

**Why it mattered is the inverse of what the ticket assumed.** A missed break does NOT
leave a false claim standing — the ledger self-corrects and the reader is never told
the false fact. What dies is the **accountability record**: no trace that the claim was
ever wrong, which is the one measurement Epic 1 exists to produce.

**Fix (in `_reaffirm`):** an echoed standing claim whose rewrite DROPS a number the
stored claim asserted keeps its original text, is forced to `challenged`, and records
the attempted rewrite as evidence. **Dropped, not changed** — adding a date is genuine
refinement. Reuses `_claim_fingerprint`. `challenged` not `broken` because a dropped
number can be innocent compression **and `challenged` is still read by nothing, so a
false fire costs zero TODAY** — that is what made this the cheap moment to enforce it,
and it stops being true the moment `jx9.6` lifts the quarantine.

Measured over 2 runs: fired 7/23 and 5/23; **on admissible rows it fired only on true
breaks, 0 false fires out of 17 in both runs.** Every false fire sat on an inadmissible
row that `93u` now rejects upstream. Suite 934 → **938**.

**Known gap, stated not solved:** a purely qualitative reversal that drops no number
("talks are advancing" → "talks have collapsed") passes untouched. Not measurable on
this fixture, where every persistent miss was numeric.

**Two method lessons worth keeping:**
- **The instrument was measuring the wrong layer.** The break probe read `status` off
  the model's reply and never called `merge_ledger`, so a guard living in the ledger
  was invisible to the harness meant to test it. Added `ledger_status`/`guard_fired`
  ALONGSIDE an unchanged `predicted`, so older runs stay comparable. Ask which layer a
  probe actually observes before trusting it to score a fix.
- **An acceptance criterion was unmeetable by the approved design** — it asked for
  gold-set break recall, but the chosen verdict is `challenged` while the fixture
  scores `broken`. Written before the enforcement decision, caught at measurement time.
  It was AMENDED in bd with the reason, not quietly reinterpreted. Re-read acceptance
  criteria against the design once the design is settled.

## EPIC 1 CLOSED 15/15 — 2026-08-31. `jx9.6` shipped and the quarantine LIFTED.

**The blocking gate was answered AGAINST ITS OWN WORDING, deliberately.** It said "lift only
after `jx9.9` restores recall AND a re-score holds precision". Recall was NOT restored (14–57%
over 8 runs, no trend). What justified lifting is a number the gate never anticipated: **on
ADMISSIBLE rows only — the population production can store now that `93u` exists — precision was
100% with ZERO false positives in all 8 runs.** Every false positive in the all-rows column sat
on an inadmissible row the admission guard rejects upstream; break mode seeds claims straight
into the probe ledger, bypassing admission, so that column scores a population production can no
longer produce. **Low recall makes the lift SAFER, not riskier** — fewer rows affected, and the
affected ones have never been wrong. Generalisable: when a pre-registered gate turns out to
measure the wrong quantity, say so and re-argue it in the open; do not quietly reinterpret it.

**What shipped:** non-standing claims exempt from the TTL; the working set SPLITS the two
statuses (deliberately against spec 12.3 fix #11's literal "exempt from TTL and cap") —
`broken` LEAVES the window (storage keeps it; rendering it as background states a fact the
ledger knows false), `challenged` RANKS FIRST so it is never crowded out, as **priority not
extra slots** because the cap is a prompt budget. Challenged renders `(in doubt)`.
`horizon_days`/`resolution_date`/`horizon_elapsed` added, quarantined. `PROMPT_VERSION` 4 → 5.
Suite **963**. **First change in the epic that alters brief output** — everything before was
byte-identical.

**The epic's governing rule, worth reusing:** *quarantine is the default for an UNMEASURED
field; measurement is what lifts it.* `status` earned its lift over 8 runs; `origin` and
`horizon_days` are still written and read by nothing. `horizon_days` survived first measurement
non-degenerate ({7:10, 30:7, 180:4, 60:2}, modal 7 NOT the stated default 30 — judgment, not
default-echo), the only new field in the epic to manage that.

**Epic 1's real theme across all 15:** `merge_ledger` treated the model's reply as authoritative
over stored state, and most of the fixes are that walk-back. The corollary bit twice: **a guard
conditioned on a model-supplied field can be walked around by the model choosing the other
value** (`jx9.5`'s freeze keyed on `status`). Condition enforcement on something the code sees
for itself.

**Open follow-up: `news-brief-6wc`** — storage is now unbounded in TIME for non-standing rows.
The fix is archiving, never eviction: deleting the accountability record defeats the point.

**DEPLOY STATE — CORRECTED 2026-09-01. These 24 commits are PUSHED.** The earlier
"24 commits UNPUSHED" line was true when written and is not any more; verified by
`git merge-base --is-ancestor 74c8f24 origin/main` (RC=0). Everything now unpushed on `main`
belongs to Epic 7, not to this epic — see [[newsbrief-runtime-foundation-phase-1]].

**Still outstanding on the HOST, and NOT settled by the push:** the Modal proxy-auth fix needs
**`MODAL_PROXY_KEY`/`MODAL_PROXY_SECRET` in the host `.env`** with containers **recreated, not
restarted** — the compose passthrough is committed, the values are not. Pushing built the image;
it did not set an env var. Treat the host side as UNVERIFIED from this repo (see
[[live-state-on-deploy-host]]) — ask, don't infer.

## Epic 3 has started — see [[newsbrief-kb-schema-0006]]

**2026-09-02: `bqa.3` CLOSED.** Migration `0006_knowledge_base` ships the KB's 16 tables, so the
schema this document's §3.1 describes now exists in Postgres. Read
[[newsbrief-kb-schema-0006]] before touching it — in particular, the suite carries a
**deliberate `strict=True` xfail** that must not be deleted to go green, and the six obligations
this migration places on `bqa.4` are collected in the new spec's §6 rather than scattered.

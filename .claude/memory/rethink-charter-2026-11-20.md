---
name: rethink-charter-2026-11-20
description: "The 2026-11-20 rethink: what news-brief is for, approach A, storylines replace comprehension, GBP 20 floor, census dropped; what NOT to re-propose"
metadata:
  node_type: memory
  type: project
  originSessionId: dd284abf-9374-4918-ac8e-e3648a2b3cca
  modified: 2026-11-20T15:28:00.883Z
---

Charter: `docs/superpowers/specs/2026-11-20-rethink-charter-design.md` (approved 2026-11-20, bead lnr closed).

- **Approach A:** news-brief senses and remembers (storylines, brief, alerts, read-only query
  interface); the private portfolio workbench reasons and decides. Data flows one way; the
  workbench may *query* on demand.
- **Per-item comprehension and the KB are superseded** (36 beads closed). Do not re-propose
  re-enabling comprehension or the KB render path. The **event census is dropped** (the operator
  stopped labelling about halfway: "very dull, a lot was nonsense"); the `census*` freeze is lifted
  and bead ag1 removes the code.
- **Floor GBP 20/month retail**; ceiling is the free API-credit allowance. Everything above the
  floor gets a setting (bead 3km).
- **Operator declined a trial phase** before storylines ("it'll be forgotten and lead to another
  rethink; we can rebuild as often as needed"). Build directly; checks happen offline within the
  build, not as weeks-long runs.
- Order: al7 storylines (first slice = dated memory, 7-day window, thesis evidence log), d7w
  paper-trade refresh (remove signals + paper sim if not useful), f71 Telegram /set and 3km
  alongside, then nyy.2 MCP and uk1 event skeletons.

**Why:** the operator's value is orientation plus thesis movement, and continuity was the main
letdown. The operator's own test found news had no predictive power, so the goal is framework
material, not forecasts.

**How to apply:** start storyline work from al7 and the charter's section 7/8. The attachment
step is unproven (trigram failed in the 2026-09-26 spike), so its spec must pick a method with an
offline check. Related: [[newsbrief-kb-architecture-2026-08-29]], [[event-census-sp0]],
[[comprehension-cost-redesign-phase-1]].

**Status 2026-11-22:** storylines **phase 1 (dated memory, `news-brief-al7.1`) BUILT and pushed** (`5b68ae4..177c0af`). Knob `BRIEF_DATED_MEMORY_ENABLED` is OFF until the plan's Rollout runs on the host (`docs/superpowers/plans/2026-11-22-storylines-phase-1.md`). Phase 2 owes, recorded on `al7`: §3.3 no double counting of evidence under the storylines layout, and migration **0019** (phase 1 took 0018). Operator decision that day: `resolution_date` stays QUARANTINED, so dated claims show `first_seen` only.

**Status 2026-11-23:** d7w ruled the signals NOT USEFUL → removal `news-brief-cg3` (see
[[trading-retrospective-2026-08-16]]). Storylines phase 2 is split into plans 2a/2b/2c (spec
revision 5, §18); **plan 2a (foundation: tables, `/storyline`, seeding, the `attach` job, the
offline check) BUILT and pushed 11-23** (`46484d5..51d7ab9`), every knob off. Next is its plan
Rollout on the host (cost script, seed, offline check, then `STORYLINES_ENABLED`), then plan 2b
(`al7.4`, written against the code as built; carries the final review's "unclaimed pool must
include failed/deferred/never-decided articles" note). Brief cost is ~$9/month, not near the
floor: see [[brief-cost-measurement]].

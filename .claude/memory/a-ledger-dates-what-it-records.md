---
name: a-ledger-dates-what-it-records
description: "A timestamp ledger dates its OWN events, never the thing you want dated — migration applied_at was used as a code-deploy cutover and predated the change by 3h27m, producing a confident wrong table"
metadata: 
  node_type: memory
  type: project
  originSessionId: 37530862-9716-4ba1-b54f-775b6f00a9a1
  modified: 2026-09-09T06:37:46.191Z
---

**A ledger dates the events IT records. Using it to date something else is only valid when
the two shipped together — and that is a condition to CHECK, not to assume.**

Measured 2026-09-09 (`news-brief-bqa.19`). To date a candidate-ranking deploy I read
`schema_migrations.applied_at` for the newest migration, reasoning that migrations run at
container boot so the ledger holds the moment that image began serving. Correct, and still
wrong: migration `0011` was added by `6baaf4c` (committed 10:58 UTC) and applied at 11:17
UTC; the ranking change `51c850c` was not committed until **14:45 UTC**. The anchor
predated the thing it claimed to date by 3h27m, and the whole cohort table was mislabelled.

**Why this is worse than an ordinary off-by-one:** the ranking change shipped with NO
migration, so the ledger could never have dated it *at any timestamp*. The instrument was
answering a question it cannot answer, and nothing in the output said so — it printed
`(migration 0011 applied_at)` beside the number, which reads as **provenance** when it is
an **assumption**. Same family as `metadata-is-not-state`: the source is real, the field is
real, the inference is the fiction.

**The precondition was already written down.** `deploy_anchor`'s own docstring said "VALID
ONLY IF that migration shipped in the same image as the change being measured" — and I
asserted it from his recollection instead of running `git log`, which was on this machine
the whole time. **A stated precondition is not a checked one**, and the doc that states it
is usually written by the same pass that skips checking it.

**How to apply:**
- Before dating X from a ledger, ask **what event that ledger actually records**, then
  prove X shipped with it. `git log --diff-filter=A -- <the artifact>` names the commit that
  introduced it; compare against the change you care about. Two minutes, and decisive.
- When an inference has a precondition, put the precondition **in the output next to the
  number**, phrased as the check the reader must perform: *"use this only if your change was
  committed before 11:17 UTC"*. That sentence would have caught this on sight. A docstring
  will not — nobody reads it at the moment of use.
- **When the instrument cannot answer in principle, make it refuse.** `--cutover` is now
  required; the ledger is a hint carrying its condition. Defaulting to a plausible wrong
  answer is worse than demanding an argument.
- `.State.StartedAt` has the same shape: it reports the **last** start, so it cannot rule
  out an intermediate one. It is a lower bound on "deployed by", never a cutover.

Sibling defect from the same session, worth its own habit: **a longer exposure horizon
produces a SHORTER window**, so sweeping a parameter can generate NESTED samples that look
like independent confirmation. Post span at horizon `h` is `[cutover, now − h)`. Two rows
agreeing was close to one observation. Say the containment out loud in the output. Related:
[[analysis-stats-traps]] (correlated observations), [[newsbrief-comprehension-pipeline]],
[[tests-asserting-less-than-their-name]].

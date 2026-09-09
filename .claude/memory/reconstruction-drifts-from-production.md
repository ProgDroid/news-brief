---
name: reconstruction-drifts-from-production
description: "A diagnostic that REIMPLEMENTS production's query stops mirroring it the moment production changes — and a control comparing two copies of the same logic cannot notice, because both sides move together"
metadata: 
  node_type: memory
  type: project
  originSessionId: 37530862-9716-4ba1-b54f-775b6f00a9a1
  modified: 2026-09-09T10:57:04.747Z
---

**A diagnostic that reimplements production's logic will silently stop mirroring it, and the
obvious control cannot catch that — because the control compares two things that move together.**

Measured 2026-09-09 (`news-brief-bqa.26`). `scripts/probe_corroboration.py:was_retrievable`
answered "could production have offered this candidate?" by reproducing `candidate_events`'
`ORDER BY occurred_at DESC`. Its own comment said the two must mirror each other, "because
ordering IS the mechanism under investigation". Production then changed ranking twice in two days
— to entity overlap, then to trigram similarity — and the reconstruction moved neither time. For
two days the probe's A/B and hub tables described a **retired** ranking while reading as current,
and its headline finding (78% of probable duplicates "never offered" at hub entities) was quoted
as live three times in one session before anyone checked.

**Why no control caught it, which is the part worth carrying.** The bake-off printed
`baseline control: recency arm agrees with was_retrievable on 800/800` — green, every run. Both
sides were recency. A control comparing a Python copy of an ordering against a SQL copy of the
same ordering verifies that `sorted()` agrees with `ORDER BY`; it cannot verify that either one
resembles production. **A symmetric control cannot see a term sitting on both sides of it**, and
a green control is the strongest possible disguise for this. Same shape as the global
`the-prediction-had-a-generator` rule, arriving from a different direction.

**How to apply:**
- **Call the production function; do not re-derive its logic.** The blocker is usually that
  production has no "as of" notion — it queries the present, the diagnostic queries a past
  moment. Add an `as_of` parameter defaulting to `now()` and let both callers share one query.
  In production the default is a no-op, so the risk is near zero and the drift becomes
  impossible rather than merely unlikely.
- **Point the control at PRODUCTION, not at a sibling copy.** After the fix, the control compares
  the arm standing in for production against the live query, so it breaks if either side moves
  alone. Before choosing a control, ask: *what change would make this fail?* If the honest answer
  is "both sides would change together", it is decoration.
- **A comment saying two things must stay in sync is a defect waiting to happen, not a
  safeguard.** It records intent, and intent is not enforcement — `metadata-is-not-state` again.
  If it matters, make it structural or make a test fail.
- **On any ranking, query or ordering change, grep for reconstructions of it.** The tell is a
  duplicated `ORDER BY`, a re-implemented scoring function, or a fixture that hardcodes an
  ordering the code computes.

Sibling defect from the same change: **a test can be satisfied by a different filter than the one
it names.** The window-anchor test asserted the empty case with a one-day-old event, which the
`created_at` filter excluded on its own — so the window anchor was never exercised, and a mutation
anchoring it to `now` changed nothing. Make each fixture straddle the boundary of the ONE
predicate it claims to test. See [[tests-asserting-less-than-their-name]],
[[mutation-diagnostic-demands-a-count]], [[a-ledger-dates-what-it-records]].

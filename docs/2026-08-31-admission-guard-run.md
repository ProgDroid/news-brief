# Claim-admission guard: build and measurement

**2026-08-31.** `news-brief-93u`. Built the claim-admission guard, measured it, and
re-scored break detection under the prompt it changed (`prompt_version` 3 → 4).

## What shipped

- **`kind: claim | observation`** on reconcile replies, with a rubric written to the
  `status` standard §12.2 identifies as the only one that produced a complete,
  non-uniform field: explicit per-value rules, a stated default (`claim`), and an
  explicit negative case with worked examples on both sides of the boundary.
- **Enforcement in `merge_ledger`**: a new row labelled `observation` is dropped and
  never gets an id. Judged on genuinely new rows only — an echoed id and a dedup
  match are both reaffirmation of a row already admitted, and `jx9.5` froze those.
- **Fails open.** A missing or unrecognised `kind` is admitted. Defaulting to
  rejection would let a model that quietly stops emitting the field empty the ledger
  with no error raised. Absence is made loud instead: `kind` is a variance field in
  the scorer, so it shows up in the report the run it happens.
- **`kind` is not stored on the row.** Everything surviving the guard is a claim by
  construction, so the column would be uniform on every read — which §12.2 rates
  worse than a missing one.
- **`--mode admission`** in `scripts/score_gold_set.py`, scoring the enforcement path
  (parse → `merge_ledger`) rather than the raw field, so the run fails if the wiring
  breaks and not only if the judgment does.

## The admission result

23 items, 6 hand-labelled `admissible: false`.

| | result |
|---|---|
| admissible claims lost | **0 of 17** |
| price prints that entered the ledger | **0 of 23** |
| `kind` variance | `claim: 18 / observation: 5` — populated, non-uniform |
| precision / recall as scored | 100% / 50% (n=2 scored positives, 4 splits excluded) |

**Read the mechanism, not the aggregate.** The model does not drop a price-anchored
claim; it *splits* it, emitting the level as an `observation` (dropped at merge) and
the durable half as a `claim` (kept). Four of the six inadmissible rows came back
this way, one was rejected outright, and one was never proposed with its numbers at
all. So precision/recall as defined are a weak instrument here: the fixture labels a
**seeded row**, and the live prompt no longer emits that row.

What actually survived on the splits:

| row | dropped as `observation` | kept as `claim` |
|---|---|---|
| `gs-03` | Brent near $90/bbl while equities hit records | *"Market is pricing a contained Middle East conflict, not a closed Strait of Hormuz; repricing trigger remains an Iranian tanker or asset hit."* |
| `gs-13` | Japan's 10-year yield around 2.88% | *"Higher oil prices are driving up Japanese government bond yields."* |
| `gs-14` | Brent surged 1.2% on Sunday | *"Markets had shown muted reaction to ceasefire breakdown until Sunday's oil move, suggesting prior pricing assumed containment."* |
| `gs-15` | SK Hynix rallied 13% overnight | *"Micron's underperformance relative to SK Hynix is a divergence within the memory-chip thesis rather than a refutation of it."* |

`gs-03` is the striking one: stripped of its price anchor, it converges on almost
exactly the text of `gs-16` — the row the annotator independently hand-labelled
**admissible**. The rubric reached the annotator's boundary from the other side.

`gs-12` is the row to argue with. It came back as a single de-numbered thesis
("chip sector positioning is momentum-driven rather than fundamentals-driven"),
scored as "junk let through" because nothing was dropped, though no price print
entered. Whether that is laundering or correct distillation is a label question,
not a code question.

## Break re-scoring under prompt v4

Adding a rubric changes the prompt the break guard runs under, so break mode was
re-scored — three times, because the first run moved the headline by two items on a
denominator of seven and one run cannot tell a regression from noise.

| run | tp | fp | precision | recall | caught |
|---|---|---|---|---|---|
| v3, first-run doc | 3 | 1 | 75.0% | 42.9% | `gs-04`, `gs-09`, `gs-10` |
| v4 A | 1 | 0 | 100% | 14.3% | `gs-04` |
| v4 B | 2 | 0 | 100% | 28.6% | `gs-04`, `gs-16` |
| v4 C | 2 | 0 | 100% | 28.6% | `gs-04`, `gs-16` |

Per-row across the three v4 runs: `gs-04` 3/3, `gs-16` 2/3, and `gs-08`, `gs-09`,
`gs-10`, `gs-14`, `gs-15` all **0/3**.

Two things follow, and they point opposite ways:

1. **Precision is now 100% and stable**, up from 75%. The one surviving false
   positive at v3 was `gs-21`, which the admission guard now rejects outright.
2. **Recall did not simply fall — its composition changed.** `gs-16`, which `jx9.9`
   names as a row that must be recovered, is now caught 2 of 3 runs after being lost
   at v3. `gs-09` and `gs-10` moved the other way. Net tp went 3 → 1/2/2.

**The right denominator has changed.** `gs-14` and `gs-15` are inadmissible and now
disappear upstream, exactly as `jx9.9` predicted, so they can no longer be scored as
lost breaks. Against the five *admissible* true breaks (`gs-04`, `gs-08`, `gs-09`,
`gs-10`, `gs-16`), v3 caught 3 and v4 catches ~2.3.

## Caveats, all of them

- **n = 7 true breaks, 5 of them admissible.** Every recall figure here moves ~14
  points per item. The v3 column is a single run with no variance estimate, so
  "3 → 2" is not a measured regression.
- **The v4 runs share a prompt, so run-to-run spread is model sampling only.** It is
  1 item wide on tp. That is enough to say precision is stably 100% and that
  `gs-08`/`gs-09`/`gs-10` are not near the decision boundary — they are missed
  outright.
- **The admission probe feeds the model the already-compressed claim text**, not the
  brief prose it was drawn from. It measures the labelling judgment, not extraction
  end to end.
- **Isolated pairs, no distractors, no retention.** As with the first run, a better
  judgment can still be invisible end-to-end if the claim was evicted before the
  contradiction arrived.
- **Single annotator, not independently reviewed**, and `gs-12`'s label is the one
  this run actively disputes.
- **`status` comes back degenerate in admission mode** (`standing` 23/23). That is
  expected — an empty ledger has nothing to contradict — and is not a finding.
  Severity, judged from scratch rather than echoed, came back `normal` 17 / `high` 4
  / `low` 2, which is the first non-degenerate severity reading this repo has taken.

## What this leaves for `jx9.9`

`jx9.9` asks for `gs-08` and `gs-16` recovered without precision below 75%. Half of
that arrived as a side effect: `gs-16` is caught 2/3 and precision is 100%. `gs-08`
(*"Portugal tops group with 4 pts"* negated by *"both tied on 4 pts, neither has
secured top"*) is still 0/3, and it sits with `gs-09` and `gs-10` in a class the
restatement guard never touches — numeric and standings negations, not restatements.
That is the shape of the remaining work, and it is a different fix from rewording the
restatement guard.

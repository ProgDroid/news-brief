# Candidate event ranking: relevance, not recency

**Parent spec:** `docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md` §6, §8.2
**Depends on:** `bqa.4b` (comprehension pipeline, closed), `bqa.18` (the diagnostic)
**Date:** 2026-09-08
**Bead:** `news-brief-bqa.18`

> **REVISED after red-team review.** The first version of this spec specified a lexical
> ranking computed in Python over a fetched pool. A hostile review
> (`docs/superpowers/reviews/2026-09-08-candidate-ranking-redteam.md`) established that its
> central comparison was **confounded**, and that the simpler design it rejected also avoids
> a failure mode it had reintroduced. Both objections were verified against the code and
> both were correct. §1.3 and §3 record what changed and why; the original reasoning is in
> git history rather than papered over here.

---

## 1. The problem, measured

`comprehend.candidate_events` offered the model up to `CANDIDATE_EVENT_CAP = 30` events
sharing **any single** entity with the batch, ordered by `occurred_at DESC` within a 14-day
window. Matching one of those is the only way corroboration is ever recorded.

Spec §8.2 makes cross-outlet corroboration the **existence test** for the event layer:

> Floor: ≥10% of events carry assertions from 2 or more distinct outlets. Below this the
> event layer bought essentially nothing over the claim ledger, which is the entire
> justification for §2.1.

### 1.1 Recall collapses on exactly the entities that matter

A/B split for probable missed duplicates, bucketed by how many events the busiest entity on
the later event carries in the window:

| entity events | pairs | A: offered, not matched | B: never offered |
|---:|---:|---:|---:|
| 0–10 | 4 | 100% | 0% |
| 10–30 | 10 | 100% | 0% |
| 30–100 | 11 | 91% | 9% |
| 100–500 | 42 | 33% | **67%** |

Monotonic, with the break at and just above the cap. Every `NOT OFFERED` sample carries
`hub=393` or `hub=491` (Iran, UN); the `OFFERED` ones are `hub=2`, `hub=6`, `hub=29`. Hub
entities are where cross-outlet overlap is most likely, so the failure is concentrated
exactly where §8.2 has to find its signal.

**This finding is not confounded.** It compares one ranking against itself across buckets.

### 1.2 Recency is the worst available ranking

`recall@30` over 800 known missed pairs. The recency arm reproduces `was_retrievable`'s SQL
verdict on **800/800**, so the deltas rest on the correct baseline.

| ranking | single-item | batched (production shape) |
|---|---:|---:|
| recency (today) | 36% | **15%** |
| **entity overlap** | **57%** | not measured |
| title similarity | 69% † | 58% † |
| hybrid 10/10/10 | 70% † | — |
| reserved per item | — | 54% † |

† **Confounded — see §1.3.**

**Production operates at 15% recall in its real batched shape.** Batching is what makes
recency so bad: five items' entity sets union into one pool, and 30 recency slots spread
across it catch almost nothing.

### 1.3 The lexical arms are confounded; entity overlap is not

`probe_corroboration.py:333` sorts candidate misses by title↔title similarity and `:768`
takes the top 800. `rank_title_similarity` at `:426` then scores candidates by
title↔**summary** similarity — and each event's summary was generated from the earlier
item's text. **The evaluation set is selected by the same signal the lexical strategies
use.**

The first version of this spec asserted that "one cut applies to all of them, so it cannot
favour a strategy". **That was wrong.** A shared cut is neutral only when it is independent
of every strategy. Every † number above is an **upper bound** for its strategy, not a floor.

Entity overlap ranks on graph structure, not text, so the selection rule does not flatter
it. Its 57% is the trustworthy figure in that table, and recency's 15% is trustworthy for
the same reason.

**A correction of record:** the first version quoted §8.2's floor (*"events carry assertions
from 2+ distinct outlets"*, which is `corroboration_by_outlet`) and then reported 17.1%,
which is `score_match_rate_corroboration` — a different quantity. Both are §8.2 directions
and both are gated at 10%, but **the quantity whose definition was quoted has never been
measured.** Reporting it is listed in §8.

### 1.4 Syndication is not the story

0% of pairs in every band except the top, and 3 of 64 merges. Corroboration here is genuine
cross-outlet coverage rather than the same wire copy in two feeds, so §8.2's premise stands.

---

## 2. The change

**Rank candidates by how many of the batch's entities the event carries, then by recency.**
Three lines of SQL in the existing query:

```sql
SELECT e.id, e.summary, count(DISTINCT ee.entity_id) AS shared
...
GROUP BY e.id, e.summary, e.occurred_at
ORDER BY shared DESC, e.occurred_at DESC, e.id DESC
LIMIT 31
```

`GROUP BY` also supplies the de-duplication `SELECT DISTINCT` used to provide. `count(*)`
would be equivalent — `event_entities` has `PRIMARY KEY (event_id, entity_id)`, so an event
cannot join one entity twice — and the `DISTINCT` is documentation of intent rather than
defence. No test can distinguish them, and none pretends to.

`e.id DESC` makes the order **total**: two events with equal overlap and identical
`occurred_at` would otherwise return in whatever order the plan produced, so the offered
list would not be reproducible for a fixed corpus. That defeats debugging and any later gold
set.

### 2.1 Ranking stays in SQL, and that is the point

The rejected lexical design fetched the whole pool and sorted it in Python, bounded by a new
`COMPREHEND_CANDIDATE_POOL_CAP`. **Whatever bounded that pool could only be truncated by
`occurred_at DESC` — reintroducing this exact burial at a larger n.** Postgres orders the
full set, so there is nothing to truncate and the failure mode cannot occur.

It also needed `similarity()` shared between production and a diagnostic script to keep the
measured number describing the shipped system. This design needs no such coupling.

### 2.2 No new knob

Nothing here is a guessed value. `CANDIDATE_EVENT_CAP` and `CANDIDATE_WINDOW_DAYS` are
unchanged, and the ranking is a measured decision rather than a guess. This repo's rule is
knobs for guesses, not for decisions.

---

## 3. Rejected, and on what evidence

| Option | Rejected because |
|---|---|
| **Title similarity in Python** | Its 58%/69% are upper bounds from a confounded evaluation (§1.3), it requires a pool whose bound can only truncate by recency (§2.1), and it couples production to a diagnostic script. Genuinely may still be better — §8. |
| **Per-item reserved slots** | 54% vs 58% — measurably *worse*, and both confounded. Six guaranteed slots per item spend 24 of 30 on items with no duplicate in the pool. |
| **Hybrid 10/10/10** | 70% vs 69% single-item: 5 pairs in 800, noise, for three arms and a tuning constant. |
| **`pg_trgm` ranking** | The bake-off measured token-set Jaccard, not trigram. Adopting it ships an unmeasured ranking. |
| **pgvector (`bqa.7`)** | Would fix ranking *and* give a detector paraphrase cannot defeat. Deferred: a three-line change captures most of the measured gap first. |
| **Smaller `COMPREHEND_INTEGRATE_BATCH`** | More calls per pass costs real money; ranking recovers dilution for free. |

---

## 4. Consequences for the pre-registered gate

**`INTEGRATE_PROMPT_VERSION` is NOT bumped.** Candidate selection changes the input data,
not the prompt template. Bumping re-queues every integrated item and wakes `news-brief-3wb`,
which mints a fresh `event_id` on re-extraction so `ON CONFLICT (item_id, event_id)` never
fires and each re-extracted item gains a **second** event and assertion (measured 1/1 before,
2/2 after). That would inflate the corroboration figure the gate reads — corrupting the
measurement in order to fix the matcher.

**The KB will therefore hold two populations**: events matched against recency-ranked
candidates, and events matched against overlap-ranked ones.
`scripts/score_comprehension.py` computes over the whole KB, so a run spanning the cutover
blends two systems. Requirements, not notes:

- The cutover timestamp is recorded on the bead when this deploys.
- **The gate needs a window argument it does not have.** Running it unscoped across the
  cutover produces a number attributable to nothing.

---

## 5. Cost

Unchanged model tokens (still `CANDIDATE_EVENT_CAP` candidates in the prompt), one extra
aggregate in a query already indexed on `event_entities (entity_id)`, no new services, no
migration, no Python-side ranking.

---

## 6. Testing

- A more-shared event outranks a more-recent one, with the two deliberately in conflict so
  the old ordering cannot pass.
- Presence sibling: equal overlap still returns newest-first, so the change cannot be an
  ORDER BY that simply dropped recency.
- At the cap: a two-entity event buried under `CANDIDATE_EVENT_CAP` newer single-entity
  events survives, and the returned list is still exactly the cap.
- De-duplication survived the move from `SELECT DISTINCT` to `GROUP BY`.
- Mutations, counts pre-registered before running: revert-to-recency → 2, drop the recency
  tie-break → 2, `count(*)` for `count(DISTINCT …)` → 0. All three matched.

---

## 7. Risks

- **Entity overlap's batched recall is unmeasured.** Recency lost 21 points to batching
  (36% → 15%); overlap may lose similarly. It is the one number this change rests on that
  the diagnostic never produced.
- **Every measurement was taken at low pool density.** `CANDIDATE_WINDOW_DAYS = 14` while the
  KB holds ~2–3 days of events, so pools will grow several-fold. More competitors at fixed
  cap means recall declines again; this buys headroom, not a permanent fix.
- **`candidate_cap_hit` conflates two caps** — entity and event — and fires on essentially
  every batch, so it cannot tell which is binding.
- **The A column is untouched.** A third of pairs are offered and still not matched: a model
  or prompt question with its own investigation.

---

## 8. Deferred, deliberately

The measurement round that would settle what was left open, all read-only:

1. **Eval-set sensitivity** — score every strategy against three independently-selected
   sets (title similarity, shared-entity count, time proximity alone). If the winner flips
   with the selection rule, the method cannot decide it and real ground truth is needed.
2. **Entity overlap in the batched table** — the missing number from §7.
3. **`corroboration_by_outlet`** — the §8.2 floor quantity quoted but never measured (§1.3).

Also out of scope: `bqa.15` (no stop-loss on `gave_up_integration`), `bqa.16` (the tool
schema requiring only `standing`), `bqa.7` (pgvector), and any change to `_INTEGRATE_SYSTEM`
or the tool schema.

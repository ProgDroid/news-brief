# Candidate event ranking: relevance, not recency

**Parent spec:** `docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md` §6, §8.2
**Depends on:** `bqa.4b` (comprehension pipeline, closed), `bqa.18` (the diagnostic, this spec's evidence)
**Date:** 2026-09-08
**Bead:** `news-brief-bqa.18`

---

## 1. The problem, measured

`comprehend.candidate_events` offers the model up to `CANDIDATE_EVENT_CAP = 30` events
that share **any single** entity with the batch, ordered by `occurred_at DESC` within a
14-day window. Matching one of those is the only way corroboration is ever recorded.

Spec §8.2 makes cross-outlet corroboration the **existence test** for the event layer:

> Floor: ≥10% of events carry assertions from 2 or more distinct outlets. Below this the
> event layer bought essentially nothing over the claim ledger, which is the entire
> justification for §2.1.

The cumulative match rate is **17.1%** (events=2999, assertions=3617) and was ~26% early
in the corpus's life. It declines as the KB grows, and `scripts/probe_corroboration.py`
established why.

### 1.1 Recall collapses on exactly the entities that matter

A/B split for probable missed duplicates, bucketed by how many events the busiest entity
on the later event carries in the window:

| entity events | pairs | A: offered, not matched | B: never offered |
|---:|---:|---:|---:|
| 0–10 | 4 | 100% | 0% |
| 10–30 | 10 | 100% | 0% |
| 30–100 | 11 | 91% | 9% |
| 100–500 | 42 | 33% | **67%** |

Monotonic, with the break at and just above the cap. Below 30 events per entity the
matcher is always shown the duplicate; above ~100 it sees it a third of the time. Every
`NOT OFFERED` sample carries `hub=393` or `hub=491` (Iran, UN); the `OFFERED` ones are
`hub=2`, `hub=6`, `hub=29`.

Hub entities are where cross-outlet overlap is most likely and most valuable, so the
failure is concentrated exactly where the metric is supposed to find signal.

### 1.2 A ranking bake-off says recency is the worst available choice

`recall@30` over 800 known missed pairs. The recency arm reproduces `was_retrievable`'s
SQL verdict on **800/800**, so the deltas rest on the correct baseline.

| ranking | single-item | **batched (production shape)** |
|---|---:|---:|
| recency (today) | 36% | **15%** |
| entity overlap | 57% | — |
| title similarity | 69% | **58%** |
| hybrid 10/10/10 | 70% | — |
| reserved per item | — | 54% |

**Production today operates at 15% recall.** Batching is what makes recency so bad: five
items' entity sets union into one pool, and 30 recency slots spread across it catch almost
nothing. Similarity ranking takes it to 58% — **a 3.9× improvement at zero token cost**,
because the model still receives exactly 30 candidates.

### 1.3 What the diagnostic could not measure, and why the numbers are floors

Lexical similarity cannot *detect* duplicates in this corpus: positives (pairs the model
itself merged across outlets) reach down to **p50 = 0.136**. Two outlets covering one event
genuinely share little vocabulary. The negative class is empty because it needs items 5+
days apart and the KB is younger, so the 0.35 separator is an **arbitrary cut, not a
measurement** — it keeps only 12% of known duplicates.

Consequences, stated so nobody reads past them:

- Every absolute count in the diagnostic is a **floor**. The reported ceiling (18.9%, or
  18.8% excluding syndication) understates the true headroom by an unknown factor.
- Comparisons **between** buckets and **between** rankings remain valid: one cut applies to
  all of them, so it cannot favour a strategy.
- Ranking is a strictly easier problem than detection. The true match only has to beat its
  neighbours, not clear an absolute line — which is why a signal useless as a detector wins
  as a ranker.

### 1.4 Syndication is not the story

0% of pairs in every band except the top, and 3 of 64 merges. Corroboration here is genuine
cross-outlet coverage rather than the same wire copy in two feeds, so §8.2's premise stands
and does not need re-specifying.

---

## 2. The change

**Rank candidate events by similarity to the batch's item titles instead of by recency.**

1. `candidate_events` fetches an **uncapped** pool for the batch's entity set, bounded by a
   new `COMPREHEND_CANDIDATE_POOL_CAP` and counted when the bound binds.
2. It ranks that pool by the **maximum** token-set similarity between any item title in the
   batch and the event's summary.
3. It returns the top `CANDIDATE_EVENT_CAP` as today — same shape, same count, same prompt.

### 2.1 The similarity function is shared, not reimplemented

`tokens()` and `similarity()` move into `comprehend.py`; `scripts/probe_corroboration.py`
imports them.

This is load-bearing. The 58% was measured with the probe's function. A production
reimplementation — even a "better" one — makes the measured number stop describing the
shipped system, and the two would drift silently. One definition, imported by both, for the
same reason `label_map` is shared between the renderer and the parser.

### 2.2 It ranks against TITLES, because that is what was measured

The bake-off scored similarity between the item **title** and the event summary. Including
body text would be a different ranking with no measurement behind it. If body text is worth
trying, it is a follow-up with its own bake-off run, not a free improvement.

### 2.3 Ties break deterministically

The probe's pool arrived in arbitrary database order and Python's stable sort preserved it.
Production breaks ties by `occurred_at DESC`, then `id DESC`. This is a deliberate
deviation from what was measured: it cannot meaningfully change recall (it only orders
events of equal similarity) and it makes the offered list reproducible for a given corpus
state, which matters for debugging and for any future gold set.

### 2.4 One new knob, and only one

`COMPREHEND_CANDIDATE_POOL_CAP` is a guessed value, so it is a settings row per this repo's
convention — a `KNOBS` entry, no module constant, and a `docker-compose.yml` anchor whose
name equals the key. When it binds, a tally counter records it, because a cap nobody can see
is indistinguishable from an absence of data.

The **ranking strategy is not a knob.** It is a measured decision, not a guess, and a knob
would create a second code path to maintain and test for the sake of reverting a change
that has evidence behind it.

---

## 3. What is rejected, and on what evidence

| Option | Rejected because |
|---|---|
| **Per-item reserved slots** | 54% vs 58% — measurably *worse*. Six guaranteed slots per item spends 24 of 30 on items with no duplicate in the pool, while capping the one item that does have a match. |
| **Hybrid 10/10/10** | 70% vs 69% single-item: 5 pairs in 800, noise at this sample size, in exchange for three arms and a split constant to tune. |
| **`pg_trgm` ranking in SQL** | The bake-off measured token-set Jaccard, not trigram similarity. Adopting it would ship a ranking whose recall is unknown — the exact failure this whole exercise existed to avoid. |
| **pgvector (`bqa.7`)** | Best ceiling and it would fix detection too, but a free change buys 3.9×. Spending embedding cost before harvesting that is paying to skip the cheap win. Revisit if recall plateaus below what §8.2 needs. |
| **Smaller `COMPREHEND_INTEGRATE_BATCH`** | Fewer items per call means more calls per pass, which costs real money. Similarity ranking recovers the dilution without spending anything. |

---

## 4. Consequences for the pre-registered gate

**`INTEGRATE_PROMPT_VERSION` is NOT bumped.** Candidate selection changes the input data,
not the prompt template, and bumping it would re-queue every integrated item — waking
`news-brief-3wb`, which mints a fresh `event_id` on re-extraction so `ON CONFLICT
(item_id, event_id)` never fires and each re-extracted item gains a **second** event and
assertion (measured: 1/1 before a bump, 2/2 after). That would inflate `events` and every
corroboration figure read off them, corrupting the measurement in order to fix the matcher.

**But the KB will contain two populations.** Events created before this change were matched
against recency-ranked candidates; events after, against similarity-ranked ones.
`scripts/score_comprehension.py` computes over the *whole* KB, so a run spanning the cutover
measures a blend of two systems.

Two things follow, and both are requirements rather than notes:

- The cutover timestamp is recorded on the bead when this deploys.
- The gate must be run over a window that starts after it, which the script cannot currently
  do. **A follow-up bead adds a window argument to the gate**; running it unscoped across the
  cutover would produce a number attributable to nothing.

---

## 5. Cost

- **Model tokens: unchanged.** The prompt still carries `CANDIDATE_EVENT_CAP` candidates.
- **Postgres: a few hundred extra rows per batch** instead of 31. At ~58 batches per pass
  and a bounded pool this is negligible, and the query is already indexed on
  `event_entities`.
- **CPU: one similarity computation per (pool event × batch item)**, pure Python set
  operations on short strings. Bounded by the pool cap.

No new services, no extension, no migration.

---

## 6. Testing

- **Contract test:** production's ranking must reproduce the probe's `batch_similarity`
  ordering on a shared fixture, so the shipped system and the measured system cannot drift.
- **Behavioural test:** a duplicate buried under `CANDIDATE_EVENT_CAP` newer events on a hub
  entity is *not* offered under recency and *is* offered under similarity — the mechanism
  from §1.1, asserted directly. Its presence sibling: an unburied duplicate is offered under
  both, so the first test cannot pass via a function that always returns True.
- **Pool-cap test:** when the bound binds, the counter moves and the returned list is still
  exactly `CANDIDATE_EVENT_CAP`.
- **Mutation pass** with counts pre-registered before running, per this repo's practice.

---

## 7. Risks

- **58% is a floor and 15% is a floor.** Both are measured through a detector that finds 12%
  of true duplicates. The *ratio* is the trustworthy part, not the absolute levels.
- **The remaining 42%.** Similarity ranking still misses two in five reachable duplicates.
  This spec does not claim to solve corroboration; it removes the largest measured cause.
- **The A column is untouched.** A third of pairs are offered and still not matched. That is
  a model or prompt question and needs its own investigation.
- **`CANDIDATE_ENTITY_CAP = 40` is unexamined.** `candidate_cap_hit` fires on essentially
  every batch, and this spec does not distinguish which cap is firing.

---

## 8. Out of scope

Everything in §7's risk list, plus: `bqa.15` (no stop-loss on `gave_up_integration`),
`bqa.16` (the tool schema requiring only `standing`), `bqa.7` (pgvector), and any change to
`_INTEGRATE_SYSTEM` or the tool schema.

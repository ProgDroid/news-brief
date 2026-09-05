# Comprehension Pipeline — Design

**Issue:** `news-brief-bqa.4` (bqa.4b: build the comprehension pipeline — triage + integration)
**Parent spec:** `docs/superpowers/specs/2026-08-29-knowledge-base-architecture-design.md` §2, §3, §6
**Depends on:** `bqa.3` (schema, closed), `b42.1` (capture, closed), `bqa.10` (ledger cutover, closed)
**Date:** 2026-09-04

---

## 1. What this builds, and what it deliberately does not

Capture writes `items`. The claim ledger writes `claims`. Between them sits a layer that does
not exist: **entities, events and assertions** — the record of *what happened* and *who said so*.

This pipeline builds that layer. It reads captured items, decides which are material, and
extracts entities, events and assertions from the material ones.

### 1.1 Explicitly out of scope

- **Claims.** `brief.py:3294-3302` already calls `claim_store.load_ledger` / `save_ledger`, so
  claims have been accumulating in the KB since the `bqa.10` cutover. Extracting claims from
  items as well would add a second writer to a populated table and buy nothing the ledger does
  not already do. Claims stay on the reconcile path until Epic 4 changes the render.
- **Links.** `links` requires `observations`, which are fetched market data on an unrelated
  path. Rule 2 ("an Observation with no explaining Link") is `bqa.5` work.
- **Alerting.** `capture` has no alerting path either (`news-brief-w3q`), and that bead exists
  precisely because a threshold invented before measurement is indistinguishable to the operator
  from a measured one. Adding one here would repeat the mistake it records.
- **Stories.** `story_members` is written by review actions, not by extraction. Out of scope.
  Note that nothing in production writes `stories` at all yet — see §5.1.1, which matters for
  triage rather than here.
- **`assertions.source_relationship`.** Nullable, and left empty. It has no rubric anywhere;
  extracting it would reproduce `severity`. Reasoning in §6.2, disposition belongs to `bqa.8`.

### 1.2 Nothing reads what this writes

`entities`, `events`, `event_entities` and `assertions` ship **quarantined**: written on every
material item, read by nothing. This is Epic 1's governing rule applied again — *quarantine is
the default for an unmeasured field; measurement is what lifts it.*

The consequence is that **the only thing that can say whether this worked is §8's
pre-registered measurement.** There is no user-visible improvement in this scope, by design, and
a build that "looks fine" tells us nothing.

---

## 2. Decisions

Four decisions were taken before design, each with the alternatives considered.

### 2.1 Depth: entities + events + assertions

**Rejected — full depth including claims.** Two claim writers to reconcile, a second extraction
contract, and all of `bqa.9` items 2–5 landing in one build.

**Rejected — claims only.** Skips the layer that makes corroboration possible. A claim ledger
structurally cannot record that two outlets asserted the same event; there is nowhere to put it.

**Rejected — triage only.** Would give real volume data cheaply, but leaves the KB with nothing
new in it and defers every hard question rather than answering one.

### 2.2 Materiality: tracked OR topical

An item is material if it touches something the KB already holds **or** fits the brief's domain.

The tracked half delivers the continuity the whole redirection exists for — the parent spec's
diagnosis is that the system is *"engineered for novelty and structurally hostile to
continuity"*, and a triage rule asking only "is this newsworthy?" reproduces that bias exactly.
The topical half exists because an empty KB tracks nothing and the tracked half alone cannot
bootstrap, nor ever learn about a subject it has not already met.

**Rejected — corroboration-gated** (integrate only where 2+ outlets carry a story). Structurally
blind to single-source exclusives, which in this domain are often the items that matter most.

### 2.3 Entity resolution: retrieve candidates, model picks

Extract surface forms, retrieve existing entities by lexical match, hand the model the candidates
and ask it to pick one or declare it new.

This is recognition rather than recall. The user's own standing rule: *"If a prompt has a blank
field where a proper noun goes, it is asking the wrong question."* An extractor emitting a
canonical entity name into a blank field is in exactly that position.

**Rejected — deterministic normalisation only.** Brittle exactly where this domain lives:
transliteration variants, institutions versus the countries that host them, non-Latin scripts.

**Rejected — extract freely and merge later.** The KB is knowingly wrong between passes, and
merging needs FK rewrites across `event_entities`, `entity_instruments`, `observations` and
`claims`.

**Why this is doubly load-bearing.** `entities` is `UNIQUE (lower(name), type)`, and the schema
comment states plainly that this *"does NOT enforce 'a company and its equity line are one
entity' — that is an extraction rule for bqa.4. No unique key can express it."* Because §2.2
makes triage read the tracked-entity set, a split entity fails **twice**: per-entity aggregates
split, *and* an item about "Apple Inc." stops matching tracked entity "Apple" and silently drops
out of materiality. A resolution bug becomes a coverage bug that presents as "the KB just did not
find that interesting".

### 2.4 Success gate: enum variance AND corroboration, both directions

Numbers in §8. Both halves are required: uniform enums mean a dead field (`severity` shipped at
`high` 25/25), and zero corroboration means the event layer bought nothing over the ledger.

---

## 3. Architecture

A new top-level module `comprehend.py` and a new `comprehend` entry in `scheduler.SCHEDULES`,
running as a supervisor job child.

```
items  (no triage verdict at the current triage_prompt_version)
  │
  ▼
TRIAGE
  ├─ rules half  : surface-form match vs entities, live claims, open stories
  │                                                    [SQL + in-memory, NO model]
  ├─ model half  : topical fit, micro-batched          [triage model, non-matches only]
  └─ sampled arm : N/day that BOTH halves rejected     [no model — the §5.3 control]
  │
  ▼
item_triage  (verdict, reason, triage_prompt_version, attempts,
               integrate_prompt_version, integrate_attempts)
  │  verdict = 'material'
  ▼
INTEGRATION
  ├─ candidate retrieval : entities by surface form,
  │                        events by shared entity + time window        [SQL]
  └─ one call per micro-batch                            [integration model]
  │
  ▼
entities · events · event_entities · assertions      ← read by NOTHING (§1.2)
```

**Reads:** `items`, `entities`, `claims`, `stories`, `item_triage`.
**Writes:** `item_triage`, `entities`, `events`, `event_entities`, `assertions`.

### 3.1 Why a separate job and not part of capture

Parent spec §2 states it as governing: *"Capture and comprehension are separate concerns.
Capture must be continuous and cheap because feeds are windows and a missed item is gone
forever. Comprehension can be tiered, lazy and batched because once captured, an item can be
reprocessed indefinitely."*

Concretely: `capture` bounds a pass at 600s against a 30-minute interval, and the `_due_jobs`
loop in `supervisor.py` (~929-946) alerts on any job still running at its next fire — 48 chances
a day. An unbounded model call inside that bound would convert a slow provider into a daily alert
storm.

### 3.2 Why the tracked half needs no model

"Does this touch a known entity, claim topic or story" is a database lookup plus in-memory string
matching. Only the topical half requires a model call, and only for items the lookup did not
already accept. Triage cost therefore scales with **unfamiliar** items rather than with all
items, which is what makes the union criterion of §2.2 affordable rather than the most expensive
of the options considered.

This is the parent spec's "rules first" (§6.1) made concrete.

### 3.3 Ordering: why one integration call, not two

Event candidates must be retrieved by shared entity, which appears to require entities to be
resolved first — a model call before the model call. It does not, because **the lexical matcher
already ran during triage**. Its output gives probable entities with no model involvement, so
entity candidates and event candidates can both be assembled before the integration call.

One model call per micro-batch, not two.

---

## 4. Migration 0009

```sql
CREATE TABLE item_triage (
    id             BIGSERIAL PRIMARY KEY,
    item_id        BIGINT  NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    verdict        TEXT    NOT NULL
                   CHECK (verdict IN ('material', 'immaterial', 'failed')),
    -- The union rule of 2.2 has two halves and they are different evidence.
    -- Collapsing them to one bit makes it impossible to ask later whether the
    -- tracked half is carrying the pipeline or the topical half is.
    reason         TEXT    NOT NULL
                   CHECK (reason IN ('tracked_entity', 'tracked_claim',
                                     'tracked_story', 'topical', 'sampled',
                                     'none', 'error')),
    -- Biconditional in both directions, in the style of observations' metric
    -- check: a material verdict must carry a real reason, and a non-material one
    -- must not. metric/return_window proved the shape has no three-valued hole.
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error'))),
    -- NULL means the rules half decided and NO model ran. Same reasoning as
    -- observations.provider being distinct from extractor_model: not every row
    -- is produced by a model, and a NOT NULL column here would force a lie.
    triage_model            TEXT    NULL,
    -- TWO versions, because there are two prompts on two models behind two
    -- knobs. One column served neither: bumping the integration prompt left
    -- integrated_at set so nothing could re-extract, and bumping the triage
    -- prompt re-paid triage cost on every item including those whose extraction
    -- nobody meant to change. The column exists to compare versions, and with
    -- one column neither prompt could be compared.
    triage_prompt_version   INTEGER NOT NULL,
    integrate_prompt_version INTEGER NULL,
    attempts                INTEGER NOT NULL DEFAULT 1,
    -- Integration gets its own ceiling for the same reason triage has one: an
    -- item whose extraction fails deterministically would otherwise re-pay the
    -- expensive tier every run, forever.
    integrate_attempts      INTEGER NOT NULL DEFAULT 0,
    integrated_at           TIMESTAMPTZ NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reprocessing after a triage-prompt bump is a NEW row, never an overwrite: the
-- point of stamping a version is to compare versions, which an upsert destroys.
CREATE UNIQUE INDEX item_triage_item_version
    ON item_triage (item_id, triage_prompt_version);

-- Material AND unintegrated. Predicated on the verdict as well as the timestamp
-- because immaterial rows never receive integrated_at and so would never leave
-- a partial index keyed on that alone -- roughly 90% of the index would be rows
-- the only query against it can never return.
CREATE INDEX item_triage_pending ON item_triage (item_id)
    WHERE verdict = 'material' AND integrated_at IS NULL;
```

**No GIN index on `entities.aliases`.** An earlier draft added one, justified as "candidate
retrieval searches name AND aliases; without this every mention is a sequential scan". That
contradicted §3.2 and §5.1, which build an in-memory surface-form index once per run over all
entities — which *is* a full scan, chosen deliberately because word-boundary and case rules
(§5.1) do not express well in SQL. One full scan per run is the design; an index with no reader
is dead weight. If the matcher ever moves into SQL, add the index in that change, not this one.

### 4.1 Why a table and not a column on `items`

There is currently no way to know which items have been processed. Deriving it from `assertions`
works for integrated items but re-triages every **rejected** item forever, and rejects are the
majority. The verdict row is also the measurement surface §8 requires.

### 4.2 Why the two attempt counters exist

With a unique key on `(item_id, triage_prompt_version)`, writing a `failed` row would make that
failure permanent until someone bumped the prompt version. The triage-pending query instead
selects items with no verdict **or** `verdict = 'failed' AND attempts < 3`, and a retry updates
the row in place.

`integrate_attempts` is the same guard on the expensive tier, and it was missing from an earlier
draft — triage had a ceiling and integration had none. An item whose extraction fails
deterministically (a body the model always truncates on, an enum it always gets wrong) would be
re-selected every run forever, paying the integration model each time, while `failed_integration`
counted upward in the tally with nothing acting on it. That is precisely the shape
`fail-closed-needs-status-not-count` warns about: a number reported but attributable to no gate
that stops anything.

The integration-pending predicate is therefore:

```sql
verdict = 'material'
  AND integrate_attempts < 3
  AND (integrated_at IS NULL OR integrate_prompt_version < :current_version)
```

which also makes an integration-prompt bump re-extract without touching triage.

### 4.3 Deliberate omissions

- **No `matched_entity_id`.** An item matches several entities, so a single column is lossy, and
  the matcher is cheap enough to re-run at integration time. `reason` records which *source*
  matched, which is what §8 needs.
- **No alerting columns.** See §1.1.

### 4.4 This migration exercises `news-brief-5db`

The rollback tests in `test_kb_schema.py` and `test_claim_store.py` now derive their step count
from `conftest.steps_back_through` (fixed 2026-09-04, `da4adfe`). Adding 0009 must therefore
require **no edit** to them. If it does, 5db's fix is incomplete and should be reopened rather
than patched around.

`tests/test_db.py`'s literal migration manifest **will** need its one-line edit. That is
intended: it is a deliberate inventory that fails loudly.

---

## 5. Triage

### 5.1 Rules half — no model call

One surface-form index is built per run from three sources, each stamping a different `reason`:

| Source | Forms | `reason` |
|---|---|---|
| `entities` | `name` and every element of `aliases` | `tracked_entity` |
| `claims` with `status IN ('standing','challenged')` | `topic` (nullable; skip NULLs) | `tracked_claim` |
| `stories` with `state <> 'closed'` | `name` | `tracked_story` |

`standing` and `challenged` are exactly the two statuses `select_working_set` renders as live or
in doubt. The four terminal statuses are excluded deliberately: a resolved claim's subject may
still be newsworthy, but tracking it here would make every settled question permanently material
and the tracked half would only ever grow.

#### 5.1.1 The tracked half is nearly empty on day one. Say so rather than discover it.

A red-team pass established this and it is easy to miss, because the table above reads as three
working sources:

- **`tracked_story` can never fire in v1.** `INSERT INTO stories` appears **only** in
  `tests/test_kb_schema.py`; nothing in production writes that table. The enum value is retained
  because it costs nothing and `bqa.6`'s review queue will write stories — but it must be
  expected at **zero**, not read as a signal.
- **`tracked_entity` starts empty**, because this pipeline is what fills `entities`. It grows
  from run one, but it contributes nothing on the first pass.
- **`tracked_claim` is the only source live at flag-flip**, and it is the weakest: `claims.topic`
  is free-text and nullable.

So on day one materiality is **effectively the topical half plus the sampled arm**, which is
exactly what the topical half was included for (§2.2). The failure this heads off is reading
§8's `reason` distribution from the first days and concluding the union rule does not work, when
what was measured is a cold start. **`reason` distribution is only interpretable once
`entities` has accumulated**; report it as a series over time, never as a single figure.

Matching rules, each of which exists because of a recorded failure:

1. **Word-boundary matching only, never substring.** PolyGram's substring search made `MU` match
   "Musk" (`polygram-candidate-search-fix`); the fix was entity-token search with a length floor.
2. **Forms of 4+ characters match case-insensitively. Shorter forms match case-sensitively as
   whole tokens**, so `US` matches `US` but not `us`. Acronyms are real entities; casefolding
   them is what makes them toxic.
3. **A stop-list** for surface forms that are also ordinary English words.
4. **Match against title plus the whole body.**

Rule 4 was originally "title plus the first 500 characters, because news copy is an inverted
pyramid". That reasoning applied to article text the KB does not hold. `capture.py` inserts
`entry.get("summary") or None` into `items.body` — the **RSS blurb**, typically a sentence or
two — and nothing in capture fetches article pages. A 500-character window over a 300-character
field is a no-op dressed as a decision, so the rule is simply "title plus body" and the
assumption disappears. See §10 item 3, which is now a probe rather than a guess.

### 5.2 Model half

Items the rules did not accept are micro-batched (default 25) as `{id, outlet, title, lead}` and
sent to the triage model with forced tool use returning `{id, material, reason}` per item.

**The topical standard is derived from the brief's existing system prompt, not written afresh.**
If triage and the brief hold different views of the domain, the KB accumulates material the brief
will never render and drops material it wants. `brief-py-sibling-prompt-strings` records that
there are three sibling prompt blocks in `brief.py`; the implementation must name the symbol
rather than describing "the prompt".

**`stop_reason` is checked before the parser on every call.**
`signals-parse-error-is-truncation` records four recurrences of a truncation being misdiagnosed
as a broken parser.

### 5.3 The `sampled` control arm

A fixed small number of items per day (default 20) that **both** halves rejected are marked
material with `reason = 'sampled'` and integrated anyway.

**Why, and it is not coverage.** A model-judged `topical` set is confounded with the triage
model's own view of the domain. Measure enum variance or corroboration over `topical` rows alone
and there is no way to separate "the extractor works" from "the triage model picked items its
sibling finds easy". The question §8 exists to answer — is the tracked half carrying this, or the
topical half? — is uninterpretable without a selection-independent baseline.

A recency sample is not confounded, so `sampled` rows are the control against which the other
arms are read.

**Why 20/day and not a larger fraction.** Removing the triage model entirely and integrating by
recency was considered and rejected on arithmetic: at ~1,200 items/day inflow it takes integration
from ~120/day to ~1,200/day — a 10x increase in the **expensive** tier, against a triage saving
the parent spec puts at ~$6/mo. Twenty a day buys the statistical control for ~17% overhead
rather than ~900%, and 600 rows a month is ample to compare distributions.

---

## 6. Integration

Micro-batch of 5 items by default — smaller than triage because bodies are long.

Each call receives three things:

1. **The items** — `{id, outlet, title, body, published_at}`.
2. **Candidate entities** — from the matcher, capped at 40, as `{id, name, type}`.
3. **Candidate events** — retrieved through `event_entities` for those candidate entities with
   `occurred_at` inside a 14-day window, capped at 30, as **`{id, summary}` and nothing else.**

Both caps are prompt-budget choices, not measurements, and both are ordered most-recent-first so
truncation drops the least likely candidates. A cap reached is counted in the run tally (§7.5):
if candidates are routinely truncated, the matcher is too loose or the window too wide, and the
count is what will say so rather than a guess.

**The surface-form index is refreshed after each batch's writes, not once per run.** Built once,
an entity created in batch 1 is invisible to batch 3, so events attached to it cannot be offered
as candidates, so `events_matched` is depressed — and `events_matched` is the numerator of
§8.2's corroboration measure. The pre-registered floor could then fail for a caching reason
while the matcher is working correctly, which is the worst kind of gate failure: it looks like
evidence. Incremental update is sufficient; a full rebuild per batch is not required.

### 6.1 Candidate events carry no enum values. This is load-bearing.

`analysis-stats-traps` #4: the gold-set probe handed the model each row's `severity` already
populated, then reported severity variance as evidence the field was healthy. It came back
unchanged on **21 of 23** — the "variance" was echo, not judgment.

§8 scores `events.type` and `events.commitment_state`. If a candidate event arrives carrying
`type: statement`, everything attached to it inherits that framing, and the gate measures the
prompt instead of the model — while still reading as a pass. The only field a candidate shows is
the one not being scored: its summary.

A test enforces this (§7).

### 6.2 Model output

Per item: resolved entities (a candidate `id`, or a new `{name, type, aliases}`); one or more
events (a candidate `id`, or a new `{summary, type, commitment_state, occurred_at}`); and one
assertion per event carrying `standing` only.

**`source_relationship` is NOT extracted in v1.** The column stays nullable and empty. `bqa.8`
records it as *"the only extracted enum with no worked example in the parent spec"* — and the
schema comment names that as *"severity's exact provenance"*. Asking a model to populate a field
with no rubric is the mechanism by which `severity` arrived at `high` 25/25, fully populated and
carrying no information. An earlier draft extracted it anyway and protected §8 with a NULL-rate
guard; inventing a guard against a field already known to be undefined was a signal that the
field should not ship, not that it needed a chaperone.

`bqa.8` decides its fate: it earns a rubric with range-spanning worked examples and a negative
case, or the column is dropped. Neither is this build's work.

Multiple events per item are permitted — `assertions_item_event` is `UNIQUE (item_id, event_id)`,
so an item may assert several distinct events but never the same one twice.

### 6.3 One entity per company — `bqa.9` item 5

The trap is that `entities.type` includes `instrument`, so the wrong answer is **expressible**:
"Apple" and "AAPL" can become two rows.

Two enforcement points, because the prompt alone is what failed last time. `jx9.5` froze claim
text conditioned on `status`, a **model-supplied** field, and the model simply chose the other
value — the epic's recorded corollary is *a guard conditioned on a model-supplied field can be
walked around by the model choosing the other value; condition enforcement on something the code
can see for itself.*

So:

1. The prompt states that a company and its equity line are one entity, with the ticker belonging
   in `entity_instruments`.
2. A **write-time guard** refuses to create an `instrument`-typed entity whose name matches an
   existing `entity_instruments.symbol` already mapped to a company, and counts the refusal.

### 6.4 Write path

One transaction per micro-batch, **and one savepoint per item inside it.**

The savepoint is not a refinement; without it this section contradicts §7.4. `events.type` and
`commitment_state` are `NOT NULL` with no default, so a single malformed model output raises a
CHECK violation that aborts the whole transaction and takes the other four items with it — and
the tally would record one failure where four items were actually lost. §7.4 promises such a
violation is "counted, never swallowed"; a batch-wide transaction cannot keep that promise.

`capture.store_items` already solved exactly this and documented why: *"Each entry gets its own
savepoint (`conn.transaction()`), so neither a duplicate nor a rejected entry can lose the
entries around it … it costs one failed entry rather than the whole pass, and rolls back only its
own savepoint rather than poisoning the transaction the caller is still using."* This pipeline is
modelled on capture; it should not discard capture's one hard-won transactional lesson at the
point where the payload is most expensive to re-derive.

Per item, inside its own savepoint:

1. `entities` — `ON CONFLICT (lower(name), type) DO NOTHING`, then re-select the conflicting rows.
2. `events` — insert new, or use the candidate id.
3. `event_entities` — `ON CONFLICT DO NOTHING` (the pair is the primary key).
4. `assertions` — `ON CONFLICT (item_id, event_id) DO NOTHING`.
5. `item_triage.integrated_at`.

**Corrected 2026-09-05, measured against the built code.** This section previously claimed the
`assertions` conflict clause meant "reprocessing cannot duplicate". It does not, and the guarantee
was never enforced. The clause protects the *pair*, but a re-extraction at a bumped
`integrate_prompt_version` takes step 2's "insert new" branch and mints a **fresh `event_id`** — so
`(item_id, event_id)` differs, the conflict never fires, and the item gains a second event and a
second assertion. Measured directly against a real database: 1 event / 1 assertion before a bump,
2 / 2 after.

Re-integration therefore **accumulates, it does not supersede**. That is unreachable at v1, where
`INTEGRATE_PROMPT_VERSION` never moves, and it is not a reason to hold the build — but it means an
operator bumping that knob doubles the affected events rather than replacing them, which inflates
both `events` and any corroboration figure read off it. Superseding needs a real design (this repo
retires rather than deletes, per `claims.retired_on`, and `assertions` carries FKs), so it is
deferred to its own issue rather than invented inside a fix round.

The general failure this records: a spec stating a guarantee is intent, never enforcement. The
sentence was written from the conflict clause's *shape* without asking which test would fail if it
were false — and none would have, because nothing re-ran an extraction at a bumped version until
one was written to.

`extractor_model` and `prompt_version` are stamped on `entities`, `events` and `assertions`
(`bqa.9` item 4 for these tables).

**The savepoint boundary is the highest-risk line in this design**, and the invariant is
per-item: `integrated_at` must never commit without the assertions it claims. An item marked done
whose content never entered the KB is silent and, before `integrate_prompt_version` existed,
would have been unrecoverable. Capture's own recorded bug was a transaction boundary too —
`record_sightings` and `record_poll` must commit together because `now()` is
`transaction_timestamp()` — which is why this design states the boundary rather than leaving it
to the implementer.

---

## 7. Failure handling, bounds, configuration

### 7.1 Ships off

`COMPREHEND_ENABLED` defaults to `False`, as capture did.

Two consequences from `env-var-needs-compose-passthrough`, both of which have already bitten:

- **On an established host no environment change can turn it on.** `import_settings_from_env`
  runs only while `settings` is empty. The row is the only path.
- **A bool knob cannot report a bad value.** `coerce_knob` returns `text.lower() in _TRUTHY`
  before the empty-string check, so `'ture'` and `''` both silently mean False with no warning.
  **Verify by effect** — an `item_triage` row appearing — never by reading the row back.

### 7.2 Knobs and packaging

Each knob is three things: a `common.KNOBS` entry, **no** module constant (the absence is what
makes PEP 562 `__getattr__` fire), and a `- VAR=${VAR:-}` line in the `x-newsbrief` anchor.

| Knob | Default | Note |
|---|---|---|
| `COMPREHEND_ENABLED` | `False` | ships off |
| `COMPREHEND_TRIAGE_BATCH` | `25` | items per triage call |
| `COMPREHEND_INTEGRATE_BATCH` | `5` | items per integration call |
| `COMPREHEND_MAX_ITEMS` | `300` | items per run; see §7.3 |
| `COMPREHEND_SAMPLE_PER_DAY` | `20` | the §5.3 control arm |
| `NEWSBRIEF_TRIAGE_MODEL` | `""` | empty means follow `MODEL` |
| `NEWSBRIEF_INTEGRATE_MODEL` | `""` | empty means follow `MODEL` |

The two model knobs default to `""`, never to a copy of the model id. A duplicated literal
strands both calls on the old model the moment `NEWSBRIEF_MODEL` moves, silently — the rot
`newsbrief-model-config` records.

`comprehend.py` is a new top-level module, so it needs the Dockerfile `COPY` line, the workflow
`paths:` filter, and both workflow ruff file lists. All three are enforced by
`tests/test_packaging.py`.

### 7.2.1 Cadence and ordering

```python
Schedule("comprehend", "interval", None, 60, grace_minutes=15)
```

Hourly, matching `monitor`'s shape. An earlier draft said "a new `comprehend` entry in
`scheduler.SCHEDULES`" and gave neither kind nor interval, which `Schedule` requires — and it is
the number that decides whether the design functions at all. At `COMPREHEND_MAX_ITEMS = 300`,
hourly gives 7,200 items/day of capacity against the parent spec's assumed ~1,200/day inflow:
roughly 6x headroom, so no persistent backlog forms. A daily schedule at the same cap would grow
a backlog of ~900 items/day forever.

**The pending queries order oldest-first.** Nothing reads this layer yet (§1.2), so completeness
beats recency and no item may starve. Newest-first would permanently skip the tail of any
backlog; with 6x headroom there should be no backlog to chew, and if one appears, FIFO makes it
visible as lag rather than hiding it as silent omission.

### 7.3 Bounds

**Item caps are the primary control; wall-clock is the backstop.** The asymmetry is deliberate: a
too-low item cap leaves a backlog the next run picks up, which is benign, whereas
`JOB_MAX_RUNTIME_MINUTES` records that a time cap guessed too low kills working runs. Because
each micro-batch is its own transaction, hitting the backstop loses at most one batch.

### 7.4 Failure modes, handled individually

| Mode | Handling |
|---|---|
| Truncation | check `stop_reason` **before** the parser |
| Unparseable tool input | `verdict='failed'`, `attempts++` |
| Candidate id not in the offered set | reject that item's output; never trust a hallucinated id |
| Enum value outside a CHECK | rolls back its own savepoint, is counted, and does not touch its neighbours (§6.4) |

The last is a recorded failure, not a hypothetical, though it is worth stating precisely because
the imprecise version is misleading. `brief_memory.py:695` documents `save_ledger`'s per-row
`except` swallowing a CHECK violation — and the consequence it records is **not** that the row
disappeared. The row **stays `'standing'`**, so "the brief renders a fact it knows to be broken as
established, every day, indefinitely." A `skipped` count is returned and logged at
`brief.py:3305` as "refused by the store", so the failure is counted but its *meaning* is lost:
the operator sees a number, not a claim that is now silently wrong.

That is the sharper lesson for this pipeline. A swallowed constraint violation here does not
produce an empty KB, which would be obvious. It produces a KB that is confidently incomplete.

### 7.5 The run tally

Per `fail-closed-needs-status-not-count`, the run returns which gate stopped what, with numbers —
never a bare count. Following capture's `Tally`:

`items_seen`, `triaged_by_rules`, `triaged_by_model`, `sampled` (§5.3), `material`,
`immaterial`, `failed_triage`, `failed_integration`, `gave_up_triage`,
`gave_up_integration` (§4.2), `entities_created`, `entities_resolved`, `events_created`,
`events_matched`, `assertions_written`, `items_lost_to_savepoint` (§6.4),
`instrument_entity_refused` (§6.3), `candidate_cap_hit` (§6).

Two counters exist specifically so a silent degradation cannot hide behind an aggregate.
`items_lost_to_savepoint` distinguishes "four items were collateral" from "one item failed" —
the exact confusion a batch-wide transaction would have produced. `gave_up_integration` is the
gate that `failed_integration` alone could not be: a rising failure count with no ceiling is a
number nothing acts on.

`events_created` versus `events_matched` is the corroboration mechanism and feeds §8 directly.

---

## 8. The pre-registered gate

**These numbers and `scripts/score_comprehension.py` land before the flag is turned on.**
Measured afterwards, they are post-hoc rationalisation wearing a gate's clothes. The user's own
record: pre-registration *"found every production bug in the podcast corpus repair; none was
found by reading code that had been reviewed repeatedly."*

### 8.1 Enum variance

Three enums, not four — `source_relationship` is not extracted (§6.2). For each of
`events.type`, `events.commitment_state` and `assertions.standing`:

- at least **2 distinct values at ≥10% each**, and
- **no single value above 90%**

The 90% ceiling is deliberately loose. `assertions.standing` should legitimately skew hard toward
`reported`, because most news copy is reported; a tighter threshold would fail for a real reason
and teach us to distrust the gate. `severity` failed this test at 100%.

**Each figure is reported per `reason` arm as well as in aggregate**, with `sampled` (§5.3) as
the control. If `topical` rows show healthy variance and `sampled` rows do not, the extractor is
being flattered by its sibling's selection rather than working — a difference the aggregate
cannot show, and the reason the control arm exists.

**Extraction depth caps what these can reach.** `items.body` is the RSS blurb (§5.1 rule 4), so
`commitment_state` in particular is being judged from a headline and a sentence. If it fails the
variance test, the first hypothesis is input depth, not model judgement, and §10 item 3's probe
is what discriminates. Recording that *before* the run is the point: after it, "the input was
thin" is indistinguishable from an excuse.

### 8.2 Corroboration — both directions

- **Floor: ≥10%** of events carry assertions from **2 or more distinct outlets**. Below this the
  event layer bought essentially nothing over the claim ledger, which is the entire justification
  for §2.1.
- **Ceiling: ≤60%.** Above this, suspect the matcher is **over-merging** distinct events into
  one. This is the mirror failure and it presents as success.

The ceiling exists because `analysis-stats-traps` #3 records a pre-registered criterion that
named only one direction: the first real run failed in the mirror direction — precision 33%→75%
while recall went 100%→42.9% — and the gate said nothing.

Report `events_matched / (events_matched + events_created)` beside the headline. If the two
disagree, trust the disagreement (`the-probe-measured-the-wrong-layer`).

### 8.3 Volume and cost envelope

Predicted items/day, material rate and $/day are written down **before** the first real run and
compared after.

**This rests on an unverified figure.** The parent spec's §6.4 assumes ~1,200 captured items/day
with ~10% integrated, and that number predates capture existing. Capture has been enabled since
2026-09-04, so real figures exist — in the host's Postgres, which this repo cannot read
(`live-state-on-deploy-host`). The query that settles it:

```sql
SELECT count(*) AS items_total,
       count(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS last_24h
FROM items;

SELECT o.name, count(*) AS items
FROM items i JOIN outlets o ON o.id = i.outlet_id
WHERE i.created_at >= now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

Until it is answered, batch sizes and `COMPREHEND_MAX_ITEMS` in §7.2 are **defaults chosen to be
safe rather than sized**, per §7.3's asymmetry.

---

## 9. Testing

House pattern, following `tests/test_kb_schema.py`.

- **Every CHECK in 0009 gets a violation attempt**, including the biconditional in both
  directions. A constraint nothing tries to break is a comment.
- **The 0009 rollback needs no test edit** (§4.4). If it does, reopen `5db`.
- **The matcher gets a positive and a negative case per rule** in §5.1 — substring rejected,
  short-form case sensitivity both ways, stop-list, the 500-character window boundary.
- **Savepoint isolation, which is now two assertions rather than one.** A malformed item in a
  5-item batch must leave *its own* `integrated_at` NULL with no partial assertions (the absence),
  **and** the other four must be written (the presence). The second is what proves the savepoint
  exists; without it the test passes identically against a batch-wide transaction that lost all
  five, which is the exact defect this replaced.
- **Integration retry ceiling**: an item failing deterministically stops being selected after
  `integrate_attempts` reaches 3, and is counted in `gave_up_integration`.
- **The `sampled` arm draws only from items both halves rejected**, and is capped per day.
- **A hallucinated candidate id is rejected** rather than written.
- **The write-time instrument guard** (§6.3) refuses and counts.
- **Prompt assembly emits no `type` or `commitment_state` on candidate events** (§6.1). If this
  regresses, §8.1 silently measures echo and still reads as a pass.
- All model calls stubbed; `tests/conftest.py`'s network guard fails any test that reaches out,
  swallowed or not.

**Vacuity discipline.** `tests-asserting-less-than-their-name` records seven tests in one build
that passed while asserting less than their names claimed, and calls it the dominant defect class
in this repo. Every absence assertion here needs a presence sibling, and each test is checked by
mentally deleting the code and asking whether it would still pass.

---

## 10. Open questions

Stated rather than resolved, because guessing them silently is the failure mode.

1. **The 10% corroboration floor is a guess.** With 26 feeds concentrated in ~4 wires and 9
   Google News `site:` proxies that re-return the same items, real multi-outlet overlap could be
   substantially higher or lower. Committed to anyway, so it fails loudly rather than being left
   open.
2. **The 14-day event-candidate window is unmeasured.** Too narrow and corroboration fails
   artificially; too wide and the matcher merges a recurring event type across weeks.
3. ~~**How much text `items.body` actually holds.**~~ **ANSWERED 2026-09-05 — see §12.**
   `capture.py` stores `entry.get("summary")`, which for some feeds is a full article and for
   others is one sentence. The answer decides whether extraction is headline-level or richer, and
   §8.1 says `commitment_state` is the field that will show it first. Twenty real rows settle it:

   ```sql
   SELECT o.name, length(i.body) AS body_len, left(i.body, 200) AS sample
   FROM items i JOIN outlets o ON o.id = i.outlet_id
   WHERE i.body IS NOT NULL
   ORDER BY random() LIMIT 20;
   ```

   If bodies turn out to be uniformly thin, the options are to accept headline-level extraction
   or to fetch article text at integration time for material items only (~10% of volume, reusing
   `fetch_web_source`). Fetching at **capture** time is rejected: §3.1's separation is governing,
   and a per-item fetch inside capture's 600s bound is how that bound gets broken.

4. **Real capture volume** (§8.3) — answerable only on the host.

Items 3 and 4 are both single queries against the deploy host and both should be answered before
implementation starts, because each can change a number in this spec. Items 1 and 2 are measured
by the first real run.

---

## 11. Revision history

**2026-09-04, after a fresh-context red-team pass.** Recorded because several of these were
self-contradictions that re-reading could not find — each section was internally coherent, which
is the `the-rule-exempts-its-own-origin` shape.

| Change | Why |
|---|---|
| Savepoint per item (§6.4) | A batch-wide transaction made §7.4's "counted, never swallowed" promise impossible to keep |
| Split `prompt_version` in two (§4) | One column served neither prompt; the column exists to compare versions |
| `integrate_attempts` (§4.2) | Triage had a retry ceiling, the expensive tier had none |
| `item_triage_pending` predicate (§4) | ~90% of the index was rows the only query could never return |
| GIN index on `aliases` removed (§4) | Contradicted the in-memory matcher two sections away |
| Matcher refreshed per batch (§6) | Staleness depressed `events_matched`, which is the gate's own numerator |
| Cadence stated (§7.2.1) | `Schedule` requires it, and it decides whether a backlog forms |
| Cold-start reality (§5.1.1) | `tracked_story` can never fire; `tracked_entity` starts empty |
| `sampled` control arm (§5.3) | `topical` alone is confounded with the triage model's own view |
| `source_relationship` dropped (§6.2) | No rubric anywhere; extracting it reproduces `severity` |
| Body window rule dropped (§5.1) | It reasoned about article text the KB does not hold |
| Two citations corrected (§3.1, §7.4) | `supervisor.py` line range wrong by ~95 lines; `brief_memory.py:695`'s consequence mischaracterised — the row stays `standing`, it does not vanish |

**Rejected from that pass:** removing the model triage half in favour of recency sampling. The
argument for it — that a model-selected set is confounded — is correct and is why §5.3 exists.
The claim that it was cost-neutral is not: it moves integration from ~120/day to ~1,200/day
against a ~$6/mo triage saving.

---

## 12. Measured against production, 2026-09-05

§10 items 3 and 4 are answered. Measured on the deploy host at 2026-09-05 11:30, over the
22-hour window since capture was enabled (oldest item 2026-09-04 13:30). Control first: 87
`capture_runs`, 45 of them `enabled`, newest run 11:30 the same day — these figures describe a
live system, not a switched-off one.

### 12.1 Volume — the spec's assumption survives, the cost estimate does not

**2,270 items in 22 hours ≈ 2,475/day**, against §6.4's assumed ~1,200/day.

**Treat that as an UPPER BOUND, not a rate.** The first enabled pass stores every feed's whole
window, so an unknown share of the 2,270 is backfill rather than throughput. IranWire at 491
items in 22 hours is ~22/hour from one outlet, which no newsroom publishes; the shape says
backfill dominates. Decomposing it needs a per-hour breakdown and is not worth blocking on,
because the bound is conservative in both directions that matter: it over-sizes the caps and
over-states the cost.

**Sizing survives.** `COMPREHEND_MAX_ITEMS=300` hourly is 7,200/day of capacity — 3x headroom
even at the upper bound. No change.

**Cost does not survive, and this is now the pre-registered figure.** At ~10% material, 2,475/day
gives ~250 integrated/day against the §6.4 model's 120/day. Integration roughly doubles, from
~$13/mo to ~$27/mo, putting the total nearer **$45-55/mo than $25-35/mo**. Recorded here before
the first run so the comparison afterwards means something.

### 12.2 Body depth — headline-level, confirmed, and UNEVENLY so

Median `body` length by outlet, 24 outlets:

| Band | Outlets | Examples |
|---|---|---|
| under 150 chars | **15 of 24** | Reuters 56, Kyiv Independent 58, NHK 77, IranWire 140 |
| 150-350 | 7 | The Hindu 176, Meduza 234, Times of Israel 310 |
| ~500 | 2 | SCMP 500, OilPrice 546 |

**No outlet carries article text.** The two richest are clamped, not merely long: SCMP runs
min 487 / max 503 and OilPrice min 520 / max 557. That spread is the signature of a hard
server-side truncation, not natural variation. So "fetch article text at integration time" would
be a genuine addition rather than a way of using what is already stored.

**The two highest-volume outlets have the least text**, because they are Google News proxies whose
`summary` is the headline restated. Reuters bodies read
`UAE pardons Egyptian-Turkish poet facing 10 years in jail&nbsp;&nbsp;Reuters`. IranWire is clamped
at 140. Together they are **926 of 2,270 items — 41% of volume with no body beyond the title.**

Two incidental findings, both actionable:

- **HTML entities are not decoded.** `&nbsp;` appears literally. The matcher and the prompt must
  `html.unescape()` before use, or a surface form spanning an entity boundary silently fails to
  match.
- **The proxy feeds carry non-news.** `(MYCN.O) | Stock Price & Latest News` is a stock-quote
  page. Triage should reject it, and the per-outlet immaterial rate becomes a real measurement of
  proxy-feed junk rather than a triage failure.

### 12.3 What this changes in the gate

**It does not change the build.** Headlines are more judgeable than "56 characters" suggests:
*"Ukrainian drones attacked Russia's Moscow region"* is unambiguously `action` / `in_force`, and
*"Iran and Oman state that negotiations are ongoing"* is unambiguously `statement` / `intended`.
`events.type` and `commitment_state` are not obviously starved by this input.

`assertions.standing` is the field at risk, for a reason worth stating precisely: separating
`official` from `attributed` from `alleged` normally needs the attribution clause, and a headline
strips it. The likely failure is not low variance — it is that **standing becomes a function of
the OUTLET rather than of the item**, which would look healthy in aggregate while carrying no
per-item information at all.

Two amendments to §8.1 follow:

1. **Report each enum per body-depth tier** (under 150 / 150-350 / 350+) as well as in aggregate.
   With 41% of volume in one tier, an aggregate figure substantially measures feed composition.
2. **For `standing`, also report variance WITHIN each outlet.** A field that varies across outlets
   and is constant within one is degenerate in the way that matters, and no aggregate distribution
   would show it. This is `severity`'s failure wearing a disguise that the original gate would
   have passed.

Neither loosens the thresholds. They add the breakdowns that stop a pass being spurious.

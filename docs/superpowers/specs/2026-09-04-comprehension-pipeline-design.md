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
items  (no triage verdict at the current prompt_version)
  │
  ▼
TRIAGE
  ├─ rules half  : surface-form match vs entities, live claims, open stories
  │                                                    [SQL + in-memory, NO model]
  └─ model half  : topical fit, micro-batched          [triage model, non-matches only]
  │
  ▼
item_triage  (verdict, reason, prompt_version, attempts)
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

Concretely: `capture` bounds a pass at 600s against a 30-minute interval, and
`supervisor.py:834-851` alerts on any job still running at its next fire — 48 chances a day. An
unbounded model call inside that bound would convert a slow provider into a daily alert storm.

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
                                     'tracked_story', 'topical', 'none', 'error')),
    -- Biconditional in both directions, in the style of observations' metric
    -- check: a material verdict must carry a real reason, and a non-material one
    -- must not. metric/return_window proved the shape has no three-valued hole.
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error'))),
    -- NULL means the rules half decided and NO model ran. Same reasoning as
    -- observations.provider being distinct from extractor_model: not every row
    -- is produced by a model, and a NOT NULL column here would force a lie.
    triage_model   TEXT    NULL,
    prompt_version INTEGER NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    integrated_at  TIMESTAMPTZ NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reprocessing after a prompt bump is a NEW row, never an overwrite: the point
-- of stamping prompt_version is to compare versions, which an upsert destroys.
CREATE UNIQUE INDEX item_triage_item_version ON item_triage (item_id, prompt_version);
CREATE INDEX item_triage_pending ON item_triage (verdict) WHERE integrated_at IS NULL;

-- Candidate retrieval searches name AND aliases. Without this every mention is
-- a sequential scan over entities.
CREATE INDEX entities_aliases ON entities USING GIN (aliases);
```

### 4.1 Why a table and not a column on `items`

There is currently no way to know which items have been processed. Deriving it from `assertions`
works for integrated items but re-triages every **rejected** item forever, and rejects are the
majority. The verdict row is also the measurement surface §8 requires.

### 4.2 Why `attempts` exists

With a unique key on `(item_id, prompt_version)`, writing a `failed` row would make that failure
permanent until someone bumped the prompt version. The pending query instead selects items with
no verdict **or** `verdict = 'failed' AND attempts < 3`, and a retry updates the row in place.

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

Matching rules, each of which exists because of a recorded failure:

1. **Word-boundary matching only, never substring.** PolyGram's substring search made `MU` match
   "Musk" (`polygram-candidate-search-fix`); the fix was entity-token search with a length floor.
2. **Forms of 4+ characters match case-insensitively. Shorter forms match case-sensitively as
   whole tokens**, so `US` matches `US` but not `us`. Acronyms are real entities; casefolding
   them is what makes them toxic.
3. **A stop-list** for surface forms that are also ordinary English words.
4. **Match against title plus the first 500 characters of body.** News copy is an inverted
   pyramid, so a genuinely relevant actor is named early. Matching the whole body makes every
   passing mention of "China" material.

Rule 4 is an **assumption, not a measurement**. `item_triage.reason` is what will allow it to be
checked: if `tracked_entity` dominates and integration quality is poor, the window is too wide.

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
assertion per event (`standing`, `source_relationship` or null).

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

One transaction per micro-batch:

1. `entities` — `ON CONFLICT (lower(name), type) DO NOTHING`, then re-select the conflicting rows.
2. `events` — insert new, or use the candidate id.
3. `event_entities` — `ON CONFLICT DO NOTHING` (the pair is the primary key).
4. `assertions` — `ON CONFLICT (item_id, event_id) DO NOTHING`, so reprocessing cannot duplicate.
5. `item_triage.integrated_at`.

`extractor_model` and `prompt_version` are stamped on `entities`, `events` and `assertions`
(`bqa.9` item 4 for these tables).

**The transaction boundary is the highest-risk line in this design.** Capture's own recorded bug
was a transaction boundary — `record_sightings` and `record_poll` must commit together because
`now()` is `transaction_timestamp()`. Here the failure is worse: `integrated_at` committed
without its assertions marks an item permanently done while its content never entered the KB.
Silent, and unrecoverable without a prompt-version bump.

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
| `NEWSBRIEF_TRIAGE_MODEL` | `""` | empty means follow `MODEL` |
| `NEWSBRIEF_INTEGRATE_MODEL` | `""` | empty means follow `MODEL` |

The two model knobs default to `""`, never to a copy of the model id. A duplicated literal
strands both calls on the old model the moment `NEWSBRIEF_MODEL` moves, silently — the rot
`newsbrief-model-config` records.

`comprehend.py` is a new top-level module, so it needs the Dockerfile `COPY` line, the workflow
`paths:` filter, and both workflow ruff file lists. All three are enforced by
`tests/test_packaging.py`.

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
| Enum value outside a CHECK | count and log attributably — **never swallow** |

The last is a recorded failure, not a hypothetical: `brief_memory.py:695` documents
`save_ledger`'s per-row `except` swallowing a constraint violation so the row silently vanished.

### 7.5 The run tally

Per `fail-closed-needs-status-not-count`, the run returns which gate stopped what, with numbers —
never a bare count. Following capture's `Tally`:

`items_seen`, `triaged_by_rules`, `triaged_by_model`, `material`, `immaterial`,
`failed_triage`, `failed_integration`, `gave_up` (attempts exhausted), `entities_created`,
`entities_resolved`, `events_created`, `events_matched`, `assertions_written`,
`instrument_entity_refused` (§6.3), `candidate_cap_hit` (§6).

`events_created` versus `events_matched` is the corroboration mechanism and feeds §8 directly.

---

## 8. The pre-registered gate

**These numbers and `scripts/score_comprehension.py` land before the flag is turned on.**
Measured afterwards, they are post-hoc rationalisation wearing a gate's clothes. The user's own
record: pre-registration *"found every production bug in the podcast corpus repair; none was
found by reading code that had been reviewed repeatedly."*

### 8.1 Enum variance

Over non-NULL rows, for each of `events.type`, `events.commitment_state`,
`assertions.standing`, `assertions.source_relationship`:

- at least **2 distinct values at ≥10% each**, and
- **no single value above 90%**

The 90% ceiling is deliberately loose. `assertions.standing` should legitimately skew hard toward
`reported`, because most news copy is reported; a tighter threshold would fail for a real reason
and teach us to distrust the gate. `severity` failed this test at 100%.

**`source_relationship` carries an extra condition.** It is nullable and NULL means *"not
labelled"*, not *"independent"*. If **NULL exceeds 50% of rows**, the field fails regardless of
how well the labelled remainder varies — otherwise 10 well-varied rows out of 1000 pass a
variance check on a field that is effectively dead. `bqa.8` names this field as having no worked
example anywhere in the parent spec, which is `severity`'s exact provenance.

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
- **Transaction boundary**: a failure mid-batch leaves `integrated_at` NULL and no partial
  assertions.
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
3. **The 500-character body window** (§5.1 rule 4) is reasoned from news structure, not measured.
4. **Real capture volume** (§8.3) — answerable only on the host.

None blocks implementation. Each is a number the first real run measures.

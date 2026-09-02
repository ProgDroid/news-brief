# Knowledge Base Schema DDL — Design

Issue: `news-brief-bqa.3`. Parent: `news-brief-bqa` (Epic 3, KB core).
Implements sections 3, 4 and 12.2 of `2026-08-29-knowledge-base-architecture-design.md`.

The KB architecture spec decided *what objects exist*. This one decides *what columns exist*,
which is a different and narrower question — and, per §12.2, one with a stated evidential bar that
the parent spec does not itself meet for nine of its fields.

Revised twice after fresh-context red-team passes; §10 records what they changed.

---

## 1. Scope

One migration pair, `0006_knowledge_base_up.sql` / `0006_knowledge_base_down.sql`, creating
sixteen tables for the ten §3.1 objects. No Python module, no reader, no data movement, no
compose change.

| Decision | Choice | Consequence |
|---|---|---|
| Which objects get tables | **All ten, Thesis included** | No DDL churn when Epic 6 undefers. |
| The unenumerated fields | **Decided now, to the §12.2 standard** | Design-then-DDL, not DDL alone. |
| The live JSON ledger | **`claims` is a superset; cutover is `bqa.4`** | The morning brief never runs against a half-migrated store. |
| Time model | **Two clocks by convention** | No generic `valid_from`/`valid_until` pair. |
| `pgvector` | **Deferred** (`news-brief-bqa.7`) | Requires an image swap; see §8. |

### 1.1 What this migration deliberately does not touch

- **`sources`** keeps its `user_id` and its subscription role, unchanged. The FK joining it to
  `outlets` lands in `bqa.4`, because adding it means backfilling a live table the Telegram
  `/addsource` wizard writes to.
- **`brief_memory.json`** keeps running. Nothing reads `claims` yet.
- **`docker-compose.yml`** is untouched. The repo's copy has drifted behind the host's
  (`news-brief-qx4`), so a schema change is the wrong place to edit it.

Every obligation this migration places on `bqa.4` is enumerated in §6 and tracked as
`news-brief-bqa.9`. Scattering them across sections is what let the most serious defect in the
first two drafts survive both.

### 1.2 Quarantine applies to tables, not only to columns

Epic 1's converged rule is *quarantine is the default for an unmeasured field; measurement lifts
it*. Epic 1 practised it on `status`, `origin`, `horizon_days` and `kind`: the field exists, is
written on every row, and is **read by nothing** until a gold set shows it carries variance.

The same rule applies one level up. Eight of the sixteen tables serve rules with no measured base
rate — parent §12.1's verdict is "build rule 1", and rules 2, 3, 4 and 5 carry no measurement.

**Provisional tables and what each is actually for.** A blanket "no DDL churn later" argument would
be circular, so each is named with its consumer:

| Table | Consumer | Exists before that consumer because |
|---|---|---|
| `observations` | rules 2, 5 | `bqa.5` writes all five rules in one epic |
| `links` | rules 2, 5 | same |
| `entity_instruments` | `observations` | symbol → entity resolution has no other home |
| `stories`, `story_members` | Epic 4 render, parent §4.2 item 3 | grouping is the render query's third clause |
| `open_questions` | Epic 4 render, parent §4.2 item 6 | "past due" needs a date column |
| `theses`, `thesis_claims` | **Epic 6 only** | **no consumer before Epic 6.** These two are in the migration because the schema was scoped to all ten objects, not because anything needs them sooner. |

The last row is stated plainly rather than dressed up. It is the weakest case in the table and the
reader should be able to see that.

**The licence, and its limit.** While a provisional table is empty **and not referenced by a
non-provisional table**, it may be reshaped by a later migration without ceremony — no design
review, no compatibility argument, no deprecation path.

One table fails the second test: **`observations` is referenced by `claim_evidence`**, which is not
provisional. Reshaping it is therefore an ordinary breaking change, not a free one. (`stories`
passes: it is referenced only by `open_questions` and `story_members`, both themselves provisional.)

**Detecting the end of the licence.** The obligation to preserve a shape begins when something
writes the table, and nothing observes first write. §7 test 10 asserts the eight are empty; it runs
in CI until `bqa.4` lands and then fails loudly, which is the intent.

**The honest statement this section exists to make:** eight of these sixteen tables have never been
checked against anything but themselves. `claims` and `entities` were audited against
`brief_memory.py` — live code with measured field outcomes that pushes back on a wrong design.
`links` and `theses` were audited for internal consistency only. Residual defect risk is higher in
the provisional eight, and the second red-team pass is weak confirmation: four of its findings
landed there.

### 1.3 The alternative that was rejected, scored

A red-team pass recommended cutting to eight tables — dropping `theses`, `thesis_claims`, `stories`,
`story_members`, `open_questions`, `observations`, `entity_instruments`, `links`.

| | 16 tables (chosen) | 8 tables |
|---|---|---|
| Migrations to reach Epic 4 | 1 | 3 (`0006`, +stories/questions, +links/observations) |
| `bqa.5` completeness | all five rules | rules 1, 3, 4 only; 2 and 5 blocked mid-epic |
| Surface designed without a consumer | 8 tables | 0 |
| Cost if a provisional shape is wrong | `ALTER` on an empty table | none |
| Reviewable unit size | one large migration | three small ones |

The deciding row is the second: `bqa.5` implements all five propagation rules in one epic, so the
cut relocates churn into the middle of an implementation rather than removing it. The cut wins the
third and fifth rows, and a preference for smaller reviewable units over fewer migrations is a
legitimate reason to take it.

---

## 2. The extracted/derived split

§12.2's warning is that a field can be fully populated, pass every completeness check, and carry no
information — `severity` returned `high` on 25 of 25 live claims.

The diagnosis matters more than the example. `severity` did not fail because its rubric was badly
worded. It failed because **nothing outside the model constrained the answer**, so the answer
collapsed to the model's prior. Compare `status`, the one field measured usable: `broken` is not
chosen in a vacuum, it fires when a contradicting event arrives.

- **Extracted** — a model labels it from text. Needs the full §12.2 apparatus: a stated default,
  worked examples spanning the range, an explicit negative case, and a variance check in the gold
  set.
- **Derived** — a propagation rule computes it. Needs a stated *function*, not a rubric. **Cannot be
  degenerate by construction**, because no model chooses it, so it needs no variance check. The
  guarantee is void if the function does not determine a value at every input, including its
  default.

### 2.1 The nine fields

| Field | Kind | Values | Default |
|---|---|---|---|
| `assertions.source_relationship` | extracted | `party` · `aligned` · `independent` · `adversarial` | none — nullable, no default |
| `observations.metric` | extracted | `price` · `return` · `yield` · `volume` · `spread` | none — `NOT NULL`, no default |
| `entities.type` | extracted | `country` · `institution` · `company` · `person` · `instrument` | none — `NOT NULL`, no default |
| `claims.horizon_days` | extracted | `INTEGER`, 1–3650 | `NULL` |
| `claims.severity` | extracted | `low` · `normal` · `high` | `normal` — **rubric owed, see §2.2** |
| `claims.status` | derived | `standing` · `challenged` · `broken` · `confirmed` · `expired` · `withdrawn` | `standing` |
| `theses.confidence` | derived | `speculative` < `tentative` < `supported` < `established` | `speculative` |
| `stories.state` | derived | `active` · `dormant` · `closed` | `active` |
| `links.status` | derived | `unchecked` · `active` · `decayed` · `refuted` | `unchecked` |

"No default" is a specification, not an omission. An observation without a metric is unusable, and
defaulting it would silently mislabel rows rather than reject them. `source_relationship` gets the
same treatment for the reason §9.1 gives: it is the enum with no anchor, so a `NOT NULL DEFAULT` is
the fastest available route to the degenerate outcome §12.2 rates worse than a missing field.

**There is no `resolved_outcome` column.** An earlier draft had one alongside a three-value
`status`. That put `broken` in two enums with nothing tying them, permitted `status='standing'` with
`resolved_outcome='broken'`, and left the parent spec's rule-4 `stale` state homeless.

The fold removes that pair. It does **not** make every impossible combination unrepresentable — an
earlier draft claimed it did, which was overstated. `status='standing'` with `broken_by_event_id`
set is still expressible, and §5's CHECKs constrain only what a CHECK can reach.

### 2.2 The five extracted rubrics

**`assertions.source_relationship`** — the relationship of the *asserting outlet* to the *event*. A
property of the edge, not of the outlet, which distinguishes it from `outlets.perspective` and
`outlets.state_funded`; those are static.

- `party` — the outlet is, or directly speaks for, an actor in this event.
- `aligned` — not an actor, but institutionally tied to one of this event's parties.
- `independent` — no institutional stake in this event.
- `adversarial` — institutionally tied to a party opposing the actor.

*Negative case: IRNA reporting a French rate decision is `independent`, despite being state-funded.
State funding is an outlet property. It becomes `aligned` only when the funding state is a party to
**this** event.*

**`observations.metric`**

- `price` — a level in the instrument's quote currency.
- `return` — a fractional change over a stated window. Requires `return_window`.
- `yield` — a rate, percent per annum.
- `volume` — traded quantity.
- `spread` — a difference between two quoted levels.

*Negative case: a percentage move is `return`, not `price`, even when the source prints it beside a
level. The replay's "SK Hynix +13%" is a `return` row; the level it moved from is a separate `price`
row.*

`return_window` is a field §3.1 does not have, added because a return without a period is not a
number. `NULL` for every other metric, enforced by CHECK.

**`entities.type`**

- `country` — a sovereign state as an actor.
- `institution` — a body with agency that is not a company: government, ministry, central bank,
  alliance, armed group.
- `company` — a commercial firm.
- `person` — a named individual.
- `instrument` — a tradeable: an equity line, index, commodity or currency pair.

*Negative case: "the Fed" is an `institution`, not a `company`, despite having a balance sheet.*

A company and its listed equity line should resolve to **one** entity with an instrument mapping,
not two rows — otherwise Links double and per-entity aggregates split. This is an **extraction
rule, enforced in `bqa.4`** (§6), not a schema guarantee: `UNIQUE (lower(name), type)` permits
`('Apple','company')` and `('Apple','instrument')` to coexist, and no unique key can express the
rule. An earlier draft asserted the schema prevented this. It does not.

**`claims.horizon_days`** — the claim's own stated horizon, in days, 1–3650.

*Negative case: a claim whose horizon cannot be determined gets `NULL` and is **exempt** from rule 4
staleness. It is never defaulted. A default would manufacture calibration data: `horizon_elapsed` is
only meaningful against a horizon somebody actually asserted.*

The range matches `brief_memory._coerce_horizon_days`, which accepts only `1 <= n <= 3650`
(`_MAX_HORIZON_DAYS`). Without the CHECK, `0` and negatives insert cleanly, put `resolution_date` on
or before `first_seen`, and make rule 4 fire on a claim the moment it is created.

**`claims.severity` — the exception, stated rather than hidden.**

`severity` is the field whose measured degeneracy motivates this entire section: `high` on 25 of 25
live claims. §12.2's rule — *a uniform field is worse than a missing one* — would delete it, and this
spec applies exactly that rule to reject `theses.origin` (§3.6) and to flag `links.origin` (§9.2).

It survives anyway, and the reason must be stated or the rule looks selectively applied:
**`severity` is load-bearing in live code.** Three call sites — `brief_memory.py:351` (`_ttl_bonus`
grants `high` claims extra retention days) and `:358`, `:390` (`_severity_rank` orders the
working-set prefix `select_working_set` slices). Dropping the column changes retention and ordering.

So it ships with a **rubric debt**: values, default and negative case are owed before any *new*
consumer reads it, and the gold set must check it for variance (`news-brief-bqa.8`). The existing
two consumers are grandfathered; nothing in the KB may add a third.

### 2.3 The four derived functions

**`claims.status`** — one lifecycle, one column. The reconcile model *proposes* a value; the merge
applies it, and the propagation rules write the terminal states. **Every writer of a non-`standing`
state also stamps `resolved_on`** — that is the writer contract the §5 CHECK enforces.

| State | Written by | Stamps |
|---|---|---|
| `standing` | default on creation | — |
| `challenged` | contradicting evidence, or the unmarked-rewrite guard in `_reaffirm` | `resolved_on` |
| `broken` | rule 1, when an `event_triggered` falsifier matches | `resolved_on`, and at least one of `broken_by_event_id` / `broken_by_note` |
| `confirmed` | rule 1, on a resolved supporting event — never on a restatement | `resolved_on` |
| `expired` | rule 4, past `resolution_date` with nothing resolving it (parent spec's `stale`) | `resolved_on` |
| `withdrawn` | a review-queue action | `resolved_on` |

Transitions: `standing → challenged → standing` is permitted (a challenge can be answered), matching
`_apply_status`, which stamps the date on the first exit from `standing` and never rewrites it.
`broken`, `confirmed`, `expired` and `withdrawn` are terminal **in this schema** — see §6, because
they are not terminal in the incumbent reader and that gap is the single most dangerous item in this
design.

*Negative case: a claim restated more forcefully is not `confirmed`. That is the entire measured
false-positive class from parent §12.1.*

**`theses.confidence`** — advances **only on resolved supporting claims**, never on their count.

| State | Condition |
|---|---|
| `speculative` | **no supporting claims at all** |
| `tentative` | supporting claims exist, none resolved |
| `supported` | ≥1 supporting claim resolved `confirmed`, no undermining claim confirmed |
| `established` | >1 independent supporting claim resolved `confirmed` |

An earlier draft defined `speculative` as "no supporting claim has resolved", which overlaps
`tentative` completely and left the function undetermined at the value every thesis starts on.

**`stories.state`**

| State | Condition |
|---|---|
| `active` | `last_material_change IS NULL` (newly created, no members yet) **or** within the staleness window |
| `dormant` | `last_material_change` older than the staleness window |
| `closed` | set only by a review action |

The `NULL` arm is explicit because the same omission is what made the old `confidence` ladder
undetermined at its default — a newly created story has no material change yet and must still have a
defined state.

*Negative case: a story with no news is `dormant`, never `closed`. Silence is not resolution* — the
same shape as the "absence is not contradiction" rule that made `status` the one field measured
usable.

**`links.status`** — written by rule 5 at the decay check.

`decay_check_date` is **`NOT NULL`**, derived at write time from `expected_persistence` (which is
itself `NOT NULL`). A nullable decay date would leave a link `unchecked` forever, never entering
rule 5 — and the negative case below makes that permanent rather than eventually-noticed.

*Negative case: a link whose `decay_check_date` has passed with no check actually run stays
`unchecked`. It does not become `decayed` through the passage of time, or the persistence priors
learn from measurements that never happened.*

### 2.4 A tenth field, and why it is not a column

§3.5 says "Members carry status" and never enumerates the statuses. It resolves to **no column**: a
member is not live exactly when its event has `superseded_by` set, or its claim's `status` is any of
`broken`, `expired` or `withdrawn`. Derivable by join.

(An earlier draft wrote that predicate as "its claim is `broken`", which was correct against the
three-value status and stale after §2.1's fold.)

§3.5's actual requirement — *"the renderer reads the member list; it never rebuilds it"* — is about
**membership**, which stays stored. Computing status at read time cannot retcon; a stored copy could
go stale and would reintroduce the Day 3 failure the sentence was written to prevent.

---

## 3. Tables

| §3.1 object | Tables |
|---|---|
| Source | `outlets` |
| Item | `items` |
| Event | `events`, `event_entities` |
| Assertion | `assertions` |
| Observation | `observations` |
| Entity | `entities`, `entity_instruments` |
| Claim | `claims`, `claim_evidence` |
| Thesis | `theses`, `thesis_claims` |
| Story | `stories`, `story_members`, `open_questions` |
| Link | `links` |

### 3.1 Source is not `sources`

§3.1's **Source** and the shipped `sources` table are not the same object, despite `sources` having
all four of Source's columns.

`migrations/0003` declares `user_id BIGINT NOT NULL` with `UNIQUE (user_id, url)`. That table is a
**subscription** — one person's feed list. An Item comes from an *outlet*, and the KB is shared. If
two readers both subscribe to Reuters, `sources` holds two rows and an Item would have to pick one,
permanently attributing a shared world-fact to one reader's subscription.

0003's own comment drew the boundary correctly — *"a source is part of how one person reads the
world... the KB tables that will hold what the sources SAY deliberately do not [carry user_id]"* —
and then placed the outlet attributes on the reader's side of it. `outlets` moves them across.
`sources.outlet_id` is a `bqa.4` change (§6).

### 3.2 Arrays versus join tables

A collection is a `TEXT[]` only when its members are bare strings with no attributes and nothing
queries across them. Everything else earns a table.

| Collection | Shape | Why |
|---|---|---|
| `entities.aliases` | `TEXT[]` | Bare strings. |
| `theses.triggers` | `TEXT[]` | Bare strings. Becomes a table the moment a trigger needs fired/unfired state — see §8. |
| `instrument_mappings[]` | table | Carries symbol, market and asset class. Asset class being implicit has already caused a production bug (`commodity-signals-are-index-class`). |
| `evidence[]` | table | Rule 3 is a **count** over evidence rows, and the span columns qualify it. Neither is expressible as an array. |
| `supporting[]` / `undermining[]` | one table, `role` column | The same edge with opposite sign. |
| `members[]` | table | §3.5 requires the renderer to read a stored list. |
| `open_questions[]` | table | Parent §4.2 item 6 queries "past due", which needs a date column. |

### 3.3 Polymorphic edges

`claim_evidence` points at an event *or* an observation; `story_members` at an event *or* a claim.
Both use **two nullable FKs plus `CHECK (num_nonnulls(...) = 1)`**, not a `target_kind`/`target_id`
pair, which forfeits referential integrity — and integrity across the propagation rules is the
stated reason parent §5 chose a transactional engine at all.

### 3.4 Keys and dedup

`BIGSERIAL PRIMARY KEY` on every table except the two pure join tables, `event_entities` and
`thesis_claims`, which use composite primary keys because the pair *is* the fact.

- `outlets UNIQUE (lower(name))`.
- **`items UNIQUE (outlet_id, content_hash)`** — see below.
- `entities UNIQUE (lower(name), type)`. Per §2.2, this does *not* enforce the one-entity-per-company
  rule.
- `entity_instruments UNIQUE NULLS NOT DISTINCT (symbol, market, entity_id)`.
- `assertions UNIQUE (item_id, event_id)` — one item asserts one event once.
- `claim_evidence UNIQUE NULLS NOT DISTINCT (claim_id, event_id, observation_id)`.
- `story_members UNIQUE NULLS NOT DISTINCT (story_id, event_id, claim_id)`.
- `claims UNIQUE (ledger_id)` — nullable, NULLs distinct, so KB-native claims without a ledger
  ancestor coexist freely.
- `claims` gets no natural key on text. Claim dedup is fuzzy (`_claim_fingerprint`,
  `_is_duplicate_claim`) and stays in application code, where it already works.

**Item dedup is per-outlet, not global.** §12.3 #15 specifies a content-hash dedup key, but it
specifies it for a capture JSONL with no outlet dimension. Promoted unchanged to a shared,
outlet-attributed table it would collapse syndicated wire copy — identical Reuters text carried by
five outlets — into one row attributed to whichever outlet was ingested first, and
`assertions UNIQUE (item_id, event_id)` would then make the other four assertions unrepresentable.
Since §3.1's entire argument for splitting `outlets` out is that corroboration is per-outlet, a
global unique hash would cap any KB-native corroboration count at one. Cross-outlet identity, if
wire-copy folding is wanted later, belongs in a separate grouping column.

**`NULLS NOT DISTINCT` is load-bearing.** Under default Postgres semantics NULLs compare distinct in
a unique index, so `(claim_id=7, event_id=3, observation_id=NULL)` inserts twice and rule 3 counts
one piece of evidence as two — clearing the evidence floor that exists specifically to stop the chip
whipsaw. The same defect in `story_members` renders a duplicate line in the brief, with no read-time
dedup to catch it because §3.5 forbids rebuilding the list. Requires PG15+; the stack is on 18.

These unique indexes also serve prefix lookups (`claim_evidence` by `claim_id`, `story_members` by
`story_id`), so no separate FK index is created for either.

### 3.5 Provenance, with one correction to parent §12.2

§12.2 requires `extractor_model` and `prompt_version` on every extracted row. Applied to `events`,
`assertions`, `claims`, `links`, `entities` and **`theses`** — six tables.

**Not** on `observations`. Those are fetched from a price provider, not extracted by a model, so
they carry `provider TEXT` instead. A column named `extractor_model` holding `'yahoo'` would be a lie
in the one field that exists to make silent model drift detectable.

`theses` carries provenance but **not** `origin` (§3.6), and the two are not in tension: provenance
answers *which model wrote this row*, which is what drift detection needs for a model-authored
thesis. `origin` answers *may a propagation rule read this row as evidence*, and for a thesis the
answer is always no.

**Both provenance columns are nullable, which needs justifying** — §12.2's own table records
`reason` at 46.8% missing with the diagnosed cause "absent from `required`", so nullability is that
failure's mechanism. It is correct here for one reason only: the `bqa.4` backfill imports ledger rows
that predate provenance stamping (`brief_memory.py:78-80`), and those rows legitimately carry none.
The columns are `NOT NULL` for every KB-native write path; enforcement lives in `bqa.4` (§6) because
a `NOT NULL` constraint would reject the backfill itself.

### 3.6 `origin` placement

Parent §4.1's `extracted` / `authored` column goes on **`claims` and `links` only**.

- `claims` — as today.
- `links` — a link asserting a mechanism is often interpretation, but "shares fell on news of X" is
  source-stated. Plausible variance, so the column ships. See §9.2: this is a guess.
- **Not** on `theses` — a thesis is interpretation by definition; the column would read `authored` on
  every row, and §12.2 rates a uniform field worse than a missing one.
- **Not** on `events` — an "authored event" is fabrication, not interpretation.

---

## 4. Cross-cutting conventions

### 4.1 Two clocks

Every table in this migration carries `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` as
transaction time, **including all five join tables** (`event_entities`, `thesis_claims`,
`claim_evidence`, `story_members`, `entity_instruments`). Valid time is the domain column each
object already has:

| Table | Valid-time column |
|---|---|
| `items` | `published_at` |
| `events` | `occurred_at` |
| `assertions` | `asserted_at` |
| `observations` | `observed_at` |
| `claims` | `first_seen` |
| `stories` | `last_material_change` |
| `links` | `decay_check_date` |

**This is a convention for the new tables, not a description of the existing ones.** Of the six
shipped tables, four carry `created_at` (`users`, `job_runs`, `sources`, `preferences`) and two carry
`updated_at` instead (`settings`, `runtime_state`) — and `job_runs` shipped without it, needing
`0002` to add the column. An earlier draft claimed the convention matched all five shipped
migrations. It does not.

No generic `valid_from`/`valid_until` pair. Parent §5's bitemporal need is real, but a generic pair
would be a *second* lifecycle mechanism competing with `status`, `superseded_by`, `resolved_on` and
`decay_check_date` — unmaintained on most tables, which is the degenerate-field problem in a new
place.

### 4.2 Renames from §3.1

- **`Event.when` → `occurred_at`.** `WHEN` is reserved (`CASE ... WHEN`); the column would need
  double-quoting at every use site forever.
- **`window` → `return_window`.** `WINDOW` is likewise reserved.
- **`Observation.timestamp` → `observed_at`.** `timestamp` is *non-reserved* and legal as a column
  name, so this one is a readability call: `SELECT timestamp FROM observations` reads as a type in
  every query skimmed at 6am.
- **Ledger `broke_on` → `resolved_on`.** After the §2.1 fold, three of the six statuses are not
  breaks, and `status='confirmed', broke_on='2026-09-04'` misleads exactly the 6am reader the
  previous rename was made for. §5.1 maps the ledger key across, as it already does for
  `id → ledger_id` and `broken_by → broken_by_note` — the superset rule is about coverage, not name
  identity.

Renaming after `bqa.4` and `bqa.5` are written means touching every rule.

### 4.3 CHECK style

Follows `0003`: explicit `IN` lists, and `NULL` semantics stated rather than implied.
`CHECK (x IS NULL OR x IN (...))` where `NULL` is meaningful; plain `IN` where it is not. Where
`NULL` carries meaning the comment says what it means *and what it does not* — 0003's *"NULL means
'no vantage claim made', NOT 'neutral'"* is the model.

### 4.4 Claim text immutability

Parent §3.3 requires claim text to be immutable once `status != 'standing'`; the 2026-08-29 replay
violated it, rewriting a broken claim into a description of its own reversal.

**The primary enforcement already exists and is richer than a constraint.** `brief_memory._reaffirm`
freezes the text *and* catches a case no status-conditioned check can: an unmarked rewrite that keeps
`status='standing'` while editing the text to match new facts. `_dropped_numbers` detects the
withdrawn figures and downgrades to `challenged`. Three gold-set runs measured the model doing
exactly this on every true break it scored `standing`.

The database trigger is **defence in depth**, and its predicate must read **both** tuples:

```
IF (OLD.status <> 'standing' OR NEW.status <> 'standing')
   AND NEW.claim IS DISTINCT FROM OLD.claim
```

An earlier draft tested `OLD.status` alone, which permits the single `UPDATE` that marks a claim
broken *and* rewrites it — precisely the Patriot mechanism the section cites. §7 test 8 covers the
transition statement, not just the two steady states, because a test written to the constraint
rather than to the failure passes over exactly that gap.

Mechanically: `db.run_migrations` reads the whole file and calls `conn.execute(sql)` with no
parameters (`db.py:212`), so psycopg3 skips placeholder conversion entirely and sends the file over
the simple query protocol — the multi-statement file, the `$$` body and the un-doubled `%` in
`RAISE EXCEPTION` all survive.

The function is `claims_freeze_claim_text()`, declared **`CREATE OR REPLACE FUNCTION`** so a
down-then-up cycle does not fail on a surviving definition. `DROP TABLE` does not drop a function, so
`0006_knowledge_base_down.sql` must `DROP FUNCTION` after the tables. The five shipped down
migrations are pure `DROP TABLE` / `DROP INDEX` / `ALTER TABLE DROP COLUMN`, so there is no precedent
in the repo to copy.

### 4.5 Indexes

Sixteen indexes beyond the primary keys, each with a named consumer. Three are partial.

| Index | Consumer |
|---|---|
| `outlets_name` (unique) | outlet resolution |
| `items_outlet_hash` (unique) | per-outlet item dedup, §3.4 |
| `entities_name_type` (unique) | entity resolution |
| `entity_instruments_symbol` (unique, NULLS NOT DISTINCT) | symbol → entity mapping |
| `events_occurred` | rule 1 contradiction |
| `event_entities_entity` | rule 1 contradiction |
| `assertions_item_event` (unique) | one assertion per item/event |
| `observations_entity_observed` | rule 5, sector-relative displacement at the decay check |
| `claims_ledger_id` (unique) | `bqa.4` cutover identity |
| `claims_open_resolution` (**partial**) | rule 4 staleness |
| `claim_evidence_unique` (unique, NULLS NOT DISTINCT) | rule 3 evidence floor |
| `stories_name` (unique) | story resolution |
| `story_members_unique` (unique, NULLS NOT DISTINCT) | membership dedup |
| `open_questions_due` (**partial**) | parent §4.2 item 6 |
| `links_observation` | rule 2 unexplained observation |
| `links_decay_due` (**partial**) | rule 5 link decay |

**`entity_instruments_symbol` leads with `symbol`, not `entity_id`.** Its stated consumer is
symbol → entity resolution, and a leading `entity_id` cannot serve that lookup. The uniqueness set is
identical either way; only the prefix differs. An earlier draft named a consumer the index could not
serve.

**Rule 4's index must match rule 4's predicate.** An earlier draft indexed
`claims (resolution_date) WHERE status = 'standing'` while rule 4 wrote a *different* column, so an
expired claim kept `status='standing'` forever, never left the index, and rule 4 re-fired on it every
morning for the life of the row — breaking the repo's own `0q0.4` rule, *a guard must test the exact
predicate its consumer reads*, in the index offered as the model for it. Post-fold, rule 4's predicate
is `status IN ('standing','challenged') AND resolution_date <= today`, and the partial index matches.

**`events_occurred` is `DESC NULLS LAST`.** `occurred_at` is nullable and `DESC` defaults to
`NULLS FIRST`, which would put every undated event at the head of rule 1's most-recent scan.

### 4.6 Quarantine stays a comment

Columns with no planned reader are marked in the migration's file comments; §1.2 names the
provisional tables.

`COMMENT ON COLUMN` was considered — it survives into the live database where an operator inspects
with `\d+` — and rejected: it duplicates the file comment, and two places to state one fact is how
`the-correction-didn't-propagate` happens.

---

## 5. DDL

Sketch, not final text. Comments in the migration itself carry the reasoning, per house style.

```sql
CREATE TABLE outlets (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    kind         TEXT        NOT NULL DEFAULT 'regional'
                 CHECK (kind IN ('wire', 'analyst', 'regional', 'primary')),
    perspective  TEXT        NULL
                 CHECK (perspective IS NULL OR perspective IN (
                     'WESTERN', 'CHINESE', 'RUSSIAN', 'IRANIAN', 'ISRAELI',
                     'ARAB', 'UKRAINIAN', 'JAPANESE', 'KOREAN', 'INDIAN')),
    state_funded BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX outlets_name ON outlets (lower(name));

CREATE TABLE items (
    id           BIGSERIAL PRIMARY KEY,
    outlet_id    BIGINT      NOT NULL REFERENCES outlets(id),
    url          TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    body         TEXT        NULL,
    published_at TIMESTAMPTZ NULL,
    content_hash TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per-outlet, NOT global: syndicated wire copy is the same text from five
-- outlets, and collapsing it to one row caps corroboration at one. See 3.4.
CREATE UNIQUE INDEX items_outlet_hash ON items (outlet_id, content_hash);

CREATE TABLE entities (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT    NOT NULL,
    type            TEXT    NOT NULL
                    CHECK (type IN ('country', 'institution', 'company', 'person', 'instrument')),
    aliases         TEXT[]  NOT NULL DEFAULT '{}',
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX entities_name_type ON entities (lower(name), type);

CREATE TABLE entity_instruments (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    symbol      TEXT   NOT NULL,
    market      TEXT   NULL,
    asset_class TEXT   NOT NULL
                CHECK (asset_class IN ('equity', 'index', 'crypto', 'commodity', 'fx')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- symbol first: the consumer is symbol -> entity resolution.
CREATE UNIQUE INDEX entity_instruments_symbol
    ON entity_instruments (symbol, market, entity_id) NULLS NOT DISTINCT;

CREATE TABLE events (
    id               BIGSERIAL PRIMARY KEY,
    summary          TEXT        NOT NULL,
    occurred_at      TIMESTAMPTZ NULL,
    type             TEXT        NOT NULL
                     CHECK (type IN ('action', 'statement', 'disclosure')),
    commitment_state TEXT        NOT NULL
                     CHECK (commitment_state IN ('in_force', 'committed', 'intended', 'proposed')),
    superseded_by    BIGINT      NULL REFERENCES events(id),
    extractor_model  TEXT        NULL,
    prompt_version   INTEGER     NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX events_occurred ON events (occurred_at DESC NULLS LAST);

CREATE TABLE event_entities (
    event_id   BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id  BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX event_entities_entity ON event_entities (entity_id);

CREATE TABLE assertions (
    id                  BIGSERIAL PRIMARY KEY,
    item_id             BIGINT      NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event_id            BIGINT      NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    standing            TEXT        NOT NULL
                        CHECK (standing IN ('verified', 'official', 'reported',
                                            'attributed', 'alleged')),
    -- Nullable with NO default, deliberately: this is the one extracted enum
    -- with no worked example anywhere in the parent spec, and a NOT NULL DEFAULT
    -- is the fastest route to the degenerate outcome 12.2 warns about. Absent
    -- means "not labelled", never "independent".
    source_relationship TEXT        NULL
                        CHECK (source_relationship IS NULL OR source_relationship IN
                               ('party', 'aligned', 'independent', 'adversarial')),
    asserted_at         TIMESTAMPTZ NULL,
    extractor_model     TEXT        NULL,
    prompt_version      INTEGER     NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX assertions_item_event ON assertions (item_id, event_id);

CREATE TABLE observations (
    id            BIGSERIAL PRIMARY KEY,
    entity_id     BIGINT      NOT NULL REFERENCES entities(id),
    -- The symbol ACTUALLY queried at the provider, recorded alongside the entity
    -- rather than joined out of entity_instruments. Deliberate duplication: the
    -- AVAV_ double-underscore bug was invisible precisely because the queried
    -- symbol was never stored next to the result it produced.
    symbol        TEXT        NOT NULL,
    metric        TEXT        NOT NULL
                  CHECK (metric IN ('price', 'return', 'yield', 'volume', 'spread')),
    value         NUMERIC     NOT NULL,
    return_window TEXT        NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    provider      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((metric = 'return') = (return_window IS NOT NULL))
);
CREATE INDEX observations_entity_observed ON observations (entity_id, observed_at DESC);

CREATE TABLE claims (
    id                 BIGSERIAL PRIMARY KEY,
    -- Preserves the JSON ledger's string identity (format 'c-0001', per
    -- merge_ledger and the r"c-(\d+)$" regex in _max_id_num). merge_ledger
    -- treats an echoed id as authoritative, so a BIGSERIAL alone would silently
    -- renumber every claim the model can cite.
    ledger_id          TEXT    NULL,
    claim              TEXT    NOT NULL,
    topic              TEXT    NULL,
    status             TEXT    NOT NULL DEFAULT 'standing'
                       CHECK (status IN ('standing', 'challenged', 'broken',
                                         'confirmed', 'expired', 'withdrawn')),
    origin             TEXT    NOT NULL DEFAULT 'extracted'
                       CHECK (origin IN ('extracted', 'authored')),
    severity           TEXT    NOT NULL DEFAULT 'normal'
                       CHECK (severity IN ('low', 'normal', 'high')),
    horizon_days       INTEGER NULL CHECK (horizon_days IS NULL
                                           OR horizon_days BETWEEN 1 AND 3650),
    resolution_date    DATE    NULL,
    horizon_elapsed    INTEGER NULL,
    falsifier          TEXT    NULL,
    falsifier_kind     TEXT    NULL
                       CHECK (falsifier_kind IS NULL OR falsifier_kind IN
                              ('event_triggered', 'review_required')),
    first_seen         DATE    NOT NULL,
    last_reaffirmed    DATE    NULL,
    restate_count      INTEGER NOT NULL DEFAULT 0,
    source_count       INTEGER NULL,
    driver             TEXT    NULL,
    -- The date the claim LEFT 'standing', in either direction. Named resolved_on
    -- rather than the ledger's broke_on because three of the six statuses are not
    -- breaks; 5.1 maps the key across.
    resolved_on        DATE    NULL,
    -- Two columns, because the incumbent and the rule disagree on type.
    -- _apply_status writes free text ('unmarked rewrite: ...'); rule 1 wants the
    -- contradicting event. RESTRICT, not CASCADE: deleting the contradicting
    -- event must never silently un-break a claim.
    broken_by_note     TEXT    NULL,
    broken_by_event_id BIGINT  NULL REFERENCES events(id) ON DELETE RESTRICT,
    extractor_model    TEXT    NULL,
    prompt_version     INTEGER NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status = 'standing' OR resolved_on IS NOT NULL),
    CHECK (horizon_elapsed IS NULL OR resolved_on IS NOT NULL),
    CHECK (status <> 'broken'
           OR num_nonnulls(broken_by_note, broken_by_event_id) >= 1)
);
CREATE UNIQUE INDEX claims_ledger_id ON claims (ledger_id);
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged');

CREATE TABLE claim_evidence (
    id             BIGSERIAL PRIMARY KEY,
    claim_id       BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- RESTRICT, not CASCADE: rule 3 is a COUNT over these rows, and a floor an
    -- unrelated delete can silently lower is not a floor.
    event_id       BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    observation_id BIGINT NULL REFERENCES observations(id) ON DELETE RESTRICT,
    span_start     DATE   NULL,
    span_end       DATE   NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, observation_id) = 1)
);
CREATE UNIQUE INDEX claim_evidence_unique
    ON claim_evidence (claim_id, event_id, observation_id) NULLS NOT DISTINCT;

CREATE TABLE theses (
    id              BIGSERIAL PRIMARY KEY,
    text            TEXT    NOT NULL,
    confidence      TEXT    NOT NULL DEFAULT 'speculative'
                    CHECK (confidence IN ('speculative', 'tentative',
                                          'supported', 'established')),
    horizon_days    INTEGER NULL CHECK (horizon_days IS NULL
                                        OR horizon_days BETWEEN 1 AND 3650),
    triggers        TEXT[]  NOT NULL DEFAULT '{}',
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE thesis_claims (
    thesis_id  BIGINT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
    claim_id   BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    role       TEXT   NOT NULL CHECK (role IN ('supporting', 'undermining')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thesis_id, claim_id)
);

CREATE TABLE stories (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL,
    scope                TEXT NOT NULL CHECK (scope IN ('episodic', 'structural')),
    -- NULL last_material_change means "created, no members yet" and reads
    -- 'active'. See 2.3: the state must be defined at the default.
    state                TEXT NOT NULL DEFAULT 'active'
                         CHECK (state IN ('active', 'dormant', 'closed')),
    last_material_change TIMESTAMPTZ NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX stories_name ON stories (lower(name));

CREATE TABLE story_members (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    -- RESTRICT: 3.5 forbids rebuilding the member list, so a cascade would be a
    -- silent retcon of it -- the Day 3 failure mechanism.
    event_id   BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    claim_id   BIGINT NULL REFERENCES claims(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, claim_id) = 1)
);
CREATE UNIQUE INDEX story_members_unique
    ON story_members (story_id, event_id, claim_id) NULLS NOT DISTINCT;

CREATE TABLE open_questions (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text       TEXT   NOT NULL,
    due_date   DATE   NULL,
    status     TEXT   NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'answered', 'dropped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX open_questions_due ON open_questions (due_date) WHERE status = 'open';

CREATE TABLE links (
    id                   BIGSERIAL PRIMARY KEY,
    event_id             BIGINT  NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    observation_id       BIGINT  NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    mechanism            TEXT    NOT NULL,
    effect_kind          TEXT    NOT NULL
                         CHECK (effect_kind IN ('re_rating', 'risk_premium',
                                                'flow', 'fundamental_revision')),
    expected_persistence TEXT    NOT NULL
                         CHECK (expected_persistence IN ('session', 'days',
                                                         'weeks', 'structural')),
    -- NOT NULL: derived at write time from expected_persistence. A nullable
    -- decay date leaves a link 'unchecked' forever, and 2.3's negative case
    -- makes that permanent rather than eventually-noticed.
    decay_check_date     DATE    NOT NULL,
    falsifier            TEXT    NULL,
    status               TEXT    NOT NULL DEFAULT 'unchecked'
                         CHECK (status IN ('unchecked', 'active', 'decayed', 'refuted')),
    origin               TEXT    NOT NULL DEFAULT 'extracted'
                         CHECK (origin IN ('extracted', 'authored')),
    extractor_model      TEXT    NULL,
    prompt_version       INTEGER NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX links_observation ON links (observation_id);
CREATE INDEX links_decay_due ON links (decay_check_date) WHERE status = 'unchecked';

CREATE OR REPLACE FUNCTION claims_freeze_claim_text() RETURNS trigger AS $$
BEGIN
    IF (OLD.status <> 'standing' OR NEW.status <> 'standing')
       AND NEW.claim IS DISTINCT FROM OLD.claim THEN
        RAISE EXCEPTION 'claim text is immutable once status leaves standing (id %)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claims_freeze_claim_text_trg
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_freeze_claim_text();
```

Links are event → observation only. Event → event links are deliberately out of scope: nothing in
parent §3.4 or the five rules needs them, and rule 2 ("an Observation with no explaining Link") is a
clean `LEFT JOIN ... WHERE links.id IS NULL` under this shape.

`0006_knowledge_base_down.sql` drops the sixteen tables in reverse dependency order, then
`DROP FUNCTION claims_freeze_claim_text()`. The function outlives `DROP TABLE` and is the half that
actually needs the explicit drop.

### 5.1 The ledger superset, column by column

All eighteen keys `brief_memory.merge_ledger` writes:

| Ledger key | Column | Note |
|---|---|---|
| `id` | `ledger_id` | format `c-0001` |
| `claim` | `claim` | |
| `topic` | `topic` | |
| `first_seen` | `first_seen` | string → `DATE` |
| `last_reaffirmed` | `last_reaffirmed` | string → `DATE` |
| `restate_count` | `restate_count` | |
| `source_count` | `source_count` | |
| `severity` | `severity` | |
| `origin` | `origin` | |
| `driver` | `driver` | |
| `horizon_days` | `horizon_days` | |
| `resolution_date` | `resolution_date` | string → `DATE` |
| `horizon_elapsed` | `horizon_elapsed` | |
| `status` | `status` | **enum widens — see §6** |
| `broke_on` | `resolved_on` | string → `DATE` |
| `broken_by` | `broken_by_note` | |
| `extractor_model` | `extractor_model` | |
| `prompt_version` | `prompt_version` | |

`kind` is deliberately absent: it is an admission guard, not stored on the row, and everything
surviving the guard is a claim — so the column would be uniform on every read, which §12.2 rates
worse than a missing one.

---

## 6. The cutover contract

Tracked as `news-brief-bqa.9`. These are the obligations this schema places on `bqa.4`. They were
scattered across four sections in earlier drafts, and scattering them is what let the first item
survive two red-team passes.

**1. Widen `_VALID_STATUS` and re-derive the predicates that depend on it.** This is the dangerous
one. `brief_memory._VALID_STATUS` is `frozenset({"standing","challenged","broken"})`
(`brief_memory.py:34`) and `_coerce_status` returns `None` outside it. Against the six-value enum:

| Site | Effect on a `confirmed` / `expired` / `withdrawn` row |
|---|---|
| TTL filter, `:350` | coerces to `standing`, fails the keep-forever disjunct, **deleted after 7 days** |
| `select_working_set`, `:385` | excludes only `broken`, so it **renders as live fact** in the ESTABLISHED block |
| `_apply_status`, `:651` | a model reply omitting status **resets a terminal row to `standing`** |

So §2.3's "terminal" holds in the schema and not in the incumbent reader. `bqa.4` must widen the
frozenset and change the TTL and render predicates from `!= 'standing'` to an explicit terminal-state
set. Until it does, the fold destroys the accountability record this epic exists to build.

**2. Marshal dates in both directions.** Ledger `first_seen`, `last_reaffirmed`, `resolution_date`
and `broke_on` are `"%Y-%m-%d"` strings; the columns are `DATE`. `merge_ledger` re-reads
`c["last_reaffirmed"]` positionally in the TTL filter (`:351`), so a one-way cast is not enough.

**3. Parse `c-0001` for the `ledger_id` backfill** — the format is `f"c-{n:04d}"` (`:322`), matched
by `r"c-(\d+)$"` (`:129`).

**4. Backfill provenance and enforce it thereafter.** `extractor_model` / `prompt_version` are
nullable only to accept pre-provenance ledger rows (§3.5). Every KB-native write path must set them.

**5. Enforce the one-entity-per-company rule** (§2.2). No unique key can express it.

**6. `sources.outlet_id`** — the FK from the subscription table to `outlets`, with its backfill.

**Also unassigned until now: parent §3.2's horizon guard.** *"A Claim with a horizon beyond 90 days
requires at least one input event at `in_force` or `committed`."* That is a named guard closing a
named §1.1 failure — the Patriot claim, `intended` + `alleged` at a 6–12 month horizon. It spans
`claims`, `claim_evidence` and `events`, so it is not a CHECK candidate. **It belongs to `bqa.5` with
the propagation rules**, and is recorded here because it was about to fall between two specs.

---

## 7. Testing

Extends `tests/test_db.py`.

1. **`test_up_creates_the_expected_tables` changes two assertions, not one** — it asserts the table
   set *and* `applied == [<exact version strings>]`.
2. **Idempotency is covered; the down round-trip is not.** `test_up_is_idempotent` uses the real
   migrations directory. But `test_down_defaults_to_exactly_one_step`,
   `test_rolling_back_everything_requires_steps_zero` and `test_down_restores_the_prior_schema` all
   run under the `two_migrations` fixture, which monkeypatches `db.MIGRATIONS_DIR` to a temp
   directory holding only `0001` plus a throwaway `0002`. The fixture docstring says this is
   deliberate and the reasoning is sound. The consequence is that **no down-migration file in this
   repo has ever been executed by a test**, and `0006`'s down drops sixteen tables, a trigger and a
   function.
3. **New: `test_0006_down_then_up_against_the_real_directory`.** Migrate up, roll back one step,
   assert the sixteen tables are gone **and `pg_proc` holds no `claims_freeze_claim_text`**, then
   migrate up again. The second `up` catches a `CREATE FUNCTION` that should have been
   `CREATE OR REPLACE`; the `pg_proc` assertion catches the orphan the first step would leave.
4. **One rejection test per CHECK constraint**, including `horizon_days` at `0` and `-1`, and the
   `metric`/`return_window` biconditional in both directions.
5. **Both polymorphic CHECKs in both directions** — neither FK set must fail, *and* both set must
   fail.
6. **Both `NULLS NOT DISTINCT` unique keys with a real double insert.** Insert the same
   `(claim_id, event_id, NULL)` twice; the second must raise. Without this the evidence floor
   silently double-counts.
7. **The claim invariants:** a non-`standing` row without `resolved_on` must fail; `horizon_elapsed`
   without `resolved_on` must fail; `status='broken'` with neither `broken_by_*` must fail.
8. **The trigger on three cases, not two:** editing `claim` on an already-`broken` row must raise;
   editing it on a `standing` row must succeed; and **the single `UPDATE` that sets
   `status='broken'` and rewrites `claim` in one statement must raise.** The third is the Patriot
   mechanism and the one a draft written to the constraint rather than to the failure misses.
9. **The ledger superset**, mechanically, per §5.1 — *and its read direction*: every value the
   `status` CHECK permits must survive `brief_memory._coerce_status` without degrading. The
   write-direction map alone is `the-probe-measured-the-wrong-layer`; it is what let §6 item 1
   through two reviews.
10. **The provisional tables are empty.** Asserts §1.2's licence still holds. Expected to fail when
    `bqa.4` lands, which is the point.

Gate before push, with the database up — `pytest` alone reports green with the whole DB layer
skipped, and a skip is not a pass:

```bash
ruff check .
ruff format --check .
pytest -q                      # with DATABASE_URL exported
pytest tests/test_db.py -q     # must report runs, not "skipped"
```

`Dockerfile:36` is `COPY migrations/ ./migrations/`, a directory copy, and the workflow already
watches `migrations/**`. No allowlist edit is needed — unlike the per-file `COPY` on line 35.

---

## 8. Deferred, with reasons

| Item | Why deferred | Where |
|---|---|---|
| `pgvector` | `postgres:18-alpine` does not bundle it; enabling means an image swap plus a compose edit against a file already drifted behind the host's. A `vector(N)` column also pins an embedding dimension into DDL, cutting against parent §6.2. | `news-brief-bqa.7` |
| Ledger data migration and reader cutover | The morning brief must not run against a half-migrated store | `bqa.4` / §6 |
| Parent §3.2's >90-day horizon guard | Spans three tables; not a CHECK candidate | `bqa.5` / §6 |
| `theses.triggers` as a table | `TEXT[]` is right until a trigger needs fired/unfired state. The most likely first amendment to this schema. | Epic 6 |
| Event → event links | Nothing in parent §3.4 or the five rules needs them | Unscheduled |

**Sequencing trap for whoever adds the `stories.state` staleness window (§9.3):** per
`env-var-needs-compose-passthrough`, the compose anchor *seeds* the settings row on first boot. The
`KNOBS` entry and the anchor line must land in the **same** change — adding the entry first freezes a
default into a row, and adding the anchor line later fixes nothing.

**What would make this migration a permanent mistake?** An un-runnable `down` on the largest
migration in the repo, discovered during an incident. §7 test 3 is the only thing standing between
this spec and that outcome, and it is the test most likely to be dropped under time pressure. It is
not optional.

---

## 9. Open

1. **`source_relationship` has no anchor.** The only extracted enum with no worked example in the
   parent spec; its four values were reasoned from purpose, not labelled data — `severity`'s exact
   provenance. It ships nullable, quarantined, and first in line for a variance check
   (`news-brief-bqa.8`).
2. **`links.origin` variance is a guess.** If it comes back uniform, drop the column — §12.2's own
   rule applied to a column this spec added.
3. **The staleness window for `stories.state`** is not set here. It is a knob: a `KNOBS` entry plus a
   compose anchor line, in one change. See §8.
4. **`severity` owes a rubric** (§2.2) before any new consumer reads it.

---

## 10. What the red-team passes changed

Two fresh-context passes found forty-odd defects between them. The ones that changed the design:

**First pass.** `resolved_outcome` folded into `status` (two enums with `broken` in both, and rule 4's
`stale` state homeless). Rule 4's partial index tested a predicate its consumer could never exit.
`claim_evidence` and `story_members` had no unique key, so the rule whose job is refusing to
double-count was built on a table permitting it. `severity` was absent from the field table while
shipping in the DDL. The `confidence` ladder's first two rungs overlapped. The trigger had no
rollback path. And six defects in `claims` came from never reading the live ledger row.

**Second pass.** The fold's own consequences: `confirmed`/`expired`/`withdrawn` silently degrade to
`standing` in `_coerce_status` and are then deleted by the TTL (§6 item 1 — the most serious defect
in any draft). The `broke_on` name became a lie for three of six statuses, hence `resolved_on`. The
trigger read `OLD.status` alone, permitting break-and-rewrite in one `UPDATE` — the exact Patriot
mechanism. `items UNIQUE (content_hash)` was global, collapsing syndicated wire copy. `ON DELETE
CASCADE` made the evidence floor silently mutable. `stories.state` and `links.status` were undefined
at their defaults — the same defect the first pass fixed in `confidence`. And parent §3.2's horizon
guard was about to fall between two specs.

**Two of its findings were not accepted as stated.** It claimed `_corroboration_cue` counts distinct
outlets; it reads `source_count`, a model-reported integer maxed across restatements — the item-dedup
defect is real, the cited mechanism was not. And it listed `stories` as unable to use §1.2's licence;
`stories` is referenced only by `open_questions` and `story_members`, both provisional, which is the
direction the same report correctly rules fine for `claims`. Only `observations` fails that test.

Both passes recommended cutting eight tables. Declined — §1.3 scores the alternative — but the first
pass correctly identified that the quarantine rule was applied to columns and never to tables, which
§1.2 now fixes.

# Claim ledger cutover — design

**Issue:** `news-brief-bqa.10` (`bqa.4a`). **Date:** 2026-09-02.
**Parents:** `docs/superpowers/specs/2026-08-29-knowledge-base-architecture-design.md` (the KB
direction), `docs/superpowers/specs/2026-09-02-kb-schema-ddl-design.md` (migration 0006 and the
cutover contract).

**Revision 3.** Revisions 1 and 2 put the retention and ordering predicates in SQL. Two
fresh-context red-team passes found six defects, four of them in that translation, and the last
pass proposed the design below instead. §10 records why, because the reasoning generalises.

---

## 1. Scope

`brief_memory`'s JSON claim ledger becomes rows in the `claims` table built by migration 0006.
This is the first increment that makes the knowledge base non-empty, and it is what unblocks
Epic 4's "render the brief as a KB query".

**In one line: swap the persistence, change nothing else.** `merge_ledger` and
`select_working_set` run unmodified, on the same dicts, with the same arithmetic.

### 1.1 Why this is its own issue

`news-brief-bqa.4` cited "spec section 6", and **both parent specs have a section 6 describing
different subsystems**: the architecture spec's §6 is models, tiers, micro-batching and adapter
capabilities; the DDL spec's §6 is the cutover contract. The issue carried two subsystems sharing
almost no code, and was split on 2026-09-02. This document is the cutover; `bqa.4` keeps the
comprehension pipeline as `bqa.4b`. The architecture spec anticipated this — §12.4 item 2 says the
work "is roughly four sub-projects … each wanting its own plan."

### 1.2 What this deliberately does not touch

- **No new model calls, tiers, prompts or adapters.** The daily reconcile call is unchanged, and
  the extraction prompt still offers three statuses — `confirmed`/`expired`/`withdrawn` are
  written by the propagation rules (`bqa.5`), never self-declared.
- **No writes to any provisional table**, preserving DDL §1.2's reshaping licence for `events`,
  `stories` and `links`.
- **No capture dependency.** `bqa.4b` reads `captured_items`, which `b42.1` has not built.
- **No retention, ordering or staleness logic in SQL.** See §2.4.

### 1.3 Cutover contract obligations

| # | Obligation | Where |
|---|---|---|
| 1 | Widen `_VALID_STATUS`, split the TTL and render predicates | **Done** — `b097a77` |
| 2 | Marshal dates in both directions | §5 |
| 3 | Parse `c-0001` for the `ledger_id` backfill | §3.2 |
| 4 | Backfill provenance and enforce it thereafter | §5 |
| 5 | Enforce one-entity-per-company | **`bqa.4b`** — acts on `entities` |
| 6 | `sources.outlet_id` and its backfill | **`bqa.4b`** — acts on `outlets` |

---

## 2. Decisions

### 2.1 Retirement never deletes a row

The TTL currently deletes aged-out standing claims. In Postgres that becomes a real `DELETE`, and
the three foreign keys pointing at `claims` disagree about what that means:

| Referencing table | On delete | Consequence of a TTL sweep |
|---|---|---|
| `claim_evidence` (`0006:223`) | `CASCADE` | evidence silently destroyed with the claim |
| `thesis_claims` (`0006:306`) | `CASCADE` | thesis membership silently destroyed |
| `story_members` (`0006:339`) | `RESTRICT` | the delete **raises** |

Silently destructive against two tables, a hard error against the third, depending on what the
claim happens to be attached to. Invisible today because all three are empty; a production failure
the day `bqa.4b` or `bqa.5` fills them.

### 2.2 Retirement is a `retired_on` date — not a status, and not derived

**Decision: a new `retired_on DATE NULL` column on `claims`, in migration `0007`.** The store
stamps it when the merge drops a row; `load_ledger` returns only rows where it is NULL.

Two alternatives were designed in full and rejected.

**Writing `status = 'expired'` overloads the enum and creates a resurrection bug.** Rule 4 writes
`expired` to mean *the claim passed its own horizon without resolving* — a lifecycle outcome —
whereas TTL-silence means *nobody restated it for seven days*, an absence of evidence. Worse,
`_find_duplicate` (`brief_memory.py:195`) matches on text alone with no status filter, so a
restated fact would fold back into the terminal row, `_apply_status` would correctly refuse the
return to `standing`, and **the fact would become permanently unrenderable.**

**Deriving staleness at read time recreates a bug migration 0006 exists to prevent.** A stale
claim would keep `status = 'standing'` forever and so would never leave the
`claims_open_resolution` index. `0006:215-218` names that exact failure:

> *Rule 4's predicate exactly: a claim that expires LEAVES this index. An earlier draft indexed
> `WHERE status = 'standing'` while rule 4 wrote a different column, so an expired claim never
> left and rule 4 re-fired on it every morning for the life of the row.*

Rule 4 would re-fire on every stale claim every morning, over a set that grows without bound.

`retired_on` avoids both: it is a separate column, so the status enum is untouched and rule 4's
meaning of `expired` is preserved; and retired rows are filtered out of the working ledger
entirely, so nothing can match, resurrect or re-fire on them.

**`0007` also recreates `claims_open_resolution` with `AND retired_on IS NULL`.** Leaving that as
an obligation on `bqa.5` would be exactly the cross-issue promise that gets forgotten; the schema
enforces it instead.

### 2.3 Behaviour is preserved exactly, and the change is two-way

Because `load_ledger` filters retired rows, **the dict handed to `merge_ledger` is precisely what
the JSON file contains today.** Every consequence follows from that:

- A restatement of a retired fact cannot match it in dedup, so it mints a **new claim** with a
  fresh `first_seen` and `restate_count` of 1 — today's behaviour exactly.
- `first_seen`, `restate_count` and `horizon_elapsed` keep their current meanings.
- `select_working_set`, `_ttl_bonus` and `_severity_rank` are untouched Python.
- **`merge_ledger` keeps its TTL filter exactly as it is.** It still drops the aged-out row
  from the dict it returns; what changes is only the store's interpretation of that drop —
  `retired_on`, not `DELETE`. This is why `test_merge_retires_stale_claims` (`:135`),
  `test_high_severity_retires_after_14_days` (`:761`),
  `test_normal_severity_still_retires_at_7_days` (`:780`) and
  `test_a_standing_claim_still_retires_on_ttl` (`:1156`) all keep passing unmodified. A
  reader who assumed the filter was removed would delete them and lose the retention net.

**Reversibility is two-way on both halves.** The JSON file is never written to or deleted, so
rollback is pointing the loader back at it. And `retired_on` *records the retirement decision that
today's TTL makes and throws away*, so the two worlds stay reconcilable rather than diverging at
the first post-retirement restatement.

**Re-admission is deliberately not implemented.** A retired row is never un-retired; a restatement
mints a new claim, as today. Clearing `retired_on` would force a fresh decision about whether
`first_seen` and `horizon_elapsed` reset, and that decision belongs with whoever needs the feature,
not here.

### 2.4 Persistence only: no predicates in SQL

**Decision: `claim_store` owns rows, marshalling and id allocation. It owns no business logic.**

Revisions 1 and 2 moved the render window into SQL so that "consumers query `claims` directly".
Two review passes found that the translation had dropped the `challenged` carve-out (reintroducing
the jx9.6 failure the constants were built to prevent), inverted NULL `last_reaffirmed` handling in
both the filter and the sort, referenced a `severity_rank` column that does not exist, and shifted
every claim's visibility by a day. Four defects, one translation step, and a purpose-built
equivalence test needed to guard the rest.

The SQL was written to serve Epic 4, which is **not yet specified** — so its query shape was a
guess, and the guess cost four defects. `claim_store.py` still exists and still owns every line of
SQL in this change; what is deferred is putting *logic* in it, until Epic 4 states what it asks
for. Adding query paths later is an addition, not a migration.

The cost, stated: `load_ledger` reads every live row each day. At 25 rows — and at ten times that —
this is a sub-millisecond read, and treating it as a performance concern would be the appearance of
an argument rather than one.

### 2.5 Degradation is loud to the operator AND to the reader

`render_established_block` returns an empty string when it has nothing, which removes the section
entirely — so a database outage and a genuinely empty ledger are **byte-identical to the reader**.
The brief silently loses its memory, re-explains yesterday's facts, and nothing says why.

**Decision:** the reconcile write is skipped for the day; the run row records which gate fired,
with counts; and the block position carries one line saying the background is unavailable this run.

Worth recording that the cutover is strictly safer than today in one respect. `load_ledger`
currently returns an empty dict on a corrupt file, `merge_ledger` then produces only today's
claims, and `save_ledger` atomically overwrites — so **a single unparseable byte silently resets
the entire accountability record.** Upsert semantics cannot do that.

---

## 3. Module boundary

A new top-level `claim_store.py` is **the only module that knows SQL, or that the ledger's
`broke_on` is the column `resolved_on`.**

| Function | Purpose |
|---|---|
| `load_ledger(conn)` | Every row with `retired_on IS NULL`, in today's dict shape |
| `save_ledger(conn, before, after)` | Diff; upsert changed rows; stamp `retired_on` on rows that left |
| `next_ledger_num(conn)` | Id allocation across **all** rows — see §3.2 |
| `import_legacy(conn, path)` | The one-shot guarded backfill |

`brief_memory` keeps only transformation logic. Note `save_ledger` takes both states: "which rows
were retired" is `before` minus `after`, and the TTL filter is the only way a row leaves
`merge_ledger`, so the diff is exactly the retirement set.

### 3.1 Writes are per-row transactions

Following `config.py:605-613`: each row in its own `with conn.transaction()`, so one row the schema
rejects cannot cost the operator the rest of the day's claims. A single transaction around the
whole batch would make one bad row discard every good one.

### 3.2 Id allocation must span retired rows

`merge_ledger` computes new ids as `_max_id_num(prior) + 1`. Since `load_ledger` filters retired
rows out of `prior`, a retired `c-0050` with a surviving maximum of `c-0049` would cause the next
new claim to be assigned **`c-0050`**.

That is not a clash that fails loudly. An upsert keyed `ON CONFLICT (ledger_id)` would **overwrite
the retired row**, handing an unrelated new claim the retired one's `first_seen`, `restate_count`
and history — silently rewriting the accountability record this epic exists to build.

**Resolution:** `next_ledger_num(conn)` computes `max(substring(ledger_id from 'c-(\d+)')::int) + 1`
over **all** rows, retired included (obligation 3). `merge_ledger` gains a keyword-only
`next_num=None` parameter defaulting to `_max_id_num(prior) + 1`, so every existing call and test
is unchanged and production passes the store's value. The function stays pure.

### 3.3 Deployment

`claim_store.py` is a new **top-level module**: it needs the Dockerfile `COPY` allowlist and both
workflow path lists, or CI passes against a full checkout and production raises
`ModuleNotFoundError`.

### 3.4 Two invariants `load_ledger` must uphold

The dict it returns is consumed by Python that was written against a JSON file, and it makes
two assumptions the file happened to satisfy and a database does not.

**`last_reaffirmed` must be a non-NULL `%Y-%m-%d` string.** The column is `DATE NULL`
(`0006:194`), but `merge_ledger` indexes it directly in both the TTL filter and the sort key
(`brief_memory.py:368`, `:375`), and `select_working_set` compares it against `""`
(`:410`) — `c.get("last_reaffirmed", "")` returns `None` when the key is *present and null*,
which then raises `TypeError: '<' not supported between 'NoneType' and 'str'`. "Sorts last"
only holds for a *missing* key, never a null one. The importer therefore defaults a missing
`last_reaffirmed` to `first_seen`, and the store treats a NULL read back from the column as a
hard error rather than passing it through. The column stays nullable for KB-native rows that
`bqa.4b` may add; ledger rows may not use that latitude.

**Row order must be deterministic, because both sorts are stable.** `list.sort(reverse=True)`
does *not* reverse equal elements, so ties in `merge_ledger` (`:374`) and
`select_working_set` (`:405`) break by **input order**. Today that is the order
`merge_ledger` last wrote to the file; from a database it would be whatever the planner
returned, so two claims tied on severity and date could swap places between runs. `load_ledger`
therefore orders by `severity_rank DESC, last_reaffirmed DESC, ledger_id`, reproducing the
stored order and making the tie-break explicit rather than incidental.

---

## 4. Data flow

`render_established_block` runs at `brief.py:3110` and `reconcile_ledger` not until `:3208`, so the
render reads yesterday's stored state and never sees today's merge.

**Read.** `render_established_block(select_working_set(load_ledger(conn)))` — unchanged Python over
a dict that now comes from Postgres.

**Write.** `before = load_ledger(conn)`; run the pure merge; `save_ledger(conn, before, after)`
upserts changed rows and stamps `retired_on` on the difference. Rows the model did not touch and
the TTL did not drop are never written.

---

## 5. The backfill

`claim_store.import_legacy(conn)` joins the four existing importers at `supervisor.py:401`, inside
the same `try` — fail-closed on work, fail-open on the bot, like its siblings.

It follows `config.import_sources_from_file` (`config.py:568`): advisory lock, empty-table guard,
per-row transaction, and a malformed file that imports nothing and logs rather than stopping a
boot. The property that makes it safe is the one that importer's docstring names — **the file is
never deleted, so rollback is "keep reading the file", not "restore a backup".**

The guard reads the **table**, not the file: the ledger is a dict with a `claims` list rather than
a bare list, so emptiness is `SELECT 1 FROM claims LIMIT 1`, and a file containing `{"claims": []}`
must not be mistaken for "already imported".

### 5.1 Absent keys are the norm

Measured against `from-server/brief_memory.json` (25 rows, 2026-08-29): rows carry **only eight
keys** — `id`, `claim`, `topic`, `first_seen`, `last_reaffirmed`, `restate_count`, `source_count`,
`severity`. `status` is absent on all 25, as are `origin`, `driver`, `horizon_days`,
`extractor_model` and `prompt_version`.

So the sibling importers' `entry["name"]` shape would fail on every row. Every optional key is read
with a default and left to the column default. `extractor_model` and `prompt_version` import as
NULL, honestly recording an unknown extractor — which is why DDL §3.5 made them nullable — and
every write thereafter sets them. Dates marshal `%Y-%m-%d` ↔ `DATE` in both directions
(obligation 2), with `broke_on` → `resolved_on`.

**The probe's limit:** that snapshot predates Epic 1's `status` work. It is evidence about *a*
ledger, not proof about the live one, which is on the deploy host. No decision depends on which
shape is live, and §5.2 makes the importer report what it found.

### 5.2 Pre-registered counts, and a named variance

Read the file, compute the expected row count and status histogram, import, read back, compare. A
mismatch means rows were individually rejected by a CHECK and skipped, which is otherwise silent.
The import logs the key coverage and status histogram it observed, so the live ledger's real shape
is read off the first boot rather than assumed.

One live hazard: `CHECK (status = 'standing' OR resolved_on IS NOT NULL)` (`0006:209`) rejects a
non-`standing` row with no `broke_on`. Such rows can exist, because `_apply_status` only began
stamping `broke_on` when `jx9.x` shipped. They import with `resolved_on = last_reaffirmed`, logged
individually and counted as a named variance — an approximate date that is documented beats an
exact one that is invented, and beats a row silently dropped.

---

## 6. Testing

Because no logic moves, **`tests/test_brief_memory.py`'s 247 tests are the regression net for
behaviour, and they keep passing untouched** — with three exceptions. `test_load_missing_returns_
empty` (`:29`), `test_load_corrupt_returns_empty` (`:35`) and `test_save_then_load_roundtrip`
(`:53`) call `load_ledger(path)` / `save_ledger(ledger, p)` with a `Path`; they are rewritten
against the store, the corrupt-file and missing-file cases becoming the unreachable-database and
empty-table cases.

No equivalence test is needed, because there is no second implementation to be equivalent to.

New DB-gated `tests/test_claim_store.py`:

- **Round trip.** Every one of DDL §5.1's eighteen ledger keys survives write-then-read
  undegraded, both directions, dates included (obligation 2).
- **Id allocation across retired rows** (§3.2). Retire `c-0050`, leave `c-0049` as the surviving
  maximum, mint a new claim, and assert it is **not** `c-0050` and that the retired row's
  `first_seen` is intact. This is the test that stops a silent overwrite of the accountability
  record.
- **Retirement.** A row dropped by the TTL gets `retired_on` stamped rather than deleted; it
  disappears from `load_ledger`; a restatement of its text mints a new claim rather than matching
  it; a retired row is absent from `claims_open_resolution`.
- **`save_ledger` writes only what changed**, and leaves untouched rows untouched.
- **Import.** Idempotent on a second run; a malformed file imports nothing and does not raise; a
  file of `{"claims": []}` against a populated table; a row carrying only the eight measured keys;
  a non-`standing` row with no `broke_on`; the count-and-histogram comparison firing on a
  deliberately rejected row.
- **Degraded render** produces the §2.5 notice, not an empty section.

Absolute expected counts are pre-registered in the implementation plan, not deltas.

---

## 7. Migration 0007

- `ALTER TABLE claims ADD COLUMN retired_on DATE NULL`
- `DROP INDEX claims_open_resolution` and recreate it as
  `... WHERE status IN ('standing','challenged') AND retired_on IS NULL` (§2.2)
- A partial index supporting `load_ledger`'s `WHERE retired_on IS NULL`
- A down migration, exercised by a test — per the precedent set for 0006

`0006` is not edited. It is committed and tested, and a schema change belongs in its own numbered
migration whether or not the previous one has reached production.

---

## 8. Reversibility

| Change | Direction | Rollback |
|---|---|---|
| Persistence swap | **Two-way** | Point the loader back at the JSON file; it is never written or deleted, and the empty-table guard means re-import cannot double rows |
| Retirement as `retired_on` | **Two-way** | The column records the decision today's TTL discards; retired rows remain fully reconstructible |

---

## 9. Open

1. **`news-brief-6wc`** — retired rows accumulate. They are now *marked*, so a future purge can
   target them precisely, which is strictly better than the current position; the bound itself is
   still undesigned.
2. **Epic 4's query paths.** Deferred by §2.4, to be added when Epic 4 states its shape.
3. **Re-admission of a retired claim** (§2.3), left to whoever needs it.
4. **The live ledger's field coverage**, unknown until the importer reports it on first boot.

---

## 10. What two red-team passes changed

Six defects were found across revisions 1 and 2. **Four came from one step: translating a Python
predicate into SQL.**

The sharpest: `brief_memory.py:47-50` carries a comment, written the same day, warning that
retention must not be written against the terminal set because *a challenge that leaves storage can
never resolve*. The SQL then made exactly that collapse. **A rule stated correctly in one language
does not survive translation into another for free, and the author of the rule is the least able to
notice — they read the SQL and see the intent they meant to encode.**

The second: derived staleness recreated the rule-4 re-firing bug that `0006:215-218` was written to
prevent. Both failures share a shape — a constraint recorded in one artifact, violated by a
decision taken in another, where each artifact is internally coherent.

The design that survived is the one that **removes the translation step entirely**. It was proposed
in answer to "is there a simpler design that gets 80% of the value", and it turned out to deliver
100% of the deliverable — `claims` non-empty, Epic 4 unblocked, obligations 2/3/4 discharged —
while eliminating the defect class, the equivalence test that guarded it, and a one-way change to
the accountability record. The speculative half was the expensive half.

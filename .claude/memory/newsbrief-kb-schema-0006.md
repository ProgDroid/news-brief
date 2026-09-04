---
name: newsbrief-kb-schema-0006
description: "Epic 3 SHIPPED 2026-09-02 — migration 0006 (the KB's 16 tables) AND bqa.10, the claim-ledger cutover into Postgres with migration 0007. Read before touching the KB schema or claim_store.py. The strict-xfail is GONE; retirement is a retired_on date, not a DELETE."
metadata: 
  node_type: memory
  type: project
  originSessionId: 16477d45-1807-43f8-9742-e61da567fe06
  modified: 2026-09-02T15:19:45.726Z
---

**2026-09-02. `news-brief-bqa.3` CLOSED.** Migration `0006_knowledge_base`: 16 tables, 16 indexes,
one plpgsql trigger, 60 constraint tests. Suite 1232 → 1291 passed + 1 xfailed, then **1313
passed / 0 xfailed** once `bqa.9` item 1 landed. **13 commits `9c7f453..b097a77` on `main`, NOT
pushed** as of 2026-09-02 (pushing publishes the image and applies the migration to production on
the next host restart — see [[live-state-on-deploy-host]]).

**2026-09-02, later the same day: `bqa.10` (the cutover) CLOSED.** The ledger now lives in
`claims`. Migration `0007` adds `retired_on DATE NULL`, and **retirement is a stamped date,
never a DELETE** — the three FKs into `claims` disagree about what a delete means (`CASCADE`
on `claim_evidence`/`thesis_claims`, `RESTRICT` on `story_members`), so one TTL sweep would be
silently destructive against two tables and a hard error against the third. `claim_store.py`
owns all SQL, marshalling and id allocation; **`merge_ledger` and `select_working_set` run
UNMODIFIED** on the same dicts, which is what let ~250 existing tests stay the regression net.
7 commits `5caee2a..696a829`, suite 1313 → **1351**, 0 skipped. Design:
`docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md`.

Three things that will bite whoever touches this next:

- **`next_ledger_num` deliberately has NO `WHERE`** — it counts retired rows. Adding one
  reissues a retired `ledger_id`, and the upsert's `ON CONFLICT (ledger_id) DO UPDATE` then
  OVERWRITES the retired row, handing a new claim the old one's `first_seen` and history.
  Silently. No error.
- **`load_ledger` treats a NULL `last_reaffirmed` as a hard error.** The column is nullable for
  KB-native rows, but `merge_ledger` indexes that key directly and `select_working_set`
  compares it to `""`, so a null reaches a sort as `None` and raises `TypeError` far from the
  cause. `bqa.4b` inherits this.
- **`_apply_status` stamps `UNATTRIBUTED_BREAK`** when a claim goes `broken` with no
  `broken_by`. Without it the 0006 CHECK refuses the row, `save_ledger` swallows it, and the
  brief renders a known-broken fact as established forever. Full write-up:
  `learnings/probe-failures.md` §23.

Spec `docs/superpowers/specs/2026-09-02-kb-schema-ddl-design.md`, plan
`docs/superpowers/plans/2026-09-02-kb-schema-ddl.md`, both committed. Read the spec before
touching the schema — it argues every column. Parent context: [[newsbrief-kb-architecture-2026-08-29]].

## The strict-xfail is GONE — it worked, and it was removed on purpose

**Superseded 2026-09-02 by `b097a77` (`bqa.9` item 1).** This section used to say "do not remove
the marker"; that instruction is spent. The tripwire fired exactly as designed, and the commit that
fixed the defect removed it. **Do not go looking for the xfail, and do not re-add one.**

What landed: `_VALID_STATUS` widened to the six values, and the two predicates written as
`!= standing` split into **two sets, not one** — `_TERMINAL_STATUS` (= the tuple in
`claims_freeze_claim_text()`) for the render exclusion, and `_TTL_EXEMPT_STATUS` (= the six minus
`standing`) for retention. **They are not interchangeable**: writing the TTL against the terminal
set would age `challenged` rows out, and a challenge that leaves storage can never resolve — the
`jx9.6` failure. `_apply_status` also now refuses a transition off a terminal status, mirroring the
trigger in the place `0006` names as the PRIMARY enforcement.

Replacing the marker: the test's hardcoded six-value constant is gone, read off `pg_constraint`
instead, and a new `test_the_status_check_and_valid_status_are_the_same_set` asserts the CHECK
equals `_VALID_STATUS`. **CI provisions Postgres and exports `DATABASE_URL`**, so this cross-layer
guard actually runs there — it is not local-only.

**The measured reason both tests exist:** under a positive control adding a 7th value to
`_VALID_STATUS`, the coercion test still **passed** and only the equality test failed. Coercion
catches a value the DDL gains; only set-equality catches one `brief_memory` gains. One test would
have covered one direction and read as if it covered both.

## Follow-ups filed, all open

- **`bqa.9` (P1) — the cutover contract. Item 1 DONE (`b097a77`); items 2-5 STILL OPEN.** They
  are date marshalling in both directions, `c-0001` `ledger_id` parsing, `extractor_model` /
  `prompt_version` backfill, and one-entity-per-company. Each constrains a migration `bqa.4` has
  not written, which is why they could not close with item 1. Read spec §6, not the sections —
  the obligations were scattered across four of them in earlier drafts, and that scattering is
  exactly how the `_VALID_STATUS` defect survived two full reviews.
- **`bqa.8` (P2)** — gold-set variance checks for the three quarantined enums:
  `assertions.source_relationship` (no anchor anywhere in the parent spec — `severity`'s exact
  provenance), `links.origin` (drop the column if it comes back uniform), and `claims.severity`
  (measured degenerate at high 25/25, survives only because `_ttl_bonus` and `_severity_rank` are
  live consumers — owes a rubric before any NEW consumer reads it).
- **`bqa.7` (P3)** — pgvector. `postgres:18-alpine` does not bundle it; enabling needs an image
  swap plus a compose edit, and a `vector(N)` column would pin an embedding dimension into DDL.

## Design decisions you should not re-litigate

- **All ten §3.1 objects got tables**, `Thesis` included. Two red-team passes each recommended
  cutting to eight; declined with the scoring in spec §1.3. The deciding argument is that `bqa.5`
  writes all five propagation rules in one epic, so cutting relocates churn into the middle of an
  implementation rather than removing it.
- **Eight tables are PROVISIONAL** (spec §1.2): empty, read by nothing, reshapeable without
  ceremony **while empty AND not referenced by a non-provisional table**. `observations` fails the
  second test (`claim_evidence` references it), so reshaping it is an ordinary breaking change.
- **Source is `outlets`, NOT `sources`.** `sources` is a per-reader subscription with
  `user_id NOT NULL`; the KB is shared. `sources.outlet_id` is `bqa.4` work.
- **`claims` is a strict superset of the 18 keys `merge_ledger` writes** (spec §5.1). Three
  renames: `id`→`ledger_id`, `broke_on`→`resolved_on`, `broken_by`→`broken_by_note`. `kind` is
  deliberately absent — it gates admission and is never stored, so a column would be uniform.

## Three Postgres facts this schema now depends on

- **An explicit NULL OVERRIDES a column DEFAULT and then violates NOT NULL** — it does not
  fall back. So a fixed all-columns `INSERT` passing `None` for absent keys raises 23502 on
  `status`/`origin`/`severity`/`restate_count`, all of which are `NOT NULL DEFAULT`. The real
  legacy ledger rows carry only 8 keys and none of the first two, so that shape would have
  raised on **every** row while the per-row `except` reported a successful no-op.
  `claim_store._write_claim` therefore builds the column list per row from the keys present.

- **`ON DELETE RESTRICT` raises `psycopg.errors.RestrictViolation` (SQLSTATE 23001), NOT
  `ForeignKeyViolation` (23503), and is not a subclass of it** — so a test expecting the latter
  *errors out* rather than failing readably. `NO ACTION`, the default, does raise 23503. Verified
  against psycopg 3.3.4 and a live PG 18. Used on `claim_evidence` and `story_members`, where a
  cascade would let an unrelated delete silently lower an evidence floor or retcon a member list.
- **`NULLS NOT DISTINCT` is load-bearing on three unique indexes** (PG15+; the stack is 18).
  Without it, `(claim_id, event_id, NULL)` inserts twice and one piece of evidence counts as two —
  defeating the rule that exists to stop the chip whipsaw.

## Method notes worth reusing

- **The rollback is tested, and its test has a negative control.**
  `test_0006_rolls_back_and_reapplies_against_the_real_directory` is the first test in this repo
  ever to execute a real down migration — the three pre-existing ones monkeypatch
  `db.MIGRATIONS_DIR` to a temp dir with a throwaway migration. Its companion strips `DROP FUNCTION`
  from a copy and asserts the function then survives, which is what makes the first test's
  `pg_proc` assertion non-vacuous. Keep both.
- **An emptiness assertion against a test schema can never fail.** The first draft's
  provisional-tables tripwire asserted those tables held no rows — but the fixture drops and
  recreates the schema, so they are empty by construction. Replaced with a scan of the repo's own
  Python sources for `INSERT INTO`/`UPDATE` naming a provisional table, which fires on the commit
  that introduces the write. Full write-up: `learnings/probe-failures.md` §21.

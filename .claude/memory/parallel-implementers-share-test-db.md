---
name: parallel-implementers-share-test-db
description: Two SDD implementers running DB tests at once collide on the single test Postgres — every DB fixture DROPs the public schema — producing spurious failures/errors; never overlap implementers that run DB tests
metadata:
  node_type: memory
  type: feedback
  originSessionId: 3643de5b-dee7-49f4-8b2e-34e984499482
  modified: 2026-09-29T10:23:37.667Z
---

2026-09-29 (event census build): I overlapped the Task 6 fix-round implementer with the Task 7 implementer because their FILES were disjoint. Task 6's first test run came back **1 failed + 22 errors** — Task 7's full `pytest` run was resetting the same test database (`DROP SCHEMA public CASCADE` in every DB fixture) at the same time. The implementer noticed, waited, re-ran clean.

**Why:** disjoint files is not independence. The test DB on :5432 is shared mutable state, and every DB-backed fixture destroys the schema. A collision can fail either run, or — worse — let a run pass against a schema the other agent just built.

**How to apply:** in subagent-driven runs here, overlap an implementer only with READ-ONLY reviewers (tell reviewers not to run pytest while an implementer is live). Never run two implementers, or an implementer and a controller gate run, at once. If it happened, treat both runs' results as UNKNOWN and re-run one at a time. Related: [[subagent-review-stalls]], [[brief-local-run]].

**Confirmed safe overlaps (2026-09-29, census minors, 0 collisions):** an implementer running
pytest alongside (a) a docs-only agent told not to run pytest and (b) read-only reviewers told the
same. The browser-verification run was given the DB exclusively, since its setup resets the schema.

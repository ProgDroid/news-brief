---
name: never-give-a-model-raw-database-ids
description: "Handing a model real row ids as reference tokens makes an invented id indistinguishable from a real one — use an opaque per-request label space instead; the failure is silent misattachment, and the safe-looking fix is the trap"
metadata: 
  node_type: memory
  type: project
  originSessionId: 84b8a6e4-c490-4962-82a8-cac4d7d603be
  modified: 2026-09-07T20:51:38.201Z
---

**When a prompt offers the model existing rows to refer back to, never send the raw primary
keys. Mint an opaque per-request label space (`ENT1`, `EVT1`) and map back server-side.**

**Why:** a model filling a schema field called `candidate_id: integer` will use it as *its own*
sequence number when nothing tells it otherwise — numbering the things it is creating, not
referring to the things you offered. Measured 2026-09-07 in news-brief's comprehension pipeline:
every entity and event came back with a `candidate_id` while the offered lists were empty, and
the model even reused id 4 for the same actor across two items, inventing a coreference mechanism
that `_resolve_entity`'s `ON CONFLICT (lower(name), type)` already provided. 179 of 179 items were
rejected. See [[newsbrief-comprehension-pipeline]].

The prompt is not the fix on its own. It said *"otherwise propose a new entity"* and never said
**omit the id**, and the schema field carried no `description` — so the model's reading was
defensible. State the rule, absolutely; but a stated rule is compliance, not a guarantee.

**The dangerous part is the fix you reach for first.** The obvious repair is to ignore an
unrecognised id and fall through to the create path. Under raw ids that is a trap, and a worse
bug than the one it fixes: `BIGSERIAL` starts at 1 and the model's local counter starts at 1, so
the moment the table holds rows an invented id **is** a valid one. The fallback then binds the
record to an unrelated row — silently, permanently, and indistinguishably from a correct match.
The original failure was loud and wrote nothing; that one is quiet and corrupts.

An opaque label space removes the ambiguity **structurally rather than by instruction**: a value
that is not a string, or a string naming nothing offered, cannot be a reference at all. That is
what makes the liberal fallback safe — and the fallback is needed, because rejecting on an
unknown reference deadlocks the cold start (empty table → nothing can match → nothing is ever
created → there are never any candidates, forever).

**How to apply:**

1. Render candidates from the **same** map the parser resolves against — one `label_map(prefix,
   candidates)` used by both, so what is offered and what is understood cannot drift. Two
   independent enumerations agreeing today is not a guarantee about tomorrow.
2. Give the field a `description` in the tool schema and state the rule in the system prompt:
   set it only to a listed label, **omit** it entirely for anything new.
3. Treat an unresolvable label as "this is new", never as an error — and **count it**, so
   non-compliance is visible rather than absorbed ([[fail-closed-needs-status-not-count]]).
4. The failure is invisible to any test whose fixture seeds candidates first. Write the
   cold-start case explicitly: zero candidates offered, model labels things anyway, extraction
   must still succeed.
5. An end-to-end test must drive the real run path. Unit tests that build the label map by hand
   cannot see rendering/parsing drift — both halves would have to be wrong in the same way to
   fail, and identically wrong is exactly what drift looks like.

Related: [[write-only-fields-hide-from-read-paths]] (a field one side writes and the other never
reads), [[tdd-plan-fixtures-drift-from-contracts]] (fixtures encoding a contract the code does
not have).

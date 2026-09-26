# Comprehension Cost Redesign — Phase 2 Implementation Plan

> **STATUS: DRAFT, NOT EXECUTABLE (operator decision, 2026-09-25).** A fresh-context red-team
> (`docs/superpowers/reviews/2026-09-25-comprehension-cost-phase-2-redteam.md`) found three
> structural problems and seven verified defects. This plan is to be **rewritten after phase 1
> ships**, against the code as it landed, and after a read-only host spike. Do not execute it
> as written. The rewrite must address:
>
> **2026-09-26: the spike exists, but it has not run on the host.** It is `scripts/probe_clustering.py`
> (bead `news-brief-1tl`, which blocks `vlg`). Its docstring holds the pre-registration.
> - **Variants:** RT (real time) is the baseline. T (spec §5.3 as written), T-cc (transitive) and
>   TE-cc (title or a prior-born entity) are compared against it.
> - **Operator's rule:** at W = 2h, the simplest of T, T-cc, TE-cc within 5 points of RT is what
>   this rewrite builds. If none is, the blindness is redesigned before any batching task.
>
> D1 was re-affirmed the same day. Haiku 4.5 in real time costs the same as a Sonnet batch, and
> the operator heard that and kept batching. Record the verbatim output on `1tl` before
> rewriting.
>
> 1. **Clustering recall is unmeasured.** Real time already lets micro-batch *k* see the events
>    batches 1..*k*-1 created in the same pass; batching removes that. Only the clusters restore
>    it. **Spike first:** over the KB's existing multi-outlet events, what share of
>    first/second-outlet item pairs would title-trigram ≥ 0.35 group into one request? And what
>    share with an added shared-entity edge? The answer sets the threshold, and whether the
>    entity edge is needed.
> 2. **Budget and collect are not crash-safe.** `Budget.debit` commits on its own connection,
>    so an interrupted collect re-credits the reservation, duplicates ledger rows and
>    double-charges items. Derive the reservation from the in-flight rows, and make collect
>    idempotent per `custom_id`: unique `(batch_id, custom_id)` in the ledger, and applied ids
>    recorded.
> 3. **A lost batch stalls comprehension forever.** A 404 or other retrieve error returns
>    before the 25 h expiry check. The expiry must be decided from `submitted_at` first, a 404
>    must expire at once, and a batch stuck in flight must alert. Collect must also run while
>    `COMPREHEND_ENABLED` is off, because results are kept only 29 days.
> 4. **Innocent referencers get charged.** A NEW-label referencer dropped because its declarer
>    was malformed is charged through the `dropped` path; it must be uncharged.
> 5. The seven extra defects in the review file:
>    - the rolled-back-declaration test injects its fault too early, and its helper is `_strikes`, not `_attempts`;
>    - trimming must drop the OLDEST groups, sorting by newest member;
>    - `batch_submit_failed` must be a declared field;
>    - the errored-type value is likely `invalid_request`, not `invalid_request_error`, and the uncharged branch needs a defer ceiling;
>    - billing behaviour at batch create is unverified;
>    - Task 1's mutation target is ambiguous, and `retirement()`'s predicate is unpinned;
>    - submit is not crash-atomic with the API (insert a placeholder row first).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Halve integration's price with the Message Batches API, without losing the third of corroboration that arrives within two hours. Requests are grouped by headline similarity, and items in the same request can link to each other's NEW events.

**Architecture:** One hourly pass: collect the in-flight batch, if it has ended; triage in real time (unchanged); then submit ONE clustered batch if none is in flight. Manifests persist in `comprehend_batches`, and a partial unique index enforces "at most one in flight". The whole path sits behind a new `COMPREHEND_BATCH_ENABLED` knob (default false). That makes the deploy inert, and makes the flip a settings statement whose `now()` is the Amendment 3 cutover. Phase 1's real-time integration stays in place as the knob-off path.

**Tech Stack:** Python 3, psycopg 3, Postgres 18 with pg_trgm, the Anthropic Message Batches REST API through `requests`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md` §5 and §6.2. **Prerequisite:** the phase-1 plan (`2026-09-25-comprehension-cost-phase-1.md`) is fully merged. This plan consumes its `pending_integration`, `record_spend`, `Budget`, `open_budget`, `_abort`, `_account_failure` and `Tally.spent_usd`.

## Global Constraints

- **Every constraint in the phase-1 plan's Global Constraints section applies** (the three-command gate with a DB, `py`, `common.X` knobs, a compose anchor line per knob, explicit-path commits to `main`, no host actions).
- **At most ONE integration batch in flight,** enforced by the database (a partial unique index), not only by code.
- **Batch price factor 0.5** (`comprehend.BATCH_FACTOR`, from phase 1).
- **The clustering threshold is `CLUSTER_SIMILARITY = 0.35`, and at most `CLUSTER_MAX_ITEMS = 8` items per clustered request.** Leftover singletons are packed `COMPREHEND_INTEGRATE_BATCH` (5) per request.
- **`INTEGRATE_PROMPT_VERSION` goes 2 → 3 for provenance ONLY.** No SELECT may re-pick a completed item because of a version bump.
- **`NEW` labels match `^NEW[1-9][0-9]*$`.**
- **A batch still unresolved 25 h after submission is treated as expired.** The API expires batches at 24 h; the extra hour absorbs clock skew.
- **Do not flip `COMPREHEND_BATCH_ENABLED` on the host.** That is the Amendment 3 cutover, and it belongs to the runbook (Task 8).

## Review Focus

1. **Results arrive in a different order from the requests.** Expected: keyed by `custom_id`, never by position. *(Task 5, `test_results_are_applied_by_custom_id_not_position`)*
2. **The item that declares a `NEW` label fails its savepoint while a later item references that label.** Expected: the referencing item is left pending and uncharged, and the map holds no id from a rolled-back savepoint. *(Task 3, `test_a_rolled_back_declaration_is_never_resolved`)*
3. **A batch submission 5xxs or times out.** Expected: no `comprehend_batches` row is written, the reservation is not debited, and the next pass retries. *(Task 5, `test_a_failed_submit_leaves_no_row_and_no_debit`)*
4. **The only in-flight batch was lost** (a row with status `in_flight` whose id the API returns 404 for). Expected: after 25 h it is marked `expired` and its items requeue uncharged; before that it is awaited. *(Task 5, `test_a_batch_unresolved_past_25h_expires_and_requeues`)*
5. **The balance is too small for even one request.** Expected: nothing is submitted, the budget is marked exhausted, and no row is written. *(Task 5, `test_a_reservation_larger_than_the_balance_submits_nothing`)*

---

### Task 0: Beads

- [ ] Create a phase-2 epic (`--type=epic --priority=2`) with one child per task, 1–8. Record on `news-brief-3wb` (`bd update news-brief-3wb --notes=...`) that §5.6 makes it unreachable once Task 1 lands. Then `bd export -o .beads/issues.jsonl`.

---

### Task 1: The prompt-version trap — never re-select a completed item

**Files:**
- Modify: `comprehend.py`: `pending_integration` (phase 1), the `write_extraction` guard (`:1579-1586`), `retirement()`'s at-risk query (`:592-598`), `INTEGRATE_PROMPT_VERSION`
- Modify: `tests/test_comprehend_integration.py:560-600` (the test asserting that a bump re-integrates)

**Interfaces:**
- Produces: `INTEGRATE_PROMPT_VERSION = 3`. The integration predicate is now `t.integrated_at IS NULL` only.

- [ ] **Step 1: Rewrite the old behaviour's test so it pins the NEW behaviour.** Read `tests/test_comprehend_integration.py:560-600`: it asserts that bumping the version re-integrates. Replace its assertion so a completed item is **not** re-offered after a bump, and rename the test `test_a_version_bump_never_re_integrates_a_completed_item`. Keep its fixture. Its docstring must say why the old behaviour was removed: spec §5.6, and `3wb`, where a re-extraction mints a duplicate event rather than superseding.

- [ ] **Step 2: Run it; it fails** (the item is re-offered).

- [ ] **Step 3: Implement.**
  - `pending_integration`: change `AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s)` to `AND t.integrated_at IS NULL`, and drop `INTEGRATE_PROMPT_VERSION` from its parameters.
  - `write_extraction`'s `already` guard: drop `AND integrate_prompt_version >= %s` and its parameter, and replace the guard's long comment with:
    ```python
    # A version bump no longer re-extracts (spec 2026-09-25 5.6): re-extraction
    # minted a SECOND event rather than superseding the first (news-brief-3wb),
    # inflating exactly the corroboration count the gate reads. Re-extraction,
    # if ever wanted, is an explicit operation, never a deploy side effect.
    ```
  - `retirement()` at-risk: the same predicate change (it must mirror its consumer).
  - `INTEGRATE_PROMPT_VERSION = 3`, with the comment `# 3: in-request NEW labels (spec 2026-09-25 5.4). Provenance only -- see pending_integration.`

- [ ] **Step 4: Run** `py -m pytest tests/test_comprehend_integration.py tests/test_comprehend_triage.py -q`. Other tests pinning version-driven re-integration may fail. Classify each in the report (pins the old policy → rewrite; incidental → fix the fixture).

- [ ] **Step 5: Mutation check.** Restoring the `OR integrate_prompt_version < %s` clause must fail **1** test (the rewritten one). Record the count, and revert.

- [ ] **Step 6: Commit** — `git add comprehend.py tests/test_comprehend_integration.py tests/test_comprehend_triage.py` and commit with `-m "fix(comprehend): a prompt-version bump never re-integrates a completed item" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"`

---

### Task 2: Schema and prompt — `NEW` labels, plus output trimming

**Files:**
- Modify: `comprehend.py`: `_INTEGRATE_TOOL` (`:1086-1198`), `_INTEGRATE_SYSTEM` (`:1061-1084`)
- Modify: `tests/test_comprehend_labels.py` (append)

**Interfaces:**
- Produces: the event schema property `new_label` (string). `candidate` may carry `EVTn` or `NEWn`.

- [ ] **Step 1: Write the failing tests.** Read the schema, then drive the validator, per `newsbrief-comprehension-pipeline` ("a tool schema is a PROMPT"):
```python
def _event_schema():
    return comprehend._INTEGRATE_TOOL["input_schema"]["properties"]["items"][
        "items"]["properties"]["events"]["items"]


def test_the_event_schema_offers_a_new_label():
    props = _event_schema()["properties"]
    assert props["new_label"]["type"] == "string"
    assert "NEW1" in props["new_label"]["description"]


def test_the_candidate_description_names_both_label_kinds():
    desc = _event_schema()["properties"]["candidate"]["description"]
    assert "EVT" in desc and "NEW" in desc


def test_aliases_are_asked_for_only_on_new_entities():
    ent = comprehend._INTEGRATE_TOOL["input_schema"]["properties"]["items"][
        "items"]["properties"]["entities"]["items"]["properties"]
    assert "new entit" in ent["aliases"]["description"].lower()


def test_the_summary_asks_for_one_short_sentence():
    assert "25 words" in _event_schema()["properties"]["summary"]["description"]


def test_the_system_prompt_explains_new_labels():
    assert "NEW1" in comprehend._INTEGRATE_SYSTEM
```

- [ ] **Step 2: Run; they fail.**

- [ ] **Step 3: Implement.** In the event item's `properties`:
```python
                                    "candidate": {
                                        "type": "string",
                                        "description": (
                                            "A label from CANDIDATE EVENTS "
                                            "(e.g. 'EVT1'), or a NEW label an "
                                            "EARLIER item in this response "
                                            "declared (e.g. 'NEW1'). Omit "
                                            "entirely for a new event; never "
                                            "invent a label."
                                        ),
                                    },
                                    "new_label": {
                                        "type": "string",
                                        "description": (
                                            "Only on a NEW event that a LATER "
                                            "item in this response reports too: "
                                            "declare 'NEW1', 'NEW2', ... here, "
                                            "and have the later item set "
                                            "`candidate` to it."
                                        ),
                                    },
                                    "summary": {
                                        "type": "string",
                                        "description": (
                                            "One sentence, at most 25 words."
                                        ),
                                    },
```
On the entity `aliases` property, add `"description": "Only for a NEW entity: other names it goes by, e.g. a ticker."`. Leave the `anyOf` blocks unchanged. Append to `_INTEGRATE_SYSTEM`:
```
NEW LABELS. Items that share a response may report the SAME new event. Declare \
it once, on the first item, with `new_label` set to NEW1 (then NEW2, ...), and \
on each later item that reports it set `candidate` to that NEW label instead of \
describing it again.
```

- [ ] **Step 4: Run; they pass.** Also run `tests/test_comprehend_integration.py`: the schema-branch test (bqa.16) must still pass, because the `anyOf` is unchanged.

- [ ] **Step 5: Commit** — `-m "feat(comprehend): NEW labels in the integration schema, and a shorter output"`, plus the Co-Authored-By line.

---

### Task 3: Validate and write in-request links

**Files:**
- Modify: `comprehend.py`: `parse_integration_response`, `_validate_item`, `write_extraction`, `write_batch`, `Tally`
- Modify: `tests/test_comprehend_labels.py`, `tests/test_comprehend_integration.py` (append)

**Interfaces:**
- Produces: `parse_integration_response(...)` validates rows **in response order**, carrying the set of `NEW` labels declared by earlier *validated* items.
- Produces: `_validate_item(row, item_ids, entity_labels, event_labels, tally=None, declared: set[str] | None = None)`. A declaring event becomes `{..., "new_label": "NEW1"}`, and a referencing event becomes `{"new_ref": "NEW1", "standing": ...}`.
- Produces: `write_batch(conn, extractions, index, tally) -> int` keeps its signature and owns a request-scoped `new_events: dict[str, int]`.
- Produces: `write_extraction(conn, extraction, index, tally, new_events: dict[str, int] | None = None) -> bool`.
- Produces: `class UnresolvedNewLabel(ValueError)` and `Tally.events_linked_in_request: int`.
- Produces: the failure keys `validate:new_label_shape`, `validate:new_label_duplicate`, `validate:new_label_undeclared` and `validate:new_label_self`.

- [ ] **Step 1: Write the failing validator tests** (`tests/test_comprehend_labels.py`)
```python
def _resp(rows):
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "name": "emit_extraction", "input": {"items": rows}}]}


ENT = [{"name": "Iran", "type": "country"}]


def _new(label=None, summary="Iran and Oman resume talks"):
    ev = {"summary": summary, "type": "action", "standing": "reported"}
    if label:
        ev["new_label"] = label
    return ev


def test_a_later_item_may_reference_an_earlier_declaration():
    out = comprehend.parse_integration_response(_resp([
        {"item_id": 1, "entities": ENT, "events": [_new("NEW1")]},
        {"item_id": 2, "entities": ENT,
         "events": [{"candidate": "NEW1", "standing": "reported"}]},
    ]), {1, 2}, {}, {})
    assert out[0]["events"][0]["new_label"] == "NEW1"
    assert out[1]["events"][0] == {"new_ref": "NEW1", "standing": "reported"}


@pytest.mark.parametrize("rows,key", [
    # referenced before any declaration
    ([{"item_id": 1, "entities": ENT,
       "events": [{"candidate": "NEW1", "standing": "reported"}]}],
     "validate:new_label_undeclared"),
    # declared twice
    ([{"item_id": 1, "entities": ENT, "events": [_new("NEW1")]},
      {"item_id": 2, "entities": ENT, "events": [_new("NEW1")]}],
     "validate:new_label_duplicate"),
    # an item referencing its own declaration
    ([{"item_id": 1, "entities": ENT,
       "events": [_new("NEW1"), {"candidate": "NEW1", "standing": "reported"}]}],
     "validate:new_label_self"),
    # not the NEW pattern
    ([{"item_id": 1, "entities": ENT, "events": [_new("EVT9")]}],
     "validate:new_label_shape"),
])
def test_bad_new_labels_drop_the_item_with_a_named_cause(rows, key):
    tally = comprehend.Tally()
    comprehend.parse_integration_response(_resp(rows), {1, 2}, {}, {}, tally)
    assert tally.failures.get(key) == 1


def test_a_dropped_declarer_does_not_declare():
    """Item 1 fails validation for an unrelated reason, so its NEW1 never
    existed and item 2's reference is undeclared."""
    tally = comprehend.Tally()
    out = comprehend.parse_integration_response(_resp([
        {"item_id": 1, "entities": [{"name": "", "type": "country"}],
         "events": [_new("NEW1")]},
        {"item_id": 2, "entities": ENT,
         "events": [{"candidate": "NEW1", "standing": "reported"}]},
    ]), {1, 2}, {}, {}, tally)
    assert out == []
    assert tally.failures.get("validate:new_label_undeclared") == 1
```

- [ ] **Step 2: Run; they fail.**

- [ ] **Step 3: Implement validation.** In `parse_integration_response`, replace the final list comprehension with an ordered loop:
```python
            declared: set[str] = set()
            out = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                p = _validate_item(
                    r, offered_item_ids, entity_labels, event_labels, tally, declared
                )
                if p:
                    # Only a VALIDATED item declares: a dropped item's NEW label
                    # never existed, so a later reference to it is undeclared.
                    declared.update(
                        e["new_label"] for e in p["events"] if "new_label" in e
                    )
                    out.append(p)
            return out
```
In `_validate_item`, add the `declared=None` parameter and `_NEW_LABEL = re.compile(r"^NEW[1-9][0-9]*$")` at module level. In the events loop, **before** `_resolve_label`:
```python
        own = {e.get("new_label") for e in (row.get("events") or [])
               if isinstance(e, dict) and e.get("new_label")}
        ref = ev.get("candidate")
        if isinstance(ref, str) and _NEW_LABEL.match(ref):
            if ref in own:
                _note(tally, "validate:new_label_self")
                return None
            if ref not in (declared or set()):
                _note(tally, "validate:new_label_undeclared")
                return None
            events.append({"new_ref": ref, "standing": ev["standing"]})
            continue
```
(Compute `own` once, before the loop, not per event.) In the NEW-event branch, before `events.append({...})`:
```python
        label = ev.get("new_label")
        if label is not None:
            if not (isinstance(label, str) and _NEW_LABEL.match(label)):
                _note(tally, "validate:new_label_shape")
                return None
            if label in (declared or set()):
                _note(tally, "validate:new_label_duplicate")
                return None
```
Then include `**({"new_label": label} if label else {})` in the appended dict.

- [ ] **Step 4: Run the validator tests; they pass.**

- [ ] **Step 5: Write the failing write-path tests** (`tests/test_comprehend_integration.py`; use its existing `kb` fixture and item helpers, reading them first)
```python
def test_a_new_label_links_two_items_to_one_event(kb):
    a, b = _two_items(kb)   # two material items; write this helper after
                            # reading the module's existing item fixtures
    tally = comprehend.Tally()
    index = comprehend.SurfaceIndex.build(kb)
    comprehend.write_batch(kb, [
        {"item_id": a, "published_at": None,
         "entities": [{"name": "Iran", "type": "country", "aliases": []}],
         "events": [{"summary": "Talks resume", "type": "action",
                     "commitment_state": None, "standing": "reported",
                     "new_label": "NEW1"}]},
        {"item_id": b, "published_at": None,
         "entities": [{"name": "Iran", "type": "country", "aliases": []}],
         "events": [{"new_ref": "NEW1", "standing": "reported"}]},
    ], index, tally)
    kb.commit()

    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert kb.execute(
        "SELECT count(DISTINCT item_id) FROM assertions").fetchone()[0] == 2
    assert tally.events_linked_in_request == 1


def test_a_rolled_back_declaration_is_never_resolved(kb, monkeypatch):
    """Review Focus 2. The declarer's savepoint rolls back AFTER it created
    the event row, so an id recorded before the savepoint closed would point
    at a row that no longer exists."""
    a, b = _two_items(kb)
    real = comprehend._resolve_entity
    calls = {"n": 0}

    def flaky(conn, spec, tally):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom inside the declarer's savepoint")
        return real(conn, spec, tally)

    monkeypatch.setattr(comprehend, "_resolve_entity", flaky)
    tally = comprehend.Tally()
    comprehend.write_batch(kb, [
        {"item_id": a, "published_at": None,
         "entities": [{"name": "Iran", "type": "country", "aliases": []}],
         "events": [{"summary": "Talks resume", "type": "action",
                     "commitment_state": None, "standing": "reported",
                     "new_label": "NEW1"}]},
        {"item_id": b, "published_at": None,
         "entities": [{"name": "Iran", "type": "country", "aliases": []}],
         "events": [{"new_ref": "NEW1", "standing": "reported"}]},
    ], comprehend.SurfaceIndex.build(kb), tally)
    kb.commit()

    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 0
    assert _attempts(kb, b) == 0, "b did nothing wrong; it stays pending, uncharged"
    assert tally.failures.get("savepoint:UnresolvedNewLabel") == 1
```

- [ ] **Step 6: Implement the write.** `write_batch`:
```python
    new_events: dict[str, int] = {}
    with conn.transaction():
        return sum(
            1 for e in extractions if write_extraction(conn, e, index, tally, new_events)
        )
```
`write_extraction(conn, extraction, index, tally, new_events=None)`:
- `new_events = {} if new_events is None else new_events`, and `declared_here: dict[str, int] = {}`.
- In the events loop, first branch:
```python
                if "new_ref" in ev:
                    event_id = new_events.get(ev["new_ref"])
                    if event_id is None:
                        raise UnresolvedNewLabel(ev["new_ref"])
                    tally.events_linked_in_request += 1
                elif "candidate_id" in ev:
```
- After the event INSERT in the new-event branch: `if ev.get("new_label"): declared_here[ev["new_label"]] = event_id`.
- **After** the `with conn.transaction():` block completes, and only then: `new_events.update(declared_here)`.
- In the `except Exception as exc:` branch, make the charge conditional:
```python
        if not isinstance(exc, UnresolvedNewLabel):
            conn.execute("UPDATE item_triage SET integrate_attempts = ...", (item_id,))
```
  `UnresolvedNewLabel` is a fault in the item's neighbour, not in the item: it stays pending and uncharged, and is re-batched next pass, when the event may exist.
- Add `class UnresolvedNewLabel(ValueError)` next to `NoEntitySurvived`, with a docstring saying exactly that. Add `events_linked_in_request: int = 0` to `Tally`, commented "in-request NEW-label links (spec 5.4); a match the candidate list could not have offered".

- [ ] **Step 7: Run all comprehend tests; they pass.**

- [ ] **Step 8: Mutation check.** Pre-register:
  - Record into `new_events` inside the savepoint instead of after it: **1** fails (rolled-back declaration).
  - Charge `UnresolvedNewLabel`: **1** fails.
  - Drop the `declared.update` guard (declare even from dropped items): **1** fails.
  - Delete each of the four validator branches in turn: **1** each.

  Record the counts, and revert.

- [ ] **Step 9: Commit** — `-m "feat(comprehend): items in one request can share a NEW event"`, plus the Co-Authored-By line.

---

### Task 4: Clustering

**Files:**
- Modify: `comprehend.py` (a clustering block before `run()`)
- Create: `tests/test_comprehend_cluster.py`

**Interfaces:**
- Produces: `CLUSTER_SIMILARITY = 0.35` and `CLUSTER_MAX_ITEMS = 8`.
- Produces: `similar_pairs(conn, item_ids: list[int], threshold: float) -> dict[tuple[int, int], float]`, keyed `(smaller_id, larger_id)`.
- Produces: `cluster(ids_newest_first: list[int], pairs: dict[tuple[int, int], float], max_items: int) -> list[list[int]]`, which is pure.
- Produces: `request_groups(conn, items: list[dict]) -> list[list[dict]]`: clusters of 2 or more, then the leftovers packed `COMPREHEND_INTEGRATE_BATCH` per group.

- [ ] **Step 1: Write the failing tests**
```python
"""Grouping only: which items SHARE A REQUEST. Never a merge decision."""

import comprehend


def test_similar_items_share_a_cluster_newest_seeded():
    pairs = {(1, 5): 0.6, (3, 5): 0.5}
    assert comprehend.cluster([5, 4, 3, 2, 1], pairs, 8) == [[5, 1, 3], [4], [2]]


def test_a_cluster_is_capped():
    pairs = {(i, 10): 0.9 for i in range(1, 10)}
    groups = comprehend.cluster(list(range(10, 0, -1)), pairs, 3)
    assert max(len(g) for g in groups) == 3


def test_every_item_lands_in_exactly_one_group():
    pairs = {(1, 2): 0.4, (2, 3): 0.4}
    groups = comprehend.cluster([3, 2, 1], pairs, 8)
    flat = [i for g in groups for i in g]
    assert sorted(flat) == [1, 2, 3] and len(flat) == 3


def test_neighbours_join_in_descending_similarity():
    pairs = {(1, 9): 0.4, (2, 9): 0.9}
    assert comprehend.cluster([9, 2, 1], pairs, 2)[0] == [9, 2]
```
Plus one DB test for `similar_pairs`, using the same migrated `kb` fixture pattern as `tests/test_comprehend_budget.py`: two near-identical titles return a pair ≥ 0.35, and an unrelated title does not.

- [ ] **Step 2: Run; they fail.**

- [ ] **Step 3: Implement**
```python
# GROUPING, NOT MERGING (spec 2026-09-25 5.3). These decide only which items
# share one batched request, so a same-hour pair can be linked by the model
# through a NEW label. The model still decides whether two items report the
# same event, so a wrong grouping costs a slightly larger prompt, never a false
# merge. That is why an uncalibrated 0.35 is acceptable HERE and nowhere a
# similarity decides an outcome.
CLUSTER_SIMILARITY = 0.35
CLUSTER_MAX_ITEMS = 8


def similar_pairs(conn, item_ids, threshold):
    rows = conn.execute(
        "SELECT a.id, b.id, similarity(a.title, b.title) "
        "FROM items a JOIN items b ON a.id < b.id "
        "WHERE a.id = ANY(%s) AND b.id = ANY(%s) "
        "  AND similarity(a.title, b.title) >= %s",
        (list(item_ids), list(item_ids), threshold),
    ).fetchall()
    return {(a, b): float(s) for a, b, s in rows}


def cluster(ids_newest_first, pairs, max_items):
    """Greedy: seed with the newest unassigned item, then add its unassigned
    neighbours by descending similarity, up to max_items. Deterministic."""
    neighbours: dict[int, list[tuple[float, int]]] = {}
    for (a, b), s in pairs.items():
        neighbours.setdefault(a, []).append((s, b))
        neighbours.setdefault(b, []).append((s, a))
    assigned: set[int] = set()
    groups = []
    for seed in ids_newest_first:
        if seed in assigned:
            continue
        group = [seed]
        assigned.add(seed)
        for _, other in sorted(neighbours.get(seed, []), key=lambda t: (-t[0], -t[1])):
            if len(group) >= max_items:
                break
            if other not in assigned:
                group.append(other)
                assigned.add(other)
        groups.append(group)
    return groups


def request_groups(conn, items):
    by_id = {it["id"]: it for it in items}
    ids = sorted(by_id, reverse=True)
    groups = cluster(ids, similar_pairs(conn, ids, CLUSTER_SIMILARITY), CLUSTER_MAX_ITEMS)
    clustered = [[by_id[i] for i in g] for g in groups if len(g) > 1]
    singles = [by_id[g[0]] for g in groups if len(g) == 1]
    return clustered + list(_chunk(singles, int(common.COMPREHEND_INTEGRATE_BATCH)))
```

- [ ] **Step 4: Run; they pass.** Mutation check: removing the `max_items` break fails **1**; seeding oldest-first fails **1**. Record the counts, and revert.

- [ ] **Step 5: Commit** — `-m "feat(comprehend): group each request's items by headline similarity"`, plus the Co-Authored-By line.

---

### Task 5: The batch path — persistence, client, collect, submit

**Files:**
- Create: `migrations/0016_comprehend_batches_up.sql`, `migrations/0016_comprehend_batches_down.sql`
- Modify: `comprehend.py` (batch client seams; `collect_batch`; `submit_batch`; `run()` dispatch; `Tally`)
- Modify: `common.py` (`KNOBS`: `COMPREHEND_BATCH_ENABLED`), `docker-compose.yml` (anchor line)
- Create: `tests/test_comprehend_batch.py`

**Interfaces:**
- Consumes: `request_groups` (Task 4); `build_integration_request`, `candidate_events`, `label_map`, `parse_integration_response`, `write_batch`; phase 1's `record_spend`, `Budget`, `_account_failure` and `_abort`.
- Produces: the network seams `batch_create(requests: list[dict]) -> str` (a batch id), `batch_retrieve(batch_id) -> dict` and `batch_results(results_url) -> Iterable[dict]`. Tests monkeypatch all three.
- Produces: `collect_batch(conn, tally, budget, index, outlets) -> None` and `submit_batch(conn, tally, budget, index, outlets, now) -> None`.
- Produces: the `Tally` fields `batch_in_flight: bool`, `batches_submitted: int`, `batch_requests: int` and `deferred_batch: int`.
- Produces: the `comprehend_spend.items` column (the per-call item count, for the output estimate).

- [ ] **Step 1: Confirm the batch result shapes against the docs before writing parsing code.** Read the claude-api skill's `python/claude-api/batches.md` (re-invoke the skill to extract it). Record in the task report the exact shape of an `errored` result's error type: is it `result.error.type` or `result.error.error.type`? The helper below accepts both, and its test pins both. Do not trust memory here.

- [ ] **Step 2: Write migration 0016**
```sql
-- The in-flight integration batch and what was offered in it (spec 5.2). The
-- manifest holds each request's item ids AND its exact ENT/EVT label maps:
-- the parser must accept against the labels that were OFFERED, hours after
-- they were built (never-give-a-model-raw-database-ids, persisted).
CREATE TABLE comprehend_batches (
    id            BIGSERIAL PRIMARY KEY,
    batch_id      TEXT          NOT NULL UNIQUE,
    submitted_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    collected_at  TIMESTAMPTZ   NULL,
    status        TEXT          NOT NULL
                  CHECK (status IN ('in_flight', 'collected', 'expired')),
    manifest      JSONB         NOT NULL,
    reserved_usd  NUMERIC(12,6) NOT NULL
);
-- At most ONE in flight, enforced here rather than trusted to code: every
-- submission must see every event the previous one created (spec 5.1).
CREATE UNIQUE INDEX comprehend_batches_one_in_flight
    ON comprehend_batches ((true)) WHERE status = 'in_flight';
ALTER TABLE comprehend_spend ADD COLUMN items INTEGER NULL;
```
Down: `ALTER TABLE comprehend_spend DROP COLUMN items; DROP TABLE comprehend_batches;`

- [ ] **Step 3: Add the knob.** In `KNOBS`, after the budget knobs:
```python
    # Phase 2 of the cost redesign: integrate through the Message Batches API
    # (half price). Default OFF so a deploy is inert. Flipping this row IS the
    # Amendment 3 cutover, and the flipping statement's now() is its timestamp.
    "COMPREHEND_BATCH_ENABLED": Knob(bool, False),
```
Add the compose anchor line `- COMPREHEND_BATCH_ENABLED=${COMPREHEND_BATCH_ENABLED:-}`.

- [ ] **Step 4: Write the failing tests** in `tests/test_comprehend_batch.py`. Use the migrated `kb` fixture pattern and the `state_store` fixture. Build a fake API with three seams:
```python
class FakeBatches:
    """In-memory Message Batches API. `results` maps custom_id -> result."""

    def __init__(self):
        self.created, self.status, self.results = [], "in_progress", {}

    def create(self, requests):
        self.created.append(requests)
        return f"msgbatch_{len(self.created)}"

    def retrieve(self, batch_id):
        return {"id": batch_id, "processing_status": self.status,
                "results_url": f"https://x/{batch_id}/results"}

    def results_iter(self, url):
        # Deliberately REVERSED: results arrive in any order (Review Focus 1).
        return [{"custom_id": k, "result": v}
                for k, v in reversed(list(self.results.items()))]


@pytest.fixture()
def api(monkeypatch):
    f = FakeBatches()
    monkeypatch.setattr(comprehend, "batch_create", f.create)
    monkeypatch.setattr(comprehend, "batch_retrieve", f.retrieve)
    monkeypatch.setattr(comprehend, "batch_results", f.results_iter)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_BATCH_ENABLED", True)
    return f
```
Tests (each pinning one behaviour):
  - `test_a_pass_submits_one_batch_and_records_its_manifest`: two material items produce one `comprehend_batches` row with `status='in_flight'` and manifest label maps, and the reservation is debited.
  - `test_no_second_batch_while_one_is_in_flight`: a second pass with the fake still `in_progress` sends no second `create` call.
  - `test_the_database_refuses_two_in_flight_rows`: a direct INSERT of a second `in_flight` row raises `psycopg.errors.UniqueViolation`.
  - `test_results_are_applied_by_custom_id_not_position` (Review Focus 1): two requests with distinct items, results reversed, and each item's assertion lands on the right item.
  - `test_an_errored_invalid_request_charges_and_other_errors_do_not`: parametrised over the `result.error.type` / `result.error.error.type` shapes. `invalid_request_error` → `integrate_attempts + 1`; `overloaded_error` → 0 and `deferred_batch` counts it.
  - `test_expired_and_canceled_requeue_uncharged`.
  - `test_a_collect_writes_ledger_rows_at_the_batch_rate`: `comprehend_spend.batch_id` is set, `usd` equals half the real-time price, and `items` is set.
  - `test_a_failed_submit_leaves_no_row_and_no_debit` (Review Focus 3): `create` raises `requests.ConnectionError`; no row, the balance is unchanged, and `tally.aborted == ""`.
  - `test_a_402_at_submit_aborts_the_pass` (a `_account_failure` → `_abort`).
  - `test_a_batch_unresolved_past_25h_expires_and_requeues` (Review Focus 4): an `in_flight` row with `submitted_at = now() - 26h` and the fake still `in_progress` → the row becomes `expired`, the reservation is credited back, and the items are selectable again.
  - `test_a_reservation_larger_than_the_balance_submits_nothing` (Review Focus 5): balance $0.0001 → no `create`, and `budget_exhausted` is set.
  - `test_with_batching_off_the_real_time_path_runs`: the knob off and nothing in flight → `call_integration` is used and `batch_create` is never called.
  - `test_an_in_flight_batch_is_collected_after_the_knob_goes_off`: submit with the knob on, flip it off, set the fake to `ended`, run → the row is `collected`, the items are integrated, and `call_integration` is never called for them.
  - `test_real_time_waits_while_a_rolled_back_batch_is_in_flight`: the knob off, an `in_flight` row, the fake still `in_progress` → `call_integration` is never called.

- [ ] **Step 5: Run; they fail.**

- [ ] **Step 6: Implement the seams** (after `call_integration`):
```python
_BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"
BATCH_EXPIRE_AFTER_HOURS = 25


def batch_create(requests_: list[dict]) -> str:
    """Network seam. Tests monkeypatch this."""
    resp = requests.post(_BATCHES_URL, headers=common.ANTHROPIC_HEADERS,
                         json={"requests": requests_}, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]


def batch_retrieve(batch_id: str) -> dict:
    resp = requests.get(f"{_BATCHES_URL}/{batch_id}",
                        headers=common.ANTHROPIC_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def batch_results(results_url: str):
    resp = requests.get(results_url, headers=common.ANTHROPIC_HEADERS,
                        timeout=120, stream=True)
    resp.raise_for_status()
    for line in resp.iter_lines():
        if line:
            yield json.loads(line)


def _batch_error_type(result: dict) -> str:
    """The error type of an `errored` result, accepting both documented
    nestings (Task 5 Step 1 records which one the API uses)."""
    err = result.get("error") or {}
    inner = err.get("error") if isinstance(err.get("error"), dict) else err
    return str(inner.get("type") or "")
```

- [ ] **Step 7: Implement submit.** One function, in this order:
  1. Stop if a row with `status = 'in_flight'` exists: set `tally.batch_in_flight = True` and return.
  2. `items = pending_integration(conn, int(common.COMPREHEND_MAX_ITEMS))`, and return if it is empty.
  3. For each group from `request_groups(conn, items)`, compute the candidates exactly as the real-time path does. **Extract that block of `run()` (`hits` → `cand_entities` → `cand_events`) into `_candidates_for(conn, batch, index, tally) -> tuple[list, list]` and use it in both paths**; never copy it. Then `params = build_integration_request(payload, cand_entities, cand_events)`, `custom_id = f"r{n}"`, and manifest entry `{"item_ids": [...], "entity_labels": label_map("ENT", cand_entities), "event_labels": label_map("EVT", cand_events), "model": params["model"]}`.
  4. Estimate: `est_in = len(json.dumps(params)) / 3.5` tokens, and `est_out = len(group) × _mean_output_per_item(conn)`. The latter is `sum(output_tokens)/sum(items)` over integration ledger rows of the last 7 days with `items IS NOT NULL`, defaulting to 150. Price both at the batch rate with `cost_usd(model, {...}, batch=True)`.
  5. Drop requests from the END (the oldest clusters) until the total reservation ≤ `budget.balance`. If none fit: `tally.budget_exhausted = True`, `budget.mark_exhausted(now)`, return.
  6. `batch_id = batch_create([{"custom_id": c, "params": p} ...])` inside `try`. On `_account_failure` → `raise _AccountAbort(kind)` (see step 9). On any other exception → log a warning, set `tally.batch_submit_failed = True`, and return without writing a row or debiting (Review Focus 3).
  7. INSERT the `comprehend_batches` row (`in_flight`, manifest, `reserved_usd`), `budget.debit(reserved)`, commit. `tally.batches_submitted += 1` and `tally.batch_requests += len(reqs)`.

- [ ] **Step 8: Implement collect.**
  1. Fetch the `in_flight` row. If there is none, return. Otherwise `status = batch_retrieve(batch_id)` inside `try`: an `_account_failure` goes to the abort path; any other exception → set `tally.batch_in_flight = True` and return (wait).
  2. If not `ended`: if `now() - submitted_at > BATCH_EXPIRE_AFTER_HOURS`, mark the row `expired`, `budget.debit(-reserved_usd)` (a credit), `tally.deferred_batch += len(all item ids)`, commit, return. Otherwise `tally.batch_in_flight = True` and return.
  3. `ended`: `budget.debit(-reserved_usd)` first (release the reservation), then for each result, keyed by `custom_id`:
     - `succeeded`: `message = result["message"]`; `usd = record_spend(conn, "integration", entry["model"], message.get("usage") or {}, batch_id=batch_id, batch=True)`; set that ledger row's `items = len(entry["item_ids"])` (extend `record_spend` with an optional `items=None` argument, rather than a second UPDATE); `budget.debit(usd)`. Then run the SAME parse → dropped-charging → `write_batch` sequence the real-time path runs, reading `published_at` for the entry's items with one SELECT. **Extract the real-time path's post-call block (`dropped` charging, `published_at` stamping, `write_batch`, and the budgeted-deferral `except` branch) into `_apply_extraction(conn, tally, index, batch_items, resp, entity_labels, event_labels)` and call it from both paths.**
     - `errored`: `invalid_request_error` → `integrate_attempts + 1` for the entry's items (`failed_integration` counts them). Anything else → uncharged, `deferred_batch`.
     - `expired` / `canceled` → uncharged, `deferred_batch`.
  4. Mark the row `collected` with `collected_at = now()`, and commit.

- [ ] **Step 9: Implement dispatch in `run()`.** After triage and sampling:
```python
    # Collect runs whatever the knob says. An in-flight batch must still be
    # collected after a rollback, or its items would sit un-integrated, and
    # its reservation un-released, until someone noticed.
    try:
        collect_batch(conn, tally, budget, index, outlets)
        if bool(common.COMPREHEND_BATCH_ENABLED):
            submit_batch(conn, tally, budget, index, outlets, now)
    except _AccountAbort as a:
        return _abort(conn, tally, a.kind)
    if not bool(common.COMPREHEND_BATCH_ENABLED) and not tally.batch_in_flight:
        ...  # the phase-1 real-time integration loop, unchanged
```
The real-time loop is skipped while a batch is still in flight after a rollback. Otherwise real-time integration could pick up items that are already in the batch, and write them twice.
`class _AccountAbort(Exception)` carries `.kind`. Raising it from deep inside collect or submit and catching it here keeps `_abort` the single exit.

- [ ] **Step 10: Run the batch tests, then everything comprehend; they pass.** The real-time tests must be untouched: `COMPREHEND_BATCH_ENABLED` defaults false.

- [ ] **Step 11: Mutation check.** Pre-register one failing test for each of:
  - applying results by position;
  - charging `overloaded_error`;
  - skipping the 25 h expiry;
  - debiting before `batch_create` succeeds;
  - dropping the in-flight guard in `submit_batch` (the DB index still refuses, so expect `test_no_second_batch_while_one_is_in_flight` to FAIL with `UniqueViolation`).

  Record the counts, and revert.

- [ ] **Step 12: Commit**
```bash
git add migrations/0016_comprehend_batches_up.sql migrations/0016_comprehend_batches_down.sql comprehend.py common.py docker-compose.yml tests/test_comprehend_batch.py
git commit -m "feat(comprehend): integrate through the Batches API, one clustered batch in flight" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Cold start through the batch path

**Files:** Modify `tests/test_comprehend_batch.py`

- [ ] **Step 1: Write the test.** An EMPTY KB (no entities, no events), two material items about the same event in one cluster, and a fake `succeeded` result that declares `NEW1` on the first and references it on the second → one event and two assertions. This is the 2026-09-07 cold-start deadlock, re-checked through the new path, since every earlier fixture seeded candidates first.
- [ ] **Step 2: Run it.** If it fails, the defect is in Tasks 3–5: fix it there, not in the test.
- [ ] **Step 3: Commit** — `-m "test(comprehend): the cold start holds through the batch path"`, plus the Co-Authored-By line.

---

### Task 7: Amendment 3 in the parent spec

**Files:** Modify `docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md` (append after Amendment 2)

- [ ] **Step 1: Write `#### Amendment 3 — the cutover moves to the batched pipeline (<date>, <phase-2 epic id>)`.** It must contain, in this order:
  - **What changed in the measured system,** stated plainly: in-request `NEW` linking and clustered requests exist BECAUSE of the §2.3 blind spot (a third of corroboration arrives within 2 h), so this change favours corroboration. Improving the system after seeing it fail is legitimate; moving a threshold is not. Say which of the two this is.
  - **Unchanged:** `--horizon-hours 6`, the 7-day window, `CORROBORATION_FLOOR` 0.10, ceiling 0.60, and `MIN_DISTINCT_AT_10PCT` 2.
  - **The cutover is the flip of `COMPREHEND_BATCH_ENABLED`,** captured from the flipping statement's `now()`, as in Amendment 2's step 2.
  - **The named hazard:** a dry bucket stalls `last_event_at`, and `zp8` cannot see a mid-window hole (`news-brief-li9`). During the window, the runbook watches the budget alert.
  - **What the 2026-09-30 run recorded** (paste its verbatim output, or state that it has not run yet; it must have run before this flip).
- [ ] **Step 2: Commit** — `-m "docs(gate): Amendment 3, the cutover for the batched pipeline"`, plus the Co-Authored-By line.

---

### Task 8: Runbook and the full gate

**Files:** Create `docs/2026-09-25-host-runbook-cost-redesign-phase-2.md`

- [ ] **Step 1: The runbook**, in order:
  1. Deploy with `COMPREHEND_BATCH_ENABLED` false. Real-time behaviour must be unchanged: comprehend log lines match phase 1.
  2. Confirm the phase-1 week's measurements are recorded (phase-1 runbook step 7).
  3. Flip, capturing the cutover in the same statement:
     ```sql
     INSERT INTO settings (key, user_id, value) VALUES ('COMPREHEND_BATCH_ENABLED', NULL, 'true')
     ON CONFLICT (key) WHERE user_id IS NULL
     DO UPDATE SET value = EXCLUDED.value, updated_at = now()
     RETURNING key, value, now() AS cutover;
     ```
     Record `cutover` verbatim in Amendment 3 and on the phase-2 epic.
  4. Verify by effect within two passes: `SELECT status, count(*) FROM comprehend_batches GROUP BY 1`; `SELECT count(*) FROM comprehend_spend WHERE batch_id IS NOT NULL`; the tally's `events_linked_in_request > 0` within a day; and the output tokens per item (`sum(output_tokens)/sum(items)` over batch rows) against the phase-1 mean.
  5. On or after cutover + 7 d + 6 h, run the gate: `py scripts/score_comprehension.py --cutover '<cutover>' --horizon-hours 6`. Record the output verbatim.
  6. **Rollback:** set the row to `false`. Any in-flight batch is still collected, and real-time integration resumes only once none is in flight. Task 5's tests pin both behaviours.
- [ ] **Step 2: The full gate** — the same three commands as phase 1 Task 11 Step 3, with the log redirected and `REAL_EXIT` recorded, and no DB skips.
- [ ] **Step 3: Commit, close the task beads, export.** Keep the phase-2 epic open until runbook step 5's verdict is recorded.

"""Integration stage: candidates, parsing, and the savepoint write path."""

import json

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def _entity(kb, name="Ukraine", type_="country"):
    return kb.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id", (name, type_)
    ).fetchone()[0]


def _event(kb, entity_id, summary="A ceasefire was announced", days_ago=1):
    eid = kb.execute(
        "INSERT INTO events (summary, type, commitment_state, occurred_at) "
        "VALUES (%s, 'statement', 'intended', now() - make_interval(days => %s)) "
        "RETURNING id",
        (summary, days_ago),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)",
        (eid, entity_id),
    )
    return eid


def test_candidate_events_are_found_through_shared_entities(kb):
    ent = _entity(kb)
    ev = _event(kb, ent)
    kb.commit()
    got = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    assert [c["id"] for c in got] == [ev]


def test_candidate_events_outside_the_window_are_excluded(kb):
    ent = _entity(kb)
    _event(kb, ent, days_ago=comprehend.CANDIDATE_WINDOW_DAYS + 5)
    kb.commit()
    assert comprehend.candidate_events(kb, [ent], comprehend.Tally()) == []


def test_candidate_events_carry_ONLY_id_and_summary(kb):
    """Load-bearing, and the gate depends on it.

    analysis-stats-traps #4: the gold-set probe handed the model each row's
    severity already populated, then reported severity variance as evidence the
    field was healthy -- unchanged on 21 of 23, because the variance was ECHO.
    scripts/score_comprehension.py scores events.type and commitment_state, so
    a candidate carrying those primes every assertion attached to it and the
    gate measures the prompt instead of the model, while still reading as a
    pass.
    """
    ent = _entity(kb)
    _event(kb, ent)
    kb.commit()
    got = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    assert set(got[0]) == {"id", "summary"}


def test_the_integration_prompt_never_mentions_a_candidate_enum_value(kb):
    """The same rule enforced at the layer that actually reaches the model.
    The test above pins the retrieval shape; this pins what is SENT, which is
    the thing that would silently invalidate the measurement."""
    ent = _entity(kb)
    _event(kb, ent)
    kb.commit()
    events = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    req = comprehend.build_integration_request(
        [{"id": 1, "title": "t", "body": "b", "outlet": "Reuters"}],
        [{"id": ent, "name": "Ukraine", "type": "country"}],
        events,
    )
    sent = json.dumps(req["messages"])
    assert "intended" not in sent, (
        "a candidate's commitment_state leaked into the prompt"
    )
    assert "A ceasefire was announced" in sent, (
        "presence sibling: the summary MUST be sent, or the absence assertion "
        "above passes for a prompt that sends no candidates at all"
    )


def test_hitting_the_candidate_cap_is_counted(kb):
    ent = _entity(kb)
    for i in range(comprehend.CANDIDATE_EVENT_CAP + 3):
        _event(kb, ent, summary=f"Event {i}")
    kb.commit()
    tally = comprehend.Tally()
    got = comprehend.candidate_events(kb, [ent], tally)
    assert len(got) == comprehend.CANDIDATE_EVENT_CAP
    assert tally.candidate_cap_hit == 1


def test_candidates_fan_in_across_entities_and_a_shared_event_appears_once(kb):
    """Pins two things no existing fixture can distinguish: `= ANY(%s)` and
    `SELECT DISTINCT`.

    Every other test passes a single-element entity list, so an implementation
    reading `= entity_ids[0]` satisfies all of them. And no other fixture joins
    one event to two entities, so deleting DISTINCT costs nothing.
    """
    a = _entity(kb, "Ukraine")
    b = _entity(kb, "Russia")
    only_a = _event(kb, a, summary="A meets", days_ago=1)
    only_b = _event(kb, b, summary="B meets", days_ago=2)
    shared = _event(kb, a, summary="A and B meet", days_ago=3)
    kb.execute(
        "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)",
        (shared, b),
    )
    kb.commit()

    rows = comprehend.candidate_events(kb, [a, b], comprehend.Tally())
    ids = [r["id"] for r in rows]
    assert sorted(ids) == sorted([only_a, only_b, shared]), (
        "all three must come back: an implementation matching only "
        "entity_ids[0] drops the event reachable solely through b"
    )
    assert len(ids) == len(set(ids)), (
        "the shared event joins twice; without SELECT DISTINCT it comes back "
        "twice and the model sees one candidate under two identical ids"
    )


def test_candidates_come_back_newest_first(kb):
    """ORDER BY occurred_at DESC decides WHICH events survive the cap, and the
    cap test gives every row the same days_ago, so nothing pins it. Drop the
    ORDER BY and the cap starts keeping arbitrary events instead of recent
    ones -- a silent quality loss, because the COUNT stays correct.
    """
    e = _entity(kb)
    oldest = _event(kb, e, summary="oldest", days_ago=10)
    newest = _event(kb, e, summary="newest", days_ago=1)
    middle = _event(kb, e, summary="middle", days_ago=5)
    kb.commit()

    rows = comprehend.candidate_events(kb, [e], comprehend.Tally())
    assert [r["id"] for r in rows] == [newest, middle, oldest], (
        "newest first; the insertion order above is deliberately not the "
        "expected order, so a missing ORDER BY cannot pass by coincidence"
    )


def _extraction(items):
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "emit_extraction", "input": {"items": items}}
        ],
    }


ONE = {
    "item_id": 1,
    "entities": [{"candidate": "ENT1"}],
    "events": [{"candidate": "EVT1", "standing": "reported"}],
}
# Label -> real id. Raw ids never reach the model now (comprehend.label_map), so
# a fixture citing 10 directly would be testing a contract that no longer exists.
ENT = {"ENT1": 10}
EVT = {"EVT1": 20}


def test_a_well_formed_extraction_is_parsed():
    got = comprehend.parse_integration_response(_extraction([ONE]), {1}, ENT, EVT)
    assert got[0]["item_id"] == 1
    assert got[0]["entities"][0]["candidate_id"] == 10


def test_a_truncated_extraction_raises_before_parsing():
    resp = _extraction([ONE])
    resp["stop_reason"] = "max_tokens"
    with pytest.raises(ValueError, match="truncated"):
        comprehend.parse_integration_response(resp, {1}, ENT, EVT)


# The six integer-coercion guard tests that stood here until 2026-09-07
# (hallucinated / boolean / float candidate ids, on both the entity and event
# arms) are GONE, and their coverage moved rather than evaporating.
#
# They guarded `candidate_id`, an INTEGER the model could collide with a real
# BIGSERIAL id -- `True == 1` and `1.0 == 1` both satisfied a membership test
# against real ids. news-brief-bqa.11 replaced that field with an opaque label
# space (`candidate`, "ENT1"/"EVT1"), which makes the collision structurally
# impossible instead of guarded: a value that is not a string, or a string
# naming nothing offered, cannot be a reference at all.
#
# The replacements live in tests/test_comprehend_labels.py -- deliberately in a
# file with no DB pytestmark, since they are pure function calls:
#   test_a_candidate_that_looks_like_a_raw_id_does_not_resolve  (the collision)
#   test_a_non_string_candidate_does_not_resolve                (bool / float)
#   test_an_unmappable_entity_label_with_no_usable_name_drops_the_item
#
# Note the behaviour change these encode: an unrecognised candidate no longer
# rejects the item, it falls back to the new-entity path and is COUNTED. That is
# deliberate -- rejecting deadlocked the cold start, because with an empty KB
# nothing can map, so no entity is ever created, so there are never candidates.


def test_an_item_id_that_was_never_sent_is_dropped():
    got = comprehend.parse_integration_response(_extraction([ONE]), {2}, ENT, EVT)
    assert got == []


def test_a_new_entity_and_a_new_event_are_accepted():
    """Presence sibling for the four rejection tests: a parser that returned []
    unconditionally would satisfy every one of them."""
    fresh = {
        "item_id": 1,
        "entities": [{"name": "Moldova", "type": "country", "aliases": []}],
        "events": [
            {
                "summary": "Border checks tightened",
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }
    got = comprehend.parse_integration_response(_extraction([fresh]), {1}, {}, {})
    assert got[0]["entities"][0]["name"] == "Moldova"
    assert got[0]["events"][0]["type"] == "action"


def test_a_row_with_one_bad_entity_is_rejected_ENTIRELY():
    """All-or-nothing, and only a multi-entity row can show it.

    Every other rejection test gives the bad row a single entity, where
    "drop the whole row" and "drop the bad field, then reject if the list
    is empty" produce the same answer. Here the row also carries a
    perfectly good entity: all-or-nothing rejects the row, field-skipping
    would return it holding just the good one.
    """
    bad = dict(
        ONE,
        entities=[
            {"name": "Moldova", "type": "country", "aliases": []},
            # An invalid `type` is now the cheapest way to make an entity bad.
            # An unoffered candidate no longer is: under opaque labels it falls
            # back to the new-entity path by design (news-brief-bqa.11).
            {"name": "Ruritania", "type": "not_a_type"},
        ],
    )
    got = comprehend.parse_integration_response(_extraction([bad]), {1}, ENT, EVT)
    assert got == [], "one bad entity must drop the whole row, not just itself"


def test_a_row_with_two_good_entities_is_accepted_with_BOTH():
    """Presence sibling for the test above. Rejecting a two-entity row is
    also what a parser that cannot handle multi-entity rows at all would do,
    and that failure is invisible while every other fixture is single-entity.
    This proves the rejection above is about the bad entity, not the count.
    """
    good = dict(
        ONE,
        entities=[
            {"name": "Moldova", "type": "country", "aliases": []},
            {"candidate": "ENT1"},
        ],
    )
    got = comprehend.parse_integration_response(_extraction([good]), {1}, ENT, EVT)
    assert len(got) == 1
    assert len(got[0]["entities"]) == 2, (
        "both entities must survive -- not just a truthy non-empty check"
    )


def _item(kb, title="Ukraine ceasefire", h="H1"):
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') "
        "ON CONFLICT DO NOTHING RETURNING id"
    ).fetchone()
    if outlet_id is None:
        outlet_id = kb.execute("SELECT id FROM outlets LIMIT 1").fetchone()
    oid = outlet_id[0]
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', %s, %s) RETURNING id",
        (oid, title, h),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', 'topical', 1)",
        (iid,),
    )
    return iid


def _fresh(item_id, summary="Border checks tightened", name="Moldova"):
    return {
        "item_id": item_id,
        "entities": [{"name": name, "type": "country", "aliases": []}],
        "events": [
            {
                "summary": summary,
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }


def test_writing_an_extraction_creates_the_whole_chain(kb):
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()
    index = comprehend.SurfaceIndex([])

    assert comprehend.write_extraction(kb, _fresh(iid), index, tally) is True
    kb.commit()

    assert kb.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM event_entities").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 1
    assert (
        kb.execute(
            "SELECT integrated_at IS NOT NULL FROM item_triage WHERE item_id = %s",
            (iid,),
        ).fetchone()[0]
        is True
    )
    assert tally.entities_created == 1
    assert tally.events_created == 1
    assert tally.assertions_written == 1


def test_provenance_is_stamped_on_every_extracted_row(kb):
    iid = _item(kb)
    kb.commit()
    comprehend.write_extraction(
        kb, _fresh(iid), comprehend.SurfaceIndex([]), comprehend.Tally()
    )
    kb.commit()
    for table in ("entities", "events", "assertions"):
        row = kb.execute(
            f"SELECT extractor_model, prompt_version FROM {table}"
        ).fetchone()
        assert row[0], f"{table}.extractor_model was not stamped"
        assert row[1] == comprehend.INTEGRATE_PROMPT_VERSION


def test_matching_a_candidate_event_records_corroboration(kb):
    """Two items, two outlets, one event. This is the property claims cannot
    represent and the reason this layer exists."""
    ent = _entity(kb)
    ev = _event(kb, ent)
    other = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Guardian', 'wire') RETURNING id"
    ).fetchone()[0]
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'H9') RETURNING id",
        (other,),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', 'topical', 1)",
        (iid,),
    )
    kb.commit()

    tally = comprehend.Tally()
    extraction = {
        "item_id": iid,
        "entities": [{"candidate_id": ent}],
        "events": [{"candidate_id": ev, "standing": "reported"}],
    }
    assert (
        comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
        is True
    )
    kb.commit()

    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 1, (
        "matching a candidate must NOT create a second event row"
    )
    assert tally.events_matched == 1
    assert tally.events_created == 0


def test_a_bad_item_does_not_take_its_neighbours_down(kb):
    """THE savepoint test, and it is two assertions rather than one.

    events.type is NOT NULL with no default, so one malformed extraction raises
    a CHECK violation. Under a batch-wide transaction that aborts the whole
    batch and loses all three items -- and an absence-only assertion would pass
    identically against that broken version. The presence half is what proves
    the savepoint exists.
    """
    good1 = _item(kb, "One", h="Ha")
    bad = _item(kb, "Two", h="Hb")
    good2 = _item(kb, "Three", h="Hc")
    kb.commit()

    batch = [
        _fresh(good1, summary="A", name="Alpha"),
        {  # bypasses the parser deliberately: an invalid enum reaching the DB
            "item_id": bad,
            "entities": [{"name": "Beta", "type": "country", "aliases": []}],
            "events": [
                {
                    "summary": "B",
                    "type": "NOT_A_TYPE",
                    "commitment_state": "in_force",
                    "standing": "reported",
                }
            ],
        },
        _fresh(good2, summary="C", name="Gamma"),
    ]
    tally = comprehend.Tally()
    written = comprehend.write_batch(kb, batch, comprehend.SurfaceIndex([]), tally)
    kb.commit()

    # Absence: the bad item wrote nothing and is not marked done.
    assert (
        kb.execute(
            "SELECT integrated_at FROM item_triage WHERE item_id = %s", (bad,)
        ).fetchone()[0]
        is None
    )
    assert not kb.execute(
        "SELECT 1 FROM assertions a JOIN items i ON i.id = a.item_id WHERE i.id = %s",
        (bad,),
    ).fetchone()
    # Presence: its neighbours survived. Without this the test passes against a
    # batch-wide transaction that lost all three.
    assert written == 2
    for good in (good1, good2):
        assert (
            kb.execute(
                "SELECT integrated_at IS NOT NULL FROM item_triage WHERE item_id = %s",
                (good,),
            ).fetchone()[0]
            is True
        )
    assert tally.items_lost_to_savepoint == 1
    assert tally.failed_integration == 1


def test_an_instrument_entity_shadowing_a_company_is_refused(kb):
    """bqa.9 item 5, enforced in CODE and not only in the prompt.

    jx9.5 froze claim text on a MODEL-SUPPLIED field and the model simply chose
    the other value. A guard must test something the code can see for itself.
    """
    company = kb.execute(
        "INSERT INTO entities (name, type) VALUES ('Apple', 'company') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (company,),
    )
    iid = _item(kb, h="Hd")
    kb.commit()

    tally = comprehend.Tally()
    extraction = {
        "item_id": iid,
        "entities": [{"name": "AAPL", "type": "instrument", "aliases": []}],
        "events": [
            {
                "summary": "Shares moved",
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }
    comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
    kb.commit()

    assert (
        kb.execute(
            "SELECT count(*) FROM entities WHERE type = 'instrument'"
        ).fetchone()[0]
        == 0
    )
    assert tally.instrument_entity_refused == 1


def test_an_event_this_pipeline_CREATED_can_be_retrieved_as_a_candidate(kb):
    """The round-trip, and it is the most important test in this file.

    Every other test here builds its candidate events with a fixture that sets
    occurred_at explicitly. Production does not: it writes what
    write_extraction writes. An earlier draft of this plan omitted occurred_at
    from the INSERT, so every created event had NULL, candidate_events'
    `occurred_at >= now() - interval` excluded it, events_matched could never
    leave 0, and the corroboration floor failed BY CONSTRUCTION -- with the
    whole suite green, because the fixtures built rows production cannot.

    That is tdd-plan-fixtures-drift-from-contracts exactly. Write the row with
    the real function, then read it back with the real query.
    """
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()
    index = comprehend.SurfaceIndex([])
    assert comprehend.write_extraction(kb, _fresh(iid), index, tally) is True
    kb.commit()

    entity_id = kb.execute("SELECT id FROM entities").fetchone()[0]
    candidates = comprehend.candidate_events(kb, [entity_id], comprehend.Tally())
    assert len(candidates) == 1, (
        "an event this pipeline just created must be offerable as a candidate, "
        "or corroboration is impossible no matter how well the matcher works"
    )
    assert candidates[0]["summary"] == "Border checks tightened"


def test_reprocessing_the_same_item_does_not_duplicate_assertions(kb):
    iid = _item(kb)
    kb.commit()
    for _ in range(2):
        comprehend.write_extraction(
            kb, _fresh(iid), comprehend.SurfaceIndex([]), comprehend.Tally()
        )
        kb.commit()
    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 1


def test_a_bumped_integration_prompt_version_re_integrates(kb, monkeypatch):
    """0009's two-column split exists so an integration-prompt bump re-extracts
    WITHOUT touching triage, and spec 4.2's pending predicate is
    `integrated_at IS NULL OR integrate_prompt_version < :current`. Keyed on
    integrated_at alone, the guard no-ops forever: task 10's SELECT re-offers
    the item every run, this function skips it, and the stored version never
    advances -- the exact single-column bug 0009 says the split retired.
    """
    iid = _item(kb)
    kb.commit()
    comprehend.write_extraction(
        kb, _fresh(iid), comprehend.SurfaceIndex([]), comprehend.Tally()
    )
    kb.commit()
    first = kb.execute(
        "SELECT integrate_prompt_version FROM item_triage WHERE item_id = %s", (iid,)
    ).fetchone()[0]

    monkeypatch.setattr(comprehend, "INTEGRATE_PROMPT_VERSION", first + 1)
    assert (
        comprehend.write_extraction(
            kb, _fresh(iid), comprehend.SurfaceIndex([]), comprehend.Tally()
        )
        is True
    )
    kb.commit()

    assert (
        kb.execute(
            "SELECT integrate_prompt_version FROM item_triage WHERE item_id = %s",
            (iid,),
        ).fetchone()[0]
        == first + 1
    ), "the bump must advance the stored version, or task 10 re-selects forever"


# --- news-brief-uer: an item with genuinely nothing in it is not a failure.
# _validate_item returned None both for a MALFORMED row and for a well-formed
# row carrying nothing, so run() could not tell them apart: the empty item was
# charged three integration calls and counted in failed_integration as though
# the model or the parser had broken. It concentrates on the `sampled` control
# arm by construction -- sampled items are the ones BOTH triage halves
# rejected -- and `sampled` is the gate's unconfounded control.


EMPTY = {"item_id": 1, "entities": [], "events": []}


def test_a_well_formed_but_empty_extraction_survives_parsing():
    """It must reach run(), which is the only layer that can mark it done.
    Dropping it at the parser is what made it indistinguishable from garbage."""
    got = comprehend.parse_integration_response(_extraction([EMPTY]), {1}, {}, {})
    assert [e["item_id"] for e in got] == [1]
    assert got[0]["entities"] == [] and got[0]["events"] == []


def test_a_malformed_row_is_still_dropped_whole():
    """The positive control for the test above: widening 'empty' must not
    widen 'malformed'. An unoffered item_id is still garbage."""
    got = comprehend.parse_integration_response(_extraction([EMPTY]), {2}, set(), set())
    assert got == []


def test_an_empty_extraction_is_marked_integrated_and_writes_nothing(kb):
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()

    assert (
        comprehend.write_extraction(
            kb, dict(EMPTY, item_id=iid), comprehend.SurfaceIndex([]), tally
        )
        is True
    )
    kb.commit()

    assert kb.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 0
    integrated_at, attempts = kb.execute(
        "SELECT integrated_at, integrate_attempts FROM item_triage WHERE item_id = %s",
        (iid,),
    ).fetchone()
    assert integrated_at is not None, (
        "an empty item must be advanced past the integration SELECT, or it is "
        "re-offered at the FRONT of every future batch forever"
    )
    assert attempts == 0, "nothing failed, so nothing may be charged an attempt"


def test_an_empty_extraction_is_counted_apart_from_failures(kb):
    """A genuinely empty sample arm is a real finding about the corpus. Folding
    it into failed_integration is the unattributable-count shape this repo has
    a rule against -- it conflates 'the pipeline broke' with 'nothing here'."""
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()

    comprehend.write_extraction(
        kb, dict(EMPTY, item_id=iid), comprehend.SurfaceIndex([]), tally
    )

    assert tally.empty_extraction == 1
    assert tally.failed_integration == 0
    assert tally.items_lost_to_savepoint == 0
    assert tally.assertions_written == 0


def test_an_extraction_whose_entities_all_fail_resolution_is_still_a_failure(kb):
    """The distinction that makes the fix honest. 'The model returned nothing'
    and 'every entity the model returned was refused' are different events:
    the second means work was done and rejected, so it keeps its attempt."""
    iid = _item(kb)
    eid = _entity(kb, name="Shell", type_="company")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'SHEL', 'equity')",
        (eid,),
    )
    kb.commit()
    tally = comprehend.Tally()

    shadowed = dict(
        _fresh(iid), entities=[{"name": "SHEL", "type": "instrument", "aliases": []}]
    )
    assert (
        comprehend.write_extraction(kb, shadowed, comprehend.SurfaceIndex([]), tally)
        is False
    )
    kb.commit()

    assert tally.empty_extraction == 0, "this is a refusal, not an empty extraction"
    assert tally.failed_integration == 1
    assert (
        kb.execute(
            "SELECT integrate_attempts FROM item_triage WHERE item_id = %s", (iid,)
        ).fetchone()[0]
        == 1
    )


def test_a_refusal_that_empties_an_extraction_names_its_cause(kb):
    """news-brief-bqa.14 at the savepoint boundary. `items_lost_to_savepoint=6`
    told the 2026-09-08 operator that six items died and nothing else; the
    cause had to be read out of six stack traces by hand. A named cause is
    what distinguishes this class -- which fails the SAME item every pass and
    is therefore certain to be retired -- from an item that was merely unlucky.
    """
    iid = _item(kb)
    eid = _entity(kb, name="Shell", type_="company")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'SHEL', 'equity')",
        (eid,),
    )
    kb.commit()
    tally = comprehend.Tally()

    shadowed = dict(
        _fresh(iid), entities=[{"name": "SHEL", "type": "instrument", "aliases": []}]
    )
    assert (
        comprehend.write_extraction(kb, shadowed, comprehend.SurfaceIndex([]), tally)
        is False
    )
    kb.commit()

    assert tally.failures == {"savepoint:NoEntitySurvived": 1}


def test_a_written_extraction_names_no_cause(kb):
    """Presence sibling: a recorder that fires on every call through
    write_extraction satisfies the assertion above without discriminating."""
    iid = _item(kb)
    tally = comprehend.Tally()

    assert (
        comprehend.write_extraction(kb, _fresh(iid), comprehend.SurfaceIndex([]), tally)
        is True
    )
    kb.commit()

    assert tally.failures == {}


def test_an_event_with_no_commitment_state_is_written_as_null(kb):
    """The end of the path, in the database. The validator accepting an absent
    commitment_state is worthless if the INSERT then raises on it, and before
    migration 0011 the column was NOT NULL -- so this test fails on the schema,
    not on the code, if the migration is ever reverted."""
    iid = _item(kb)
    tally = comprehend.Tally()

    extraction = _fresh(iid)
    for ev in extraction["events"]:
        ev["commitment_state"] = None

    assert (
        comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
        is True
    )
    kb.commit()

    stored = kb.execute(
        "SELECT e.commitment_state FROM events e "
        "JOIN assertions a ON a.event_id = e.id WHERE a.item_id = %s",
        (iid,),
    ).fetchall()
    assert stored, "the event must actually have been written"
    assert all(row[0] is None for row in stored)


def test_the_check_constraint_still_rejects_a_non_member_value(kb):
    """0011 drops NOT NULL and deliberately leaves the CHECK alone, on the
    grounds that a Postgres CHECK passes when its expression is NULL. That
    reasoning is only safe if the constraint still REJECTS a bad value -- so
    assert the half that must not have changed."""
    with pytest.raises(Exception):
        kb.execute(
            "INSERT INTO events (summary, type, commitment_state, occurred_at) "
            "VALUES ('s', 'action', 'pondering', now())"
        )
    kb.rollback()


def test_write_extraction_tolerates_an_event_with_no_commitment_key_at_all(kb):
    """Makes the `.get` load-bearing instead of decorative.

    A mutation swapping `ev.get("commitment_state")` for `ev[...]` failed ZERO
    tests, because _validate_item always sets the key and every other test
    reaches this function through it. A defensive branch no test can
    distinguish from its absence is a fix shipped on trust -- so drive the
    path that has no validator in front of it, which is what any future caller
    building an extraction by hand would do.
    """
    iid = _item(kb)
    tally = comprehend.Tally()

    extraction = _fresh(iid)
    for ev in extraction["events"]:
        ev.pop("commitment_state", None)
    assert all("commitment_state" not in ev for ev in extraction["events"]), (
        "the fixture must actually omit the key, or this asserts nothing"
    )

    assert (
        comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
        is True
    )
    kb.commit()

    stored = kb.execute(
        "SELECT e.commitment_state FROM events e "
        "JOIN assertions a ON a.event_id = e.id WHERE a.item_id = %s",
        (iid,),
    ).fetchall()
    assert stored and all(row[0] is None for row in stored)


def test_an_extraction_with_events_but_no_entities_is_terminal_not_retried(kb):
    """news-brief-bqa.17, confirmed against a real payload on 2026-09-08.

    Refusing is correct: candidate_events retrieves BY entity id, so an
    entity-less event can never be offered as a candidate or matched, and would
    inflate events_created while never touching events_matched -- depressing
    the corroboration ratio the pre-registered gate reads. What was wrong was
    refusing by raising and charging: the outcome is DETERMINISTIC for this
    item at this prompt version, so it failed identically every pass and burned
    three 8192-token generations to reach a verdict available on the first.
    """
    iid = _item(kb)
    tally = comprehend.Tally()

    extraction = dict(_fresh(iid), entities=[])
    assert extraction["events"], (
        "the fixture must carry events, or it is the empty case"
    )

    assert (
        comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
        is True
    )
    kb.commit()

    assert tally.entityless_extraction == 1
    assert tally.items_lost_to_savepoint == 0, "nothing was rolled back; nothing failed"
    assert tally.failed_integration == 0
    row = kb.execute(
        "SELECT integrated_at, integrate_attempts FROM item_triage WHERE item_id = %s",
        (iid,),
    ).fetchone()
    assert row[0] is not None, "must be terminal, or it is re-offered forever"
    assert row[1] == 0, "the model answered; it just answered 'no entities'"

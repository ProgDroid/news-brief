"""Opaque candidate labels: the extraction contract between prompt and parser.

Deliberately NOT in test_comprehend_integration.py. That module carries a
module-level `pytestmark` skipping the whole file without a database, and every
test here is a pure function call -- `parse_integration_response` and
`build_integration_request` both take plain dicts and touch no connection.
Dropped in there they would have skipped in CI forever while looking like
coverage. Place a test by what it NEEDS, not by what it is ABOUT.

WHY THE CONTRACT CHANGED (news-brief-bqa.11). The first live comprehension pass
rejected 179 of 179 eligible items. The model emitted `candidate_id` on every
entity and event while the offered candidate lists were empty, using the field
as its own local sequence number -- it even reused id 4 for the same actor
across two items, reinventing a coreference mechanism `_resolve_entity` already
provides via ON CONFLICT (lower(name), type).

Raw integer ids make that ambiguous forever. `entities.id` is BIGSERIAL from 1,
so a model counting 1, 2, 3 collides with real ids as soon as the KB holds any
rows, and a validator liberal enough to survive the cold start would then bind
an assertion to an unrelated entity -- silently, permanently, and
indistinguishably from a genuine match. Opaque labels remove the ambiguity
structurally instead of by instruction, which is what makes the liberal
fallback below safe rather than reckless.
"""

import comprehend

NEW_ENTITY = {"name": "Iran", "type": "country"}
NEW_EVENT = {
    "summary": "Iran resumed enrichment at Fordow.",
    "type": "action",
    "commitment_state": "in_force",
    "standing": "reported",
}


def _extraction(items):
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "emit_extraction", "input": {"items": items}}
        ],
    }


def _labelled(entities=None, events=None):
    """One item citing whatever candidate labels the caller names."""
    return {
        "item_id": 1,
        "entities": entities if entities is not None else [],
        "events": events if events is not None else [],
    }


def test_a_mappable_entity_label_resolves_to_the_real_id():
    got = comprehend.parse_integration_response(
        _extraction([_labelled(entities=[{"candidate": "ENT2"}])]),
        {1},
        {"ENT1": 10, "ENT2": 11},
        {},
    )
    assert got[0]["entities"][0]["candidate_id"] == 11, (
        "ENT2 must resolve to 11, not 10 -- two labels are offered precisely so "
        "that returning the first one cannot pass by coincidence"
    )


def test_a_mappable_event_label_resolves_to_the_real_id():
    got = comprehend.parse_integration_response(
        _extraction(
            [_labelled(events=[{"candidate": "EVT2", "standing": "reported"}])]
        ),
        {1},
        {},
        {"EVT1": 20, "EVT2": 21},
    )
    assert got[0]["events"][0]["candidate_id"] == 21


def test_an_unmappable_entity_label_falls_back_to_the_new_entity_path():
    """The cold start depends on this. With an empty KB no label can map, so a
    validator that rejected on an unknown label could never create the first
    entity -- and with no entities there are never any candidates. The pipeline
    would be unable to bootstrap itself by construction."""
    row = _labelled(
        entities=[{"candidate": "ENT1"}, dict(NEW_ENTITY, candidate="ENT9")]
    )
    got = comprehend.parse_integration_response(
        _extraction([row]), {1}, {"ENT1": 10}, {}
    )
    assert got[0]["entities"] == [
        {"candidate_id": 10},
        {"name": "Iran", "type": "country", "aliases": []},
    ], "the mappable label must resolve AND the unmappable one fall back"


def test_an_unmappable_entity_label_with_no_usable_name_drops_the_item():
    """The presence-sibling above must not be readable as 'unmappable labels are
    always fine'. With nothing to fall back to, the item still dies."""
    row = _labelled(entities=[{"candidate": "ENT9"}])
    got = comprehend.parse_integration_response(_extraction([row]), {1}, {}, {})
    assert got == []


def test_an_unmappable_event_label_falls_back_to_the_new_event_path():
    """Both loops need the fallback. Fixing one arm and testing only that one is
    how the boolean/float candidate-id hole survived on the event side."""
    row = _labelled(
        events=[
            {"candidate": "EVT1", "standing": "reported"},
            dict(NEW_EVENT, candidate="EVT9"),
        ]
    )
    got = comprehend.parse_integration_response(
        _extraction([row]), {1}, {}, {"EVT1": 20}
    )
    assert got[0]["events"][0]["candidate_id"] == 20
    assert got[0]["events"][1]["summary"] == "Iran resumed enrichment at Fordow."


def test_an_unmappable_label_is_counted_rather_than_silently_absorbed():
    """failed_integration=172 was unattributable because Tally.failures is
    written by nothing. A fallback that left no trace would repeat that."""
    tally = comprehend.Tally()
    row = _labelled(entities=[dict(NEW_ENTITY, candidate="ENT9")])
    comprehend.parse_integration_response(_extraction([row]), {1}, {}, {}, tally)
    assert tally.unmapped_candidate == 1


def test_a_compliant_extraction_counts_no_unmapped_candidates():
    """Presence sibling for the counter: it must distinguish compliance from
    non-compliance, not increment on everything that passes through."""
    tally = comprehend.Tally()
    row = _labelled(entities=[{"candidate": "ENT1"}])
    comprehend.parse_integration_response(
        _extraction([row]), {1}, {"ENT1": 10}, {}, tally
    )
    assert tally.unmapped_candidate == 0


def test_a_candidate_that_looks_like_a_raw_id_does_not_resolve():
    """THE regression this redesign exists to prevent. Under integer ids the
    model's local counter collided with real BIGSERIAL ids and bound the
    assertion to an unrelated row. '4' is not a label, so it must miss."""
    row = _labelled(entities=[{"candidate": "ENT1"}, dict(NEW_ENTITY, candidate="4")])
    got = comprehend.parse_integration_response(
        _extraction([row]), {1}, {"ENT1": 4}, {}
    )
    assert got[0]["entities"] == [
        {"candidate_id": 4},
        {"name": "Iran", "type": "country", "aliases": []},
    ], (
        "ENT1 must resolve to 4, while the literal '4' must NOT -- that is the "
        "whole difference between a label space and a raw id space"
    )


def test_a_non_string_candidate_does_not_resolve():
    """The integer-coercion hole in its new form: under labels the hazard is a
    value that is not a string at all. `True in {'ENT1': 4}` is False only by
    luck of type -- assert it rather than trusting it."""
    row = _labelled(entities=[{"candidate": "ENT1"}, dict(NEW_ENTITY, candidate=True)])
    got = comprehend.parse_integration_response(
        _extraction([row]), {1}, {"ENT1": 4}, {}
    )
    assert got[0]["entities"] == [
        {"candidate_id": 4},
        {"name": "Iran", "type": "country", "aliases": []},
    ]


def test_a_cold_start_extraction_succeeds_with_no_candidates_offered():
    """End to end for the deadlock: zero candidates, model labels everything
    anyway, and the item still yields a new entity and a new event."""
    row = _labelled(
        entities=[dict(NEW_ENTITY, candidate="ENT1")],
        events=[dict(NEW_EVENT, candidate="EVT1")],
    )
    got = comprehend.parse_integration_response(_extraction([row]), {1}, {}, {})
    assert len(got) == 1
    assert got[0]["entities"][0]["name"] == "Iran"
    assert got[0]["events"][0]["summary"] == "Iran resumed enrichment at Fordow."


def test_the_integration_prompt_renders_labels_not_raw_ids():
    """Raw ids must not reach the model at all. Exposing them is what made an
    invented integer indistinguishable from a real reference."""
    req = comprehend.build_integration_request(
        [{"id": 1, "title": "t", "body": "b", "outlet": "o"}],
        [{"id": 4, "name": "Iran", "type": "country"}],
        [{"id": 77, "summary": "something happened"}],
    )
    content = req["messages"][0]["content"]
    assert "ENT1 Iran" in content
    assert "EVT1 something happened" in content
    assert "id=4" not in content
    assert "id=77" not in content


def test_the_integration_prompt_tells_the_model_to_omit_the_label_for_new_things():
    """The label scheme alone does not fix the cold start -- the model has to be
    told the field is a reference, not an id it may mint. This is the half of
    the fix the fallback exists to back up, not replace."""
    system = comprehend.build_integration_request([], [], [])["system"]
    assert "omit" in system.lower()

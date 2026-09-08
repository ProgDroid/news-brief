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

import pytest

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


# --- Which branch rejected the row (news-brief-bqa.14).
#
# `Tally.failures` was declared at comprehend.py:73 and written by nothing, so
# a pass reporting failed_integration=72 of 259 material items still printed
# `failures={}` -- which reads as "no failure details" rather than "field nobody
# populates". The aggregate cannot separate an item that was UNLUCKY from one
# that is DOOMED: against a 3-strike one-way door an independent 28% failure
# rate retires ~2% of the corpus per sweep, while a systematic class fails the
# same item every time and is certain to die, just three hours slower.
#
# _validate_item reports on ITSELF. news-brief-bqa.12 refused a hand-written
# mirror of this function on the grounds that a copy agrees with itself by
# construction and cannot detect its own drift; a function naming its own
# branch has no drift surface at all.


def _rejected_because(row):
    """The failure keys recorded while rejecting `row`, having checked it was
    actually rejected -- a cause recorded for a row that survived would be a
    lie the assertions below could not see."""
    tally = comprehend.Tally()
    got = comprehend.parse_integration_response(_extraction([row]), {1}, {}, {}, tally)
    assert got == [], "the row must be rejected for its recorded cause to mean anything"
    return tally.failures


def test_an_item_id_that_was_never_offered_names_its_branch():
    assert _rejected_because(dict(_labelled(), item_id=99)) == {"validate:item_id": 1}


def test_an_entity_that_is_not_an_object_names_its_branch():
    assert _rejected_because(_labelled(entities=["Iran"])) == {
        "validate:entity_shape": 1
    }


def test_an_entity_with_an_unknown_type_names_its_branch():
    assert _rejected_because(
        _labelled(entities=[dict(NEW_ENTITY, type="spaceship")])
    ) == {"validate:entity_type_unknown": 1}


def test_an_event_with_an_unknown_standing_names_its_branch():
    assert _rejected_because(
        _labelled(events=[dict(NEW_EVENT, standing="rumoured")])
    ) == {"validate:event_shape": 1}


def test_an_event_with_a_blank_summary_names_its_branch():
    assert _rejected_because(_labelled(events=[dict(NEW_EVENT, summary="   ")])) == {
        "validate:event_summary": 1
    }


def test_an_event_with_an_unknown_commitment_state_names_its_branch():
    assert _rejected_because(
        _labelled(events=[dict(NEW_EVENT, commitment_state="pondering")])
    ) == {"validate:commitment_unknown": 1}


def test_a_clean_extraction_records_no_failure_at_all():
    """Presence sibling for the whole dict, and the only test above that can
    fail a `_note()` firing unconditionally -- six assertions that a key is
    PRESENT are each satisfied by a recorder that never stops recording."""
    tally = comprehend.Tally()
    got = comprehend.parse_integration_response(
        _extraction([_labelled(entities=[dict(NEW_ENTITY)], events=[dict(NEW_EVENT)])]),
        {1},
        {},
        {},
        tally,
    )
    assert len(got) == 1
    assert tally.failures == {}


def test_the_recorded_cause_distinguishes_two_different_defects():
    """Independently-derived expectation rather than a restatement: two rows
    failing for two reasons must produce two DIFFERENT keys. A recorder keyed
    by a constant passes every single-row test above."""
    tally = comprehend.Tally()
    comprehend.parse_integration_response(
        _extraction(
            [
                _labelled(entities=[dict(NEW_ENTITY, type="spaceship")]),
                dict(_labelled(), item_id=99),
            ]
        ),
        {1},
        {},
        {},
        tally,
    )
    assert tally.failures == {"validate:entity_type_unknown": 1, "validate:item_id": 1}


# --- MISSING vs UNKNOWN, the distinction that chooses the fix.
#
# The merged `validate:event_enums` key ranked this cause first on 2026-09-08
# (73 of 108 failures) and then could not settle what to do about it. `missing`
# means the tool schema does not require the field and the model is obeying it
# as published, which is a SCHEMA fix (news-brief-bqa.16). `unknown` means the
# model invented a value its own declared enum forbids, which is not.


def test_an_omitted_event_type_reads_as_missing():
    ev = {k: v for k, v in NEW_EVENT.items() if k != "type"}
    assert _rejected_because(_labelled(events=[ev])) == {
        "validate:event_type_missing": 1
    }


def test_an_explicit_null_reads_as_unknown_not_missing():
    """The discriminator is MEMBERSHIP, not a None check. A field explicitly
    set to null was supplied — the model made a choice — so it is
    present-but-wrong, and pointing the fix at the schema would be wrong."""
    assert _rejected_because(_labelled(events=[dict(NEW_EVENT, type=None)])) == {
        "validate:event_type_unknown": 1
    }


def test_an_omitted_entity_type_reads_as_missing():
    """The entity object carries the identical latent hazard: it declares NO
    required list at all, so this branch is one payload away from firing."""
    ent = {k: v for k, v in NEW_ENTITY.items() if k != "type"}
    assert _rejected_because(_labelled(entities=[ent])) == {
        "validate:entity_type_missing": 1
    }


def test_a_blank_entity_name_is_named_apart_from_its_type():
    """The old key merged name and type. Splitting them is only real if a name
    defect cannot be reported as a type defect."""
    assert _rejected_because(_labelled(entities=[dict(NEW_ENTITY, name="  ")])) == {
        "validate:entity_name": 1
    }


def test_the_rejected_value_is_logged_but_stays_out_of_the_key(caplog):
    """A model can emit arbitrary strings, so an unbounded key space would make
    `failures` unreadable exactly when it matters most. The key must stay at
    four bounded outcomes while the value is still recoverable."""
    with caplog.at_level("WARNING"):
        failures = _rejected_because(
            _labelled(events=[dict(NEW_EVENT, commitment_state="pondering")])
        )
    assert failures == {"validate:commitment_unknown": 1}
    assert "pondering" in caplog.text
    assert not any("pondering" in k for k in failures)


def test_a_missing_field_logs_no_value_because_there_is_none(caplog):
    """Presence sibling for the logger: one that fired unconditionally would
    satisfy the test above and emit a bare `None` on every omission.

    Uses `type` rather than `commitment_state` as the vehicle: 0011 made an
    absent commitment_state legitimate, so it no longer reaches a rejection at
    all. `type` still does, which is what keeps this test about the LOGGER."""
    ev = {k: v for k, v in NEW_EVENT.items() if k != "type"}
    with caplog.at_level("WARNING"):
        _rejected_because(_labelled(events=[ev]))
    assert "rejected event type" not in caplog.text


# --- commitment_state is optional, because it is a property of COMMITMENTS
# --- (news-brief-bqa.16, migration 0011).
#
# 112 of 152 integration failures in the 11:00 pass on 2026-09-08 were
# `validate:commitment_missing` -- 38% of all material items dropped WHOLE for
# a field the tool schema never required. The model supplied `type` on 100% of
# events over the same corpus, so it is discriminating rather than sloppy: for
# a factual report there IS no commitment state.


def test_a_new_event_without_a_commitment_state_is_accepted():
    ev = {k: v for k, v in NEW_EVENT.items() if k != "commitment_state"}
    got = comprehend.parse_integration_response(
        _extraction([_labelled(events=[ev])]), {1}, {}, {}
    )
    assert len(got) == 1, "an omitted commitment_state must not drop the item"
    assert got[0]["events"][0]["commitment_state"] is None


def test_an_omitted_commitment_state_is_counted():
    """It is now a field that is sometimes absent, and this repo measures those
    rather than letting them go quiet. Without a count nobody can tell 'a rare
    edge case' from 'the majority of the corpus'."""
    tally = comprehend.Tally()
    ev = {k: v for k, v in NEW_EVENT.items() if k != "commitment_state"}
    comprehend.parse_integration_response(
        _extraction([_labelled(events=[ev])]), {1}, {}, {}, tally
    )
    assert tally.commitment_omitted == 1
    assert tally.failures == {}, "an omission is no longer a failure"


def test_a_supplied_commitment_state_is_not_counted_as_omitted():
    """Presence sibling. A counter that incremented on every event would
    satisfy the test above while measuring nothing."""
    tally = comprehend.Tally()
    comprehend.parse_integration_response(
        _extraction([_labelled(events=[dict(NEW_EVENT)])]), {1}, {}, {}, tally
    )
    assert tally.commitment_omitted == 0


def test_an_invalid_commitment_state_is_still_rejected():
    """Relaxing the field must not relax the ENUM. 'Absent' means the event is
    not a commitment; 'pondering' means the model ignored its own schema, and
    accepting that would write a value the CHECK constraint rejects anyway."""
    assert _rejected_because(
        _labelled(events=[dict(NEW_EVENT, commitment_state="pondering")])
    ) == {"validate:commitment_unknown": 1}


def test_an_explicit_null_commitment_state_is_accepted_as_absent():
    """A model that writes the field as null is saying the same thing as one
    that omits it, and dropping the item over the difference would reintroduce
    the bug for a second spelling of the same answer."""
    got = comprehend.parse_integration_response(
        _extraction([_labelled(events=[dict(NEW_EVENT, commitment_state=None)])]),
        {1},
        {},
        {},
    )
    assert len(got) == 1
    assert got[0]["events"][0]["commitment_state"] is None


def test_an_event_type_is_still_required():
    """The two fields are NOT symmetric and must not be relaxed together.
    `type` was supplied on 100% of events across the measured corpus, so an
    absent one is a real defect rather than a category that does not apply."""
    ev = {k: v for k, v in NEW_EVENT.items() if k != "type"}
    assert _rejected_because(_labelled(events=[ev])) == {
        "validate:event_type_missing": 1
    }


def test_a_malformed_tool_input_names_what_actually_arrived():
    """news-brief-bqa: this fired 35 times in one pass, whole batches at a
    time, and the bare message could not distinguish 'items was absent' from
    'items was a dict' from 'it arrived under another key'. Same unactionable
    shape as an HTTPError that stringifies to a status code."""
    resp = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "emit_extraction",
                "input": {"extractions": {"item_id": 1}},
            }
        ],
    }
    with pytest.raises(ValueError) as exc:
        comprehend.parse_integration_response(resp, {1}, {}, {})

    message = str(exc.value)
    assert "extractions" in message, "the key that DID arrive must be named"
    assert "NoneType" in message, "the type that arrived under 'items' must be named"


def test_a_wrongly_typed_items_field_is_distinguishable_from_an_absent_one():
    """The two produce the same exception without the detail, and they are not
    the same defect: one is a missing key, the other a shape error."""
    resp = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "emit_extraction",
                "input": {"items": {"item_id": 1}},
            }
        ],
    }
    with pytest.raises(ValueError) as exc:
        comprehend.parse_integration_response(resp, {1}, {}, {})
    assert "dict" in str(exc.value)

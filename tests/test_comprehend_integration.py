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

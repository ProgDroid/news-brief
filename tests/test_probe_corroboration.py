"""scripts/probe_corroboration.py: the diagnostic's own logic.

NO module-level skipmark, deliberately. The similarity and quantile functions
are pure and must run in CI; only the query-backed tests need a database, and
they skip through the `kb` fixture instead. Placing the pure tests behind a
file-wide skip would have them silently absent from CI while looking like
coverage -- the mistake tests/test_comprehend_labels.py exists to avoid.

This probe decides which of three fixes the corroboration problem gets, so an
error in it misdirects the entire design. Its two load-bearing properties are
that the threshold is CALIBRATED on real duplicates rather than invented, and
that `was_retrievable` reconstructs candidate_events' ranking rather than an
adjacent one.
"""

import datetime as dt

import pytest

import db
from scripts import probe_corroboration as pc


@pytest.fixture()
def kb():
    if not db.is_configured():
        pytest.skip("No database configured: start a Postgres and export DATABASE_URL")
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


# --- Pure: the detector itself.


def test_identical_headlines_score_one():
    assert pc.similarity("Iran resumes enrichment", "Iran resumes enrichment") == 1.0


def test_unrelated_headlines_score_zero():
    assert pc.similarity("Iran resumes enrichment", "Brazil harvest beats") == 0.0


def test_headlines_sharing_only_noise_words_score_zero():
    """The noise list is load-bearing rather than cosmetic. If 'the', 'says'
    and 'after' counted, every pair of headlines would drift toward the mean
    and flatten the very distinction the calibrated threshold depends on."""
    assert pc.similarity("The bank says it is up", "The court says it is up") < 0.34


def test_similarity_is_symmetric():
    a, b = "Iran resumes enrichment at Fordow", "Fordow enrichment resumes, Iran says"
    assert pc.similarity(a, b) == pc.similarity(b, a)


def test_an_empty_headline_scores_zero_rather_than_dividing_by_zero():
    assert pc.similarity("", "Iran resumes enrichment") == 0.0


def test_quantile_picks_a_real_element():
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert pc.quantile(values, 0.0) == 0.1
    assert pc.quantile(values, 0.5) == 0.3
    assert pc.quantile(values, 1.0) == 0.5


def test_quantile_of_nothing_is_zero_not_an_error():
    assert pc.quantile([], 0.25) == 0.0


# --- Query-backed helpers.


def _outlet(kb, name):
    return kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _item(kb, outlet_id, title, h):
    return kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', %s, %s) RETURNING id",
        (outlet_id, title, h),
    ).fetchone()[0]


def _event(kb, summary, occurred_at=None, created_at=None):
    event_id = kb.execute(
        "INSERT INTO events (summary, type, occurred_at) "
        "VALUES (%s, 'action', COALESCE(%s, now())) RETURNING id",
        (summary, occurred_at),
    ).fetchone()[0]
    if created_at is not None:
        kb.execute(
            "UPDATE events SET created_at = %s WHERE id = %s", (created_at, event_id)
        )
    return event_id


def _assert(kb, item_id, event_id):
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )


def _entity(kb, name):
    return kb.execute(
        "INSERT INTO entities (name, type) VALUES (%s, 'country') RETURNING id", (name,)
    ).fetchone()[0]


def _link(kb, event_id, entity_id):
    kb.execute(
        "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)",
        (event_id, entity_id),
    )


# --- Calibration: the positive control the threshold is read from.


def test_calibration_uses_cross_outlet_pairs_of_one_event(kb):
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    event_id = _event(kb, "Iran resumed enrichment")
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment", "h1"), event_id)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment at Fordow", "h2"), event_id)
    kb.commit()

    pairs = pc.calibration_pairs(kb, 7)
    assert len(pairs) == 1
    assert pairs[0][0] > 0.5, "two reports of one event must score as similar"


def test_calibration_ignores_two_items_from_the_SAME_outlet(kb):
    """Republication within one outlet is not corroboration, and counting it
    would calibrate the threshold on near-identical text -- pushing it so high
    that genuine cross-outlet duplicates fall below the line."""
    reuters = _outlet(kb, "Reuters")
    event_id = _event(kb, "Iran resumed enrichment")
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment", "h1"), event_id)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment", "h2"), event_id)
    kb.commit()

    assert pc.calibration_pairs(kb, 7) == []


# --- Misses: what the matcher failed to merge.


def _two_outlet_miss(kb, gap_hours=1, same_outlet=False, share_entity=True):
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    now = dt.datetime.now(dt.timezone.utc)
    iran, other = _entity(kb, "Iran"), _entity(kb, "Chile")

    e1 = _event(
        kb, "Iran resumed enrichment", created_at=now - dt.timedelta(hours=gap_hours)
    )
    e2 = _event(kb, "Iran restarted enrichment", created_at=now)
    _link(kb, e1, iran)
    _link(kb, e2, iran if share_entity else other)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment at Fordow", "h1"), e1)
    _assert(
        kb,
        _item(
            kb, reuters if same_outlet else ap, "Iran resumes enrichment Fordow", "h2"
        ),
        e2,
    )
    kb.commit()
    return e1, e2


def test_a_cross_outlet_pair_with_different_events_is_a_miss(kb):
    _two_outlet_miss(kb)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    assert len(pc.probable_misses(rows, ents, 0.3)) == 1


def test_an_already_merged_pair_is_a_success_not_a_miss(kb):
    """The absence assertion that keeps the count honest. Two items on ONE
    event is corroboration working; counting it as a miss would report the
    successes as failures and inflate the ceiling."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    iran = _entity(kb, "Iran")
    event_id = _event(kb, "Iran resumed enrichment")
    _link(kb, event_id, iran)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment Fordow", "h1"), event_id)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment Fordow", "h2"), event_id)
    kb.commit()

    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    assert pc.probable_misses(rows, ents, 0.3) == []


def test_a_same_outlet_pair_is_not_a_miss(kb):
    _two_outlet_miss(kb, same_outlet=True)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    assert pc.probable_misses(rows, ents, 0.3) == []


def test_a_pair_outside_the_time_window_is_not_a_miss(kb):
    _two_outlet_miss(kb, gap_hours=pc.PAIR_WINDOW_HOURS + 5)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    assert pc.probable_misses(rows, ents, 0.3) == []


def test_a_pair_sharing_no_entity_is_not_a_miss(kb):
    """It could never have been offered under ANY ranking, so counting it
    would blame retrieval for something retrieval was never asked to do."""
    _two_outlet_miss(kb, share_entity=False)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    assert pc.probable_misses(rows, ents, 0.3) == []


# --- Retrievability: the reconstruction that decides A vs B.


def test_a_nearby_duplicate_was_retrievable(kb):
    e1, e2 = _two_outlet_miss(kb)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    miss = pc.probable_misses(rows, ents, 0.3)[0]
    assert pc.was_retrievable(kb, miss, ents) is True


def test_an_event_created_LATER_could_not_have_been_offered(kb):
    """What makes this a reconstruction rather than a query about today. The
    live candidate_events has no created_at filter because it runs in the
    present; without one here every duplicate would look retrievable."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    now = dt.datetime.now(dt.timezone.utc)
    iran = _entity(kb, "Iran")
    early = _event(
        kb, "Iran resumed enrichment", created_at=now - dt.timedelta(hours=2)
    )
    late = _event(kb, "Iran restarted enrichment", created_at=now)
    _link(kb, early, iran)
    _link(kb, late, iran)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment Fordow", "h1"), early)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment Fordow", "h2"), late)
    kb.commit()

    ents = pc.entities_by_event(kb, 7)
    # Reversed on purpose: ask whether the LATER event could have been offered
    # when the EARLIER one was created. It did not exist yet.
    backwards = {
        "earlier": {"event_id": late},
        "later": {"event_id": early, "created_at": now - dt.timedelta(hours=2)},
    }
    assert pc.was_retrievable(kb, backwards, ents) is False


def test_the_cap_pushes_an_older_duplicate_out_of_reach(kb):
    """THE thesis of this whole investigation, asserted directly rather than
    argued. candidate_events offers the CANDIDATE_EVENT_CAP most RECENT events
    sharing an entity. Bury a genuine duplicate under that many newer events on
    the same hub entity and it becomes unreachable -- not because the matcher
    is weak, but because it is never shown the answer.
    """
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    now = dt.datetime.now(dt.timezone.utc)
    iran = _entity(kb, "Iran")

    old = _event(
        kb,
        "Iran resumed enrichment",
        occurred_at=now - dt.timedelta(days=2),
        created_at=now - dt.timedelta(days=2),
    )
    _link(kb, old, iran)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment Fordow", "h-old"), old)

    # Bury it: CAP newer events on the same entity, all more recent.
    for n in range(pc.comprehend.CANDIDATE_EVENT_CAP):
        filler = _event(
            kb,
            f"Unrelated Iran development {n}",
            occurred_at=now - dt.timedelta(hours=n + 1),
            created_at=now - dt.timedelta(hours=n + 1),
        )
        _link(kb, filler, iran)

    new = _event(kb, "Iran restarted enrichment", occurred_at=now, created_at=now)
    _link(kb, new, iran)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment Fordow", "h-new"), new)
    kb.commit()

    ents = pc.entities_by_event(kb, 7)
    miss = {
        "earlier": {"event_id": old},
        "later": {"event_id": new, "created_at": now + dt.timedelta(seconds=1)},
    }
    assert pc.was_retrievable(kb, miss, ents) is False, (
        "the duplicate must be out of reach once CAP newer events bury it"
    )


def test_the_same_duplicate_IS_reachable_when_nothing_buries_it(kb):
    """Presence sibling for the test above. Without it, that assertion is
    satisfied by a was_retrievable() that always returns False, and the whole
    A-versus-B split would read as B by construction."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    now = dt.datetime.now(dt.timezone.utc)
    iran = _entity(kb, "Iran")

    old = _event(
        kb,
        "Iran resumed enrichment",
        occurred_at=now - dt.timedelta(days=2),
        created_at=now - dt.timedelta(days=2),
    )
    _link(kb, old, iran)
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment Fordow", "h-old"), old)

    new = _event(kb, "Iran restarted enrichment", occurred_at=now, created_at=now)
    _link(kb, new, iran)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment Fordow", "h-new"), new)
    kb.commit()

    ents = pc.entities_by_event(kb, 7)
    miss = {
        "earlier": {"event_id": old},
        "later": {"event_id": new, "created_at": now + dt.timedelta(seconds=1)},
    }
    assert pc.was_retrievable(kb, miss, ents) is True


# --- The guard that stops the probe reporting a shaped guess.


def test_too_few_calibration_pairs_refuses_to_report(kb):
    """A threshold read off a handful of pairs is noise, and every count
    downstream would inherit it. Exit 2 means NOT MEASURABLE, which must stay
    distinguishable from a measured zero."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    event_id = _event(kb, "Iran resumed enrichment")
    _assert(kb, _item(kb, reuters, "Iran resumes enrichment", "h1"), event_id)
    _assert(kb, _item(kb, ap, "Iran resumes enrichment", "h2"), event_id)
    kb.commit()

    assert pc.report(kb, 7) == 2

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
import random

import pytest

import comprehend
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
    misses, _ = pc.probable_misses(rows, ents, 0.3)
    assert len(misses) == 1


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
    misses, _ = pc.probable_misses(rows, ents, 0.3)
    assert misses == []


def test_a_same_outlet_pair_is_not_a_miss(kb):
    _two_outlet_miss(kb, same_outlet=True)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    misses, _ = pc.probable_misses(rows, ents, 0.3)
    assert misses == []


def test_a_pair_outside_the_time_window_is_not_a_miss(kb):
    _two_outlet_miss(kb, gap_hours=pc.PAIR_WINDOW_HOURS + 5)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    misses, _ = pc.probable_misses(rows, ents, 0.3)
    assert misses == []


def test_a_pair_sharing_no_entity_is_not_a_miss(kb):
    """It could never have been offered under ANY ranking, so counting it
    would blame retrieval for something retrieval was never asked to do."""
    _two_outlet_miss(kb, share_entity=False)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    misses, _ = pc.probable_misses(rows, ents, 0.3)
    assert misses == []


# --- Retrievability: the reconstruction that decides A vs B.


def test_a_nearby_duplicate_was_retrievable(kb):
    e1, e2 = _two_outlet_miss(kb)
    rows, ents = pc.event_rows(kb, 7), pc.entities_by_event(kb, 7)
    miss = pc.probable_misses(rows, ents, 0.3)[0][0]
    assert pc.was_retrievable(kb, miss, ents) is True


def test_an_event_created_LATER_could_not_have_been_offered(kb):
    """What makes this a reconstruction rather than a query about today.
    candidate_events now takes an as_of (news-brief-bqa.26) and filters
    `created_at < as_of`; in production that is now(), so it excludes nothing
    but this batch's own writes. Here it is what stops every duplicate from
    looking retrievable in hindsight."""
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
        "later": {
            "event_id": early,
            "title": "Iran resumes enrichment Fordow",
            "created_at": now - dt.timedelta(hours=2),
        },
    }
    assert pc.was_retrievable(kb, backwards, ents) is False


def test_the_cap_still_buries_a_duplicate_that_shares_no_vocabulary(kb):
    """The thesis, restated for the ranking that now runs. Under recency ANY
    older duplicate fell off the cap, which is what this test used to assert;
    under trigram similarity a duplicate the item READS like survives burial
    (see test_was_retrievable_follows_the_live_ranking_not_recency), and only
    one sharing no vocabulary is still lost.

    That is a narrower failure mode than the old one, and it is the one left
    to fix: positives reach p50=0.143, so most true duplicates do share little
    vocabulary.
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

    for n in range(pc.comprehend.CANDIDATE_EVENT_CAP):
        filler = _event(
            kb,
            f"Sanctions relief talks stall in round {n}",
            occurred_at=now - dt.timedelta(hours=n + 1),
            created_at=now - dt.timedelta(hours=n + 1),
        )
        _link(kb, filler, iran)

    new = _event(kb, "Iran restarted enrichment", occurred_at=now, created_at=now)
    _link(kb, new, iran)
    _assert(kb, _item(kb, ap, "Sanctions relief talks stall in Vienna", "h-new"), new)
    kb.commit()

    ents = pc.entities_by_event(kb, 7)
    miss = {
        "earlier": {"event_id": old},
        # The FILLERS match this title and the duplicate does not, so the
        # burial is decisive rather than a coin flip. An earlier version gave
        # every candidate a near-zero score, which made the ordering noise and
        # the duplicate survived by luck -- a flaky test dressed as a passing
        # one.
        "later": {
            "event_id": new,
            "title": "Sanctions relief talks stall in Vienna",
            "created_at": now + dt.timedelta(seconds=1),
        },
    }
    assert pc.was_retrievable(kb, miss, ents) is False, (
        "with no lexical signal to lift it, the older duplicate is still the "
        "row that falls off the end"
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
        "later": {
            "event_id": new,
            "title": "Iran resumes enrichment Fordow",
            "created_at": now + dt.timedelta(seconds=1),
        },
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


# --- The ceiling. The first version counted PAIRS as merges and reported
# --- 1779%, so this is the arithmetic that has already been wrong once.


def test_a_chain_of_three_events_is_two_merges_not_three_pairs():
    """A->B, B->C and A->C are three PAIRS but one cluster of three, which
    collapses to a single event: two merges. Counting pairs is what produced a
    ceiling above 100%."""
    misses = [
        {"earlier": {"event_id": 1}, "later": {"event_id": 2}},
        {"earlier": {"event_id": 2}, "later": {"event_id": 3}},
        {"earlier": {"event_id": 1}, "later": {"event_id": 3}},
    ]
    assert pc.merges_from_pairs(misses) == 2


def test_two_disjoint_pairs_are_two_merges():
    """Presence sibling: a counter that always returned cluster_count - 1, or
    always 1, would satisfy the test above."""
    misses = [
        {"earlier": {"event_id": 1}, "later": {"event_id": 2}},
        {"earlier": {"event_id": 3}, "later": {"event_id": 4}},
    ]
    assert pc.merges_from_pairs(misses) == 2


def test_a_repeated_pair_is_still_one_merge():
    misses = [
        {"earlier": {"event_id": 1}, "later": {"event_id": 2}},
        {"earlier": {"event_id": 1}, "later": {"event_id": 2}},
    ]
    assert pc.merges_from_pairs(misses) == 1


def test_no_pairs_is_no_merges():
    assert pc.merges_from_pairs([]) == 0


# --- Syndication: the same wire copy in two feeds is not confirmation.


def test_a_source_suffix_does_not_make_two_headlines_different():
    assert pc.is_syndication(
        "Uber to exit Nigeria after 12 years of operations - Reuters",
        "Uber to exit Nigeria after 12 years of operations",
    )


def test_curly_and_straight_quotes_are_the_same_headline():
    assert pc.is_syndication(
        "Vance says Iran conflict is 'not a war', declines to offer timeline",
        "Vance says Iran conflict is \u2018not a war\u2019, declines to offer timeline",
    )


def test_two_outlets_writing_their_own_headline_is_NOT_syndication(kb=None):
    """The distinction the whole flag exists for. These report one event in
    different words -- genuine independent confirmation, which section 8.2 is
    asking about. Marking it syndicated would erase the finding."""
    assert not pc.is_syndication(
        "UK minister condemns anti-migrant protests in Portsmouth",
        "Minister slams thuggish behaviour at Portsmouth migrant demonstration",
    )


def test_the_suffix_stripper_does_not_amputate_a_real_headline():
    """`_SOURCE_SUFFIX` is bounded on purpose. An unbounded trailing-dash rule
    would eat the second half of any headline containing a dash, silently
    turning unrelated stories into syndication."""
    long_tail = "Iran - what happens next in the long war over enrichment and sanctions"
    assert pc.normalise_title(long_tail).endswith("sanctions")


# --- Banding and bucketing.


def test_every_band_is_reachable_and_they_do_not_overlap():
    """A band table is only readable if each score lands in exactly one row."""
    for score in (0.15, 0.25, 0.4, 0.6, 0.9, 1.0):
        assert pc.band_of(score) is not None
    assert pc.band_of(0.05) is None, "below the lowest band is not a band"
    seen = [pc.band_of(s) for s in (0.15, 0.25, 0.4, 0.6, 0.9)]
    assert len(set(seen)) == len(seen)


def test_hub_buckets_straddle_the_candidate_cap():
    """The hypothesis is that B dominates once an entity carries more events
    than the cap can offer, so a bucket boundary must sit AT the cap or the
    table cannot show the transition."""
    boundaries = {lo for lo, _ in pc.HUB_BUCKETS} | {hi for _, hi in pc.HUB_BUCKETS}
    assert comprehend.CANDIDATE_EVENT_CAP in boundaries


def test_hub_bucket_places_a_value_in_exactly_one_bucket():
    assert pc.hub_bucket(5) == (0, 10)
    assert pc.hub_bucket(30) == (30, 100)
    assert pc.hub_bucket(99999) == pc.HUB_BUCKETS[-1]


# --- The negative class.


def test_negatives_are_pairs_far_apart_in_time(kb):
    """Contamination is the risk: sampling ALL pairs would include real
    duplicates and drag the negative distribution up, which would push the
    threshold up and hide the misses. Temporal separation is what keeps the
    class clean."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    now = dt.datetime.now(dt.timezone.utc)
    iran = _entity(kb, "Iran")

    near_a = _event(kb, "a", created_at=now)
    near_b = _event(kb, "b", created_at=now - dt.timedelta(hours=1))
    far = _event(kb, "c", created_at=now - dt.timedelta(days=pc.NEGATIVE_MIN_DAYS + 1))
    for e in (near_a, near_b, far):
        _link(kb, e, iran)
    _assert(kb, _item(kb, reuters, "Iran enrichment resumes today", "h1"), near_a)
    _assert(kb, _item(kb, ap, "Iran enrichment resumes today", "h2"), near_b)
    _assert(kb, _item(kb, ap, "Iran enrichment resumes today", "h3"), far)
    kb.commit()

    scores = pc.negative_pairs(kb, 14, __import__("random").Random(1))
    # near_a/near_b are 1h apart and must NOT appear; only pairs involving
    # `far` qualify, and there is exactly one such cross-outlet pair.
    assert len(scores) <= 1


# --- The ranking bake-off. Its whole value is the comparison, so the thing to
# --- guard is that the strategies actually differ AND that the baseline arm
# --- reproduces the live query.


def _pool(n_entities_shared, occurred_at, summary="x", eid=1):
    return {
        "id": eid,
        "occurred_at": occurred_at,
        "summary": summary,
        "entities": set(range(n_entities_shared)),
    }


def _miss(title="Iran resumes enrichment at Fordow", event_id=99):
    return {"later": {"event_id": event_id, "title": title}}


def test_entity_overlap_beats_recency_when_they_disagree():
    """The whole hypothesis in one assertion. An older event sharing three
    actors must outrank a newer one sharing one -- if the two strategies cannot
    disagree here, the bake-off can never show a difference."""
    now = dt.datetime.now(dt.timezone.utc)
    old_rich = _pool(3, now - dt.timedelta(days=2), eid=1)
    new_poor = _pool(1, now, eid=2)
    ents = {99: {0, 1, 2}}

    by_overlap = pc.rank_entity_overlap([new_poor, old_rich], _miss(), ents)
    by_recency = pc.rank_recency([new_poor, old_rich], _miss(), ents)

    assert by_overlap[0]["id"] == 1, "three shared entities must win"
    assert by_recency[0]["id"] == 2, "and recency must disagree, or nothing is tested"


def test_title_similarity_ranks_the_matching_summary_first():
    now = dt.datetime.now(dt.timezone.utc)
    decoy = _pool(1, now, summary="Brazil soybean harvest beats forecasts", eid=1)
    match = _pool(
        1,
        now - dt.timedelta(days=3),
        summary="Iran resumed enrichment at Fordow",
        eid=2,
    )
    ranked = pc.rank_title_similarity([decoy, match], _miss(), {99: {0}})
    assert ranked[0]["id"] == 2


def test_the_hybrid_takes_from_more_than_one_arm():
    """A hybrid that silently collapses to one strategy would score identically
    to it and read as agreement between methods."""
    now = dt.datetime.now(dt.timezone.utc)
    overlap_pick = _pool(3, now - dt.timedelta(days=5), summary="unrelated text", eid=1)
    lexical_pick = _pool(
        1, now - dt.timedelta(days=4), summary="Iran resumed enrichment Fordow", eid=2
    )
    recent_pick = _pool(1, now, summary="another unrelated thing", eid=3)
    ents = {99: {0, 1, 2}}

    picked = {
        e["id"]
        for e in pc.rank_hybrid(
            [overlap_pick, lexical_pick, recent_pick], _miss(), ents
        )
    }
    assert {1, 2, 3} <= picked


def test_the_hybrid_budget_equals_the_candidate_cap():
    """Equal budget is what makes the comparison fair. A hybrid handed more
    slots than the baseline would win on volume rather than on ranking."""
    assert sum(pc.HYBRID_SPLIT) == comprehend.CANDIDATE_EVENT_CAP


def test_every_strategy_returns_the_whole_pool_or_the_hybrid_budget():
    """Truncation must happen at the CAP in bake_off, not inside a strategy --
    a strategy that truncated early would be measured at a smaller budget."""
    now = dt.datetime.now(dt.timezone.utc)
    pool = [_pool(1, now - dt.timedelta(hours=i), eid=i) for i in range(50)]
    ents = {99: {0}}
    skipped = set()
    for name, fn in pc.STRATEGIES.items():
        if name in pc.SQL_SCORED:
            skipped.add(name)
            continue
        got = fn(pool, _miss(), ents)
        expected = sum(pc.HYBRID_SPLIT) if "hybrid" in name else len(pool)
        assert len(got) == expected, name
    # The skip is bounded: a new SQL-scored arm must be declared, or this
    # fails rather than silently leaving the arm untested.
    assert skipped == pc.SQL_SCORED & set(pc.STRATEGIES)


def test_the_bakeoff_baseline_reproduces_the_live_query(kb):
    """THE control. bake_off's recency arm ranks a Python-fetched pool while
    was_retrievable asks Postgres. If those two disagree, every delta in the
    table is measured against the wrong baseline -- so the probe reports the
    agreement count and this test proves it can reach zero disagreements."""
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
    for n in range(comprehend.CANDIDATE_EVENT_CAP):
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
        "score": 0.9,
        "earlier": {"event_id": old},
        "later": {
            "event_id": new,
            "title": "Iran resumes enrichment Fordow",
            "created_at": now + dt.timedelta(seconds=1),
        },
    }
    results, (agree, disagree) = pc.bake_off(
        kb, [miss], ents, comprehend.CANDIDATE_EVENT_CAP
    )
    assert disagree == 0, "the Python baseline must agree with the SQL one"
    assert agree == 1
    assert results["recency (today)"] == 0, "buried by the cap, as in production"
    assert results["title similarity"] == 1, (
        "and a similarity ranking must RECOVER it, or the bake-off has nothing "
        "to report"
    )


# --- Batch dilution: 30 slots shared by COMPREHEND_INTEGRATE_BATCH items.


def _batch_row(title, event_id, created_at):
    return {"title": title, "event_id": event_id, "created_at": created_at}


def test_reserved_gives_every_item_its_own_slots():
    """The point of reservation: a loud item must not crowd out a quiet one.
    Without per-item floors, one item whose entity dominates the pool takes
    every slot and the other four get nothing."""
    now = dt.datetime.now(dt.timezone.utc)
    pool = [
        {
            "id": 1,
            "occurred_at": now,
            "summary": "Iran resumed enrichment",
            "entities": set(),
        },
        {
            "id": 2,
            "occurred_at": now,
            "summary": "Brazil soybean harvest beats",
            "entities": set(),
        },
        {
            "id": 3,
            "occurred_at": now,
            "summary": "Iran enrichment continues apace",
            "entities": set(),
        },
        {
            "id": 4,
            "occurred_at": now,
            "summary": "Brazil harvest forecast raised",
            "entities": set(),
        },
    ]
    batch = [
        _batch_row("Iran resumes enrichment", 10, now),
        _batch_row("Brazil soybean harvest beats forecasts", 11, now),
    ]
    picked = pc.batch_reserved(pool, batch, {}, 2)
    assert 1 in picked, "the Iran item must get a slot"
    assert 2 in picked, "and so must the Brazil item, or reservation does nothing"


def test_unreserved_batch_similarity_can_starve_an_item():
    """The failure reservation exists to prevent, asserted rather than assumed.
    If this could not be made to fail, reservation would be complexity with no
    justification.

    Starvation needs ONE item holding several strong matches, not merely two
    items competing: a max-over-titles ranking already lets each item's single
    best match rank high, so the first version of this test passed for the
    wrong reason.
    """
    now = dt.datetime.now(dt.timezone.utc)
    pool = [
        {
            "id": 1,
            "occurred_at": now,
            "summary": "Iran resumes enrichment at Fordow",
            "entities": set(),
        },
        {
            "id": 3,
            "occurred_at": now,
            "summary": "Iran resumes enrichment Fordow today",
            "entities": set(),
        },
        {
            "id": 2,
            "occurred_at": now,
            "summary": "Brazil soybean harvest",
            "entities": set(),
        },
    ]
    batch = [
        _batch_row("Iran resumes enrichment at Fordow", 10, now),
        _batch_row("Brazil soybean harvest beats forecasts", 11, now),
    ]
    picked = pc.batch_similarity(pool, batch, {}, 2)
    assert picked == [1, 3], "the loud item takes both slots"
    assert 2 not in picked, "and the quiet item is starved"

    reserved = pc.batch_reserved(pool, batch, {}, 2)
    assert 2 in reserved, "reservation must rescue exactly this case"


def test_every_batch_strategy_respects_the_cap():
    """Equal budget again. A strategy returning more than `cap` would be
    compared against others at a larger prompt, which is not a ranking win."""
    now = dt.datetime.now(dt.timezone.utc)
    pool = [
        {
            "id": i,
            "occurred_at": now - dt.timedelta(hours=i),
            "summary": f"s{i}",
            "entities": set(),
        }
        for i in range(50)
    ]
    batch = [_batch_row(f"t{i}", 100 + i, now) for i in range(5)]
    skipped = set()
    for name, fn in pc.BATCH_STRATEGIES.items():
        if name in pc.SQL_SCORED:
            skipped.add(name)
            continue
        assert len(fn(pool, batch, {}, 30)) <= 30, name
    assert skipped == pc.SQL_SCORED & set(pc.BATCH_STRATEGIES)


def test_the_sql_scored_arms_hold_the_same_budget_contract(kb):
    """What the two tests above can no longer cover without a database. Same
    properties, same pool shape -- the SQL arms are not exempt from the budget
    contract, they just cannot be checked in a pure test."""
    now = dt.datetime.now(dt.timezone.utc)
    pool = [
        {
            "id": i,
            "occurred_at": now - dt.timedelta(hours=i),
            "summary": f"summary number {i}",
            "entities": set(),
        }
        for i in range(50)
    ]
    assert len(pc.rank_pg_trgm(pool, _miss(), {}, conn=kb)) == len(pool)
    batch = [_batch_row(f"title {i}", 100 + i, now) for i in range(5)]
    assert len(pc.batch_pg_trgm(pool, batch, {}, 30, conn=kb)) <= 30


def test_batch_neighbours_excludes_the_pair_itself():
    """Including the miss's own events would hand the answer to the ranking and
    every strategy would score 100%."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = [
        _batch_row("other one", 1, now),
        _batch_row("other two", 2, now),
        _batch_row("the later", 99, now),
        _batch_row("the earlier", 98, now),
    ]
    miss = {
        "later": {"event_id": 99, "title": "the later", "created_at": now},
        "earlier": {"event_id": 98},
    }
    batch = pc.batch_neighbours(rows, miss, 3)
    ids = [row["event_id"] for row in batch]
    assert ids[0] == 99, "the miss's own item leads the batch"
    assert 98 not in ids, "the TARGET must never be seeded into the batch"


def test_batch_neighbours_stops_at_the_batch_size():
    now = dt.datetime.now(dt.timezone.utc)
    rows = [_batch_row(f"o{i}", i, now) for i in range(20)]
    miss = {
        "later": {"event_id": 99, "title": "x", "created_at": now},
        "earlier": {"event_id": 98},
    }
    assert len(pc.batch_neighbours(rows, miss, 5)) == 5


def test_the_batch_size_comes_from_the_live_knob():
    """A hardcoded 5 would measure a system nobody runs if the host retunes
    COMPREHEND_INTEGRATE_BATCH."""
    assert pc.integrate_batch_size() == max(
        1, int(comprehend.common.COMPREHEND_INTEGRATE_BATCH)
    )


# --- The ceiling on the FAILING 8.2 direction (news-brief-bqa.20) -----------
#
# The shipped ceiling was `(assertions - (events - merges)) / assertions`,
# which is score_match_rate_corroboration -- the direction that already clears
# 10% with no merges at all. The direction that is FAILING counts events
# carrying assertions from 2+ distinct outlets, and merging changes both its
# numerator and its denominator. Nothing computed it.


def _pair(earlier_id, later_id, score=0.5):
    return {
        "score": score,
        "earlier": {"event_id": earlier_id},
        "later": {"event_id": later_id},
        "syndicated": False,
        "hubness": 0,
    }


def test_merging_two_single_outlet_events_yields_one_corroborated_event():
    # Two events, one outlet each, joined by one pair. probable_misses only
    # ever pairs DIFFERENT outlets, so the merged event is multi-outlet.
    outlets = {1: {10}, 2: {20}}
    rate, after, multi_after = pc.outlet_ceiling(
        [_pair(1, 2)], outlets, total=2, multi=0
    )
    assert (after, multi_after) == (1, 1)
    assert rate == 1.0


def test_the_outlet_ceiling_is_not_the_match_rate_formula():
    # 10 events, 1 already multi-outlet and OUTSIDE the cluster; one cluster of
    # two single-outlet events. Derived by hand: 9 events remain, the surviving
    # multi is still multi, the cluster becomes multi -> 2/9.
    outlets = {1: {10}, 2: {20}, 3: {30, 31}}
    rate, after, multi_after = pc.outlet_ceiling(
        [_pair(1, 2)], outlets, total=10, multi=1
    )
    assert (after, multi_after) == (9, 2)
    assert rate == pytest.approx(2 / 9)


def test_an_already_corroborated_event_inside_a_cluster_is_not_double_counted():
    # The clustered pair includes the ONLY already-multi event. Merging it with
    # a single-outlet event must still leave exactly one corroborated event --
    # adding the cluster without removing its members inflates to 2.
    outlets = {1: {10, 11}, 2: {20}}
    rate, after, multi_after = pc.outlet_ceiling(
        [_pair(1, 2)], outlets, total=5, multi=1
    )
    assert (after, multi_after) == (4, 1)
    assert rate == pytest.approx(1 / 4)


# --- The shipped ranking needs a batched arm (news-brief-bqa.21) ------------


def test_the_shipped_ranking_has_a_batched_arm():
    # rank_entity_overlap is what 51c850c deployed; production shares one
    # candidate list across a batch, and batching cost recency 42% -> 18%.
    assert any("entity overlap" in name for name in pc.BATCH_STRATEGIES)


def test_batch_entity_overlap_prefers_shared_entities_over_recency():
    older = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
    pool = [
        {"id": 1, "occurred_at": older, "summary": "shares two", "entities": {7, 8}},
        {"id": 2, "occurred_at": newer, "summary": "shares none", "entities": {99}},
    ]
    batch = [{"event_id": 100, "title": "t"}]
    ents = {100: {7, 8}}
    fn = next(f for n, f in pc.BATCH_STRATEGIES.items() if "entity overlap" in n)
    assert fn(pool, batch, ents, 2)[0] == 1


# --- The bake-off's eval set must not be one arm's own signal (bqa.22) ------


def test_the_bakeoff_sample_is_drawn_within_bands_not_off_the_top():
    # The shipped subset was the top 800 by title-title similarity, and one arm
    # ranks on that same signal through a paraphrase. Sampling WITHIN a band
    # keeps the selection constant across arms.
    lo, hi = pc.BANDS[0]
    misses = [_pair(i, i + 1000, score=lo + i * (hi - lo) / 200) for i in range(100)]
    sampled = pc.sample_by_band(misses, 10, random.Random(1))
    drawn = sampled[(lo, hi)]
    assert len(drawn) == 10
    assert all(lo <= m["score"] < hi for m in drawn)
    top = sorted(misses, key=lambda m: -m["score"])[:10]
    assert sorted(m["score"] for m in drawn) != sorted(m["score"] for m in top)


def test_every_populated_band_is_represented_in_the_sample():
    first, second = pc.BANDS[0], pc.BANDS[1]
    misses = [_pair(1, 2, score=first[0] + 0.01) for _ in range(5)]
    misses += [_pair(3, 4, score=second[0] + 0.01) for _ in range(5)]
    sampled = pc.sample_by_band(misses, 3, random.Random(1))
    assert set(sampled) == {first, second}
    assert all(len(v) == 3 for v in sampled.values())


# --- Trigram ranking, measured before it is shipped (news-brief-bqa.24) -----
#
# The 48% batched recall that makes the case for lexical ranking was measured
# with THIS module's similarity(): token Jaccard over a stopword-filtered set.
# pg_trgm's similarity() is a different signal -- trigrams, so "Iran"/"Iranian"
# score high (helpful) and a shared "- Reuters" suffix also scores high (not
# helpful, and the probe separates syndication for exactly that reason).
#
# Shipping pg_trgm on the strength of a token-Jaccard number would repeat
# news-brief-bqa.21: deploying an arm nobody measured. So the arm exists here
# first, computing similarity in the DATABASE -- the exact predicate the
# ranking would use, not a Python re-derivation of it.


def test_pg_trgm_is_available_after_migrations(kb):
    # Migration 0012. If this fails the extension is not in the image, and
    # every trigram number below is unobtainable in production.
    assert kb.execute("SELECT similarity('abc', 'abc')").fetchone()[0] == 1.0


def test_trgm_scores_reproduces_postgres_for_every_candidate(kb):
    title = "Iran declares prohibited zone near Hormuz"
    summaries = [
        "Iran says it will declare prohibited zone near Strait of Hormuz",
        "Uber to exit Nigeria after twelve years",
        "",
    ]
    got = pc.trgm_scores(kb, title, summaries)
    expected = [
        kb.execute("SELECT similarity(%s, %s)", (title, s)).fetchone()[0]
        for s in summaries
    ]
    assert got == expected


def test_pg_trgm_ranks_the_paraphrase_above_a_newer_unrelated_event(kb):
    # The whole point of a lexical ranking: the older paraphrase must beat the
    # newer stranger, which is exactly where recency loses.
    older = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
    # The unrelated event is FIRST on purpose: _by_score breaks ties by
    # original position, so an arm that scores nothing would return id 2 and
    # fail. Only a ranking that reads the text can put id 1 in front.
    pool = [
        {
            "id": 2,
            "occurred_at": newer,
            "summary": "Uber to exit Nigeria after twelve years of operations",
            "entities": set(),
        },
        {
            "id": 1,
            "occurred_at": older,
            "summary": "Iran says it will declare a prohibited zone near Hormuz",
            "entities": set(),
        },
    ]
    miss = {
        "later": {
            "event_id": 100,
            "title": "Iran declares prohibited zone near Strait of Hormuz",
        }
    }
    ranked = pc.rank_pg_trgm(pool, miss, {}, conn=kb)
    assert [e["id"] for e in ranked][0] == 1


def test_batch_pg_trgm_is_registered_and_ranks_on_the_best_batch_match(kb):
    # Registered, so the batched table reports it; and ranked on the BEST match
    # across the batch, so a second item's story is not buried by the first.
    assert any("trgm" in name for name in pc.BATCH_STRATEGIES)
    older = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
    # Same trap, same fix: the unrelated event sits at pool[0].
    pool = [
        {
            "id": 2,
            "occurred_at": newer,
            "summary": "wholly unrelated market wrap for the trading session",
            "entities": set(),
        },
        {
            "id": 1,
            "occurred_at": older,
            "summary": "Uber to exit Nigeria after twelve years of operations",
            "entities": set(),
        },
    ]
    batch = [
        {"event_id": 100, "title": "Iran declares prohibited zone"},
        {"event_id": 101, "title": "Uber to exit Nigeria after 12 years"},
    ]
    fn = next(f for n, f in pc.BATCH_STRATEGIES.items() if "trgm" in n)
    assert fn(pool, batch, {}, 2, conn=kb)[0] == 1


# --- The reconstruction mirrors production (news-brief-bqa.26) -------------
#
# was_retrievable reimplemented candidate_events' ORDER BY, and its own comment
# said the two must mirror each other. That broke silently when the ranking
# changed -- twice -- and the bake-off's control could not notice, because both
# of its sides were recency. A symmetric control cannot see a term sitting on
# both sides of it.


def _buried_by_recency(kb):
    """A lexical match buried under a full cap of newer unrelated events.
    Recency cannot offer it; the live ranking can. Returns (old, miss, ents)."""
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
    for n in range(comprehend.CANDIDATE_EVENT_CAP):
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
    miss = {
        "score": 0.9,
        "earlier": {"event_id": old},
        "later": {
            "event_id": new,
            "title": "Iran resumes enrichment Fordow",
            "created_at": now + dt.timedelta(seconds=1),
        },
    }
    return old, miss, pc.entities_by_event(kb, 7)


def test_was_retrievable_follows_the_live_ranking_not_recency(kb):
    """The whole bead in one assertion. The match is older than a full cap of
    fillers, so a recency reconstruction reports NOT OFFERED -- which is what
    it did for two rankings after production stopped using recency."""
    _old, miss, ents = _buried_by_recency(kb)
    assert pc.was_retrievable(kb, miss, ents) is True


def test_the_python_arm_reproduces_the_live_query_ordering(kb):
    """The contract the bake-off's control encodes: the arm standing in for
    production must return what production returns, in the same order. If
    either side moves alone, this fails -- which is what the old control could
    not do."""
    old, miss, ents = _buried_by_recency(kb)
    pool = pc.candidate_pool(kb, miss, ents)
    cap = comprehend.CANDIDATE_EVENT_CAP
    arm = [e["id"] for e in pc.rank_pg_trgm(pool, miss, ents, conn=kb)][:cap]
    live = [
        r["id"]
        for r in comprehend.candidate_events(
            kb,
            list(ents.get(miss["later"]["event_id"], ())),
            [miss["later"]["title"]],
            comprehend.Tally(),
            as_of=miss["later"]["created_at"],
        )
    ]
    assert arm == live
    assert old in live, "the fixture must contain the match, or this is vacuous"

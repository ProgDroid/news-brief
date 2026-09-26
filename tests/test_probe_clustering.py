"""scripts/probe_clustering.py: the phase-2 grouping-recall spike (news-brief-vlg).

NO module-level skipmark, for the reason tests/test_probe_corroboration.py
gives: the grouping and accounting functions are pure and must run in CI; only
the query-backed tests skip, through the `kb` fixture.

What an error here would cost: the spike's verdict picks the grouping the
phase-2 plan builds, against a pre-registered 5-point tolerance. A probe that
credited batching with visibility it does not have would ship a pipeline that
loses corroboration on a gate with no appeal. So the accounting tests pin the
direction of each variant's visibility rule, and the entity tests pin that an
entity the pair itself minted is NOT an edge.
"""

import datetime as dt

import pytest

import comprehend
import db
from scripts import probe_clustering as pcl

T0 = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)


# --- Pure: request shapes.


def test_real_time_chunks_newest_first():
    groups = pcl.rt_groups([1, 2, 3, 4, 5, 6, 7], size=5)
    # id DESC: 7..3 is the first micro-batch, 2 and 1 the second.
    assert groups[7] == groups[3]
    assert groups[2] == groups[1]
    assert groups[3] != groups[2]


def test_seed_star_is_not_transitive():
    # A-B and B-C are edges, A-C is not. Seed = newest = 3 (A). Seed-star takes
    # only the seed's direct neighbours, so C is left out: the red-team's case.
    a, b, c = 3, 2, 1
    groups = pcl.seed_star([a, b, c], {(b, a): 0.5, (c, b): 0.5}, cap=8)
    assert [a, b] in groups
    assert [c] in groups


def test_capped_bfs_is_transitive():
    a, b, c = 3, 2, 1
    groups = pcl.capped_bfs([a, b, c], {(b, a): 0.5, (c, b): 0.5}, cap=8)
    assert sorted(groups[0]) == [1, 2, 3]


def test_capped_bfs_respects_the_cap():
    ids = list(range(10, 0, -1))
    edges = {(i, 10): 0.9 for i in range(1, 10)}  # a star around 10
    groups = pcl.capped_bfs(ids, edges, cap=4)
    assert max(len(g) for g in groups) == 4
    assert sorted(x for g in groups for x in g) == sorted(ids)


def test_a_title_edge_outranks_an_entity_edge_at_the_cap():
    # Seed 3 has an entity edge to 2 (newer) and a title edge to 1. With room
    # for one neighbour, the headline match must win the slot.
    edges = {**pcl.entity_edges({3: {99}, 2: {99}}), (1, 3): 0.4}
    groups = pcl.capped_bfs([3, 2, 1], edges, cap=2)
    assert sorted(groups[0]) == [1, 3]


def test_singletons_are_packed_newest_first_and_share_a_request():
    groups = [[9, 8], [7], [6], [5], [4], [3], [2]]
    reqs = pcl.pack_requests(groups, size=5)
    assert [9, 8] in reqs
    assert [7, 6, 5, 4, 3] in reqs
    assert [2] in reqs


# --- Pure: the visibility accounting, the part a wrong sign would invert.


def _pair(a, b, a_at, b_at):
    return pcl.Pair(a_id=a, b_id=b, a_at=a_at, b_at=b_at)


def test_batching_sees_a_co_windowed_pair_only_in_one_request():
    p = _pair(1, 2, T0, T0 + dt.timedelta(minutes=10))
    assert pcl.batch_visible(p, {1: 0, 2: 0}, window_hours=1)
    assert not pcl.batch_visible(p, {1: 0, 2: 1}, window_hours=1)


def test_real_time_sees_a_co_windowed_pair_only_across_chunks():
    p = _pair(1, 2, T0, T0 + dt.timedelta(minutes=10))
    assert pcl.rt_visible(p, {1: 0, 2: 1})
    assert not pcl.rt_visible(p, {1: 0, 2: 0})


def test_a_pair_split_across_windows_is_visible_to_both():
    # Different submissions: the later one is offered the earlier one's events.
    p = _pair(1, 2, T0 + dt.timedelta(minutes=50), T0 + dt.timedelta(minutes=70))
    same = {1: 0, 2: 0}
    assert pcl.batch_visible(p, {1: 0, 2: 1}, window_hours=1)
    assert pcl.rt_visible(p, same)


def test_window_key_aligns_to_the_clock():
    assert pcl.window_key(T0, 1) == pcl.window_key(T0 + dt.timedelta(minutes=59), 1)
    assert pcl.window_key(T0, 1) != pcl.window_key(T0 + dt.timedelta(minutes=60), 1)
    assert pcl.window_key(T0, 2) == pcl.window_key(T0 + dt.timedelta(minutes=119), 2)


# --- Pure: the pre-registered decision rule.


def test_the_simplest_variant_within_tolerance_wins():
    shares = {"T": 0.80, "T-cc": 0.86, "TE-cc": 0.95}
    assert pcl.decide(shares, rt=0.90, n=300) == "T-cc"


def test_the_tolerance_is_inclusive_at_five_points():
    shares = {"T": 0.85, "T-cc": 0.86, "TE-cc": 0.95}
    assert pcl.decide(shares, rt=0.90, n=300) == "T"


def test_no_variant_within_tolerance_sends_the_design_back():
    shares = {"T": 0.40, "T-cc": 0.50, "TE-cc": 0.80}
    assert pcl.decide(shares, rt=0.90, n=300) == pcl.REDESIGN


def test_too_few_pairs_refuses_to_decide():
    shares = {"T": 0.99, "T-cc": 0.99, "TE-cc": 0.99}
    assert pcl.decide(shares, rt=0.90, n=pcl.MIN_PAIRS - 1) == pcl.NOT_MEASURABLE


def test_entity_edges_join_items_sharing_any_entity():
    edges = pcl.entity_edges({1: {10}, 2: {10, 11}, 3: {12}})
    assert (1, 2) in edges or (2, 1) in edges
    assert not any(3 in e for e in edges)


# --- Query-backed.


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


def _outlet(kb, name):
    return kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _item(kb, outlet_id, title, at, body=None, material=True):
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at) "
        "VALUES (%s, 'u', %s, %s, %s, %s) RETURNING id",
        (outlet_id, title, body, f"h-{title}-{at}", at),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s)",
        (
            item_id,
            "material" if material else "immaterial",
            "topical" if material else "none",
            comprehend.TRIAGE_PROMPT_VERSION,
        ),
    )
    return item_id


def _event(kb, summary):
    return kb.execute(
        "INSERT INTO events (summary, type, commitment_state) "
        "VALUES (%s, 'action', 'in_force') RETURNING id",
        (summary,),
    ).fetchone()[0]


def _assert(kb, item_id, event_id):
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )


def _entity(kb, name, aliases=(), created_at=None):
    return kb.execute(
        "INSERT INTO entities (name, type, aliases, created_at) "
        "VALUES (%s, 'country', %s, COALESCE(%s, now())) RETURNING id",
        (name, list(aliases), created_at),
    ).fetchone()[0]


def _link(kb, event_id, entity_id):
    kb.execute(
        "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)",
        (event_id, entity_id),
    )


def test_pairs_are_cross_outlet_within_the_gap(kb):
    r, a, b = _outlet(kb, "R"), _outlet(kb, "A"), _outlet(kb, "B")
    e = _event(kb, "strike")
    i1 = _item(kb, r, "strike one", T0)
    i2 = _item(kb, a, "strike two", T0 + dt.timedelta(minutes=30))
    i3 = _item(
        kb, r, "strike three", T0 + dt.timedelta(minutes=40)
    )  # same outlet as i1
    i4 = _item(kb, b, "strike four", T0 + dt.timedelta(hours=3))  # too late for i1..i3
    for i in (i1, i2, i3, i4):
        _assert(kb, i, e)
    got = {(p.a_id, p.b_id) for p in pcl.cross_outlet_pairs(kb, max_gap_hours=2)}
    assert got == {(i1, i2), (i2, i3)}


def test_material_items_exclude_quote_pages_and_immaterial(kb):
    r = _outlet(kb, "R")
    keep = _item(kb, r, "Iran strikes back", T0)
    _item(kb, r, "MSTS.DE - Reuters", T0)
    _item(kb, r, "A dull story", T0, material=False)
    ids = [it["id"] for it in pcl.material_items(kb, T0, T0 + dt.timedelta(hours=1))]
    assert ids == [keep]


def test_title_edges_use_pg_trgm(kb):
    r = _outlet(kb, "R")
    a = _item(kb, r, "Iran fires missiles at Israel", T0)
    b = _item(kb, r, "Iran fires missiles at Israel - Reuters", T0)
    c = _item(kb, r, "Bank of Japan holds rates", T0)
    edges = pcl.title_edges(kb, [a, b, c], threshold=0.35)
    assert set(edges) == {(a, b)}


def test_an_entity_is_born_at_its_earliest_linked_capture(kb):
    r = _outlet(kb, "R")
    ent = _entity(kb, "Iran", created_at=T0 + dt.timedelta(days=3))  # backlog lag
    e = _event(kb, "strike")
    _link(kb, e, ent)
    _assert(kb, _item(kb, r, "later", T0 + dt.timedelta(hours=5)), e)
    _assert(kb, _item(kb, r, "first", T0 + dt.timedelta(hours=1)), e)
    assert pcl.entity_births(kb)[ent] == T0 + dt.timedelta(hours=1)


def test_an_entity_the_window_itself_minted_is_not_an_edge(kb):
    """The inflation this probe exists to avoid: today's index holds entities
    that these very items created, so an unfiltered match would join a pair
    through an entity that did not exist when their submission was built."""
    r = _outlet(kb, "R")
    old = _entity(kb, "Iran")
    new = _entity(kb, "Hormuzia")
    for ent, born in ((old, T0 - dt.timedelta(days=1)), (new, T0)):
        e = _event(kb, f"ev-{ent}")
        _link(kb, e, ent)
        _assert(kb, _item(kb, r, f"seed {ent}", born), e)
    items = [
        {"id": 101, "title": "Iran and Hormuzia talk", "body": "", "created_at": T0},
        {"id": 102, "title": "Hormuzia and Iran", "body": "", "created_at": T0},
    ]
    index = comprehend.SurfaceIndex.build(kb)
    hits = pcl.item_entity_hits(index, items)
    births = pcl.entity_births(kb)
    prior = pcl.prior_hits(hits, births, window_start=T0)
    assert prior == {101: {old}, 102: {old}}


def test_the_prefiltered_matcher_agrees_with_surface_index(kb):
    """The speed-up is only admissible if it is EXACT. Covers a short
    case-sensitive acronym, an alias, a form starting with a non-word char, and
    a pronoun that must not match the acronym."""
    us = _entity(kb, "US")
    ukr = _entity(kb, "Ukraine", aliases=["Kyiv"])
    s_p = _entity(kb, "#MeToo")
    index = comprehend.SurfaceIndex.build(kb)
    items = [
        {"id": 1, "title": "US sanctions", "body": "Kyiv responds"},
        {"id": 2, "title": "tell us more", "body": ""},
        {"id": 3, "title": "The #MeToo movement", "body": ""},
        {"id": 4, "title": "Ukrainian grain", "body": ""},
    ]
    got = pcl.item_entity_hits(index, items)
    for it in items:
        text = f"{comprehend.clean(it['title'])}\n{comprehend.clean(it['body'])}"
        want = {sf.entity_id for sf in index.match(text) if sf.entity_id is not None}
        assert got[it["id"]] == want
    assert got[1] == {us, ukr}
    assert got[2] == set()
    assert got[3] == {s_p}

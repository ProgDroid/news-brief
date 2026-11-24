"""Tests for retrieval (storylines plan 1, task 5): hybrid group search with an
as_of cut. Pure RRF first, then DB-gated (a skip is not a pass). Every headline
is invented; the repo is public. The embedder is a fake that maps each query to
a fixed vector and records every input it was sent.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import db
import embeddings
import group_store as gs
import retrieval
from embeddings import CallRecord, EmbedError

SPACE = "modal:fake@3"
DIM = 3
T = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
LONG_AGO = T - timedelta(days=30)
A = [1.0, 0.0, 0.0]
NEAR = [0.8, 0.6, 0.0]  # cosine 0.8 with A
FAR = [0.0, 1.0, 0.0]  # cosine 0.0 with A

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test "
    "pgvector/pgvector:0.8.7-pg18-trixie",
)


# ---- pure ----


def test_rrf_orders_by_fused_rank():
    fused = retrieval.rrf([[1, 2, 3], [3, 1]])
    assert [g for g, _ in fused] == [1, 3, 2]
    scores = dict(fused)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[2] == pytest.approx(1 / 62)


def test_rrf_breaks_ties_by_the_lower_id():
    assert [g for g, _ in retrieval.rrf([[7], [5]])] == [5, 7]


def test_rrf_of_nothing_is_empty():
    assert retrieval.rrf([]) == []
    assert retrieval.rrf([[], []]) == []


# ---- fixtures and helpers ----


@pytest.fixture()
def conn():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


def _record(n, outcome="ok", problem=None):
    return CallRecord(
        "modal",
        SPACE,
        n,
        10 * n,
        0.1,
        0.0,
        False,
        False,
        0.001 if outcome == "ok" else 0.0,
        0.002 if outcome == "ok" else 0.0,
        outcome,
        problem,
    )


class QueryEmbedder:
    """Maps a query text to a vector (default A), counts provider calls."""

    provider = "modal"

    def __init__(self, vectors=None, error_kind=None, space=SPACE):
        self.space = space
        self.vectors = dict(vectors or {})
        self.error_kind = error_kind
        self.sent: list[list[str]] = []

    def embed(self, texts, *, kind):
        assert kind == "query"
        self.sent.append(list(texts))
        if self.error_kind:
            msg = f"fake {self.error_kind}: refused"
            raise EmbedError(self.error_kind, msg, [_record(len(texts), "failed", msg)])
        return [self.vectors.get(t, A) for t in texts], [_record(len(texts))]


_seq = iter(range(10**6))


def _outlet(conn, name, kind="wire", perspective=None):
    row = conn.execute(
        "SELECT id FROM outlets WHERE lower(name) = lower(%s)", (name,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO outlets (name, kind, perspective) VALUES (%s, %s, %s)"
        " RETURNING id",
        (name, kind, perspective),
    ).fetchone()[0]


def _member(conn, title, at, vec, *, group=None, outlet="Widget Wire", **outlet_kw):
    """An item with a vector, put into `group` (a new group when None). Returns
    (group_id, item_id)."""
    n = next(_seq)
    oid = _outlet(conn, outlet, **outlet_kw)
    iid = conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (oid, f"https://example.test/{n}", title, f"h{n}", at),
    ).fetchone()[0]
    gs.store_vectors(
        conn,
        space=SPACE,
        dim=DIM,
        rows=[(iid, f"th{iid}", "[" + ",".join(str(x) for x in vec) + "]")],
    )
    kind = conn.execute("SELECT kind FROM outlets WHERE id = %s", (oid,)).fetchone()[0]
    item = gs.PendingItem(
        iid, title, None, f"https://example.test/{n}", outlet, kind, at
    )
    if group is None:
        group = gs.open_group(conn, space=SPACE, item=item)
    else:
        gs.join_group(
            conn,
            space=SPACE,
            group_id=group,
            item=item,
            candidate=gs.Candidate(group, 0.9, 0.5),
        )
    conn.commit()
    return group, iid


def _search(conn, embedder, queries, **kw):
    kw.setdefault("since", LONG_AGO)
    kw.setdefault("as_of", T + 10 * H)
    return retrieval.search_groups(
        conn, embedder, space=SPACE, dim=DIM, queries=queries, **kw
    )


def _calibrate(conn, retrieval_min_sim):
    gs.write_calibration(
        conn,
        gs.Calibration(
            space=SPACE,
            join_sim=0.85,
            pair_sim=0.7,
            title_sim=0.5,
            retrieval_min_sim=retrieval_min_sim,
            weighted_precision=0.9,
            weighted_recall=0.8,
            pairs_labelled=200,
            prompt_version=1,
            judge_model="judge-m",
            week_ending=date(2026, 11, 22),
        ),
    )
    conn.commit()


def _ids(result):
    return [h.group.group_id for h in result.hits]


# ---- Review Focus 4: nothing after as_of leaks ----


@needs_db
def test_as_of_hides_later_members_and_their_outlets(store):
    g, first = _member(store, "Widget output falls", T, A, outlet="Widget Wire")
    _member(
        store, "Widget output falls again", T + 2 * H, A, group=g, outlet="Gizmo Daily"
    )

    early = _search(store, QueryEmbedder(), ["Widget output falls"], as_of=T + H)
    (hit,) = early.hits
    assert hit.group.outlet_count == 1
    assert hit.group.outlets == ("Widget Wire",)
    assert hit.group.last_seen == T
    assert hit.group.item_ids == (first,)

    late = _search(store, QueryEmbedder(), ["Widget output falls"], as_of=T + 3 * H)
    (hit,) = late.hits
    assert hit.group.outlet_count == 2
    assert hit.group.last_seen == T + 2 * H
    assert len(hit.group.item_ids) == 2


@needs_db
def test_as_of_ignores_the_stored_current_group_fields(store):
    # The stored row says 2 outlets, last seen at T+2h, with a primary; the view
    # at T+1h must say 1, T, and no primary.
    g, _ = _member(store, "Widget output falls", T, A, outlet="Widget Wire")
    _member(
        store,
        "Widget output falls again",
        T + 2 * H,
        A,
        group=g,
        outlet="Gizmo Ministry",
        kind="primary",
    )
    stored = store.execute(
        "SELECT outlet_count, has_primary FROM item_groups WHERE id = %s", (g,)
    ).fetchone()
    assert stored == (2, True)
    view = gs.groups_as_of(store, group_ids=[g], as_of=T + H)[g]
    assert (view.outlet_count, view.has_primary, view.last_seen) == (1, False, T)


@needs_db
def test_group_first_seen_after_as_of_is_absent(store):
    g, _ = _member(store, "Widget output falls", T + 2 * H, A)
    assert gs.groups_as_of(store, group_ids=[g], as_of=T + H) == {}
    result = _search(store, QueryEmbedder(), ["Widget output falls"], as_of=T + H)
    assert result.hits == []
    assert not result.degraded
    # the same group is found once the cut passes its first member
    assert _ids(
        _search(store, QueryEmbedder(), ["Widget output falls"], as_of=T + 3 * H)
    ) == [g]


@needs_db
def test_the_view_reports_first_seen_perspectives_and_the_earliest_title(store):
    g, _ = _member(
        store, "Widget output falls", T, A, outlet="Widget Wire", perspective="WESTERN"
    )
    _member(
        store,
        "Gizmo plants idle",
        T + H,
        A,
        group=g,
        outlet="Gizmo Gazette",
        kind="primary",
        perspective="CHINESE",
    )
    _member(store, "Gizmo plants idle again", T + H, A, group=g, outlet="Plain Post")
    view = gs.groups_as_of(store, group_ids=[g], as_of=T + 2 * H)[g]
    assert view.title == "Widget output falls"
    assert view.first_seen == T
    assert view.outlets == ("Gizmo Gazette", "Plain Post", "Widget Wire")
    assert view.outlet_count == 3
    assert view.perspectives == ("CHINESE", "WESTERN")  # an outlet with none adds none
    assert view.has_primary is True


# ---- since ----


@needs_db
def test_since_bounds_by_member_time_not_group_open(store):
    g, _ = _member(store, "Widget output falls", T - 5 * H, A)
    _member(store, "Widget output falls again", T + H, A, group=g, outlet="Gizmo Daily")
    result = _search(store, QueryEmbedder(), ["Widget output falls"], since=T)
    assert _ids(result) == [g]


@needs_db
def test_a_group_with_no_member_after_since_is_not_returned(store):
    _member(store, "Widget output falls", T - 5 * H, A)
    assert _search(store, QueryEmbedder(), ["Widget output falls"], since=T).hits == []


# ---- query vector cache ----


@needs_db
def test_query_vectors_are_cached_by_hash(store, monkeypatch):
    _member(store, "Widget output falls", T, A)
    emb = QueryEmbedder()
    _search(store, emb, ["widget outlook"])
    assert len(emb.sent) == 1
    _search(store, emb, ["widget outlook"])
    assert len(emb.sent) == 1  # a second search makes no provider call
    monkeypatch.setattr(embeddings, "EMBED_PROMPT_VERSION", 2)
    _search(store, emb, ["widget outlook"])
    assert len(emb.sent) == 2  # a new prompt version is a new key
    assert store.execute("SELECT count(*) FROM query_embeddings").fetchone()[0] == 2


@needs_db
def test_misses_go_to_the_provider_in_one_call_and_are_recorded(store):
    emb = QueryEmbedder()
    _search(store, emb, ["alpha query", "beta query", "alpha query"])
    assert emb.sent == [["alpha query", "beta query"]]  # one call, deduplicated
    rows = store.execute(
        "SELECT purpose, inputs, outcome FROM embedding_calls"
    ).fetchall()
    assert rows == [("queries", 2, "ok")]


# ---- provider faults ----


@needs_db
def test_provider_fault_degrades_to_trigram(store):
    g, _ = _member(store, "Widget output falls", T, A)
    emb = QueryEmbedder(error_kind="transient")
    result = _search(store, emb, ["Widget output falls"])
    assert result.degraded
    assert "transient" in result.reason
    assert _ids(result) == [g]  # the lexical arm still answers
    assert result.hits[0].best_similarity is None
    assert result.hits[0].best_title_score is not None
    rows = store.execute("SELECT purpose, outcome FROM embedding_calls").fetchall()
    assert rows == [("queries", "failed")]  # the failed call is on the ledger


@needs_db
@pytest.mark.parametrize("kind", ["config", "bad_response", "input"])
def test_every_provider_error_kind_degrades_rather_than_raises(store, kind):
    g, _ = _member(store, "Widget output falls", T, A)
    result = _search(store, QueryEmbedder(error_kind=kind), ["Widget output falls"])
    assert result.degraded and kind in result.reason
    assert _ids(result) == [g]


@needs_db
def test_no_embedder_is_a_degraded_trigram_search(store):
    g, _ = _member(store, "Widget output falls", T, A)
    result = _search(store, None, ["Widget output falls"])
    assert result.degraded and result.reason
    assert _ids(result) == [g]


@needs_db
def test_a_database_error_still_raises(store):
    with pytest.raises(Exception):
        _search(store, QueryEmbedder(), ["x"], since="not a time")


# ---- the similarity floor ----


def _strong_and_weak(store):
    strong, _ = _member(store, "Gizmo plants idle", T, A)
    weak, _ = _member(store, "Sprocket demand rises", T, NEAR, outlet="Gizmo Daily")
    return strong, weak


@needs_db
def test_min_similarity_floor_drops_weak_vector_hits(store):
    strong, weak = _strong_and_weak(store)
    with_floor = _search(
        store, QueryEmbedder(), ["unrelated words"], min_similarity=0.9
    )
    assert _ids(with_floor) == [strong]
    without = _search(store, QueryEmbedder(), ["unrelated words"], min_similarity=0.5)
    assert _ids(without) == [strong, weak]
    assert without.hits[1].best_similarity == pytest.approx(0.8, abs=1e-4)


@needs_db
def test_null_floor_keeps_ranked_vector_hits_and_is_not_degraded(store):
    strong, weak = _strong_and_weak(store)
    _calibrate(store, None)  # a calibrated space whose retrieval floor is NULL
    far, _ = _member(store, "Gadget stocks slip", T, FAR, outlet="Third Wire")
    result = _search(store, QueryEmbedder(), ["unrelated words"])
    assert _ids(result) == [strong, weak, far]  # rank counts, even at similarity 0
    assert not result.degraded and result.reason is None


@needs_db
def test_the_default_floor_is_the_calibrated_retrieval_min_sim(store):
    strong, weak = _strong_and_weak(store)
    _calibrate(store, 0.9)
    assert _ids(_search(store, QueryEmbedder(), ["unrelated words"])) == [strong]
    # an explicit floor wins over the calibrated one
    both = _search(store, QueryEmbedder(), ["unrelated words"], min_similarity=0.5)
    assert _ids(both) == [strong, weak]


# ---- fusion and limits ----


@needs_db
def test_a_group_found_by_both_arms_outranks_one_found_by_one(store):
    both, _ = _member(store, "Widget output falls", T, A)
    vec_only, _ = _member(store, "Sprocket demand rises", T, NEAR, outlet="Gizmo Daily")
    result = _search(store, QueryEmbedder(), ["Widget output falls"])
    assert _ids(result) == [both, vec_only]
    assert result.hits[0].score > result.hits[1].score
    assert result.hits[0].best_title_score is not None
    assert result.hits[1].best_title_score is None


@needs_db
def test_a_short_title_inside_a_long_query_scores_high(store):
    g, _ = _member(store, "Widget output falls", T, FAR)
    query = "what is happening with widget output falls across the gizmo sector"
    result = _search(store, None, [query])
    assert _ids(result) == [g]
    assert result.hits[0].best_title_score >= retrieval.LEXICAL_MIN


@needs_db
def test_limit_caps_the_hits(store):
    for i in range(4):
        _member(store, f"Topic {i} unique words", T, A, outlet=f"Wire {i}")
    assert len(_search(store, QueryEmbedder(), ["unrelated"], limit=2).hits) == 2


@needs_db
def test_a_later_member_cannot_lift_a_groups_similarity_or_title_score(store):
    # Group g's early member is a weak vector match with an unrelated title; its
    # later member is an exact match with a matching title. At T+1h only the
    # early one may count.
    g, _ = _member(store, "Sprocket demand rises", T, NEAR)
    _member(store, "Widget output falls", T + 2 * H, A, group=g, outlet="Gizmo Daily")
    other, _ = _member(
        store, "Gadget stocks slip", T, [0.9, 0.436, 0.0], outlet="Third Wire"
    )

    cut = _search(store, QueryEmbedder(), ["Widget output falls"], as_of=T + H)
    assert _ids(cut) == [other, g]  # ranked on what was known at as_of
    assert cut.hits[1].best_similarity == pytest.approx(0.8, abs=1e-4)
    assert cut.hits[1].best_title_score is None
    floored = _search(
        store,
        QueryEmbedder(),
        ["Widget output falls"],
        as_of=T + H,
        min_similarity=0.85,
    )
    assert _ids(floored) == [other]

    lexical = _search(store, None, ["Widget output falls"], as_of=T + H)
    assert lexical.hits == []
    assert _ids(_search(store, None, ["Widget output falls"], as_of=T + 3 * H)) == [g]

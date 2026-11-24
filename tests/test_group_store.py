"""DB tests for group_store and grouping (storylines plan 1, task 4).

DB-gated: a skip is not a pass. Every headline is invented; the repo is public.
The embedder is a fake that maps each text to a fixed unit vector and records
every input it was sent.
"""

import hashlib
import math
from datetime import date, datetime, timedelta, timezone

import pytest

import db
import group_store as gs
import grouping
from embeddings import CallRecord, EmbedError, embed_text, text_hash

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test "
    "pgvector/pgvector:0.8.7-pg18-trixie",
)

SPACE = "modal:fake@3"
DIM = 3
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
WINDOW = 72
A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]
C = [0.0, 0.0, 1.0]


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


def _record(n, outcome="ok", problem=None, space=SPACE):
    return CallRecord(
        "modal",
        space,
        n,
        10 * n,
        0.1,
        0.0,
        False,
        False,
        0.001 if outcome == "ok" else 0.0,  # a failure is zero-cost, as in embeddings
        0.002 if outcome == "ok" else 0.0,
        outcome,
        problem,
    )


def _hash_vec(text):
    d = hashlib.sha256(text.encode()).digest()
    v = [d[i] - 127.5 for i in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbedder:
    """`vectors` maps text to a vector (others get a hash-based one). `fail(texts,
    call_no)` returns an EmbedError kind to raise, or None. call_no counts from 1."""

    provider = "modal"

    def __init__(self, vectors=None, fail=None, space=SPACE):
        self.space = space
        self.vectors = dict(vectors or {})
        self.fail = fail
        self.sent: list[list[str]] = []

    @property
    def inputs(self):
        return [t for batch in self.sent for t in batch]

    def embed(self, texts, *, kind):
        assert kind == "document"
        self.sent.append(list(texts))
        kind_err = self.fail(texts, len(self.sent)) if self.fail else None
        if kind_err:
            msg = f"fake {kind_err}: refused"
            raise EmbedError(kind_err, msg, [_record(len(texts), "failed", msg)])
        vecs = [self.vectors.get(t) or _hash_vec(t) for t in texts]
        return vecs, [_record(len(texts))]


def _outlet(conn, name="Test Wire", kind="wire"):
    row = conn.execute("SELECT id FROM outlets WHERE name = %s", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, %s) RETURNING id", (name, kind)
    ).fetchone()[0]


_seq = iter(range(10**6))


def _item(conn, title, at, *, body=None, outlet="Test Wire", kind="wire"):
    n = next(_seq)
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            _outlet(conn, outlet, kind),
            f"https://example.test/{n}",
            title,
            body,
            f"h{n}",
            at,
        ),
    ).fetchone()[0]


def _calibrate(conn, space=SPACE, join=0.85, pair=0.7, title=0.5):
    gs.write_calibration(
        conn,
        gs.Calibration(
            space=space,
            join_sim=join,
            pair_sim=pair,
            title_sim=title,
            retrieval_min_sim=None,
            weighted_precision=0.9,
            weighted_recall=0.8,
            pairs_labelled=200,
            prompt_version=1,
            judge_model="judge-m",
            week_ending=date(2026, 11, 22),
        ),
    )
    conn.commit()


def _vec_lit(v):
    return "[" + ",".join(str(x) for x in v) + "]"


def _store_vec(conn, item_id, vec, space=SPACE):
    gs.store_vectors(
        conn, space=space, dim=len(vec), rows=[(item_id, f"th{item_id}", _vec_lit(vec))]
    )


def _pass(conn, embedder, **kw):
    kw.setdefault("space", SPACE)
    kw.setdefault("dim", DIM)
    kw.setdefault("window_hours", WINDOW)
    kw.setdefault("max_items", grouping.LIVE_MAX_ITEMS)
    return grouping.group_pass(conn, embedder, **kw)


def _group_of(conn, item_id, space=SPACE):
    row = conn.execute(
        "SELECT group_id FROM item_group_members WHERE item_id = %s AND space = %s",
        (item_id, space),
    ).fetchone()
    return row[0] if row else None


def _count(conn, table, where="TRUE", args=()):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[
        0
    ]


TH = grouping.Thresholds(join_sim=0.85, pair_sim=0.7, title_sim=0.5)


# ---- Review Focus 2: syndicated copy ----


def test_identical_text_is_embedded_once_and_groups_together(store):
    a = _item(
        store, "Widget output falls", T0, body="Gizmo plants idle.", outlet="Wire One"
    )
    b = _item(
        store,
        "Widget output falls",
        T0 + timedelta(minutes=5),
        body="Gizmo plants idle.",
        outlet="Wire Two",
    )
    store.commit()
    _calibrate(store)
    fake = FakeEmbedder()
    out = _pass(store, fake)
    assert out["status"] == "ok", out
    assert fake.inputs == [embed_text("Widget output falls", "Gizmo plants idle.", "")]
    assert (out["embedded"], out["reused"]) == (1, 1)
    assert _count(store, "item_embeddings") == 2
    assert _group_of(store, a) is not None and _group_of(store, a) == _group_of(
        store, b
    )
    row = store.execute("SELECT outlet_count FROM item_groups").fetchall()
    assert row == [(2,)]


def test_a_stored_vector_is_reused_by_hash_without_a_call(store):
    text = embed_text("Gizmo recall widens", None, "")
    a = _item(store, "Gizmo recall widens", T0, outlet="Wire One")
    gs.store_vectors(
        store, space=SPACE, dim=DIM, rows=[(a, text_hash(text), _vec_lit(A))]
    )
    b = _item(store, "Gizmo recall widens", T0 + timedelta(hours=1), outlet="Wire Two")
    store.commit()
    fake = FakeEmbedder()
    out = grouping.embed_pending(
        store, fake, space=SPACE, dim=DIM, limit=10, purpose="items"
    )
    assert fake.sent == []
    assert out == {"embedded": 0, "reused": 1, "skipped": 0, "calls": 0}
    got = store.execute(
        "SELECT embedding::text FROM item_embeddings WHERE item_id = %s", (b,)
    ).fetchone()[0]
    assert got == "[1,0,0]"


# ---- Review Focus 1: order survives an outage ----


def test_assignment_stops_at_the_first_item_without_a_vector(store):
    a = _item(store, "Widget A", T0)
    b = _item(store, "Widget B", T0 + timedelta(hours=1))
    c = _item(store, "Widget C", T0 + timedelta(hours=2))
    _store_vec(store, a, A)
    _store_vec(store, c, A)
    store.commit()
    out = grouping.assign_pending(
        store, space=SPACE, thresholds=TH, window_hours=WINDOW, limit=100
    )
    assert out == {
        "grouped": 1,
        "opened": 1,
        "joined": 0,
        "stopped_at_missing_vector": True,
    }
    assert _group_of(store, a) is not None
    assert _group_of(store, b) is None
    assert _group_of(store, c) is None


def test_an_embed_outage_still_assigns_what_already_has_vectors(store):
    a = _item(store, "Widget A", T0)
    b = _item(store, "Widget B", T0 + timedelta(hours=1))
    c = _item(store, "Widget C", T0 + timedelta(hours=2))
    _store_vec(store, a, A)
    _store_vec(store, c, A)
    store.commit()
    _calibrate(store)
    fake = FakeEmbedder(fail=lambda texts, n: "transient")
    out = _pass(store, fake)
    assert out["status"] == "transient"
    assert "refused" in out["error"]
    assert (out["grouped"], out["opened"]) == (1, 1)
    assert out["stopped_at_missing_vector"] is True
    assert [_group_of(store, x) is None for x in (a, b, c)] == [False, True, True]


def test_an_item_never_joins_a_group_through_a_later_member(store):
    founder = _item(store, "Widget founder", T0)
    later = _item(store, "Gizmo later member", T0 + timedelta(hours=2))
    middle = _item(store, "Gizmo middle", T0 + timedelta(hours=1))
    for iid, v in ((founder, A), (later, B), (middle, B)):
        _store_vec(store, iid, v)
    gid = gs.open_group(store, space=SPACE, item=_pending(store, founder))
    gs.join_group(
        store,
        space=SPACE,
        group_id=gid,
        item=_pending(store, later),
        candidate=gs.Candidate(gid, 0.9, 0.0),
    )
    store.commit()
    # The control: an item after the later member does see it, at similarity 1.
    cands = gs.candidate_groups(
        store,
        space=SPACE,
        vector=_vec_lit(B),
        title="Gizmo",
        created_at=T0 + timedelta(hours=3),
        window_hours=WINDOW,
    )
    assert [(x.group_id, round(x.similarity, 6)) for x in cands] == [(gid, 1.0)]
    out = grouping.assign_pending(
        store, space=SPACE, thresholds=TH, window_hours=WINDOW, limit=100
    )
    assert (out["opened"], out["joined"]) == (1, 0)
    assert _group_of(store, middle) not in (None, gid)


def _pending(conn, item_id):
    (p,) = [
        x
        for x, _ in gs.next_ungrouped(conn, space=SPACE, limit=1000)
        if x.id == item_id
    ]
    return p


def test_window_closes_after_72_hours(store):
    founder = _item(store, "Widget founder", T0)
    inside = _item(store, "Widget inside", T0 + timedelta(hours=71))
    outside = _item(store, "Widget outside", T0 + timedelta(hours=73))
    for iid in (founder, inside, outside):
        _store_vec(store, iid, A)
    store.commit()
    out = grouping.assign_pending(
        store, space=SPACE, thresholds=TH, window_hours=WINDOW, limit=100
    )
    assert (out["opened"], out["joined"]) == (2, 1)
    assert _group_of(store, inside) == _group_of(store, founder)  # the control
    assert _group_of(store, outside) != _group_of(store, founder)


# ---- Review Focus 3: no thresholds borrowed ----


def test_uncalibrated_space_embeds_but_does_not_assign(store):
    _calibrate(store, space="modal:other@3")  # another space's row must not count
    _item(store, "Widget A", T0)
    _item(store, "Widget B", T0 + timedelta(hours=1))
    store.commit()
    out = _pass(store, FakeEmbedder())
    assert out["status"] == "uncalibrated"
    assert SPACE in out["error"]
    assert out["embedded"] == 2
    assert _count(store, "item_embeddings") == 2
    assert _count(store, "item_groups") == 0
    assert _count(store, "item_group_members") == 0


# ---- spend and failure recording ----


def _many(conn, n, prefix="Widget item"):
    return [
        _item(conn, f"{prefix} {i:03d}", T0 + timedelta(seconds=i)) for i in range(n)
    ]


def test_transient_error_records_the_failed_call_and_keeps_earlier_chunks(store):
    ids = _many(store, grouping.EMBED_CHUNK + 2)
    store.commit()
    fake = FakeEmbedder(fail=lambda texts, n: "transient" if n >= 2 else None)
    with pytest.raises(EmbedError) as ei:
        grouping.embed_pending(
            store, fake, space=SPACE, dim=DIM, limit=500, purpose="items"
        )
    assert ei.value.kind == "transient"
    # Everything below was committed before the raise: a rollback keeps it.
    store.rollback()
    assert _count(store, "item_embeddings") == grouping.EMBED_CHUNK
    assert _count(store, "item_embeddings", "item_id = %s", (ids[0],)) == 1
    assert _count(store, "item_embeddings", "item_id = %s", (ids[-1],)) == 0
    rows = store.execute(
        "SELECT outcome, inputs, purpose FROM embedding_calls ORDER BY id"
    ).fetchall()
    assert rows == [("ok", 128, "items"), ("failed", 2, "items")]
    # And through the pass: the status names the kind, and nothing is skipped.
    _calibrate(store)
    out = _pass(store, fake)
    assert out["status"] == "transient"
    assert out["skipped"] == 0 and _count(store, "item_embed_skips") == 0


def test_a_config_error_is_recorded_and_returned(store):
    _item(store, "Widget A", T0)
    store.commit()
    _calibrate(store)
    out = _pass(store, FakeEmbedder(fail=lambda texts, n: "config"))
    assert out["status"] == "config"
    assert out["calls"] == 1
    rows = store.execute("SELECT outcome, usd FROM embedding_calls").fetchall()
    assert [(o, float(u)) for o, u in rows] == [("failed", 0.0)]
    assert _count(store, "item_embed_skips") == 0


def test_a_bad_response_never_skips(store):
    _item(store, "Widget A", T0)
    store.commit()
    out = _pass(store, FakeEmbedder(fail=lambda texts, n: "bad_response"))
    assert out["status"] == "bad_response"
    assert _count(store, "item_embed_skips") == 0


# ---- Review Focus 6: one bad item never stalls the queue ----


def test_an_unembeddable_item_is_skipped_and_the_queue_moves(store):
    ids = _many(store, grouping.EMBED_CHUNK)
    store.commit()
    _calibrate(store)
    bad = "Widget item 037"
    fake = FakeEmbedder(fail=lambda texts, n: "input" if bad in texts else None)
    out = _pass(store, fake)
    assert out["status"] == "ok", out
    # Pre-registered: 1 full call, then 7 levels of halves at 2 calls each.
    assert out["calls"] == 15
    outcomes = store.execute(
        "SELECT outcome, count(*) FROM embedding_calls GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert outcomes == [("failed", 8), ("ok", 7)]
    assert out["skipped"] == 1 and out["embedded"] == 127
    reasons = store.execute("SELECT item_id, reason FROM item_embed_skips").fetchall()
    assert reasons == [(ids[37], "fake input: refused")]
    assert _count(store, "item_embeddings") == 127
    assert _group_of(store, ids[37]) is None
    assert _group_of(store, ids[38]) is not None
    assert out["grouped"] == 127
    assert gs.oldest_ungrouped_created_at(store, space=SPACE) is None


def test_a_blank_title_is_skipped_without_a_call(store):
    blank = _item(store, "   ", T0, body="Widget body")
    store.commit()
    fake = FakeEmbedder()
    out = grouping.embed_pending(
        store, fake, space=SPACE, dim=DIM, limit=10, purpose="items"
    )
    assert fake.sent == []
    assert out == {"embedded": 0, "reused": 0, "skipped": 1, "calls": 0}
    assert store.execute("SELECT item_id, reason FROM item_embed_skips").fetchall() == [
        (blank, "empty_text")
    ]
    assert _count(store, "embedding_calls") == 0
    # The queue moves: a later item is embedded and then grouped past it.
    after = _item(store, "Widget after", T0 + timedelta(hours=1))
    store.commit()
    _calibrate(store)
    out = _pass(store, fake)
    assert out["status"] == "ok" and out["grouped"] == 1
    assert _group_of(store, after) is not None


# ---- the interlock ----


def test_a_second_pass_on_the_same_space_reports_busy(store):
    _item(store, "Widget A", T0)
    store.commit()
    _calibrate(store)
    other = db.connect()
    try:
        with grouping.space_lock(other, SPACE) as got:
            assert got
            fake = FakeEmbedder()
            out = _pass(store, fake)
            assert out["status"] == "busy"
            assert fake.sent == []
        # The control: once released, the same pass runs.
        assert _pass(store, fake)["status"] == "ok"
    finally:
        other.close()


def test_spaces_do_not_mix(store):
    item = _item(store, "Widget A", T0)
    gs.store_vectors(store, space="a@2", dim=2, rows=[(item, "th", "[1,0]")])
    gs.store_vectors(store, space="b@2", dim=2, rows=[(item, "th", "[1,0]")])
    ((pending, _),) = gs.next_ungrouped(store, space="a@2", limit=10)
    gid = gs.open_group(store, space="a@2", item=pending)
    store.commit()
    later = T0 + timedelta(hours=1)
    kw = dict(vector="[1,0]", title="Widget A", created_at=later, window_hours=WINDOW)
    assert [c.group_id for c in gs.candidate_groups(store, space="a@2", **kw)] == [gid]
    assert gs.candidate_groups(store, space="b@2", **kw) == []
    assert gs.next_ungrouped(store, space="a@2", limit=10) == []
    assert len(gs.next_ungrouped(store, space="b@2", limit=10)) == 1


# ---- group bookkeeping ----


def test_has_primary_and_outlet_count(store):
    w1 = _item(store, "Widget talks", T0, outlet="Wire One")
    w2 = _item(store, "Widget talks resume", T0 + timedelta(hours=1), outlet="Wire One")
    p = _item(
        store,
        "Widget statement",
        T0 + timedelta(hours=2),
        outlet="Test Ministry",
        kind="primary",
    )
    lone = _item(
        store,
        "Gizmo notice",
        T0 + timedelta(hours=3),
        outlet="Test Ministry",
        kind="primary",
    )
    for iid, v in ((w1, A), (w2, A), (p, A), (lone, C)):
        _store_vec(store, iid, v)
    store.commit()

    def group(iid):
        return store.execute(
            "SELECT outlet_count, has_primary, last_seen_at, opened_at, title"
            " FROM item_groups g JOIN item_group_members m ON m.group_id = g.id"
            " WHERE m.item_id = %s",
            (iid,),
        ).fetchone()

    out = grouping.assign_pending(
        store, space=SPACE, thresholds=TH, window_hours=WINDOW, limit=2
    )
    assert out["grouped"] == 2
    assert group(w1)[:2] == (1, False)  # two items, one outlet
    out = grouping.assign_pending(
        store, space=SPACE, thresholds=TH, window_hours=WINDOW, limit=100
    )
    assert (out["opened"], out["joined"]) == (1, 1)
    count, primary, last_seen, opened, title = group(w1)
    assert (count, primary) == (2, True)
    assert last_seen == T0 + timedelta(hours=2)
    assert opened == T0 and title == "Widget talks"
    assert group(lone)[:2] == (1, True)  # a primary founder
    sims = store.execute(
        "SELECT similarity, title_similarity FROM item_group_members WHERE item_id = %s",
        (w1,),
    ).fetchone()
    assert sims == (None, None)  # the founder's similarities are NULL
    joined = store.execute(
        "SELECT round(similarity::numeric, 4), joined_at FROM item_group_members"
        " WHERE item_id = %s",
        (p,),
    ).fetchone()
    assert (float(joined[0]), joined[1]) == (1.0, T0 + timedelta(hours=2))


# ---- the store's remaining surface ----


def test_latest_calibration_is_the_newest_row_for_its_space(store):
    assert gs.latest_calibration(store, SPACE) is None
    _calibrate(store, join=0.8)
    _calibrate(store, join=0.9)
    _calibrate(store, space="modal:other@3", join=0.95)
    cal = gs.latest_calibration(store, SPACE)
    assert cal.space == SPACE and round(cal.join_sim, 6) == 0.9
    assert cal.week_ending == date(2026, 11, 22) and cal.retrieval_min_sim is None
    assert cal.judge_model == "judge-m" and cal.pairs_labelled == 200


def test_record_calls_writes_every_field(store):
    long_problem = "x" * 500
    gs.record_calls(
        store, [_record(4), _record(2, "failed", long_problem)], purpose="backfill"
    )
    rows = store.execute(
        "SELECT provider, space, purpose, inputs, tokens, outcome, length(problem)"
        " FROM embedding_calls ORDER BY id"
    ).fetchall()
    assert rows == [
        ("modal", SPACE, "backfill", 4, 40, "ok", None),
        ("modal", SPACE, "backfill", 2, 20, "failed", 300),
    ]


def test_item_ids_limit_what_is_embedded(store):
    ids = _many(store, 5)
    store.commit()
    fake = FakeEmbedder()
    out = grouping.embed_pending(
        store,
        fake,
        space=SPACE,
        dim=DIM,
        limit=100,
        purpose="calibration",
        item_ids=[ids[3], ids[1]],
    )
    assert out["embedded"] == 2
    assert fake.inputs == ["Widget item 001", "Widget item 003"]  # oldest first
    assert store.execute("SELECT DISTINCT purpose FROM embedding_calls").fetchall() == [
        ("calibration",)
    ]


def test_oldest_ungrouped_ignores_grouped_and_skipped_items(store):
    a = _item(store, "Widget A", T0)
    b = _item(store, "Widget B", T0 + timedelta(hours=1))
    c = _item(store, "Widget C", T0 + timedelta(hours=2))
    assert gs.oldest_ungrouped_created_at(store, space=SPACE) == T0
    _store_vec(store, a, A)
    ((pa, _),) = [
        r for r in gs.next_ungrouped(store, space=SPACE, limit=10) if r[0].id == a
    ]
    gs.open_group(store, space=SPACE, item=pa)
    gs.record_skip(store, space=SPACE, item_id=b, reason="empty_text")
    gs.record_skip(store, space=SPACE, item_id=b, reason="again")  # no conflict error
    assert gs.oldest_ungrouped_created_at(store, space=SPACE) == T0 + timedelta(hours=2)
    assert [p.id for p, _ in gs.next_ungrouped(store, space=SPACE, limit=10)] == [c]
    assert gs.oldest_ungrouped_created_at(store, space="modal:other@3") == T0


def test_an_embedder_for_another_space_is_refused(store):
    _item(store, "Widget A", T0)
    store.commit()
    fake = FakeEmbedder(space="modal:other@3")
    with pytest.raises(EmbedError) as ei:
        grouping.embed_pending(
            store, fake, space=SPACE, dim=DIM, limit=10, purpose="items"
        )
    assert ei.value.kind == "config" and fake.sent == []

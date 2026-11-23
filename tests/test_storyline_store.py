"""Storage tests for migration 0019 and storyline_store (plan 2a, task 2).

DB-gated: a skip is not a pass. Storylines and headlines are invented; the repo
is public.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import comprehend
import conftest
import db
import storyline_store as ss

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)

TARGET = "0019_storylines"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
USAGE = {"input_tokens": 10, "output_tokens": 5}


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


def _outlet(conn):
    row = conn.execute("SELECT id FROM outlets WHERE name = 'Test Wire'").fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO outlets (name) VALUES ('Test Wire') RETURNING id"
    ).fetchone()[0]


def _item(conn, n, age_hours=1.0):
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            _outlet(conn),
            f"https://example.test/{n}",
            f"Gizmo strike headline {n}",
            f"body {n}",
            f"hash{n}",
            NOW - timedelta(hours=age_hours),
        ),
    ).fetchone()[0]


def _decision(conn, item_id, outcome, attempts=0, defers=0):
    conn.execute(
        "INSERT INTO item_attach_decisions (item_id, outcome, model, attempts, defers)"
        " VALUES (%s, %s, 'm', %s, %s)",
        (item_id, outcome, attempts, defers),
    )


def _add(conn, name="Widget tariffs", **kw):
    kw.setdefault("origin", "operator")
    kw.setdefault("max_live", 60)
    return ss.add_storyline(conn, name, "Invented description", **kw)


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _down(conn):
    db.run_migrations(
        conn, direction="down", steps=conftest.steps_back_through(conn, TARGET)
    )


NEW_TABLES = (
    "storylines",
    "storyline_proposals",
    "item_attach_decisions",
    "item_attachments",
)


def test_up_and_down_round_trip_when_empty(store):
    for t in NEW_TABLES:
        assert store.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None
    _down(store)
    store.commit()
    for t in NEW_TABLES:
        assert store.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is None
    db.run_migrations(store)
    store.commit()
    assert _count(store, "storylines") == 0


def test_down_refuses_while_a_storyline_exists(store):
    _add(store)
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        _down(store)
    store.rollback()
    assert _count(store, "storylines") == 1


def test_down_refuses_while_a_proposal_exists(store):
    ss.add_proposal(store, "Gizmo strike", "d", origin="seed")
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        _down(store)
    store.rollback()
    assert _count(store, "storyline_proposals") == 1


def test_down_refuses_while_a_decision_exists(store):
    item = _item(store, 1)
    ss.record_decision(store, item, [], model="m")
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        _down(store)
    store.rollback()
    assert _count(store, "item_attach_decisions") == 1


def test_down_refuses_while_an_attachment_exists(store):
    sid = _add(store).storyline_id
    item = _item(store, 1)
    store.execute(
        "INSERT INTO item_attachments (item_id, storyline_id) VALUES (%s, %s)",
        (item, sid),
    )
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        _down(store)
    store.rollback()
    assert _count(store, "item_attachments") == 1


@pytest.mark.parametrize("stage", ["attach", "seed", "attach_check"])
def test_down_refuses_while_attach_spend_exists(store, stage):
    comprehend.record_spend(store, stage, "claude-haiku-5-5", USAGE)
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        _down(store)
    store.rollback()
    assert _count(store, "comprehend_spend") == 1


def test_down_restores_the_two_stage_check_and_drops_the_fk(store):
    comprehend.record_spend(store, "triage", "claude-haiku-5-5", USAGE)
    store.commit()
    _down(store)
    store.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        comprehend.record_spend(store, "attach", "claude-haiku-5-5", USAGE)
    store.rollback()
    # the FK is gone: an unknown storyline id is accepted again
    store.execute(
        "INSERT INTO thesis_evidence (thesis_key, storyline_id, on_date, direction,"
        " line, source) VALUES ('TESTCO', 999, '2026-11-20', 'supports', 'x',"
        " 'storyline')"
    )
    store.rollback()


def test_spend_accepts_the_attach_stage(store):
    for stage in ("attach", "seed", "attach_check", "triage", "integration"):
        comprehend.record_spend(store, stage, "claude-haiku-5-5", USAGE)
    assert _count(store, "comprehend_spend") == 5
    with pytest.raises(psycopg.errors.CheckViolation):
        comprehend.record_spend(store, "bogus", "claude-haiku-5-5", USAGE)
    store.rollback()


def test_thesis_evidence_storyline_fk_rejects_an_unknown_id(store):
    sql = (
        "INSERT INTO thesis_evidence (thesis_key, storyline_id, on_date, direction,"
        " line, source) VALUES ('TESTCO', %s, '2026-11-20', 'supports', 'x',"
        " 'storyline')"
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.execute(sql, (999,))
    store.rollback()
    sid = _add(store).storyline_id
    store.execute(sql, (sid,))
    assert _count(store, "thesis_evidence") == 1


def test_add_returns_the_new_id_and_it_is_live(store):
    out = _add(store, pinned=True)
    assert out.problem is None and out.storyline_id is not None
    [s] = ss.live_storylines(store)
    assert s == ss.Storyline(
        out.storyline_id, "Widget tariffs", "Invented description", "active", True, None
    )
    assert ss.count_live(store) == 1


def test_live_storylines_includes_dormant_orders_by_id_and_excludes_retired(store):
    a = _add(store, "Alpha").storyline_id
    b = _add(store, "Beta").storyline_id
    c = _add(store, "Gamma").storyline_id
    store.execute("UPDATE storylines SET status = 'dormant' WHERE id = %s", (a,))
    assert ss.retire_storyline(store, b, on_date="2026-11-23")
    live = ss.live_storylines(store)
    assert [s.id for s in live] == [a, c]
    assert live[0].status == "dormant"
    assert ss.count_live(store) == 2


def test_live_names_are_unique_case_insensitively_but_a_retired_name_is_reusable(
    store,
):
    first = _add(store, "Widget tariffs").storyline_id
    store.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.execute(
            "INSERT INTO storylines (name, description, origin)"
            " VALUES ('WIDGET TARIFFS', 'd', 'operator')"
        )
    store.rollback()
    assert ss.retire_storyline(store, first, on_date="2026-11-23")
    again = _add(store, "WIDGET TARIFFS")
    assert again.problem is None and again.storyline_id != first


def test_add_refuses_at_the_bound(store):
    _add(store, "One", max_live=2)
    _add(store, "Two", max_live=2)
    out = _add(store, "Three", max_live=2)
    assert out == ss.Outcome(None, "bound")
    assert ss.count_live(store) == 2


def test_the_bound_is_checked_before_the_duplicate(store):
    _add(store, "One", max_live=1)
    assert _add(store, "One", max_live=1) == ss.Outcome(None, "bound")


def test_add_refuses_a_duplicate_live_name(store):
    _add(store, "Widget tariffs")
    out = _add(store, "widget TARIFFS")
    assert out == ss.Outcome(None, "duplicate")
    assert ss.count_live(store) == 1


def test_retire_sets_retired_on_and_leaves_live_list(store):
    sid = _add(store).storyline_id
    assert ss.retire_storyline(store, sid, on_date="2026-11-23") is True
    row = store.execute(
        "SELECT status, retired_on FROM storylines WHERE id = %s", (sid,)
    ).fetchone()
    assert row[0] == "retired" and str(row[1]) == "2026-11-23"
    assert ss.live_storylines(store) == []
    assert ss.count_live(store) == 0


def test_retire_twice_returns_false(store):
    sid = _add(store).storyline_id
    assert ss.retire_storyline(store, sid, on_date="2026-11-23")
    assert ss.retire_storyline(store, sid, on_date="2026-11-24") is False
    retired_on = store.execute(
        "SELECT retired_on FROM storylines WHERE id = %s", (sid,)
    ).fetchone()[0]
    assert str(retired_on) == "2026-11-23"


def test_retire_unknown_returns_false(store):
    assert ss.retire_storyline(store, 12345, on_date="2026-11-23") is False


def test_add_proposal_and_pending_proposals_oldest_first_with_origin_filter(store):
    p1 = ss.add_proposal(store, "Gizmo strike", "d1", origin="seed", pinned=True)
    p2 = ss.add_proposal(store, "Widget tariffs", "d2", origin="burst")
    p3 = ss.add_proposal(store, "Doohickey ban", "d3", origin="seed")
    rows = ss.pending_proposals(store)
    assert [r["id"] for r in rows] == [p1, p2, p3]
    assert rows[0] == {
        "id": p1,
        "name": "Gizmo strike",
        "description": "d1",
        "pinned": True,
        "origin": "seed",
    }
    assert [r["id"] for r in ss.pending_proposals(store, origin="seed")] == [p1, p3]
    assert [r["id"] for r in ss.pending_proposals(store, origin="burst")] == [p2]


def test_decide_accept_creates_seed_storyline_and_links_proposal(store):
    pid = ss.add_proposal(store, "Gizmo strike", "d", origin="seed", pinned=True)
    out = ss.decide_proposal(store, pid, True, max_live=60)
    assert out.problem is None and out.storyline_id is not None
    row = store.execute(
        "SELECT outcome, storyline_id, decided_at FROM storyline_proposals WHERE id=%s",
        (pid,),
    ).fetchone()
    assert row[0] == "accepted" and row[1] == out.storyline_id and row[2] is not None
    s = store.execute(
        "SELECT origin, pinned, name, status FROM storylines WHERE id = %s",
        (out.storyline_id,),
    ).fetchone()
    assert s == ("seed", True, "Gizmo strike", "active")
    assert ss.pending_proposals(store) == []


def test_decide_accept_a_non_seed_proposal_uses_origin_proposal(store):
    pid = ss.add_proposal(store, "Gizmo strike", "d", origin="burst")
    out = ss.decide_proposal(store, pid, True, max_live=60)
    origin = store.execute(
        "SELECT origin FROM storylines WHERE id = %s", (out.storyline_id,)
    ).fetchone()[0]
    assert origin == "proposal"


def test_decide_twice_reports_already(store):
    pid = ss.add_proposal(store, "Gizmo strike", "d", origin="seed")
    first = ss.decide_proposal(store, pid, True, max_live=60)
    assert first.storyline_id is not None
    assert ss.decide_proposal(store, pid, True, max_live=60) == ss.Outcome(
        None, "already accepted"
    )
    assert _count(store, "storylines") == 1
    rid = ss.add_proposal(store, "Widget tariffs", "d", origin="seed")
    ss.decide_proposal(store, rid, False, max_live=60)
    assert ss.decide_proposal(store, rid, False, max_live=60) == ss.Outcome(
        None, "already rejected"
    )
    assert ss.decide_proposal(store, rid, True, max_live=60) == ss.Outcome(
        None, "already rejected"
    )
    assert _count(store, "storylines") == 1


def test_decide_at_bound_keeps_proposal_pending(store):
    _add(store, "One", max_live=1)
    pid = ss.add_proposal(store, "Gizmo strike", "d", origin="seed")
    assert ss.decide_proposal(store, pid, True, max_live=1) == ss.Outcome(None, "bound")
    assert [r["id"] for r in ss.pending_proposals(store)] == [pid]
    outcome = store.execute(
        "SELECT outcome FROM storyline_proposals WHERE id = %s", (pid,)
    ).fetchone()[0]
    assert outcome is None


def test_decide_duplicate_keeps_proposal_pending(store):
    _add(store, "Gizmo strike")
    pid = ss.add_proposal(store, "gizmo STRIKE", "d", origin="seed")
    assert ss.decide_proposal(store, pid, True, max_live=60) == ss.Outcome(
        None, "duplicate"
    )
    assert [r["id"] for r in ss.pending_proposals(store)] == [pid]


def test_decide_reject_creates_nothing(store):
    pid = ss.add_proposal(store, "Gizmo strike", "d", origin="seed")
    out = ss.decide_proposal(store, pid, False, max_live=60)
    assert out == ss.Outcome(None, None)
    assert _count(store, "storylines") == 0
    row = store.execute(
        "SELECT outcome, storyline_id, decided_at FROM storyline_proposals WHERE id=%s",
        (pid,),
    ).fetchone()
    assert row[0] == "rejected" and row[1] is None and row[2] is not None
    assert ss.pending_proposals(store) == []


def test_decide_unknown_id(store):
    assert ss.decide_proposal(store, 999, True, max_live=60) == ss.Outcome(
        None, "unknown"
    )
    assert ss.decide_proposal(store, 999, False, max_live=60) == ss.Outcome(
        None, "unknown"
    )


def test_undecided_items_respects_lookback_limit_attempts_and_defers(store):
    old = _item(store, 1, age_hours=30)
    fresh = _item(store, 2, age_hours=2)
    failed3 = _item(store, 3, age_hours=1)
    failed1 = _item(store, 4, age_hours=1)
    deferred10 = _item(store, 5, age_hours=1)
    deferred2 = _item(store, 6, age_hours=1)
    none_ = _item(store, 7, age_hours=1)
    _decision(store, failed3, "failed", attempts=3)
    _decision(store, failed1, "failed", attempts=1)
    _decision(store, deferred10, "deferred", defers=10)
    _decision(store, deferred2, "deferred", defers=2)
    _decision(store, none_, "none")
    kw = dict(now=NOW, lookback_hours=24, max_attempts=3, max_defers=10)
    got = ss.undecided_items(store, limit=50, **kw)
    ids = [r["id"] for r in got]
    assert old not in ids and failed3 not in ids and deferred10 not in ids
    assert none_ not in ids
    assert ids[0] == fresh
    assert set(ids) == {fresh, failed1, deferred2}
    assert len(ids) == 3
    assert set(got[0]) == {"id", "title", "body"}
    assert got[0]["title"] == "Gizmo strike headline 2"
    assert [r["id"] for r in ss.undecided_items(store, limit=1, **kw)] == [fresh]


def test_undecided_items_orders_oldest_first(store):
    a = _item(store, 1, age_hours=5)
    b = _item(store, 2, age_hours=9)
    c = _item(store, 3, age_hours=3)
    got = ss.undecided_items(
        store, now=NOW, lookback_hours=24, max_attempts=3, max_defers=10, limit=10
    )
    assert [r["id"] for r in got] == [b, a, c]


def test_record_decision_attached_writes_attachments_and_last_attached_at(store):
    s1 = _add(store, "One").storyline_id
    s2 = _add(store, "Two").storyline_id
    s3 = _add(store, "Three").storyline_id
    item = _item(store, 1)
    ss.record_decision(store, item, [s1, s2], model="claude-haiku-5-5")
    d = store.execute(
        "SELECT outcome, model, attempts, defers FROM item_attach_decisions"
        " WHERE item_id = %s",
        (item,),
    ).fetchone()
    assert d == ("attached", "claude-haiku-5-5", 0, 0)
    rows = store.execute(
        "SELECT storyline_id, status FROM item_attachments WHERE item_id = %s"
        " ORDER BY storyline_id",
        (item,),
    ).fetchall()
    assert rows == [(s1, "attached"), (s2, "attached")]
    last = dict(store.execute("SELECT id, last_attached_at FROM storylines").fetchall())
    assert last[s1] is not None and last[s2] is not None and last[s3] is None


def test_record_decision_empty_list_records_none(store):
    item = _item(store, 1)
    ss.record_decision(store, item, [], model="m")
    outcome = store.execute(
        "SELECT outcome FROM item_attach_decisions WHERE item_id = %s", (item,)
    ).fetchone()[0]
    assert outcome == "none"
    assert _count(store, "item_attachments") == 0


def test_record_decision_overwrites_failed_keeping_counters(store):
    sid = _add(store).storyline_id
    item = _item(store, 1)
    ss.record_failure(store, item, model="m", problem="bad label")
    ss.record_deferral(store, item, model="m", problem="max_tokens")
    ss.record_failure(store, item, model="m", problem="bad again")
    ss.record_decision(store, item, [sid], model="m2")
    d = store.execute(
        "SELECT outcome, model, attempts, defers FROM item_attach_decisions"
    ).fetchone()
    assert d == ("attached", "m2", 2, 1)
    assert _count(store, "item_attachments") == 1


def test_record_decision_overwrites_deferred_row(store):
    item = _item(store, 1)
    ss.record_deferral(store, item, model="m", problem="p")
    ss.record_decision(store, item, [], model="m")
    d = store.execute("SELECT outcome, defers FROM item_attach_decisions").fetchone()
    assert d == ("none", 1)


def test_record_decision_never_overwrites_a_final_outcome(store):
    s1 = _add(store, "One").storyline_id
    s2 = _add(store, "Two").storyline_id
    none_item = _item(store, 1)
    att_item = _item(store, 2)
    ss.record_decision(store, none_item, [], model="first")
    ss.record_decision(store, att_item, [s1], model="first")
    store.commit()
    q = (
        "SELECT item_id, outcome, model, decided_at FROM item_attach_decisions"
        " ORDER BY item_id"
    )
    before = store.execute(q).fetchall()
    ss.record_decision(store, none_item, [s1, s2], model="second")
    ss.record_decision(store, att_item, [s2], model="second")
    ss.record_decision(store, att_item, [], model="second")
    assert store.execute(q).fetchall() == before
    assert store.execute(
        "SELECT item_id, storyline_id FROM item_attachments ORDER BY 1, 2"
    ).fetchall() == [(att_item, s1)]
    last = dict(store.execute("SELECT id, last_attached_at FROM storylines").fetchall())
    assert last[s2] is None


def test_record_failure_counts_attempts(store):
    item = _item(store, 1)
    ss.record_failure(store, item, model="m", problem="first")
    ss.record_failure(store, item, model="m", problem="second")
    row = store.execute(
        "SELECT outcome, attempts, defers, last_problem FROM item_attach_decisions"
    ).fetchone()
    assert row == ("failed", 2, 0, "second")


def test_record_failure_cuts_the_problem_to_300_characters(store):
    item = _item(store, 1)
    ss.record_failure(store, item, model="m", problem="y" * 500)
    n = store.execute(
        "SELECT length(last_problem) FROM item_attach_decisions"
    ).fetchone()
    assert n[0] == 300


def test_record_failure_does_not_touch_a_final_row(store):
    item = _item(store, 1)
    ss.record_decision(store, item, [], model="m")
    ss.record_failure(store, item, model="m", problem="late")
    row = store.execute(
        "SELECT outcome, attempts, last_problem FROM item_attach_decisions"
    ).fetchone()
    assert row == ("none", 0, None)


def test_record_deferral_counts_defers_not_attempts(store):
    item = _item(store, 1)
    ss.record_deferral(store, item, model="m", problem="max_tokens")
    ss.record_deferral(store, item, model="m", problem="no tool block")
    row = store.execute(
        "SELECT outcome, attempts, defers, last_problem FROM item_attach_decisions"
    ).fetchone()
    assert row == ("deferred", 0, 2, "no tool block")


def test_record_deferral_turns_a_failed_row_deferred(store):
    item = _item(store, 1)
    ss.record_failure(store, item, model="m", problem="bad")
    ss.record_deferral(store, item, model="m", problem="stop")
    row = store.execute(
        "SELECT outcome, attempts, defers, last_problem FROM item_attach_decisions"
    ).fetchone()
    assert row == ("deferred", 1, 1, "stop")


def test_record_failure_turns_a_deferred_row_failed(store):
    item = _item(store, 1)
    ss.record_deferral(store, item, model="m", problem="stop")
    ss.record_failure(store, item, model="m", problem="bad")
    row = store.execute(
        "SELECT outcome, attempts, defers FROM item_attach_decisions"
    ).fetchone()
    assert row == ("failed", 1, 1)


def test_record_deferral_does_not_touch_a_final_row(store):
    item = _item(store, 1)
    ss.record_decision(store, item, [], model="m")
    ss.record_deferral(store, item, model="m", problem="late")
    row = store.execute("SELECT outcome, defers FROM item_attach_decisions").fetchone()
    assert row == ("none", 0)


def test_attached_since_counts_per_storyline(store):
    s1 = _add(store, "One").storyline_id
    s2 = _add(store, "Two").storyline_id
    _add(store, "Three")
    a, b, c = _item(store, 1), _item(store, 2), _item(store, 3)
    ss.record_decision(store, a, [s1, s2], model="m")
    ss.record_decision(store, b, [s1], model="m")
    ss.record_decision(store, c, [s1], model="m")
    store.execute(
        "UPDATE item_attachments SET attached_at = %s WHERE item_id = %s",
        (NOW - timedelta(days=3), c),
    )
    assert ss.attached_since(store, since=NOW - timedelta(days=30)) == {s1: 3, s2: 1}
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    assert ss.attached_since(store, since=cutoff) == {s1: 2, s2: 1}


def test_get_proposal_returns_the_row_and_none_for_unknown(store):
    pid = ss.add_proposal(store, "Gizmo strike", "d1", origin="seed", pinned=True)
    assert ss.get_proposal(store, pid) == {
        "id": pid,
        "name": "Gizmo strike",
        "description": "d1",
        "pinned": True,
        "origin": "seed",
        "outcome": None,
    }
    ss.decide_proposal(store, pid, False, max_live=5)
    assert ss.get_proposal(store, pid)["outcome"] == "rejected"
    assert ss.get_proposal(store, 987654) is None

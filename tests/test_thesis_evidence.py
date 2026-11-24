"""Storage tests for thesis_evidence (storylines phase 1, task 1).

DB-gated: a skip is not a pass, every assertion here is about what Postgres does.
Keys and lines are invented; the repo is public.
"""

import psycopg
import pytest

import conftest
import dated_memory
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test pgvector/pgvector:0.8.7-pg18-trixie",
)

TARGET = "0018_thesis_evidence"


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


def _row(key="TESTCO", direction="supports", line="Widget orders beat forecast"):
    return dated_memory.EvidenceRow(key, direction, line)


def test_save_and_read_back_newest_first(store):
    assert (
        dated_memory.save_brief_evidence(store, "2026-11-20", [_row()], model="m") == 1
    )
    dated_memory.save_brief_evidence(
        store,
        "2026-11-21",
        [_row(direction="challenges", line="Regulator opens probe")],
        model="m",
    )
    got = dated_memory.recent_evidence(store, "TESTCO", since="2026-11-01")
    assert [(r["on_date"], r["direction"], r["source"]) for r in got] == [
        ("2026-11-21", "challenges", "brief"),
        ("2026-11-20", "supports", "brief"),
    ]
    assert got[0]["line"] == "Regulator opens probe"
    assert isinstance(got[0]["on_date"], str)


def test_same_day_rows_are_newest_id_first(store):
    dated_memory.save_brief_evidence(
        store, "2026-11-20", [_row(line="first"), _row(line="second")], model="m"
    )
    got = dated_memory.recent_evidence(store, "TESTCO", since="2026-11-01")
    assert [r["line"] for r in got] == ["second", "first"]


def test_save_records_model_and_empty_list_writes_nothing(store):
    assert dated_memory.save_brief_evidence(store, "2026-11-20", [], model="m") == 0
    assert not dated_memory.has_brief_evidence(store, "2026-11-20")
    dated_memory.save_brief_evidence(store, "2026-11-20", [_row()], model="model-x")
    assert store.execute("SELECT model, source FROM thesis_evidence").fetchall() == [
        ("model-x", "brief")
    ]


def test_recent_evidence_respects_since(store):
    dated_memory.save_brief_evidence(store, "2026-11-10", [_row(line="old")], model="m")
    dated_memory.save_brief_evidence(
        store, "2026-11-15", [_row(line="edge")], model="m"
    )
    got = dated_memory.recent_evidence(store, "TESTCO", since="2026-11-15")
    assert [r["line"] for r in got] == ["edge"]


def test_recent_evidence_filters_by_key(store):
    dated_memory.save_brief_evidence(
        store,
        "2026-11-20",
        [_row(), _row(key="cluster:widgets", line="other")],
        model="m",
    )
    got = dated_memory.recent_evidence(store, "cluster:widgets", since="2026-11-01")
    assert [r["line"] for r in got] == ["other"]
    assert dated_memory.recent_evidence(store, "NOPE", since="2026-11-01") == []


def test_has_brief_evidence_is_per_day(store):
    dated_memory.save_brief_evidence(store, "2026-11-20", [_row()], model="m")
    assert dated_memory.has_brief_evidence(store, "2026-11-20") is True
    assert dated_memory.has_brief_evidence(store, "2026-11-21") is False


def test_has_brief_evidence_ignores_storyline_rows(store):
    store.execute(
        "INSERT INTO thesis_evidence (thesis_key, on_date, direction, line, source)"
        " VALUES ('TESTCO', '2026-11-20', 'supports', 'x', 'storyline')"
    )
    assert dated_memory.has_brief_evidence(store, "2026-11-20") is False


def test_keys_with_evidence_is_distinct_and_sorted(store):
    dated_memory.save_brief_evidence(
        store,
        "2026-11-20",
        [_row("ZED"), _row("TESTCO"), _row("TESTCO", line="again")],
        model="m",
    )
    dated_memory.save_brief_evidence(store, "2026-11-01", [_row("OLDCO")], model="m")
    assert dated_memory.keys_with_evidence(store, since="2026-11-10") == [
        "TESTCO",
        "ZED",
    ]
    assert dated_memory.keys_with_evidence(store, since="2026-12-01") == []


def test_direction_outside_the_enum_is_refused_by_postgres(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute(
            "INSERT INTO thesis_evidence (thesis_key, on_date, direction, line, source)"
            " VALUES ('TESTCO', '2026-11-20', 'neutral', 'x', 'brief')"
        )


def test_brief_row_cannot_carry_a_storyline_id(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute(
            "INSERT INTO thesis_evidence"
            " (thesis_key, storyline_id, on_date, direction, line, source)"
            " VALUES ('TESTCO', 1, '2026-11-20', 'supports', 'x', 'brief')"
        )


def test_line_length_and_empty_key_are_refused(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        dated_memory.save_brief_evidence(
            store, "2026-11-20", [_row(line="x" * 301)], model="m"
        )
    store.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        dated_memory.save_brief_evidence(store, "2026-11-20", [_row(key="")], model="m")


def test_down_refuses_while_rows_exist(store):
    dated_memory.save_brief_evidence(store, "2026-11-20", [_row()], model="m")
    store.commit()
    with pytest.raises(Exception, match="refusing"):
        db.run_migrations(
            store, direction="down", steps=conftest.steps_back_through(store, TARGET)
        )
    store.rollback()
    assert (
        store.execute("SELECT to_regclass('thesis_evidence')").fetchone()[0] is not None
    )
    assert store.execute("SELECT count(*) FROM thesis_evidence").fetchone()[0] == 1


def test_down_drops_the_empty_table(store):
    db.run_migrations(
        store, direction="down", steps=conftest.steps_back_through(store, TARGET)
    )
    store.commit()
    assert store.execute("SELECT to_regclass('thesis_evidence')").fetchone()[0] is None

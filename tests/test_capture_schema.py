"""Schema tests for capture telemetry (news-brief-b42.1).

DB-gated for the same reason tests/test_kb_schema.py is: a skip is not a pass,
and every assertion here is about what Postgres actually does.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


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


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_the_three_telemetry_tables_exist(store):
    assert _columns(store, "capture_runs")
    assert _columns(store, "feed_polls")
    assert _columns(store, "feed_sightings")


def test_a_sighting_is_unique_per_feed_and_hash(store):
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters Markets', 'abc', 1)"
    )
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO feed_sightings (source_name, content_hash, position) "
            "VALUES ('Reuters Markets', 'abc', 2)"
        )


def test_the_same_hash_on_two_feeds_is_two_sightings(store):
    """The per-FEED key, not a global one: one item carried by two feeds is two
    sightings, which is what makes per-feed dwell time computable at all."""
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters Markets', 'abc', 1)"
    )
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters World', 'abc', 1)"
    )
    n = store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0]
    assert n == 2


def test_a_poll_records_its_failure_kind_and_belongs_to_a_run(store):
    run_id = store.execute(
        "INSERT INTO capture_runs (enabled) VALUES (true) RETURNING id"
    ).fetchone()[0]
    store.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, failure, entries_seen) "
        "VALUES (%s, 'Kyiv Independent', 'http_403', 0)",
        (run_id,),
    )
    row = store.execute(
        "SELECT failure, entries_seen FROM feed_polls WHERE capture_run_id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("http_403", 0)


def test_a_successful_poll_stores_null_failure(store):
    run_id = store.execute(
        "INSERT INTO capture_runs (enabled) VALUES (true) RETURNING id"
    ).fetchone()[0]
    store.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, entries_seen) "
        "VALUES (%s, 'TASS', 42)",
        (run_id,),
    )
    failure = store.execute(
        "SELECT failure FROM feed_polls WHERE capture_run_id = %s", (run_id,)
    ).fetchone()[0]
    assert failure is None


def test_a_poll_cannot_orphan_itself_from_a_run(store):
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO feed_polls (capture_run_id, source_name) VALUES (999999, 'x')"
        )


def test_the_down_migration_removes_all_three(conn):
    """Executed, not assumed: no down migration is trusted until it has run."""
    db.run_migrations(conn)
    conn.commit()
    db.run_migrations(conn, direction="down")
    conn.commit()
    for table in ("capture_runs", "feed_polls", "feed_sightings"):
        assert not _columns(conn, table), f"{table} survived the down migration"

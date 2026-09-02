"""Migration runner and advisory lock.

These tests need a real Postgres: the runner's whole job is to talk to one, and
a fake would test the fake. They skip loudly when no database is configured so a
missing database can never read as a pass — on `db.is_configured`, the same
predicate `db.connect` reads, so a run pointed at a database through the
discrete POSTGRES_* variables is not reported as skipped.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing."""
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def test_up_creates_the_expected_tables(conn):
    applied = db.run_migrations(conn)
    assert applied == [
        "0001_runtime_foundation",
        "0002_job_runs_created_at",
        "0003_sources",
        "0004_preferences",
        "0005_runtime_state",
    ]
    assert {
        "schema_migrations",
        "users",
        "settings",
        "job_runs",
        "sources",
        "preferences",
        "runtime_state",
    } <= _tables(conn)


def test_0002_gives_job_runs_the_created_at_a_queued_row_is_aged_by(conn):
    db.run_migrations(conn)
    columns = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'job_runs'"
        ).fetchall()
    }
    assert "created_at" in columns


def test_up_is_idempotent(conn):
    db.run_migrations(conn)
    assert db.run_migrations(conn) == []


@pytest.fixture()
def two_migrations(tmp_path, monkeypatch):
    """0001 plus a throwaway 0002, in a temp directory.

    With ONE migration on disk, a correct `down` and one that drops the entire
    database are indistinguishable — both leave an empty schema and both make
    the test pass. A second migration is what makes default-one-step behaviour
    observable at all, which is why this fixture exists rather than testing
    against the real migrations directory.
    """
    for name in ("0001_runtime_foundation_up.sql", "0001_runtime_foundation_down.sql"):
        (tmp_path / name).write_text(
            (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / "0002_throwaway_up.sql").write_text(
        "CREATE TABLE throwaway (id INT);", encoding="utf-8"
    )
    (tmp_path / "0002_throwaway_down.sql").write_text(
        "DROP TABLE throwaway;", encoding="utf-8"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)
    return tmp_path


def test_down_defaults_to_exactly_one_step(conn, two_migrations):
    db.run_migrations(conn)
    assert "throwaway" in _tables(conn)

    reverted = db.run_migrations(conn, direction="down")

    assert reverted == ["0002_throwaway"]
    assert "throwaway" not in _tables(conn)
    assert {"users", "settings", "job_runs"} <= _tables(conn), (
        "a default rollback must undo the LAST migration, not the database"
    )
    assert db.applied_versions(conn) == ["0001_runtime_foundation"]


def test_rolling_back_everything_requires_steps_zero(conn, two_migrations):
    db.run_migrations(conn)
    db.run_migrations(conn, direction="down", steps=0)
    assert _tables(conn) == {"schema_migrations"}
    assert db.applied_versions(conn) == []


def test_down_restores_the_prior_schema(conn, two_migrations):
    before = _tables(conn)
    db.run_migrations(conn)
    db.run_migrations(conn, direction="down", steps=0)
    after = _tables(conn) - {"schema_migrations"}
    assert after == before - {"schema_migrations"}


def test_advisory_lock_is_exclusive_across_connections(conn):
    db.run_migrations(conn)
    with db.connect() as other:
        with db.advisory_lock(conn, "collect") as first:
            assert first is True
            with db.advisory_lock(other, "collect") as second:
                assert second is False


def test_advisory_lock_releases_when_the_connection_closes(conn):
    db.run_migrations(conn)
    holder = db.connect()
    with db.advisory_lock(holder, "collect") as acquired:
        assert acquired is True
        holder.close()
    with db.connect() as taker:
        with db.advisory_lock(taker, "collect") as acquired:
            assert acquired is True


def test_different_job_names_do_not_collide(conn):
    db.run_migrations(conn)
    with db.connect() as other:
        with db.advisory_lock(conn, "collect") as a:
            with db.advisory_lock(other, "weekly") as b:
                assert a is True and b is True


def test_connect_options_reach_the_server_as_a_statement_timeout():
    """The bound supervisor.shutdown depends on, asserted against a real server.

    A wrong libpq option name is not an error — it is silently nothing — so the
    only honest check is to ask the session what it ended up with, and then to
    watch a slow statement actually be cut off. Without this, `connect(options=...)`
    could be inert and the shutdown budget would be a comment rather than a bound.
    """
    import psycopg
    import supervisor

    with db.connect(
        connect_timeout=supervisor.SHUTDOWN_DB_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={supervisor.SHUTDOWN_DB_STATEMENT_TIMEOUT_MS}",
    ) as c:
        setting = c.execute("SHOW statement_timeout").fetchone()[0]
        assert setting == f"{supervisor.SHUTDOWN_DB_STATEMENT_TIMEOUT_MS}ms"
        with pytest.raises(psycopg.errors.QueryCanceled):
            c.execute("SELECT pg_sleep(5)")


# ── The /jobs read path and the manual-run queue ─────────────────────────────


@pytest.fixture()
def schema(conn):
    """A migrated database. The ledger tests below need tables, not a blank slate."""
    db.run_migrations(conn)
    return conn


def test_latest_runs_returns_the_most_recent_row_per_job(schema):
    db.finish_run(schema, db.start_run(schema, "collect", None, "manual"), 0)
    second = db.start_run(schema, "collect", None, "manual")
    db.finish_run(schema, second, 3)

    latest = db.latest_runs(schema, ["collect"])

    assert latest["collect"]["exit_code"] == 3
    assert latest["collect"]["status"] == "finished"


def test_latest_runs_does_not_let_a_missed_row_hide_behind_an_older_success(schema):
    """The ordering test. A `missed` row is inserted with started_at NULL, so
    ordering by started_at would surface Tuesday's green run and leave the
    operator believing a job that has stopped running is fine — the exact
    silence /jobs exists to break."""
    db.finish_run(schema, db.start_run(schema, "weekly", None, "scheduled"), 0)
    db.record_missed(schema, "weekly", None)

    latest = db.latest_runs(schema, ["weekly"])

    assert latest["weekly"]["status"] == "missed"
    assert latest["weekly"]["started_at"] is None


def test_latest_runs_omits_a_job_that_has_never_run(schema):
    db.finish_run(schema, db.start_run(schema, "collect", None, "manual"), 0)

    latest = db.latest_runs(schema, ["collect", "submit"])

    assert set(latest) == {"collect"}


def test_enqueue_manual_leaves_the_catch_up_rule_untouched(schema):
    """A manual row carries scheduled_for NULL, and latest_scheduled_for is
    max(scheduled_for), which ignores NULLs. So /run collect at 15:00 cannot
    consume tomorrow's 06:00 fire time — the property the seeding fix in
    425b736 depends on."""
    db.enqueue_manual(schema, "collect")

    assert db.latest_scheduled_for(schema, "collect") is None


def test_enqueue_manual_records_a_queued_row_with_an_age(schema):
    run_id = db.enqueue_manual(schema, "collect")

    queued = db.queued_runs(schema)

    assert [(r["id"], r["job_name"]) for r in queued] == [(run_id, "collect")]
    assert queued[0]["created_at"] is not None


def test_claim_queued_moves_the_row_to_running(schema):
    run_id = db.enqueue_manual(schema, "monitor")

    assert db.claim_queued(schema, run_id) is True

    latest = db.latest_runs(schema, ["monitor"])["monitor"]
    assert latest["status"] == "running"
    assert latest["started_at"] is not None
    assert db.queued_runs(schema) == []


def test_claim_queued_refuses_a_row_that_is_no_longer_queued(schema):
    """Guards the double-spawn: whatever the tick believed when it read the
    queue, only a row still in `queued` may be claimed."""
    run_id = db.enqueue_manual(schema, "monitor")
    assert db.claim_queued(schema, run_id) is True

    assert db.claim_queued(schema, run_id) is False


def test_queued_rows_are_returned_oldest_first(schema):
    first = db.enqueue_manual(schema, "collect")
    second = db.enqueue_manual(schema, "monitor")

    assert [r["id"] for r in db.queued_runs(schema)] == [first, second]

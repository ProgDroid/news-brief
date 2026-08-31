"""Migration runner and advisory lock.

These tests need a real Postgres: the runner's whole job is to talk to one, and
a fake would test the fake. They skip loudly when DATABASE_URL is unset so a
missing database can never read as a pass.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.database_url(),
    reason="DATABASE_URL is not set: start a Postgres and export it, e.g. "
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


def test_up_creates_the_four_tables(conn):
    applied = db.run_migrations(conn)
    assert applied == ["0001_runtime_foundation"]
    assert {"schema_migrations", "users", "settings", "job_runs"} <= _tables(conn)


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

"""Storage tests for the claim ledger cutover (news-brief-bqa.10).

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
    """A connection to a schema-less database: every test starts from nothing."""
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    """A fully migrated database, through 0007."""
    db.run_migrations(conn)
    conn.commit()
    return conn


def _indexdef(conn, name):
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


def test_claims_has_a_nullable_retired_on_date(store):
    row = store.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert row == ("date", "YES")


def test_rule_four_index_excludes_retired_claims(store):
    """0006:215-218 records the earlier version of this bug: a claim that should
    have left the index never did, and rule 4 re-fired on it every morning."""
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")


def test_a_live_index_supports_the_daily_load(store):
    assert "retired_on IS NULL" in _indexdef(store, "claims_live")


def test_0007_rolls_back_and_reapplies(store):
    """No down migration is trusted until it has been run. The index must come
    BACK on rollback, not merely be dropped -- 0006 owns it, so leaving it
    missing would corrupt the schema 0006 promises."""
    db.run_migrations(store, direction="down", steps=1)
    store.commit()
    cols = store.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert cols is None
    restored = _indexdef(store, "claims_open_resolution")
    assert restored is not None and "retired_on" not in restored
    db.run_migrations(store)
    store.commit()
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")

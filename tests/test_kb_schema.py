"""Constraint tests for the knowledge base schema (migration 0006).

Every CHECK, unique key and trigger in 0006 gets a test that tries to violate
it. A constraint nothing attempts to break is a comment: spec 12.2's argument
is that a field can look correct and carry no information, and the same is
true of a constraint nothing exercises.
"""

import contextlib

import psycopg
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
    """A connection to a schema-less database: every test starts from nothing.

    Module-local, matching tests/test_db.py and tests/test_config.py. Shared in
    conftest it would be reachable from every test module by parameter name
    alone, and it runs DROP SCHEMA public CASCADE.
    """
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    """A fully migrated database, including 0006."""
    db.run_migrations(conn)
    conn.commit()
    return conn


@contextlib.contextmanager
def rejects(conn, exc):
    """Assert the block raises `exc` AND leave the transaction usable.

    db.connect sets autocommit=False (db.py:110), so a constraint violation
    aborts the whole transaction and every later statement raises
    InFailedSqlTransaction -- a sibling of the error you expected, not a
    subclass, so the test fails on the wrong line with the wrong message.
    conn.transaction() opens a SAVEPOINT when already inside a transaction and
    rolls back to it, so the next assertion in the same test still works.

    Use this for EVERY expected violation, even where a bare pytest.raises
    would happen to work today -- the next author to add a second assertion to
    the test will not know to change it.
    """
    with pytest.raises(exc):
        with conn.transaction():
            yield


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _outlet(conn, name="Reuters"):
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _entity(conn, name="Apple", type_="company"):
    return conn.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id",
        (name, type_),
    ).fetchone()[0]


def test_outlet_kind_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, kind) VALUES ('X', 'blog')")


def test_perspective_null_is_allowed_and_a_bad_value_is_not(kb):
    """NULL means "no vantage claim made", NOT "neutral" -- calling a source
    neutral is a positive editorial claim, as contestable as picking a side."""
    kb.execute("INSERT INTO outlets (name, perspective) VALUES ('A', NULL)")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, perspective) VALUES ('B', 'NEUTRAL')")


def test_entity_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO entities (name, type) VALUES ('X', 'thing')")


def test_the_same_text_from_two_outlets_is_two_items(kb):
    """Syndicated wire copy must not collapse. Spec 3.4.

    A global UNIQUE (content_hash) would attribute a Reuters story carried by
    five outlets to whichever was ingested first, capping corroboration at one
    -- the opposite of why outlets was split out of sources.
    """
    for outlet_id in (_outlet(kb, "Reuters"), _outlet(kb, "Guardian")):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u', 't', 'HASH')",
            (outlet_id,),
        )
    assert kb.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_the_same_text_twice_from_one_outlet_is_rejected(kb):
    outlet_id = _outlet(kb)
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'HASH')",
        (outlet_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u2', 't2', 'HASH')",
            (outlet_id,),
        )


def test_two_instrument_mappings_with_no_market_collide(kb):
    """NULLS NOT DISTINCT, spec 3.4. Under default Postgres semantics NULLs
    compare distinct, so both rows would insert and one symbol would map to the
    same entity twice."""
    entity_id = _entity(kb, "Apple")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (entity_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
            "VALUES (%s, 'AAPL', 'equity')",
            (entity_id,),
        )


def test_asset_class_admits_index_not_only_equity_and_crypto(kb):
    """The commodity-signals-are-index-class bug: "no instrument for BRENT" was
    a wrong asset CLASS, not a missing symbol."""
    entity_id = _entity(kb, "Brent", "instrument")
    for cls in ("equity", "index", "crypto", "commodity", "fx"):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, market, asset_class) "
            "VALUES (%s, %s, 'X', %s)",
            (entity_id, f"S{cls}", cls),
        )
    assert kb.execute("SELECT count(*) FROM entity_instruments").fetchone()[0] == 5

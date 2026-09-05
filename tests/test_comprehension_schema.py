"""Constraint tests for migration 0009 (item_triage).

Every CHECK gets a test that tries to violate it. A constraint nothing
attempts to break is a comment -- the same argument tests/test_kb_schema.py
opens with.
"""

import psycopg
import pytest

import conftest
import db

TARGET = "0009_comprehension"

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


def _item(conn) -> int:
    outlet_id = conn.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'H') RETURNING id",
        (outlet_id,),
    ).fetchone()[0]


def _triage(conn, item_id, verdict="material", reason="topical", version=1):
    conn.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s)",
        (item_id, verdict, reason, version),
    )


def test_0009_creates_item_triage(kb):
    rows = kb.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert "item_triage" in {r[0] for r in rows}


def test_verdict_rejects_an_unknown_value(kb):
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="maybe", reason="topical")


def test_reason_rejects_an_unknown_value(kb):
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, reason="vibes")


def test_material_requires_a_real_reason(kb):
    """The biconditional, forward direction: material must not carry 'none'."""
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="material", reason="none")


def test_immaterial_must_not_carry_a_real_reason(kb):
    """The biconditional, REVERSE direction. Without this test the CHECK could
    be written one-way and pass -- the same one-directional blindness
    analysis-stats-traps #3 records in a pre-registered gate."""
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="immaterial", reason="topical")


def test_a_valid_material_row_is_accepted(kb):
    """Presence sibling for the four rejection tests above: a CHECK that
    rejects everything would satisfy all of them."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="material", reason="tracked_entity")
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 1


def test_sampled_is_a_material_reason(kb):
    """The control arm of spec 5.3 must be storable, and it is material."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="material", reason="sampled")
    assert (
        kb.execute(
            "SELECT reason FROM item_triage WHERE item_id = %s", (item_id,)
        ).fetchone()[0]
        == "sampled"
    )


def test_material_must_not_carry_error(kb):
    """The forward direction's SECOND excluded reason. Every other forward test
    uses 'none', so without this one the CHECK could read `reason <> 'none'`
    and the whole suite would still pass -- the exclusion set has two members
    and only one was ever attempted."""
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="material", reason="error")


def test_an_immaterial_row_with_no_reason_is_accepted(kb):
    """Presence sibling on the NON-material side. Both other accept tests use
    verdict='material', so a constraint over-broad enough to forbid the ordinary
    immaterial+'none' row -- the majority row in production -- had nothing to
    catch it."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="immaterial", reason="none")
    assert kb.execute(
        "SELECT verdict, reason FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone() == ("immaterial", "none")


def test_a_failed_row_carries_error(kb):
    """`failed` is in the verdict enum and was inserted by nothing. It is how a
    triage call that raised is recorded, so if the enum member were dropped the
    error path would fail to write and no test would report it."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="failed", reason="error")
    assert kb.execute(
        "SELECT verdict, reason FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone() == ("failed", "error")


def test_one_row_per_item_per_triage_version(kb):
    item_id = _item(kb)
    _triage(kb, item_id, version=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with kb.transaction():
            _triage(kb, item_id, version=1)


def test_a_new_triage_version_is_a_new_row_not_an_overwrite(kb):
    """Presence sibling for the uniqueness test: the point of stamping a
    version is to COMPARE versions, which an overwrite destroys."""
    item_id = _item(kb)
    _triage(kb, item_id, version=1)
    _triage(kb, item_id, version=2)
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 2


def test_deleting_an_item_removes_its_triage(kb):
    item_id = _item(kb)
    _triage(kb, item_id)
    kb.execute("DELETE FROM items WHERE id = %s", (item_id,))
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0


def test_0009_rolls_back_and_reapplies(conn):
    """The step count is DERIVED (news-brief-5db). Adding migration 0010 must
    not require an edit here."""
    db.run_migrations(conn)
    conn.commit()
    stack = db.applied_versions(conn)

    reverted = db.run_migrations(
        conn, direction="down", steps=conftest.steps_back_through(conn, TARGET)
    )
    conn.commit()

    assert reverted[-1] == TARGET
    assert reverted[0] == stack[-1]
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert "item_triage" not in tables
    assert "items" in tables, "rolling back 0009 must not take 0006 with it"

    reapplied = db.run_migrations(conn)
    conn.commit()
    assert reapplied == reverted[::-1]
    assert db.applied_versions(conn) == stack

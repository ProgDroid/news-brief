"""Triage stage: the rules half, the model half, and the sampled control arm."""

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def test_a_disabled_run_writes_no_triage_rows(kb, monkeypatch):
    """Ships off. The disabled path must be a real no-op, not a path that
    happens to find nothing -- so this test seeds an item that WOULD be
    triaged if the flag were on."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", False)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0
    assert tally.items_seen == 0
    assert tally.enabled is False


def test_an_enabled_run_sees_the_item(kb, monkeypatch):
    """Presence sibling. Without it, the disabled assertion above is satisfied
    for free by a run() that does nothing under any setting."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.enabled is True
    assert tally.items_seen == 1

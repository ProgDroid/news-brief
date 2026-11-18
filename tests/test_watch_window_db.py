"""The watch job's 48 h window over captured items, against real Postgres
(news-brief-8lp). Skips without DATABASE_URL — and a skip is not a pass."""

from datetime import datetime, timedelta, timezone

import pytest

import db
from watch import Item, fetch_window_items

pytestmark = pytest.mark.skipif(
    not db.is_configured(), reason="No database is configured; export DATABASE_URL"
)

AS_OF = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)


@pytest.fixture()
def store():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


def _item(store, title, created_at, outlet="Wire A"):
    row = store.execute("SELECT id FROM outlets WHERE name = %s", (outlet,)).fetchone()
    oid = (
        row[0]
        if row
        else store.execute(
            "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id",
            (outlet,),
        ).fetchone()[0]
    )
    store.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (oid, f"https://example.com/{title}", title, f"h-{title}", created_at),
    )
    store.commit()


def test_window_is_exactly_the_48h_before_as_of(store):
    _item(store, "too-old", AS_OF - timedelta(hours=49))
    _item(store, "edge-out", AS_OF - timedelta(hours=48))
    _item(store, "inside", AS_OF - timedelta(hours=47))
    _item(store, "at-as-of", AS_OF)
    _item(store, "future", AS_OF + timedelta(hours=1))

    got = fetch_window_items(store, AS_OF)

    assert [i.title for i in got] == ["inside", "at-as-of"]
    assert got[0] == Item("inside", "https://example.com/inside", "Wire A")


def test_naive_as_of_is_treated_as_utc(store):
    _item(store, "inside", AS_OF - timedelta(hours=1))
    _item(store, "future", AS_OF + timedelta(hours=1))
    # A UTC session would read a naive parameter as UTC by itself, and this
    # test could not fail; five hours east makes a naive value land 5 h off.
    store.execute("SET TIME ZONE '+05'")

    got = fetch_window_items(store, AS_OF.replace(tzinfo=None))

    assert [i.title for i in got] == ["inside"]

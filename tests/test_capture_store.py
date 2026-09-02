"""DB-backed capture storage tests (news-brief-b42.1)."""

import pytest

import capture
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(), reason="No database is configured; export DATABASE_URL"
)


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


def _entry(url="https://example.com/a", guid=None, title="A headline"):
    return {
        "title": title,
        "url": url,
        "summary": "Body.",
        "published_raw": "",
        "published_at": None,
        "guid": guid,
    }


FEED = {"name": "Test Wire", "url": "u", "category": "macro", "kind": "wire"}


def test_a_guid_beats_the_url_for_identity():
    same_guid_different_url = [
        capture.content_hash(_entry(url="https://a/1", guid="g")),
        capture.content_hash(_entry(url="https://a/2", guid="g")),
    ]
    assert len(set(same_guid_different_url)) == 1


def test_without_a_guid_the_normalized_url_is_the_identity():
    plain = capture.content_hash(_entry(url="https://a/1"))
    tracked = capture.content_hash(_entry(url="https://a/1?utm_source=rss#frag"))
    assert plain == tracked


def test_two_different_items_hash_differently():
    assert capture.content_hash(_entry(url="https://a/1")) != capture.content_hash(
        _entry(url="https://a/2")
    )


def test_an_outlet_is_inserted_once_then_looked_up(store):
    first = capture.resolve_outlet(store, FEED)
    second = capture.resolve_outlet(store, FEED)
    assert first == second
    n = store.execute("SELECT count(*) FROM outlets").fetchone()[0]
    assert n == 1


def test_capture_never_overwrites_an_outlets_metadata(store):
    capture.resolve_outlet(store, {**FEED, "kind": "wire"})
    capture.resolve_outlet(store, {**FEED, "kind": "analyst"})
    kind = store.execute("SELECT kind FROM outlets").fetchone()[0]
    assert kind == "wire", "an existing outlet row is looked up, never rewritten"


def test_a_source_conflicting_with_an_existing_outlet_is_refused(store):
    capture.resolve_outlet(store, {**FEED, "kind": "wire"})
    got = capture.resolve_outlet(store, {**FEED, "kind": "analyst"}, strict=True)
    assert got is None, "a user source that disagrees is dropped, not merged silently"


def test_the_same_item_captured_twice_writes_one_row(store):
    outlet_id = capture.resolve_outlet(store, FEED)
    first = capture.store_items(store, outlet_id, [_entry()])
    second = capture.store_items(store, outlet_id, [_entry()])
    assert first == (1, 0)
    assert second == (0, 1)
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_the_same_hash_under_two_outlets_both_store(store):
    """The per-outlet key, positive control: dedup must NOT collapse syndicated
    wire copy across publishers, or any corroboration count caps at one."""
    a = capture.resolve_outlet(store, FEED)
    b = capture.resolve_outlet(store, {**FEED, "name": "Other Wire"})
    capture.store_items(store, a, [_entry()])
    capture.store_items(store, b, [_entry()])
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_an_entry_with_no_title_is_skipped_not_fatal(store):
    """An entry missing title or url is filtered by the Python guard before it
    ever reaches SQL: it is not counted as written, and it does not stop the
    entries around it from being captured."""
    outlet_id = capture.resolve_outlet(store, FEED)
    written, _ = capture.store_items(
        store, outlet_id, [_entry(title=""), _entry(url="https://example.com/ok")]
    )
    assert written == 1
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_a_db_error_on_one_entry_does_not_lose_its_neighbor(store):
    """A NUL byte passes the Python guard (title and url are both non-empty) but
    Postgres text fields reject it outright -- a real database-level failure the
    guard cannot see coming. One bad entry must not lose a good neighbor, and the
    connection must still be usable for the caller's next statement afterward."""
    outlet_id = capture.resolve_outlet(store, FEED)
    written, _ = capture.store_items(
        store,
        outlet_id,
        [
            _entry(title="bad\x00title", url="https://example.com/bad"),
            _entry(url="https://example.com/ok"),
        ],
    )
    assert written == 1
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert store.execute("SELECT 1").fetchone() == (1,)

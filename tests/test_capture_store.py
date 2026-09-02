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
    assert first == (1, 0, 0)
    assert second == (0, 1, 0)
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
    written, already, failed = capture.store_items(
        store, outlet_id, [_entry(title=""), _entry(url="https://example.com/ok")]
    )
    assert written == 1
    assert failed == 0, "a missing title/url is filtered, not a database failure"
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_a_db_error_on_one_entry_does_not_lose_its_neighbor(store):
    """A NUL byte passes the Python guard (title and url are both non-empty) but
    Postgres text fields reject it outright -- a real database-level failure the
    guard cannot see coming. One bad entry must not lose a good neighbor, and the
    connection must still be usable for the caller's next statement afterward."""
    outlet_id = capture.resolve_outlet(store, FEED)
    written, already, failed = capture.store_items(
        store,
        outlet_id,
        [
            _entry(title="bad\x00title", url="https://example.com/bad"),
            _entry(url="https://example.com/ok"),
        ],
    )
    assert written == 1
    assert failed == 1
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert store.execute("SELECT 1").fetchone() == (1,)


def test_a_sighting_is_created_then_advanced(store):
    entries = [_entry()]
    capture.record_sightings(store, "Test Wire", entries, {})
    first = store.execute(
        "SELECT first_seen_at, last_seen_at FROM feed_sightings"
    ).fetchone()
    store.execute(
        "UPDATE feed_sightings SET last_seen_at = last_seen_at - interval '1 hour'"
    )
    capture.record_sightings(store, "Test Wire", entries, {})
    second = store.execute(
        "SELECT first_seen_at, last_seen_at, position FROM feed_sightings"
    ).fetchone()
    assert store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0] == 1
    assert second[0] == first[0], "first_seen_at must never move"
    assert second[1] > first[1] - __import__("datetime").timedelta(hours=1)


def test_one_item_on_two_feeds_is_two_sightings(store):
    capture.record_sightings(store, "Reuters Markets", [_entry()], {})
    capture.record_sightings(store, "Reuters World", [_entry()], {})
    assert store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0] == 2


def test_roll_off_is_computed_only_against_successful_polls(store):
    """Three passes: the item is present, present, then absent from a SUCCESSFUL
    poll. Only then has it left the window."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    # Move pass 1 WHOLLY into the past -- the sighting AND the poll that
    # produced it. Backdating only the sighting leaves it behind its own poll,
    # which then satisfies the predicate by itself and makes this test pass
    # without pass 3 existing at all. The poll goes one second further back so
    # the "a poll strictly after the sighting" rule is tested, not equality.
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )

    run3 = capture.start_run(store, enabled=True)
    capture.record_poll(store, run3, "Test Wire", None, 0)

    assert capture.rolled_off(store, "Test Wire") == [capture.content_hash(_entry())]


def test_no_later_poll_means_nothing_has_rolled_off(store):
    """Guards the two roll-off tests against passing vacuously. With only the
    original poll on record, nothing has left the window yet -- so if this
    returns a hash, the predicate is being satisfied by the poll that recorded
    the sighting rather than by a later one."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )
    assert capture.rolled_off(store, "Test Wire") == []


def test_a_failed_poll_produces_no_roll_off_signal(store):
    """The negative control for the whole measurement. A feed 403ing for a day
    freezes every last_seen_at, which is byte-identical to its entire window
    rolling over at once. Without feed_polls this test cannot be written."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )

    run3 = capture.start_run(store, enabled=True)
    capture.record_poll(store, run3, "Test Wire", "http_403", 0)

    assert capture.rolled_off(store, "Test Wire") == []


def test_a_disabled_run_is_distinguishable_from_one_that_never_fired(store):
    run = capture.start_run(store, enabled=False)
    capture.finish_run(store, run, capture.Tally())
    row = store.execute(
        "SELECT enabled, finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()
    assert row[0] is False
    assert row[1] is not None


def test_an_unfinished_run_keeps_a_null_finished_at(store):
    run = capture.start_run(store, enabled=True)
    finished = store.execute(
        "SELECT finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()[0]
    assert finished is None, "a crashed pass must be tellable from a completed one"


def test_the_run_row_survives_a_crash_mid_pass(store):
    """db.connect() is autocommit=False, so without an explicit commit the
    capture_runs row rolls back with everything else and a crashed pass becomes
    indistinguishable from one that never fired -- the exact ambiguity the row
    exists to remove. This test fails if the commit after start_run is dropped.
    """
    run = capture.start_run(store, enabled=True)
    store.commit()
    try:
        capture.record_sightings(store, "Test Wire", [_entry()], {})
        raise RuntimeError("simulated pass crash")
    except RuntimeError:
        store.rollback()
    row = store.execute(
        "SELECT enabled, finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()
    assert row is not None, "the run row was rolled back with the crash"
    assert row[1] is None, "an unfinished run must keep a NULL finished_at"

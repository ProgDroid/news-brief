"""DB-backed capture storage tests (news-brief-b42.1)."""

from datetime import datetime, timedelta, timezone

import pytest

import brief
import capture
import common
import db
import scheduler

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


def test_two_differently_named_feeds_sharing_an_outlet_key_collapse(store):
    """The Reuters case: Reuters Markets and Reuters World are two feeds but one
    outlet. Passing the SAME feed dict twice (as the insert-once test does)
    only proves idempotence, not collapse -- this proves two different feed
    names sharing an `outlet` key resolve to one row."""
    a = capture.resolve_outlet(
        store, {**FEED, "name": "Reuters Markets", "outlet": "Reuters"}
    )
    b = capture.resolve_outlet(
        store, {**FEED, "name": "Reuters World", "outlet": "Reuters"}
    )
    assert a == b
    n = store.execute("SELECT count(*) FROM outlets").fetchone()[0]
    assert n == 1


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


def test_feed_sightings_item_id_is_populated_after_a_store_pass(store):
    """FIX 1 regression guard: `record_sightings(conn, name, entries, {})` with
    an always-empty id map made item_id NULL by construction on every row --
    `store_items`'s `ON CONFLICT DO NOTHING RETURNING id` returns nothing for
    the already-present case, which is the majority. item_id is what
    disambiguates a shared content_hash across outlets; a NULL here is
    unrecoverable since source_name carries no FK back to `items`."""
    outlet_id = capture.resolve_outlet(store, FEED)
    entries = [_entry()]
    capture.store_items(store, outlet_id, entries)
    item_ids = capture._lookup_item_ids(store, outlet_id, entries)
    capture.record_sightings(store, "Test Wire", entries, item_ids)
    row = store.execute(
        "SELECT s.item_id, i.id FROM feed_sightings s "
        "JOIN items i ON i.content_hash = s.content_hash AND i.outlet_id = %s",
        (outlet_id,),
    ).fetchone()
    assert row[0] is not None
    assert row[0] == row[1]


def test_a_sighting_is_created_then_advanced(store):
    entries = [_entry()]
    capture.record_sightings(store, "Test Wire", entries, {})
    first = store.execute(
        "SELECT first_seen_at, last_seen_at FROM feed_sightings"
    ).fetchone()
    store.execute(
        "UPDATE feed_sightings SET last_seen_at = last_seen_at - interval '1 hour'"
    )
    backdated = store.execute("SELECT last_seen_at FROM feed_sightings").fetchone()[0]
    capture.record_sightings(store, "Test Wire", entries, {})
    second = store.execute(
        "SELECT first_seen_at, last_seen_at, position FROM feed_sightings"
    ).fetchone()
    assert store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0] == 1
    assert second[0] == first[0], "first_seen_at must never move"
    # Postgres's now() is transaction_timestamp(): frozen for the whole
    # transaction, so the two record_sightings calls above -- both inside
    # this test's one transaction -- write the SAME last_seen_at as each
    # other. second[1] can therefore never exceed first[1] here; the only
    # thing the upsert actually guarantees is an advance from the backdated
    # value. This same freeze is what makes rolled_off's same-pass exclusion
    # work: a sighting and the poll that produced it share a transaction
    # timestamp, so `polled_at > last_seen_at` is false for that pass.
    assert second[1] > backdated
    assert second[2] == 1, "position = EXCLUDED.position is part of the upsert"


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


def test_a_disabled_pass_polls_nothing_but_still_finishes(store, monkeypatch):
    """capture.run's early-return path when CAPTURE_ENABLED is false, exercised
    end to end rather than just at start_run/finish_run: a disabled pass must
    still leave a finished capture_runs row (distinguishable from a crash), but
    write zero feed_polls rows and report feeds_total == 0 -- the only thing
    that tells "disabled, so polled nothing" apart from "enabled but every feed
    failed"."""
    monkeypatch.setattr(common, "CAPTURE_ENABLED", False)
    tally = capture.run(store)
    row = store.execute(
        "SELECT enabled, finished_at FROM capture_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is False
    assert row[1] is not None
    polls = store.execute("SELECT count(*) FROM feed_polls").fetchone()[0]
    assert polls == 0
    assert tally.feeds_total == 0


def test_run_end_to_end_succeeds_one_feed_and_fails_the_other(store, monkeypatch):
    """FIX 3: the only test that exercises capture.run's SUCCESS path end to
    end -- outlet resolution, item storage, and sighting/poll recording,
    against real Postgres rather than a fake conn. Before this, feeds_ok,
    items_new, items_seen and tally.failures were asserted nowhere, and the
    outlet_conflict failure-count fix shipped with no test at all.

    Also the FIX 4 regression guard: rolled_off must read [] for a feed just
    polled successfully in this same pass, which only holds if record_sightings
    and record_poll commit in the same transaction (see the comment at the
    per-feed commit in capture.run)."""
    ok_feed = {
        "name": "OK Wire",
        "url": "https://ok.example/feed",
        "category": "macro",
        "kind": "wire",
    }
    bad_feed = {
        "name": "Bad Wire",
        "url": "https://bad.example/feed",
        "category": "macro",
        "kind": "wire",
    }
    entries = [
        _entry(url="https://ok.example/a", guid="g1", title="A"),
        _entry(url="https://ok.example/b", guid="g2", title="B"),
    ]

    def fake_fetch(feed):
        if feed["name"] == "OK Wire":
            return brief.FeedFetch(entries=list(entries), failure=None)
        return brief.FeedFetch(entries=[], failure="http_403")

    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "capture_sources", lambda: [ok_feed, bad_feed])
    monkeypatch.setattr(capture, "HOST_GAP_SECONDS", 0)
    monkeypatch.setattr(brief, "fetch_feed_entries", fake_fetch)

    tally = capture.run(store)

    assert tally.feeds_ok == 1
    assert tally.feeds_failed == 1
    assert tally.failures == {"http_403": 1}
    assert tally.items_new == len(entries)
    assert tally.items_seen == len(entries)

    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == len(entries)
    assert store.execute(
        "SELECT count(*) FROM feed_sightings WHERE source_name = %s", ("OK Wire",)
    ).fetchone()[0] == len(entries)
    ok_failure = store.execute(
        "SELECT failure FROM feed_polls WHERE source_name = %s", ("OK Wire",)
    ).fetchone()[0]
    bad_failure = store.execute(
        "SELECT failure FROM feed_polls WHERE source_name = %s", ("Bad Wire",)
    ).fetchone()[0]
    assert ok_failure is None
    assert bad_failure == "http_403"

    assert capture.rolled_off(store, "OK Wire") == []


# ── Liveness (news-brief-a9q) ─────────────────────────────────────────────────
# The half of the capture health question that needs no measured threshold.
# "Too many feeds failed" is a rate, and inventing one puts a fabricated number
# on the critical path (news-brief-w3q, deferred to b42.2). "It has stopped
# firing" and "a pass died" are not rates: they follow from the schedule and
# from finished_at, both of which are already facts.

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
INTERVAL = next(s.every_minutes for s in scheduler.SCHEDULES if s.job == "capture")


def _run(store, *, minutes_ago, finished=True, enabled=True):
    started = NOW - timedelta(minutes=minutes_ago)
    return store.execute(
        "INSERT INTO capture_runs (started_at, finished_at, enabled) "
        "VALUES (%s, %s, %s) RETURNING id",
        (started, started + timedelta(minutes=2) if finished else None, enabled),
    ).fetchone()[0]


def test_a_capture_that_fired_this_interval_is_not_flagged(store):
    _run(store, minutes_ago=5)
    assert capture.liveness(store, NOW) is None


def test_a_capture_that_has_stopped_firing_is_flagged(store):
    _run(store, minutes_ago=INTERVAL * capture.STALE_AFTER_INTERVALS + 1)
    verdict = capture.liveness(store, NOW)
    assert verdict is not None
    assert verdict[0].startswith("stale:")


def test_the_tolerance_follows_the_schedule_rather_than_a_copy_of_it(store):
    """One interval inside the window is silence; one minute past it speaks. The
    bound is read from scheduler.SCHEDULES, so retiming capture retimes the
    alert -- a hardcoded 90 here would keep passing while meaning the wrong
    thing."""
    inside = _run(store, minutes_ago=INTERVAL * capture.STALE_AFTER_INTERVALS - 1)
    assert capture.liveness(store, NOW) is None
    store.execute("DELETE FROM capture_runs WHERE id = %s", (inside,))
    _run(store, minutes_ago=INTERVAL * capture.STALE_AFTER_INTERVALS + 1)
    assert capture.liveness(store, NOW) is not None


def test_the_stale_episode_is_named_by_the_run_it_stopped_after(store):
    """The episode key is what makes one alert per outage rather than one per
    hour, so two different outages must not share a key."""
    first = _run(store, minutes_ago=INTERVAL * 10)
    assert capture.liveness(store, NOW)[0] == f"stale:{first}"
    second = _run(store, minutes_ago=INTERVAL * 5)
    assert capture.liveness(store, NOW)[0] == f"stale:{second}"


def test_a_disabled_capture_is_quiet_rather_than_stale(store):
    """A disabled pass still writes its row every fire, so switched-off never
    reads as dead. This is why capture_runs.enabled exists at all."""
    _run(store, minutes_ago=5, enabled=False)
    assert capture.liveness(store, NOW) is None


def test_a_pass_that_died_mid_flight_is_flagged(store):
    """finished_at NULL on a run a later run has overtaken can never be filled
    in: that pass is gone, and nothing else reports it."""
    died = _run(store, minutes_ago=INTERVAL * 2, finished=False)
    _run(store, minutes_ago=5)
    verdict = capture.liveness(store, NOW)
    assert verdict is not None
    assert verdict[0] == f"crashed:{died}"


def test_the_newest_run_being_unfinished_is_not_yet_a_crash(store):
    """The presence control for the test above: a pass in flight has a NULL
    finished_at too, and calling that a crash would alert on every healthy
    capture. Only a successor proves it can never finish."""
    _run(store, minutes_ago=1, finished=False)
    assert capture.liveness(store, NOW) is None


def test_a_capture_that_has_never_run_is_unknown_not_dead(store):
    """No rows cannot distinguish 'never deployed' from 'broken', and the two
    want opposite responses. Absence is reported by /capture, not alerted on."""
    assert capture.liveness(store, NOW) is None


# ── One alert per outage (news-brief-a9q) ────────────────────────────────────
# The dedupe state lives in runtime_state and NOT in a set on the object, the
# way the supervisor's spawn_alerted does. The supervisor is a resident and can
# remember; mode_monitor is a fresh job child every hour, so an in-process set
# forgets between checks and turns one outage into 24 messages a day -- the
# failure w3q's acceptance criterion names.


def _alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_alert", sent.append)
    return sent


def test_a_healthy_capture_sends_nothing(store, monkeypatch):
    sent = _alerts(monkeypatch)
    _run(store, minutes_ago=5)
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert sent == []


def test_a_stopped_capture_alerts_once_not_once_per_check(store, monkeypatch):
    sent = _alerts(monkeypatch)
    _run(store, minutes_ago=INTERVAL * 10)
    store.commit()
    for _ in range(4):
        brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 1
    assert "has not run" in sent[0]


def test_a_second_outage_after_a_recovery_alerts_again(store, monkeypatch):
    """The positive control for the test above. Alerting once is trivially
    satisfied by never alerting a second time for any reason, which would make
    the first outage the only one this ever reports."""
    sent = _alerts(monkeypatch)
    _run(store, minutes_ago=INTERVAL * 10)
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 1

    recovered = _run(store, minutes_ago=5)
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 1, "a recovery must not itself be an alert"

    store.execute(
        "UPDATE capture_runs SET started_at = %s WHERE id = %s",
        (NOW - timedelta(minutes=INTERVAL * 10), recovered),
    )
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 2


def test_a_crash_and_a_later_stall_are_different_episodes(store, monkeypatch):
    """Two outages of DIFFERENT kinds must not silence each other: the stored
    key is the episode, not the fact that something was once wrong."""
    sent = _alerts(monkeypatch)
    _run(store, minutes_ago=INTERVAL * 2, finished=False)
    _run(store, minutes_ago=5)
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 1
    assert "died mid-flight" in sent[0]

    store.execute(
        "UPDATE capture_runs SET started_at = started_at - %s",
        (timedelta(minutes=INTERVAL * 10),),
    )
    store.commit()
    brief.capture_liveness_alert(store, NOW)
    assert len(sent) == 2
    assert "has not run" in sent[1]


def test_an_unreadable_capture_table_does_not_break_the_monitor(store, monkeypatch):
    """The monitor's other work -- volume alerts, the live exit sweep -- must
    survive a capture check that cannot read. Fail-safe, like every other block
    in mode_monitor."""
    sent = _alerts(monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(capture, "liveness", explode)
    brief.capture_liveness_alert(store, NOW)
    assert sent == []


# ── What /capture reads (news-brief-a9q) ─────────────────────────────────────
# The pull half. These queries carry no thresholds at all -- they report, and
# the operator judges. That is deliberate: the numbers they surface are exactly
# what b42.2 needs in order to set a threshold that is measured rather than
# guessed, and a surface that pre-judged them would hide its own evidence.


def _poll(store, run_id, source, failure=None, entries=0, minutes_ago=0):
    store.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, polled_at, failure, "
        "entries_seen) VALUES (%s, %s, %s, %s, %s)",
        (run_id, source, NOW - timedelta(minutes=minutes_ago), failure, entries),
    )


def test_last_run_is_the_newest_pass(store):
    _run(store, minutes_ago=INTERVAL * 3)
    newest = _run(store, minutes_ago=2)
    assert capture.last_run(store)["id"] == newest


def test_last_run_is_none_when_capture_has_never_run(store):
    assert capture.last_run(store) is None


def test_last_run_carries_the_tallies_the_operator_reads(store):
    run_id = _run(store, minutes_ago=2)
    store.execute(
        "UPDATE capture_runs SET feeds_total = 26, feeds_ok = 24, feeds_failed = 2, "
        "items_seen = 900, items_new = 40 WHERE id = %s",
        (run_id,),
    )
    row = capture.last_run(store)
    assert (row["feeds_total"], row["feeds_ok"], row["feeds_failed"]) == (26, 24, 2)
    assert (row["items_seen"], row["items_new"]) == (900, 40)
    assert row["enabled"] is True


def test_the_failure_breakdown_counts_each_kind(store):
    run_id = _run(store, minutes_ago=2)
    _poll(store, run_id, "A", failure="http_403")
    _poll(store, run_id, "B", failure="http_403")
    _poll(store, run_id, "C", failure="deadline")
    _poll(store, run_id, "D")
    assert dict(capture.failure_breakdown(store, runs=1)) == {
        "http_403": 2,
        "deadline": 1,
    }


def test_the_failure_breakdown_looks_only_at_recent_passes(store):
    """Its window is a count of passes, so a feed that broke a week ago and was
    fixed does not go on being reported as broken. The recent kind must still
    appear -- an empty result would satisfy 'the old one is gone' for free."""
    old = _run(store, minutes_ago=INTERVAL * 20)
    _poll(store, old, "A", failure="ancient_history", minutes_ago=INTERVAL * 20)
    recent = _run(store, minutes_ago=2)
    _poll(store, recent, "A", failure="http_403", minutes_ago=2)
    kinds = dict(capture.failure_breakdown(store, runs=1))
    assert "ancient_history" not in kinds
    assert kinds == {"http_403": 1}


def test_feed_health_reports_when_each_feed_last_worked(store):
    run_id = _run(store, minutes_ago=2)
    _poll(store, run_id, "Reuters", minutes_ago=90)
    _poll(store, run_id, "Reuters", minutes_ago=30)
    _poll(store, run_id, "Al Jazeera", minutes_ago=10)
    health = {row["source_name"]: row for row in capture.feed_health(store)}
    assert health["Reuters"]["last_ok_at"] == NOW - timedelta(minutes=30)
    assert health["Al Jazeera"]["last_ok_at"] == NOW - timedelta(minutes=10)


def test_a_feed_failing_since_its_last_success_counts_those_failures(store):
    """The number that makes 'half the feeds are 403ing' visible. It counts only
    failures AFTER the last success, so a feed that broke once in March and has
    worked ever since reads as healthy."""
    run_id = _run(store, minutes_ago=2)
    _poll(store, run_id, "Reuters", failure="http_403", minutes_ago=200)
    _poll(store, run_id, "Reuters", minutes_ago=100)
    _poll(store, run_id, "Reuters", failure="http_403", minutes_ago=60)
    _poll(store, run_id, "Reuters", failure="http_403", minutes_ago=30)
    row = next(r for r in capture.feed_health(store) if r["source_name"] == "Reuters")
    assert row["failures_since"] == 2
    assert row["last_failure"] == "http_403"


def test_a_feed_that_has_never_succeeded_says_so(store):
    """A NULL last success is not the same as a recent one, and a render that
    formatted it as an age would print a confident wrong number."""
    run_id = _run(store, minutes_ago=2)
    _poll(store, run_id, "Broken Wire", failure="ssl_error", minutes_ago=40)
    _poll(store, run_id, "Broken Wire", failure="ssl_error", minutes_ago=10)
    row = next(
        r for r in capture.feed_health(store) if r["source_name"] == "Broken Wire"
    )
    assert row["last_ok_at"] is None
    assert row["failures_since"] == 2


def test_feed_health_puts_the_worst_feeds_first(store):
    """The operator reads the top of a Telegram message, so ordering is part of
    the report rather than a detail: never-succeeded, then longest-since."""
    run_id = _run(store, minutes_ago=2)
    _poll(store, run_id, "Healthy", minutes_ago=5)
    _poll(store, run_id, "Stale", minutes_ago=600)
    _poll(store, run_id, "Never", failure="http_403", minutes_ago=5)
    assert [r["source_name"] for r in capture.feed_health(store)] == [
        "Never",
        "Stale",
        "Healthy",
    ]

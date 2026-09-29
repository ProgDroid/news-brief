"""`census` session state: sequencing, autosave, Finish, Abandon, the
precision sample and the go/no-go gate, against a real database.

Plan Task 4 (docs/superpowers/plans/2026-09-28-event-census.md). Uses the
same fresh-schema fixture shape as tests/test_census_prepare.py. Every test
pins time to `cf.NOW` and offsets from it (F13).
"""

import uuid
from datetime import timedelta

import psycopg
import pytest

import census
import census_fixtures as cf
import common
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)

NOW = cf.NOW
IDLE = psycopg.pq.TransactionStatus.IDLE


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


@pytest.fixture()
def ready(kb):
    """A prepared census, nothing labelled yet."""
    cf.prepared(kb)
    return kb


def _status(conn, window_id: int) -> tuple:
    row = conn.execute(
        "SELECT status, abandon_reason, blind_done_at, completed_at "
        "FROM census_windows WHERE id = %s",
        (window_id,),
    ).fetchone()
    conn.commit()
    return row


def _log_rows(conn, window_id: int, item_id: int) -> int:
    n = conn.execute(
        "SELECT count(*) FROM census_assignments WHERE window_id = %s AND item_id = %s",
        (window_id, item_id),
    ).fetchone()[0]
    conn.commit()
    return n


def _two_tab_fixture(conn):
    """Tab `p` writes X->A with seq 25, then tab `l` writes X->B with seq 11:
    the later ARRIVAL carries the lower sequence number (B5)."""
    w1 = cf.window_id(conn, 1)
    x = census.window_items(conn, w1)[0]["id"]
    group_a = census.create_group(conn, w1, NOW)
    group_b = census.create_group(conn, w1, NOW)
    census.save_assignments(conn, w1, "p", 25, [(x, group_a, False)], NOW)
    census.save_assignments(
        conn, w1, "l", 11, [(x, group_b, False)], NOW + timedelta(seconds=5)
    )
    return w1, x, group_a, group_b


# ── Sequencing ──────────────────────────────────────────────────────────────


def test_no_block_is_not_prepared(kb):
    task = census.current_task(kb, NOW)
    assert task.kind == "not_prepared"
    assert task.window_id is None
    assert kb.info.transaction_status == IDLE


def test_first_task_is_window_one(ready):
    task = census.current_task(ready, NOW)
    assert task.kind == "blind"
    assert task.window_id == cf.window_id(ready, 1)
    assert task.session_no == 1


# ── Autosave: the log and its one reader (B5) ───────────────────────────────


def test_later_arrival_wins_across_tabs(ready):
    w1, x, _group_a, group_b = _two_tab_fixture(ready)
    assert census.latest_assignments(ready, w1)[x] == (group_b, False)


def test_retried_write_is_ignored(ready):
    w1 = cf.window_id(ready, 1)
    x = census.window_items(ready, w1)[0]["id"]
    group_a = census.create_group(ready, w1, NOW)
    group_b = census.create_group(ready, w1, NOW)
    census.save_assignments(ready, w1, "p", 1, [(x, group_a, False)], NOW)
    census.save_assignments(
        ready, w1, "l", 1, [(x, group_b, False)], NOW + timedelta(seconds=5)
    )
    # Tab `p` never saw its first response and resends the same action.
    census.save_assignments(
        ready, w1, "p", 1, [(x, group_a, False)], NOW + timedelta(seconds=9)
    )

    assert census.latest_assignments(ready, w1)[x] == (group_b, False)
    assert _log_rows(ready, w1, x) == 2


def test_window_assignments_uses_the_same_order(ready):
    w1, x, _group_a, group_b = _two_tab_fixture(ready)
    by_item = {a.item_id: a for a in census.window_assignments(ready, w1)}
    assert by_item[x].group_id == group_b
    assert by_item[x].unsure is False


def test_window_assignments_counts_untouched_items_as_singletons(ready):
    w1 = cf.window_id(ready, 1)
    items = census.window_items(ready, w1)
    x, y = items[0]["id"], items[1]["id"]
    group = census.create_group(ready, w1, NOW)
    census.save_assignments(ready, w1, "p", 1, [(x, group, False)], NOW)
    census.save_assignments(ready, w1, "p", 2, [(y, None, True)], NOW)

    assignments = census.window_assignments(ready, w1)
    assert [a.item_id for a in assignments] == [i["id"] for i in items]
    by_item = {a.item_id: a for a in assignments}
    assert (by_item[x].group_id, by_item[x].unsure) == (group, False)
    assert (by_item[y].group_id, by_item[y].unsure) == (None, True)
    untouched = [a for a in assignments if a.item_id not in (x, y)]
    assert untouched and all(
        a.group_id is None and a.unsure is False for a in untouched
    )


def test_assignments_after_finish_are_rejected(ready):
    w1 = cf.window_id(ready, 1)
    groups = cf.cross_outlet_groups(ready, w1, 3)
    cf.label(ready, w1, groups, NOW)
    before = census.latest_assignments(ready, w1)
    x = groups[0][0]

    with pytest.raises(census.WindowClosed):
        census.save_assignments(
            ready, w1, "late", 99, [(x, None, False)], NOW + timedelta(minutes=1)
        )

    assert ready.info.transaction_status == IDLE
    assert census.latest_assignments(ready, w1) == before
    assert _log_rows(ready, w1, x) == 1

    # An abandoned window is closed too.
    w2 = cf.window_id(ready, 2)
    census.abandon(ready, w2, "ran out of time", NOW)
    y = census.window_items(ready, w2)[0]["id"]
    with pytest.raises(census.WindowClosed):
        census.save_assignments(ready, w2, "late", 1, [(y, None, False)], NOW)
    assert _log_rows(ready, w2, y) == 0


def test_foreign_item_or_group_is_rejected(ready):
    w1 = cf.window_id(ready, 1)
    w2 = cf.window_id(ready, 2)
    own = census.window_items(ready, w1)[0]["id"]
    foreign_item = census.window_items(ready, w2)[0]["id"]
    own_group = census.create_group(ready, w1, NOW)
    foreign_group = census.create_group(ready, w2, NOW)

    with pytest.raises(census.BadWrite):
        census.save_assignments(
            ready, w1, "p", 1, [(foreign_item, own_group, False)], NOW
        )
    assert ready.info.transaction_status == IDLE
    with pytest.raises(census.BadWrite):
        census.save_assignments(ready, w1, "p", 2, [(own, foreign_group, False)], NOW)
    assert ready.info.transaction_status == IDLE

    # Neither write left a row, and a refused write does not open the window.
    assert census.latest_assignments(ready, w1) == {}
    assert _status(ready, w1)[0] == "prepared"


def test_save_assignments_opens_the_window(ready):
    w1 = cf.window_id(ready, 1)
    x = census.window_items(ready, w1)[0]["id"]
    assert _status(ready, w1)[0] == "prepared"
    census.save_assignments(ready, w1, "p", 1, [(x, None, True)], NOW)
    assert _status(ready, w1)[0] == "open"


# ── Membership is frozen at prepare time (F6, B3) ───────────────────────────


def test_membership_ignores_later_items_and_predicate_changes(ready, monkeypatch):
    w1 = cf.window_id(ready, 1)
    before = census.window_items(ready, w1)
    window_start = ready.execute(
        "SELECT window_start FROM census_windows WHERE id = %s", (w1,)
    ).fetchone()[0]
    outlet = ready.execute(
        "INSERT INTO outlets (name) VALUES (%s) RETURNING id",
        (f"Late {uuid.uuid4().hex[:8]}",),
    ).fetchone()[0]
    for i in range(500):
        created_at = window_start + timedelta(seconds=30 * i)
        ready.execute(
            "INSERT INTO items "
            "(outlet_id, url, title, published_at, content_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                outlet,
                f"https://example.test/late/{i}",
                f"Late item {i}",
                created_at,
                f"late-{i}-{uuid.uuid4().hex[:8]}",
                created_at,
            ),
        )
    ready.commit()
    monkeypatch.setattr(common, "is_quote_page", lambda title: True)

    after = census.window_items(ready, w1)
    assert len(before) >= census.MIN_WINDOW_ITEMS
    assert after == before
    assert set(before[0]) == {"id", "title", "url", "outlet", "created_at"}


def test_repeat_serves_window_two_items(ready):
    w2 = cf.window_id(ready, 2)
    pass2 = cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)
    items = census.window_items(ready, w2)
    assert items
    assert census.window_items(ready, pass2) == items


# ── The precision sample (sec 6.4) ──────────────────────────────────────────


def test_precision_follows_finish_and_completes_the_window(ready):
    w1 = cf.window_id(ready, 1)
    groups = cf.cross_outlet_groups(ready, w1, 12)
    outlet_of = {i["id"]: i["outlet"] for i in census.window_items(ready, w1)}
    group_of = {item: n for n, members in enumerate(groups) for item in members}

    # Nothing is offered before Finish.
    for n, members in enumerate(groups, start=1):
        gid = census.create_group(ready, w1, NOW)
        census.save_assignments(
            ready, w1, "t", n, [(i, gid, False) for i in members], NOW
        )
    assert census.precision_pairs(ready, w1) == []

    finish_at = NOW + timedelta(minutes=30)
    census.finish_blind(ready, w1, finish_at)
    assert _status(ready, w1)[:3] == ("blind_done", None, finish_at)

    task = census.current_task(ready, finish_at)
    assert (task.kind, task.window_id) == ("precision", w1)

    pairs = census.precision_pairs(ready, w1)
    assert len(pairs) == census.PRECISION_PAIRS
    assert len({group_of[a] for a, _b in pairs}) == len(pairs)
    for a, b in pairs:
        assert a < b
        assert group_of[a] == group_of[b]
        assert outlet_of[a] != outlet_of[b]

    for a, b in pairs[:-1]:
        census.save_precision(ready, w1, a, b, "same", finish_at)
    assert census.precision_pairs(ready, w1) == pairs[-1:]
    assert _status(ready, w1)[0] == "blind_done"
    assert census.current_task(ready, finish_at).kind == "precision"

    done_at = finish_at + timedelta(minutes=5)
    a, b = pairs[-1]
    census.save_precision(ready, w1, a, b, "different", done_at)
    assert _status(ready, w1)[0] == "complete"
    assert _status(ready, w1)[3] == done_at

    task = census.current_task(ready, done_at)
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 2))
    assert task.session_no == 2


def test_window_without_multi_outlet_groups_completes_at_finish(ready):
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, [], NOW)
    assert _status(ready, w1)[0] == "complete"
    assert _status(ready, w1)[3] == NOW
    assert census.precision_pairs(ready, w1) == []


def test_precision_rejects_a_pair_not_offered(ready):
    w1 = cf.window_id(ready, 1)
    groups = cf.cross_outlet_groups(ready, w1, 12)
    cf.label(ready, w1, groups, NOW)
    offered = census.precision_pairs(ready, w1)
    sampled = {a for pair in offered for a in pair}
    unsampled = [g for g in groups if not (set(g) & sampled)]
    assert len(unsampled) == 2

    def adjudications() -> int:
        n = ready.execute("SELECT count(*) FROM census_adjudications").fetchone()[0]
        ready.commit()
        return n

    # A real cross-outlet group, but not one the sample drew.
    with pytest.raises(census.BadWrite):
        census.save_precision(ready, w1, *sorted(unsampled[0]), "same", NOW)
    assert ready.info.transaction_status == IDLE
    # Two items from different groups.
    with pytest.raises(census.BadWrite):
        census.save_precision(ready, w1, groups[0][0], groups[1][0], "same", NOW)
    # An offered pair with a decision outside same/different.
    with pytest.raises(census.BadWrite):
        census.save_precision(ready, w1, *offered[0], "maybe", NOW)
    assert adjudications() == 0

    # An offered pair given in reverse order is normalised to a < b.
    a, b = offered[0]
    census.save_precision(ready, w1, b, a, "same", NOW)
    assert adjudications() == 1
    assert (a, b) not in census.precision_pairs(ready, w1)
    # ...and answering it again is no longer an offered pair.
    with pytest.raises(census.BadWrite):
        census.save_precision(ready, w1, a, b, "same", NOW)
    assert adjudications() == 1


def test_window_two_precision_waits_for_the_repeat_then_is_served(ready):
    cf.pass_gate(ready, NOW)
    w2 = cf.window_id(ready, 2)
    pass2 = cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)

    # Withheld after order_no 2's Finish: the next window is served instead.
    assert _status(ready, w2)[0] == "blind_done"
    assert census.precision_pairs(ready, w2) == []
    task = census.current_task(ready, NOW)
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 3))

    cf.finish_empty(ready, range(3, 9), NOW)
    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS)
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("blind", pass2)
    assert census.precision_pairs(ready, w2) == []

    cf.label(ready, pass2, cf.cross_outlet_groups(ready, pass2, 8), repeat_at)
    assert _status(ready, pass2)[0] == "blind_done"
    # Pass 2 measures consistency only: it has no precision sample of its own.
    assert census.precision_pairs(ready, pass2) == []

    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("precision", w2)
    assert len(census.precision_pairs(ready, w2)) == 8

    cf.answer_precision(ready, w2, repeat_at)
    assert _status(ready, w2)[0] == "complete"
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 9))


# ── The go/no-go gate (sec 4.5) ─────────────────────────────────────────────


def test_go_status_is_none_until_both_gate_windows_are_done(ready):
    assert census.go_status(ready) is None
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 8), NOW)
    assert census.go_status(ready) is None


def test_gate_blocks_after_two_thin_windows(ready):
    cf.pass_gate(ready, NOW, groups=3)

    status = census.go_status(ready)
    assert status is not None and status.ok is False
    assert status.values == (3, 3)

    task = census.current_task(ready, NOW)
    assert task.kind == "gate_failed"
    assert task.window_id is None
    assert "mean" in task.detail
    assert task.detail == status.reason


def test_gate_passes_with_eight_groups_each(ready):
    cf.pass_gate(ready, NOW)
    status = census.go_status(ready)
    assert status is not None and status.ok is True
    assert status.values == (8, 8)
    task = census.current_task(ready, NOW)
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 3))
    assert task.session_no == 3


def test_gate_override_unblocks(ready):
    cf.pass_gate(ready, NOW, groups=3)
    assert census.current_task(ready, NOW).kind == "gate_failed"

    ready.execute(
        "UPDATE census_block SET go_override_reason = %s",
        ("operator ruling 2026-10-01: continue",),
    )
    ready.commit()

    task = census.current_task(ready, NOW)
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 3))


def test_abandoned_gate_window_blocks(ready):
    w1 = cf.window_id(ready, 1)
    w2 = cf.window_id(ready, 2)
    census.abandon(ready, w1, "could not finish", NOW)
    assert _status(ready, w1)[:2] == ("abandoned", "could not finish")

    # Window 2 not done yet: the gate cannot be evaluated, so it is not failed.
    assert census.go_status(ready) is None
    task = census.current_task(ready, NOW)
    assert (task.kind, task.window_id) == ("blind", w2)

    cf.label(ready, w2, cf.cross_outlet_groups(ready, w2, 8), NOW)
    status = census.go_status(ready)
    assert status.ok is False
    assert status.reason == "window 1 abandoned; the gate cannot be evaluated"

    task = census.current_task(ready, NOW)
    assert task.kind == "gate_failed"
    assert "abandoned" in task.detail


# ── The repeat (sec 4.6, B4, F15, F16) ──────────────────────────────────────


def test_abandoning_window_two_voids_the_repeat(ready):
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 8), NOW)
    cf.answer_precision(ready, w1, NOW)
    census.abandon(ready, cf.window_id(ready, 2), "interrupted", NOW)

    pass2 = cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)
    assert _status(ready, pass2)[:2] == (
        "abandoned",
        "repeat void: window 2 abandoned",
    )

    # An abandoned gate window fails the gate; the operator overrides.
    assert census.current_task(ready, NOW).kind == "gate_failed"
    ready.execute("UPDATE census_block SET go_override_reason = 'ruling'")
    ready.commit()

    cf.finish_empty(ready, range(3, 17), NOW)
    task = census.current_task(ready, NOW)
    assert task.kind == "complete"
    assert task.window_id is None


def test_abandoned_window_eight_still_releases_the_repeat(ready):
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 8), NOW)
    census.abandon(ready, cf.window_id(ready, 8), "bad window", NOW)

    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS)
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )


def test_abandoned_repeat_releases_window_two_precision(ready):
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 9), NOW)
    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS)
    pass2 = cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)
    assert census.current_task(ready, repeat_at).window_id == pass2

    census.abandon(ready, pass2, "no time for the repeat", repeat_at)

    w2 = cf.window_id(ready, 2)
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("precision", w2)
    assert len(census.precision_pairs(ready, w2)) == 8


def test_open_window_is_finished_before_the_repeat(ready):
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 11), NOW)
    w11 = cf.window_id(ready, 11)
    x = census.window_items(ready, w11)[0]["id"]
    census.save_assignments(
        ready, w11, "p", 1, [(x, None, False)], NOW + timedelta(days=1)
    )
    assert _status(ready, w11)[0] == "open"

    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS + 1)
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("blind", w11)

    census.finish_blind(ready, w11, repeat_at)
    task = census.current_task(ready, repeat_at)
    assert task.window_id == cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)


def test_repeat_is_served_after_window_eight_and_seven_days(ready):
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 8), NOW)

    # Window 8 not done: however late it is, the repeat is not yet served.
    task = census.current_task(ready, NOW + timedelta(days=30))
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 8))

    cf.finish_empty(ready, [8], NOW)
    task = census.current_task(ready, NOW + timedelta(days=6))
    assert (task.kind, task.window_id) == ("blind", cf.window_id(ready, 9))

    task = census.current_task(ready, NOW + timedelta(days=7))
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )
    assert task.session_no == 9


def test_waiting_when_only_the_repeat_remains_early(ready):
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 17), NOW)

    task = census.current_task(ready, NOW + timedelta(days=1))
    assert task.kind == "waiting"
    assert task.window_id is None
    assert "2026-10-08" in task.detail
    assert task.session_no == 17

    task = census.current_task(ready, NOW + timedelta(days=7))
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )


# ── Timing (sec 6.3, F19) ───────────────────────────────────────────────────


def test_active_minutes_stop_at_blind_done(ready):
    w1 = cf.window_id(ready, 1)
    m = timedelta(minutes=1)
    census.record_event(ready, w1, "open", NOW)
    # Heartbeats bridge what would otherwise be a 10-minute idle gap.
    census.record_event(ready, w1, "heartbeat", NOW + 4 * m)
    census.record_event(ready, w1, "heartbeat", NOW + 8 * m)
    census.record_event(ready, w1, "action", NOW + 10 * m)
    census.finish_blind(ready, w1, NOW + 12 * m)
    # After blind_done_at: the precision page and a stale tab's heartbeats.
    census.record_event(ready, w1, "heartbeat", NOW + 13 * m)
    census.record_event(ready, w1, "open", NOW + 14 * m)

    assert census.window_active_minutes(ready, w1) == pytest.approx(12.0)


def test_active_minutes_of_an_abandoned_window_count_to_the_end(ready):
    w1 = cf.window_id(ready, 1)
    m = timedelta(minutes=1)
    census.record_event(ready, w1, "open", NOW)
    census.record_event(ready, w1, "action", NOW + 3 * m)
    census.abandon(ready, w1, "stopped", NOW + 5 * m)
    # A tab left open after the abandon keeps heartbeating; it adds nothing.
    census.record_event(ready, w1, "heartbeat", NOW + 6 * m)
    assert census.window_active_minutes(ready, w1) == pytest.approx(5.0)


# ── Connections are never left in a transaction (F21) ───────────────────────


def test_every_function_leaves_no_transaction_open(ready):
    w1 = cf.window_id(ready, 1)
    x = census.window_items(ready, w1)[0]["id"]

    calls = [
        lambda: census.current_task(ready, NOW),
        lambda: census.window_items(ready, w1),
        lambda: census.record_event(ready, w1, "open", NOW),
        lambda: census.create_group(ready, w1, NOW),
        lambda: census.save_assignments(ready, w1, "p", 1, [(x, None, False)], NOW),
        lambda: census.latest_assignments(ready, w1),
        lambda: census.window_assignments(ready, w1),
        lambda: census.window_active_minutes(ready, w1),
        lambda: census.go_status(ready),
        lambda: census.finish_blind(ready, w1, NOW),
        lambda: census.precision_pairs(ready, w1),
        lambda: census.abandon(ready, cf.window_id(ready, 3), "reason", NOW),
    ]
    for call in calls:
        call()
        assert ready.info.transaction_status == IDLE, call

    failing = [
        (census.WindowClosed, lambda: census.finish_blind(ready, w1, NOW)),
        (census.WindowClosed, lambda: census.abandon(ready, w1, "again", NOW)),
        (census.BadWrite, lambda: census.save_precision(ready, w1, 1, 2, "same", NOW)),
        (
            census.BadWrite,
            lambda: census.abandon(ready, cf.window_id(ready, 4), " ", NOW),
        ),
    ]
    for exc, call in failing:
        with pytest.raises(exc):
            call()
        assert ready.info.transaction_status == IDLE, call

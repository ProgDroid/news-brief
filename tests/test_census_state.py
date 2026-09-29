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
    assert task.session_no == 0
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

    # An abandoned window is closed too. Window 2 is served once window 1's
    # precision sample is answered (D1: abandon only the served window).
    cf.answer_precision(ready, w1, NOW)
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
    # Seeded: D1 refuses create_group on window 2, which is not served.
    foreign_group = ready.execute(
        "INSERT INTO census_groups (window_id, created_at) VALUES (%s, %s) "
        "RETURNING id",
        (w2, NOW),
    ).fetchone()[0]
    ready.commit()

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
    # R11: window 1's precision is still session 1.
    assert task.session_no == 1

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
    assert task.session_no == 2
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


def test_abandoned_window_two_blocks_the_gate(ready):
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 8), NOW)
    cf.answer_precision(ready, w1, NOW)
    census.abandon(ready, cf.window_id(ready, 2), "interrupted", NOW)

    status = census.go_status(ready)
    assert status.ok is False
    assert status.reason == "window 2 abandoned; the gate cannot be evaluated"

    task = census.current_task(ready, NOW)
    assert task.kind == "gate_failed"
    assert task.window_id is None
    assert "abandoned" in task.detail
    assert task.detail == status.reason


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
    task = census.current_task(ready, NOW)
    assert task.kind == "gate_failed"
    assert "window 2 abandoned" in task.detail
    ready.execute("UPDATE census_block SET go_override_reason = 'ruling'")
    ready.commit()

    cf.finish_empty(ready, range(3, 17), NOW)
    task = census.current_task(ready, NOW)
    assert task.kind == "complete"
    assert task.window_id is None
    assert task.session_no == census.TOTAL_SESSIONS


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
    assert task.session_no == 16

    task = census.current_task(ready, NOW + timedelta(days=7))
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )


def test_complete_census_is_session_seventeen(ready):
    """R11: with all 16 pass-1 windows and the repeat done, the census is
    complete and reports TOTAL_SESSIONS, never TOTAL_SESSIONS + 1."""
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 17), NOW)
    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS)
    pass2 = cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2)
    cf.label(ready, pass2, cf.cross_outlet_groups(ready, pass2, 8), repeat_at)

    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("precision", cf.window_id(ready, 2))
    assert task.session_no == census.TOTAL_SESSIONS

    cf.answer_precision(ready, cf.window_id(ready, 2), repeat_at)
    task = census.current_task(ready, repeat_at)
    assert task.kind == "complete"
    assert task.session_no == census.TOTAL_SESSIONS == 17


# ── Claim on serve (D1a) and page loads ─────────────────────────────────────


def _events(conn, window_id: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT kind, at FROM census_events WHERE window_id = %s ORDER BY id",
        (window_id,),
    ).fetchall()
    conn.commit()
    return rows


def _to_window_nine(conn):
    """Windows 1-8 done at NOW with the gate passed: window 9 is the next
    pass-1 window, and the repeat becomes eligible at NOW + REPEAT_MIN_DAYS."""
    cf.pass_gate(conn, NOW)
    cf.finish_empty(conn, range(3, 9), NOW)
    eligible = NOW + timedelta(days=census.REPEAT_MIN_DAYS)
    return (
        cf.window_id(conn, 9),
        cf.window_id(conn, census.REPEAT_ORDER_NO, pass_=2),
        eligible,
    )


def test_a_served_window_is_not_preempted_by_the_repeat(ready):
    """The red team's sequence: window 9's page is served, the repeat's
    eligibility passes while the operator reads, and window 9 is still what
    the census serves."""
    w9, _repeat, eligible = _to_window_nine(ready)
    task = census.serve_page(ready, eligible - timedelta(minutes=5))
    assert (task.kind, task.window_id) == ("blind", w9)

    first_action = eligible + timedelta(minutes=1)
    task = census.current_task(ready, first_action)
    assert (task.kind, task.window_id) == ("blind", w9)
    # ...so the operator's first action is accepted by the write guard (D1).
    assert census.create_group(ready, w9, first_action) > 0


def test_without_an_open_event_the_repeat_is_served(ready):
    w9, repeat, eligible = _to_window_nine(ready)
    # current_task stamps nothing, so asking does not claim window 9.
    assert census.current_task(ready, eligible - timedelta(minutes=5)).window_id == w9
    task = census.current_task(ready, eligible + timedelta(minutes=1))
    assert (task.kind, task.window_id) == ("blind", repeat)
    assert _events(ready, w9) == []
    # Unclaimed, window 9 is not served, so the guard refuses a write to it.
    with pytest.raises(census.BadWrite):
        census.create_group(ready, w9, eligible + timedelta(minutes=1))


def test_the_reload_after_the_claimed_window_completes_serves_the_repeat(ready):
    w9, repeat, eligible = _to_window_nine(ready)
    census.serve_page(ready, eligible - timedelta(minutes=5))
    done_at = eligible + timedelta(minutes=20)
    cf.label(ready, w9, [], done_at)  # nothing grouped: completes at Finish

    reload_at = done_at + timedelta(seconds=1)
    task = census.serve_page(ready, reload_at)
    assert (task.kind, task.window_id) == ("blind", repeat)
    assert _events(ready, repeat) == [("open", reload_at)]


def test_an_open_event_before_the_last_completion_claims_nothing(ready):
    """Only a page served since the most recent completion claims."""
    w9, repeat, eligible = _to_window_nine(ready)
    # Seeded: the served order cannot stamp window 9 before window 8 is done.
    ready.execute(
        "INSERT INTO census_events (window_id, kind, at) VALUES (%s, 'open', %s)",
        (w9, NOW - timedelta(minutes=1)),
    )
    ready.commit()
    task = census.current_task(ready, eligible)
    assert (task.kind, task.window_id) == ("blind", repeat)


def test_an_abandon_counts_as_a_completion(ready):
    """`abandon` records no timestamp column; its event is its completion
    time. An open on window 9 that predates window 8's abandon claims
    nothing."""
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 8), NOW)
    w9 = cf.window_id(ready, 9)
    ready.execute(
        "INSERT INTO census_events (window_id, kind, at) VALUES (%s, 'open', %s)",
        (w9, NOW + timedelta(hours=1)),
    )
    ready.commit()
    census.abandon(
        ready, cf.window_id(ready, 8), "bad window", NOW + timedelta(hours=2)
    )

    task = census.current_task(ready, NOW + timedelta(days=census.REPEAT_MIN_DAYS))
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )


def test_serve_page_stamps_the_blind_and_the_precision_page(ready):
    w1 = cf.window_id(ready, 1)
    task = census.serve_page(ready, NOW)
    assert (task.kind, task.window_id, task.session_no) == ("blind", w1, 1)
    assert ready.info.transaction_status == IDLE
    assert _events(ready, w1) == [("open", NOW)]

    finish_at = NOW + timedelta(minutes=10)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 3), finish_at)
    page_at = finish_at + timedelta(seconds=1)
    task = census.serve_page(ready, page_at)
    assert (task.kind, task.window_id) == ("precision", w1)
    assert _events(ready, w1) == [
        ("open", NOW),
        ("finish", finish_at),
        ("open", page_at),
    ]


def test_serve_page_stamps_nothing_without_a_window(ready):
    cf.pass_gate(ready, NOW, groups=3)
    before = ready.execute("SELECT count(*) FROM census_events").fetchone()[0]
    ready.commit()
    assert census.serve_page(ready, NOW).kind == "gate_failed"
    after = ready.execute("SELECT count(*) FROM census_events").fetchone()[0]
    ready.commit()
    assert after == before
    assert ready.info.transaction_status == IDLE


# ── Timing (sec 6.3, F19) ───────────────────────────────────────────────────


def _event_count(conn, window_id: int) -> int:
    n = conn.execute(
        "SELECT count(*) FROM census_events WHERE window_id = %s", (window_id,)
    ).fetchone()[0]
    conn.commit()
    return n


def test_record_event_refuses_finish_abandon_and_open(ready):
    """m1: only finish_blind and abandon write those kinds -- a stray
    abandon event would cap a live window's active minutes. D1a: only
    serve_page writes `open`, in the transaction that decides what to serve."""
    w1 = cf.window_id(ready, 1)
    for kind in ("abandon", "finish", "open", "bogus"):
        with pytest.raises(census.BadWrite):
            census.record_event(ready, w1, kind, NOW)
        assert ready.info.transaction_status == IDLE
    assert _event_count(ready, w1) == 0
    assert _status(ready, w1)[0] == "prepared"


def test_record_event_refuses_unknown_and_closed_windows(ready):
    """m2: a BadWrite, never a psycopg error, for an unknown window; and no
    blind-pass event on a window whose blind pass is over."""
    unknown = cf.window_id(ready, 1) + 10_000
    with pytest.raises(census.BadWrite):
        census.record_event(ready, unknown, "heartbeat", NOW)
    assert ready.info.transaction_status == IDLE

    # Walked in the served order: window 1 saved and its precision answered,
    # then window 2 (served next) abandoned.
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 3), NOW)
    cf.answer_precision(ready, w1, NOW)
    w2 = cf.window_id(ready, 2)
    census.abandon(ready, w2, "stopped", NOW)
    before = {w: _event_count(ready, w) for w in (w1, w2)}

    for window in (w1, w2):
        for kind in ("action", "heartbeat"):
            with pytest.raises(census.BadWrite):
                census.record_event(ready, window, kind, NOW + timedelta(minutes=1))
            assert ready.info.transaction_status == IDLE
    assert {w: _event_count(ready, w) for w in (w1, w2)} == before


def test_active_minutes_stop_at_blind_done(ready):
    w1 = cf.window_id(ready, 1)
    m = timedelta(minutes=1)
    assert census.serve_page(ready, NOW).window_id == w1  # the `open` event
    # Heartbeats bridge what would otherwise be a 10-minute idle gap.
    census.record_event(ready, w1, "heartbeat", NOW + 4 * m)
    census.record_event(ready, w1, "heartbeat", NOW + 8 * m)
    for seq, members in enumerate(cf.cross_outlet_groups(ready, w1, 3), start=1):
        gid = census.create_group(ready, w1, NOW + 10 * m)
        census.save_assignments(
            ready, w1, "t", seq, [(i, gid, False) for i in members], NOW + 10 * m
        )
    census.record_event(ready, w1, "action", NOW + 10 * m)
    census.finish_blind(ready, w1, NOW + 12 * m)
    # After blind_done_at: the precision page is opened, twice. Each would
    # add minutes (gaps of 1 and 1) if the cap were missing.
    for at in (NOW + 13 * m, NOW + 14 * m):
        task = census.serve_page(ready, at)
        assert (task.kind, task.window_id) == ("precision", w1)

    assert census.window_active_minutes(ready, w1) == pytest.approx(12.0)


def test_active_minutes_of_an_abandoned_window_count_to_the_end(ready):
    w1 = cf.window_id(ready, 1)
    m = timedelta(minutes=1)
    assert census.serve_page(ready, NOW).window_id == w1  # the `open` event
    census.record_event(ready, w1, "action", NOW + 3 * m)
    census.abandon(ready, w1, "stopped", NOW + 5 * m)
    # An event after the abandon adds nothing. Seeded in SQL: the served
    # order would now stamp window 2, and every writer refuses window 1.
    ready.execute(
        "INSERT INTO census_events (window_id, kind, at) VALUES (%s, 'open', %s)",
        (w1, NOW + 6 * m),
    )
    ready.commit()
    assert census.window_active_minutes(ready, w1) == pytest.approx(5.0)


# ── Connections are never left in a transaction (F21) ───────────────────────


def test_every_function_leaves_no_transaction_open(ready):
    w1 = cf.window_id(ready, 1)
    x = census.window_items(ready, w1)[0]["id"]

    # In the served order: window 1 is finished with nothing grouped (so it
    # completes), and window 2, served next, is abandoned.
    calls = [
        lambda: census.current_task(ready, NOW),
        lambda: census.serve_page(ready, NOW),
        lambda: census.window_items(ready, w1),
        lambda: census.record_event(ready, w1, "heartbeat", NOW),
        lambda: census.create_group(ready, w1, NOW),
        lambda: census.save_assignments(ready, w1, "p", 1, [(x, None, False)], NOW),
        lambda: census.latest_assignments(ready, w1),
        lambda: census.window_assignments(ready, w1),
        lambda: census.window_active_minutes(ready, w1),
        lambda: census.go_status(ready),
        lambda: census.finish_blind(ready, w1, NOW),
        lambda: census.precision_pairs(ready, w1),
        lambda: census.abandon(ready, cf.window_id(ready, 2), "reason", NOW),
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


# ── The write guard (D1) ────────────────────────────────────────────────────


def _row_counts(conn) -> tuple:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM census_groups),"
        " (SELECT count(*) FROM census_assignments),"
        " (SELECT count(*) FROM census_events),"
        " (SELECT count(*) FROM census_adjudications),"
        " (SELECT array_agg(status ORDER BY id) FROM census_windows)"
    ).fetchone()
    conn.commit()
    return row


def _write_to_window_two(conn, writer: str):
    """One write per blind writer to window 2 while window 1 is served blind.
    Window 2 is `prepared`, so only the guard can refuse it."""
    w2 = cf.window_id(conn, 2)
    item = census.window_items(conn, w2)[0]["id"]
    return {
        "create_group": lambda: census.create_group(conn, w2, NOW),
        "save_assignments": lambda: census.save_assignments(
            conn, w2, "t", 1, [(item, None, False)], NOW
        ),
        "record_event": lambda: census.record_event(conn, w2, "heartbeat", NOW),
        "finish_blind": lambda: census.finish_blind(conn, w2, NOW),
        "abandon": lambda: census.abandon(conn, w2, "not served", NOW),
    }[writer]


@pytest.mark.parametrize(
    "writer",
    ["create_group", "save_assignments", "record_event", "finish_blind", "abandon"],
)
def test_a_blind_write_to_a_window_not_served_is_refused(ready, writer):
    assert census.current_task(ready, NOW).window_id == cf.window_id(ready, 1)
    before = _row_counts(ready)
    with pytest.raises(census.BadWrite, match="not served"):
        _write_to_window_two(ready, writer)()
    assert ready.info.transaction_status == IDLE
    assert _row_counts(ready) == before


def test_a_precision_answer_to_a_window_not_served_is_refused(ready):
    """The pair IS offered, so only the guard can refuse it: a window in
    progress (step 3) outranks window 1's precision sample."""
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 3), NOW)
    offered = census.precision_pairs(ready, w1)
    assert offered  # control: the pair below is on offer
    # Seeded: the guard itself refuses every write that could open window 2.
    ready.execute(
        "UPDATE census_windows SET status = 'open', opened_at = %s WHERE id = %s",
        (NOW, cf.window_id(ready, 2)),
    )
    ready.commit()
    assert census.current_task(ready, NOW).window_id == cf.window_id(ready, 2)

    before = _row_counts(ready)
    with pytest.raises(census.BadWrite, match="not served"):
        census.save_precision(ready, w1, *offered[0], "same", NOW)
    assert ready.info.transaction_status == IDLE
    assert _row_counts(ready) == before


def test_the_guard_keeps_the_transaction_and_the_row_lock(ready, monkeypatch):
    """The guard runs after `_lock_window`, inside the writer's transaction.
    Calling the committing public `current_task` there would end the
    transaction and release the F17 row lock mid-write. Checked in every
    writer, right after the guard returns, from a second connection."""
    real = census._require_served
    seen = []

    def spy(conn, window_id, kind, at):
        real(conn, window_id, kind, at)
        status = conn.info.transaction_status
        with db.connect() as other:
            try:
                other.execute(
                    "SELECT 1 FROM census_windows WHERE id = %s FOR UPDATE NOWAIT",
                    (window_id,),
                )
                locked = False
            except psycopg.errors.LockNotAvailable:
                locked = True
            other.rollback()
        seen.append((kind, window_id, status, locked))

    monkeypatch.setattr(census, "_require_served", spy)
    w1 = cf.window_id(ready, 1)
    w2 = cf.window_id(ready, 2)
    [pair] = cf.cross_outlet_groups(ready, w1, 1)
    census.record_event(ready, w1, "heartbeat", NOW)
    gid = census.create_group(ready, w1, NOW)
    census.save_assignments(ready, w1, "t", 1, [(i, gid, False) for i in pair], NOW)
    census.finish_blind(ready, w1, NOW)
    [(a, b)] = census.precision_pairs(ready, w1)
    census.save_precision(ready, w1, a, b, "same", NOW)
    census.abandon(ready, w2, "stopped", NOW)

    held = (psycopg.pq.TransactionStatus.INTRANS, True)
    assert seen == [
        ("blind", w1, *held),
        ("blind", w1, *held),
        ("blind", w1, *held),
        ("blind", w1, *held),
        ("precision", w1, *held),
        ("blind", w2, *held),
    ]


def test_save_assignments_waits_for_the_window_row_lock(ready):
    """F17: `_lock_window` takes FOR UPDATE on the window row. The other
    connection holds FOR KEY SHARE, which conflicts with FOR UPDATE but not
    with save_assignments' foreign-key checks or its plain UPDATE of the
    status -- so only the FOR UPDATE can make this save wait."""
    w1 = cf.window_id(ready, 1)
    x = census.window_items(ready, w1)[0]["id"]
    ready.execute("SET lock_timeout = '500ms'")
    ready.commit()
    with db.connect() as holder:
        holder.execute(
            "SELECT 1 FROM census_windows WHERE id = %s FOR KEY SHARE", (w1,)
        )
        try:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                census.save_assignments(ready, w1, "p", 1, [(x, None, False)], NOW)
        finally:
            holder.rollback()
    assert ready.info.transaction_status == IDLE
    assert census.latest_assignments(ready, w1) == {}

    # Control: released, the same save goes through.
    census.save_assignments(ready, w1, "p", 1, [(x, None, False)], NOW)
    ready.execute("RESET lock_timeout")
    ready.commit()
    assert census.latest_assignments(ready, w1) == {x: (None, False)}


# ── current_task's priority boundaries ──────────────────────────────────────


def test_a_failed_gate_outranks_an_open_window(ready):
    """Step 2 before step 3."""
    cf.pass_gate(ready, NOW, groups=3)
    w3 = cf.window_id(ready, 3)
    # Seeded: with the gate failed, the guard refuses the write that would
    # open window 3.
    ready.execute(
        "UPDATE census_windows SET status = 'open', opened_at = %s WHERE id = %s",
        (NOW, w3),
    )
    ready.commit()
    assert census.current_task(ready, NOW).kind == "gate_failed"

    # Control: the override lifts the gate and the open window is served.
    ready.execute("UPDATE census_block SET go_override_reason = 'ruling'")
    ready.commit()
    task = census.current_task(ready, NOW)
    assert (task.kind, task.window_id) == ("blind", w3)


def test_precision_outranks_an_eligible_repeat(ready):
    """Step 4 before step 5."""
    cf.pass_gate(ready, NOW)
    cf.finish_empty(ready, range(3, 8), NOW)
    w8 = cf.window_id(ready, 8)
    cf.label(ready, w8, cf.cross_outlet_groups(ready, w8, 8), NOW)
    repeat_at = NOW + timedelta(days=census.REPEAT_MIN_DAYS)

    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == ("precision", w8)

    # Control: with window 8's sample answered, the eligible repeat is served.
    cf.answer_precision(ready, w8, repeat_at)
    task = census.current_task(ready, repeat_at)
    assert (task.kind, task.window_id) == (
        "blind",
        cf.window_id(ready, census.REPEAT_ORDER_NO, pass_=2),
    )


# Positions (in the window's 12 cross-outlet groups) of the pairs
# `random.Random(1)` draws for window 1, in draw order.
PINNED_SAMPLE_WINDOW_1 = [2, 9, 1, 4, 10, 3, 6, 5, 8, 0]


def test_precision_sample_is_seeded_by_the_window_id(ready):
    """`precision_pairs` draws with `random.Random(window_id)`: a known window
    id gives a known sample."""
    w1 = cf.window_id(ready, 1)
    assert w1 == 1  # a fresh schema: the first census_windows row
    groups = cf.cross_outlet_groups(ready, w1, 12)
    cf.label(ready, w1, groups, NOW)
    position = {frozenset(g): n for n, g in enumerate(groups)}
    drawn = [position[frozenset(p)] for p in census.precision_pairs(ready, w1)]
    assert drawn == PINNED_SAMPLE_WINDOW_1

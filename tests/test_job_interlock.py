"""The interlock, exercised through the path that bypasses the supervisor.

A test that drives the supervisor proves nothing about the case that motivated
the rule (spec section 4.4a): a SECOND entry path starting a job that is already
running. So this holds the lock the way the supervisor would, then invokes the
mode directly, as `docker compose run --rm newsbrief collect` does.
"""

from datetime import datetime, timedelta, timezone

import pytest

import db
import brief

pytestmark = pytest.mark.skipif(
    not db.is_configured(), reason="No database is configured; see tests/test_db.py"
)


@pytest.fixture()
def clean_db():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        yield c


def _runs(conn, job):
    return conn.execute(
        "SELECT status, trigger FROM job_runs WHERE job_name = %s ORDER BY id", (job,)
    ).fetchall()


def test_a_job_runs_and_records_one_finished_row(clean_db):
    calls = []
    code = brief.run_job("collect", lambda: calls.append(1), trigger="manual")
    assert code == 0
    assert calls == [1]
    assert _runs(clean_db, "collect") == [("finished", "manual")]


def test_a_second_entry_path_refuses_while_the_job_is_running(clean_db):
    """The bypass case. The holder is a separate connection, as it would be a
    separate process in production."""
    holder = db.connect()
    with db.advisory_lock(holder, "collect") as got:
        assert got is True
        ran = []
        code = brief.run_job("collect", lambda: ran.append(1), trigger="manual")
        assert code == brief.EX_ALREADY_RUNNING
        assert ran == [], "the second entry path must not execute the mode"
    holder.close()
    assert _runs(clean_db, "collect") == [], "a refused run writes no running row"


def test_a_crashing_job_records_a_nonzero_exit_and_releases_the_lock(clean_db):
    def boom():
        raise RuntimeError("collect exploded")

    code = brief.run_job("collect", boom, trigger="manual")
    assert code != 0
    assert _runs(clean_db, "collect") == [("finished", "manual")]

    row = clean_db.execute(
        "SELECT exit_code FROM job_runs WHERE job_name = 'collect'"
    ).fetchone()
    assert row[0] != 0

    with db.connect() as other:
        with db.advisory_lock(other, "collect") as acquired:
            assert acquired is True, "the lock must not survive a crashed job"


def test_a_supervisor_owned_run_leaves_the_ledger_to_the_supervisor(clean_db):
    """The supervisor writes the row before spawning and closes it at reap, so
    the child must neither open nor close one."""
    run_id = db.start_run(clean_db, "collect", None, "scheduled")

    code = brief.run_job("collect", lambda: None, run_id=run_id)

    assert code == 0
    rows = clean_db.execute(
        "SELECT id, status FROM job_runs WHERE job_name = 'collect'"
    ).fetchall()
    assert rows == [(run_id, "running")]


def test_a_bookkeeping_failure_does_not_fail_the_job(clean_db, monkeypatch):
    """The lock connection sits idle for the job's whole duration, so closing
    the row can fail on a connection the server has since dropped. A SUCCESSFUL
    collect must not become an exit-1 crash alert because of that."""

    def work():
        def broken_connect():
            raise RuntimeError("connection dropped while the job ran")

        monkeypatch.setattr(db, "connect", broken_connect)

    code = brief.run_job("collect", work, trigger="manual")

    assert code == 0, "bookkeeping must not be able to fail a job"


def test_a_spawned_run_consumes_its_fire_time_immediately(clean_db):
    """The anti-storm invariant, and the reason the supervisor writes the row
    before spawning rather than letting the child write it after taking the
    lock. Lives here rather than in test_supervisor.py because it needs a real
    database and this is where that fixture is.

    Without it: a child that dies before recording anything leaves
    latest_scheduled_for empty, decide() still says "run", and the supervisor
    respawns every tick for the whole grace window — ~240 spawns and ~240
    Telegram alerts across collect's two hours.
    """
    import scheduler

    spec = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    now = datetime(2026, 8, 31, 6, 0, 30, tzinfo=timezone.utc)

    before = scheduler.decide(spec, now, db.latest_scheduled_for(clean_db, "collect"))
    assert before.action == "run"

    db.start_run(clean_db, "collect", before.scheduled_for, "scheduled")

    later = now + timedelta(seconds=scheduler.TICK_SECONDS)
    after = scheduler.decide(spec, later, db.latest_scheduled_for(clean_db, "collect"))
    assert after.action == "skip", "the fire time must be spent at spawn, not at lock"


def test_an_infrastructure_failure_alerts_and_exits_nonzero(clean_db, monkeypatch):
    """A Postgres outage on the connect/lock/start_run path — not just a
    crash inside fn() — must alert like any other failure, not escape as an
    unhandled traceback that never reaches Telegram (fix round 1, finding 1)."""
    alerts = []
    monkeypatch.setattr(brief, "telegram_alert", lambda msg: alerts.append(msg))

    def broken_connect():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(db, "connect", broken_connect)

    code = brief.run_job("collect", lambda: None, trigger="manual")

    assert code != 0
    assert alerts, "an infra failure must alert, not vanish as a bare traceback"
    assert "collect" in alerts[0]


def test_an_unknown_trigger_is_coerced_rather_than_reaching_the_database(clean_db):
    """job_runs.trigger carries CHECK (trigger IN ('scheduled','catchup',
    'manual')). NEWSBRIEF_TRIGGER arrives from the environment unvalidated, so
    a typo must be coerced to 'manual' rather than reaching that constraint
    (fix round 1, finding 2)."""
    code = brief.run_job("collect", lambda: None, trigger="not-a-real-trigger")
    assert code == 0
    assert _runs(clean_db, "collect") == [("finished", "manual")]


def test_advisory_lock_cleanup_does_not_mask_the_original_exception(clean_db):
    """A statement that fails inside the lock aborts the transaction; the
    unlock in advisory_lock's own cleanup must not raise InFailedSqlTransaction
    over top of the real error (fix round 1, finding 2)."""
    import psycopg

    conn = db.connect()
    try:
        with pytest.raises(Exception) as exc_info:
            with db.advisory_lock(conn, "collect") as got:
                assert got is True
                conn.execute("SELECT this_column_does_not_exist FROM job_runs")
        assert not isinstance(exc_info.value, psycopg.errors.InFailedSqlTransaction), (
            "the release-time error masked the original failure"
        )
        assert "this_column_does_not_exist" in str(exc_info.value)
    finally:
        conn.close()


# APPENDED IN TASK 4, NOT TASK 3. Everything below imports `supervisor`, which
# does not exist yet at Task 3 — but the `clean_db` fixture lives here, so these
# ledger tests belong in this file rather than in a duplicated fixture next door.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_refused_run_is_recorded_as_missed_not_finished(clean_db):
    """A supervisor-spawned child that finds the lock held never ran. Recording
    it 'finished' would make /jobs report work that did not happen."""
    import supervisor

    run_id = db.start_run(clean_db, "collect", None, "scheduled")
    supervisor._close_run(run_id, brief.EX_ALREADY_RUNNING)

    status, code = clean_db.execute(
        "SELECT status, exit_code FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status == "missed"
    assert code == brief.EX_ALREADY_RUNNING


def test_reclaim_closes_a_run_orphaned_by_a_restart(clean_db):
    """A container restart kills the child, so nobody closes its row. Without
    reclaim the run shows 'running' forever and no alert ever fires."""
    import supervisor

    run_id = db.start_run(clean_db, "collect", None, "scheduled")

    reclaimed = supervisor.reclaim_orphans(clean_db)

    assert reclaimed == ["collect"]
    status, code = clean_db.execute(
        "SELECT status, exit_code FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status == "missed"
    assert code == supervisor.EX_ORPHANED


def test_reclaim_leaves_a_genuinely_running_job_alone(clean_db):
    """Ownership is decided by the advisory lock, not guessed: if a live process
    still holds it, the run is not an orphan."""
    import supervisor

    run_id = db.start_run(clean_db, "collect", None, "scheduled")
    holder = db.connect()
    with db.advisory_lock(holder, "collect") as got:
        assert got is True
        assert supervisor.reclaim_orphans(clean_db) == []
    holder.close()

    status = clean_db.execute(
        "SELECT status FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status == "running"


def test_a_reclaimed_run_is_not_retried(clean_db):
    """Deliberate: a collect killed halfway has polled its batch and may have
    opened paper positions, so re-running it blind is worse than not running it.
    The fire time stays consumed; the operator gets an alert instead."""
    import scheduler
    import supervisor

    spec = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    now = datetime(2026, 8, 31, 6, 0, 30, tzinfo=timezone.utc)
    fire = scheduler.previous_fire(spec, now)
    db.start_run(clean_db, "collect", fire, "scheduled")

    supervisor.reclaim_orphans(clean_db)

    d = scheduler.decide(spec, now, db.latest_scheduled_for(clean_db, "collect"))
    assert d.action == "skip"


def test_a_planned_shutdown_closes_the_job_row(clean_db):
    """A `compose down` mid-job must leave no `running` row behind. Otherwise
    the next boot's reclaim_orphans closes it and fires "Orphaned by a restart
    and NOT retried" on every ordinary deploy, training the operator to ignore
    the one alert that matters (fix round 2, Important 3)."""
    import sys

    import supervisor

    run_id = db.start_run(clean_db, "collect", None, "scheduled")
    child = supervisor.Child(
        "collect", [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    child.run_id = run_id
    child.start()

    supervisor.shutdown({"collect": child}, {})

    status, code = clean_db.execute(
        "SELECT status, exit_code FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status != "running", "a planned stop must not leave an orphan row"
    assert status == "finished"
    assert code not in (None, 0), "a terminated child did not exit cleanly"


def test_a_child_that_ignores_sigterm_still_gets_its_row_closed(clean_db, monkeypatch):
    """The row must be written before the SIGKILL, against a real process.

    The test above uses a child that dies on SIGTERM, so it never reaches the
    deadline — it would pass even if shutdown escalated first and wrote the
    ledger afterwards, which is precisely the ordering Docker's stop timeout
    interrupts.

    The child ignores SIGTERM by having its `signal_stop` neutered rather than
    by installing SIG_IGN: on Windows `Popen.terminate` is TerminateProcess,
    which cannot be ignored, and this test has to run on both platforms. The
    process really does survive the broadcast either way.
    """
    import sys
    import time

    import supervisor

    monkeypatch.setattr(supervisor, "SHUTDOWN_BUDGET_SECONDS", 1.0)

    run_id = db.start_run(clean_db, "collect", None, "scheduled")
    child = supervisor.Child(
        "collect", [sys.executable, "-c", "import time; time.sleep(120)"]
    )
    child.run_id = run_id
    child.start()
    monkeypatch.setattr(child, "signal_stop", lambda: None)

    started = time.monotonic()
    supervisor.shutdown({"collect": child}, {})
    elapsed = time.monotonic() - started

    status, code = clean_db.execute(
        "SELECT status, exit_code FROM job_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert status == "finished", "the row must close even when the child will not"
    assert code == supervisor.EX_SHUTDOWN_KILLED, (
        "SIGKILLed at the deadline is its own fact, distinct from -1's 'we never "
        "learned the exit code'"
    )
    assert not child.running, "a child that ignores SIGTERM must still be killed"
    assert elapsed < supervisor.SHUTDOWN_BUDGET_SECONDS + 15, (
        "the budget is shared across children and bounded, not per-child"
    )


def _seed_now():
    """06:05 UTC on a Wednesday: inside collect's 2h grace AND monitor's 15m."""
    return datetime(2026, 9, 2, 6, 5, 0, tzinfo=timezone.utc)


def test_a_first_boot_inside_a_grace_window_runs_nothing(clean_db):
    """The cutover defect, asserted at the decision rather than the row.

    On first boot job_runs is empty, latest_scheduled_for is None, and decide
    never reaches its already-recorded branch — so every schedule still inside
    its grace window fires at once. Deploy at 06:30 and that is a SECOND collect
    for a morning host cron already ran, and a monitor that calls
    trading.sweep_live_exits and polygram_live.reconcile_live_book: the live
    sell path, with real money.
    """
    import scheduler
    import supervisor

    now = _seed_now()
    # Asked of `decide` directly, because `_due_jobs` WRITES the missed rows it
    # finds — running it first would seed half the table and hide the defect.
    would_run = {
        spec.job
        for spec in scheduler.SCHEDULES
        if scheduler.decide(spec, now, None).action == "run"
    }
    assert would_run == {"collect", "monitor"}, (
        "an empty ledger runs both immediately; if that stops being true the "
        "seed is guarding nothing and this fixture time is wrong"
    )

    seeded = supervisor.seed_first_boot(clean_db, now)

    assert set(seeded) == {s.job for s in scheduler.SCHEDULES}
    assert supervisor._due_jobs(clean_db, now) == [], "a first boot must run nothing"
    for spec in scheduler.SCHEDULES:
        status, trigger, started = clean_db.execute(
            "SELECT status, trigger, started_at FROM job_runs WHERE job_name = %s",
            (spec.job,),
        ).fetchone()
        assert (status, trigger) == ("missed", "scheduled")
        assert started is None, "nothing ran, so nothing has a start time"


def test_a_second_boot_does_not_re_seed(clean_db):
    """Seeding twice would consume a fire time the operator expected to run."""
    import supervisor

    now = _seed_now()
    supervisor.seed_first_boot(clean_db, now)
    before = clean_db.execute("SELECT count(*) FROM job_runs").fetchone()[0]

    assert supervisor.seed_first_boot(clean_db, now) == []
    assert clean_db.execute("SELECT count(*) FROM job_runs").fetchone()[0] == before


def test_a_newly_added_schedule_seeds_although_the_table_is_not_empty(
    clean_db, monkeypatch
):
    """Why the emptiness check is per JOB. A whole-table check would pass on the
    day a fifth schedule is added — the table is not empty, the new job gets no
    seed, and the double-run comes back for that job alone."""
    import scheduler
    import supervisor

    now = _seed_now()
    supervisor.seed_first_boot(clean_db, now)

    fresh = scheduler.Schedule("digest", "daily", "06:00", None, grace_minutes=120)
    monkeypatch.setattr(scheduler, "SCHEDULES", scheduler.SCHEDULES + (fresh,))

    assert supervisor.seed_first_boot(clean_db, now) == ["digest"]
    assert supervisor._due_jobs(clean_db, now) == []


def test_seeding_only_suppresses_the_fire_time_it_consumed(clean_db):
    """The seed must not disable the job — the NEXT fire time still runs."""
    import supervisor

    supervisor.seed_first_boot(clean_db, _seed_now())

    tomorrow = datetime(2026, 9, 3, 6, 0, 20, tzinfo=timezone.utc)
    due = {spec.job for spec, _, _ in supervisor._due_jobs(clean_db, tomorrow)}
    assert "collect" in due, "ordinary scheduling resumes from the next fire time"


def test_a_job_whose_only_history_is_manual_is_still_seeded(clean_db):
    """The seed must key on the value `decide` consumes, not on row existence.

    A manual run records a NULL scheduled_for. A row-existence check calls the
    job touched and skips the seed, but `decide` still sees None and fires it —
    which is the double-run this whole function exists to prevent, reached by a
    different door. Testing `latest_scheduled_for(...) is None` keeps the two in
    agreement; the cost is one lost catch-up for a manual-only job, which is the
    safe direction.
    """
    import scheduler
    import supervisor

    now = _seed_now()
    db.finish_run(clean_db, db.start_run(clean_db, "collect", None, "manual"), 0)

    spec = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    assert db.latest_scheduled_for(clean_db, "collect") is None
    assert scheduler.decide(spec, now, None).action == "run", (
        "the manual row did not change what decide would do, so a seed is owed"
    )

    assert "collect" in supervisor.seed_first_boot(clean_db, now)
    assert supervisor._due_jobs(clean_db, now) == []

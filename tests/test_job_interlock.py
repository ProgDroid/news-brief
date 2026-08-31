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
    not db.database_url(), reason="DATABASE_URL is not set; see tests/test_db.py"
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

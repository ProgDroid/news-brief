"""Supervisor process handling, with fake commands rather than real modes.

Real modes need an API key, a network and hours; none of that tests spawn,
reap, drain, backoff or fail-open startup, which is all this module does.
"""

import sys
import time
from datetime import datetime, timezone

import supervisor


def _fake(code: int = 0, out: str = "", sleep: float = 0.0):
    """A child command that prints, optionally sleeps, and exits with `code`."""
    script = f"import sys,time; print({out!r}); time.sleep({sleep}); sys.exit({code})"
    return [sys.executable, "-c", script]


def test_spawn_captures_exit_code_zero():
    child = supervisor.Child("ok", _fake(0))
    child.start()
    assert child.wait(timeout=30) == 0


def test_spawn_captures_a_nonzero_exit_code():
    child = supervisor.Child("bad", _fake(3))
    child.start()
    assert child.wait(timeout=30) == 3


def test_child_output_is_drained_and_prefixed(caplog):
    child = supervisor.Child("noisy", _fake(0, out="hello from the child"))
    with caplog.at_level("INFO"):
        child.start()
        child.wait(timeout=30)
        child.join_reader(timeout=10)
    assert any("[noisy] hello from the child" in r.message for r in caplog.records)


def test_wait_times_out_then_terminate_stops_the_child():
    child = supervisor.Child("slow", _fake(0, sleep=30))
    child.start()
    assert child.wait(timeout=0.5) is None, "still running, so no exit code yet"
    child.terminate(grace=5)
    assert child.wait(timeout=10) is not None


def test_backoff_grows_and_is_capped():
    delays = [supervisor.backoff_delay(n) for n in range(0, 8)]
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= supervisor.BACKOFF_CEILING_SECONDS
    assert all(d > 0 for d in delays)


def test_crash_loop_is_detected_within_the_window():
    tracker = supervisor.RestartTracker(limit=3, window_seconds=60)
    now = time.monotonic()
    assert tracker.record("commands", now) is False
    assert tracker.record("commands", now + 1) is False
    assert tracker.record("commands", now + 2) is True


def test_restarts_outside_the_window_do_not_trip_the_alert():
    tracker = supervisor.RestartTracker(limit=3, window_seconds=60)
    now = time.monotonic()
    assert tracker.record("commands", now) is False
    assert tracker.record("commands", now + 100) is False
    assert tracker.record("commands", now + 200) is False


def test_a_failed_migration_blocks_jobs_but_still_starts_residents():
    """Fail closed on work, fail open on observability (spec section 8)."""

    def boom(conn):
        raise RuntimeError("relation already exists")

    state = supervisor.startup(migrate=boom, connect=lambda: object())
    assert state.jobs_enabled is False
    assert state.residents_enabled is True
    assert "relation already exists" in state.reason


# ── Fix round 2 ───────────────────────────────────────────────────────────────


class _FakeChild:
    """A Child stand-in: no subprocess, so a tick loop can be driven by hand."""

    def __init__(self, name, argv=None, env=None):
        self.name = name
        self.run_id = None
        self.alive = True
        self.exit_code = 7
        self.started = False
        self.joined = 0
        self.terminated = 0

    def start(self):
        self.started = True

    @property
    def running(self):
        return self.alive

    def wait(self, timeout=None):
        return None if self.alive else self.exit_code

    def join_reader(self, timeout=5.0):
        self.joined += 1

    def terminate(self, grace=20.0):
        self.terminated += 1
        self.alive = False


def _spawner(into):
    def spawn(mode):
        child = _FakeChild(mode)
        into.append(child)
        return child

    return spawn


def test_a_dead_resident_is_restarted_on_a_later_tick(monkeypatch):
    """The Telegram bot is the operator's only control channel, so a resident
    that exits has to come back. It cannot come back on the crash tick itself —
    the backoff is measured from that same instant — so a later tick must see
    the elapsed delay and start a fresh child (fix round 2, Critical 1)."""
    monkeypatch.setattr(supervisor, "telegram_alert", lambda msg: None)
    spawned = []
    pool = supervisor.ResidentPool()

    pool.tick(0.0, spawn=_spawner(spawned))
    assert len(spawned) == 1 and spawned[0].started

    spawned[0].alive = False
    pool.tick(1.0, spawn=_spawner(spawned))
    assert len(spawned) == 1, "the crash tick reaps; it does not also spawn"
    assert spawned[0].joined == 1, "the dead child's last output must be drained"

    pool.tick(1.0 + supervisor.backoff_delay(1), spawn=_spawner(spawned))
    assert len(spawned) == 2, "a later tick must start a fresh resident"
    assert spawned[1].started and spawned[1].running


def test_repeated_ticks_over_one_dead_resident_do_not_inflate_or_realert(monkeypatch):
    """One crash is one failure. Leaving the dead Child in place made every
    tick re-reap it: the count climbed, the backoff outgrew the tick interval
    so the restart never fired, and the crash-loop alert repeated every 30
    seconds forever (fix round 2, Critical 1)."""
    alerts = []
    monkeypatch.setattr(supervisor, "telegram_alert", alerts.append)
    spawned = []
    spawn = _spawner(spawned)
    pool = supervisor.ResidentPool()

    pool.tick(0.0, spawn=spawn)
    spawned[0].alive = False

    for t in range(1, 4):  # the crash tick, then two more inside the backoff
        pool.tick(float(t), spawn=spawn)

    assert pool.failures["commands"] == 1, "one crash must count once"
    assert alerts == [], "one dead child must not manufacture a crash loop"
    assert len(spawned) == 1, "no restart until the backoff has actually elapsed"


def test_a_resident_failure_count_resets_only_after_it_proves_stable(monkeypatch):
    """Resetting at spawn cleared the count before a crash loop could grow it,
    leaving the backoff ceiling unreachable (fix round 2, Minor 5)."""
    monkeypatch.setattr(supervisor, "telegram_alert", lambda msg: None)
    spawned = []
    spawn = _spawner(spawned)
    pool = supervisor.ResidentPool()

    pool.tick(0.0, spawn=spawn)
    spawned[0].alive = False
    pool.tick(1.0, spawn=spawn)
    pool.tick(1.0 + supervisor.backoff_delay(1), spawn=spawn)
    assert pool.failures["commands"] == 1, "a spawn alone proves nothing"

    started = pool.started_at["commands"]
    pool.tick(started + supervisor.RESIDENT_STABLE_SECONDS - 1, spawn=spawn)
    assert pool.failures["commands"] == 1, "not stable yet"

    pool.tick(started + supervisor.RESIDENT_STABLE_SECONDS, spawn=spawn)
    assert pool.failures["commands"] == 0, "surviving the stability window clears it"


def test_shutdown_closes_job_rows_rather_than_leaving_them_running(monkeypatch):
    """A planned stop that only terminates leaves `running` rows behind, so the
    next boot's reclaim alerts "orphaned by a restart" on every ordinary
    `compose down` (fix round 2, Important 3)."""
    closed = []
    monkeypatch.setattr(
        supervisor, "_close_run", lambda run_id, code: closed.append((run_id, code))
    )
    job = _FakeChild("collect")
    job.run_id = 41
    job.exit_code = -15
    resident = _FakeChild("commands")
    jobs = {"collect": job}
    residents = {"commands": resident}

    supervisor.shutdown(jobs, residents)

    assert closed == [(41, -15)], "a planned stop must leave no orphan row"
    assert job.terminated == 1 and resident.terminated == 1
    assert job.joined == 1, "the child's final output must be drained, not dropped"
    assert jobs == {}, "a stopped job must not stay in the live set"


def _triggers_at(now):
    return {spec.job: trigger for spec, _, trigger in supervisor._due_jobs(None, now)}


def test_an_on_time_run_records_trigger_scheduled(monkeypatch):
    """decide() returns previous_fire(), which is always <= now, so the old
    `now > scheduled_for` test made every single run a catchup and the column
    stopped distinguishing anything (fix round 2, Important 2)."""
    monkeypatch.setattr(supervisor.db, "latest_scheduled_for", lambda conn, job: None)
    monkeypatch.setattr(supervisor.db, "record_missed", lambda *a, **k: None)

    # Two seconds after collect's 06:00 fire time: an utterly ordinary tick.
    triggers = _triggers_at(datetime(2026, 8, 31, 6, 0, 2, tzinfo=timezone.utc))
    assert triggers["collect"] == "scheduled"


def test_a_late_run_records_trigger_catchup(monkeypatch):
    """Still within collect's two-hour grace, but 45 minutes late — the case
    the column exists to name."""
    monkeypatch.setattr(supervisor.db, "latest_scheduled_for", lambda conn, job: None)
    monkeypatch.setattr(supervisor.db, "record_missed", lambda *a, **k: None)

    triggers = _triggers_at(datetime(2026, 8, 31, 6, 45, 0, tzinfo=timezone.utc))
    assert triggers["collect"] == "catchup"

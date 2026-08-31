"""Supervisor process handling, with fake commands rather than real modes.

Real modes need an API key, a network and hours; none of that tests spawn,
reap, drain, backoff or fail-open startup, which is all this module does.
"""

import sys
import time

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

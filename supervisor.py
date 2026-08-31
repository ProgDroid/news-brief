#!/usr/bin/env python3
"""Process supervision for the single application container.

Owns four things and no business logic (spec section 3.2): resident children,
job children, the log file, and shutdown. The no-business-logic rule is not
tidiness — it is the mitigation for consolidating the bot into this process
(spec section 3.3), because it keeps this module's own crash surface small.

The three seam invariants of spec section 3.4 are load-bearing and are why a
child is spawned with argv and environment only, and why every piece of
coordination goes through Postgres: given them, promoting a child to its own
container later is a compose edit rather than a refactor.
"""

import os
import sys
import time
import signal
import threading
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import db
import scheduler
from common import log, telegram_alert, EX_ALREADY_RUNNING

# Children that stay up. `commands` is the Telegram long-poll daemon and the
# ONLY getUpdates consumer (a second one 409s) — under the supervisor exactly
# one process owns that constraint, which a stray `compose up` used to violate.
RESIDENT_MODES = ("commands",)

# The tick interval lives in scheduler.py, next to the grace-floor rule that
# depends on it: a schedule whose grace is shorter than one tick can never fire.
BACKOFF_BASE_SECONDS = 2
BACKOFF_CEILING_SECONDS = 300
CRASH_LOOP_LIMIT = 5
CRASH_LOOP_WINDOW_SECONDS = 600


def backoff_delay(consecutive_failures: int) -> float:
    return float(
        min(BACKOFF_BASE_SECONDS * (2**consecutive_failures), BACKOFF_CEILING_SECONDS)
    )


@dataclass
class RestartTracker:
    """Detects a child restarting too often to be healthy."""

    limit: int = CRASH_LOOP_LIMIT
    window_seconds: float = CRASH_LOOP_WINDOW_SECONDS
    _events: dict[str, list[float]] = field(default_factory=dict)

    def record(self, name: str, at: float | None = None) -> bool:
        """Record a restart; return True if this trips the crash-loop threshold."""
        at = time.monotonic() if at is None else at
        events = [t for t in self._events.get(name, []) if at - t < self.window_seconds]
        events.append(at)
        self._events[name] = events
        return len(events) >= self.limit


class Child:
    """One `python brief.py <mode>` process, with its output drained to the log."""

    def __init__(
        self, name: str, argv: list[str] | None = None, env: dict | None = None
    ):
        self.name = name
        self.argv = argv or [sys.executable, "brief.py", name]
        self.env = env
        self.run_id: int | None = None  # set for job children; None for residents
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        child_env = dict(os.environ)
        # Seam invariant 1: a child receives nothing but argv and environment.
        # Invariant for the log: only the supervisor writes the file.
        child_env["NEWSBRIEF_LOG_FILE"] = "0"
        if self.env:
            child_env.update(self.env)
        self.proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        log.info(f"[{self.name}] started pid={self.proc.pid}")

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            log.info(f"[{self.name}] {line.rstrip()}")

    def wait(self, timeout: float | None = None) -> int | None:
        """Exit code, or None if still running when `timeout` expires."""
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def join_reader(self, timeout: float = 5.0) -> None:
        if self._reader is not None:
            self._reader.join(timeout=timeout)

    def terminate(self, grace: float = 20.0) -> None:
        """SIGTERM, wait, then SIGKILL — so a host restart mid-run is safe."""
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        if self.wait(timeout=grace) is None:
            log.warning(f"[{self.name}] did not exit in {grace}s; killing")
            self.proc.kill()
            self.wait(timeout=5)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


@dataclass
class StartupState:
    jobs_enabled: bool
    residents_enabled: bool
    reason: str = ""


def startup(*, migrate=None, connect=None) -> StartupState:
    """Run migrations. Fail closed on work, fail open on observability.

    A failed migration must not take down the Telegram bot: it is the channel
    the operator would use to find out the migration failed, and recovery would
    otherwise mean SSH plus psql (spec sections 3.3 and 8).
    """
    migrate = migrate or db.run_migrations
    connect = connect or db.connect
    try:
        conn = connect()
        migrate(conn)
        try:
            reclaim_orphans(conn)
        except Exception:
            # Reclaim is hygiene, not a precondition: failing it must not stop
            # the supervisor from doing today's work.
            log.exception("Orphan reclaim failed; continuing")
        return StartupState(jobs_enabled=True, residents_enabled=True)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        log.exception("Migration failed; jobs are disabled, bot still starting")
        telegram_alert(f"Migration failed — jobs disabled, bot still up. {reason}")
        return StartupState(jobs_enabled=False, residents_enabled=True, reason=reason)


EX_ORPHANED = -2  # a run whose supervisor died before it could be closed


def _close_run(run_id: int | None, exit_code: int | None) -> None:
    """Close a ledger row on its own connection.

    The supervisor is authoritative for the children it spawned: it has the exit
    code even when the child was OOM-killed or SIGKILLed and could therefore
    write nothing itself — which is the failure class that is invisible today
    (spec section 8). A missing exit code is recorded as -1, not as success.

    A refusal closes as 'missed', not 'finished': the child never ran, and a
    'finished' row would let /jobs report work that did not happen.
    """
    if run_id is None:
        return
    code = -1 if exit_code is None else exit_code
    status = "missed" if code == EX_ALREADY_RUNNING else "finished"
    try:
        with db.connect() as conn:
            db.finish_run(conn, run_id, code, status=status)
    except Exception:
        log.exception(f"Could not close run {run_id}")


def reclaim_orphans(conn) -> list[str]:
    """Close rows left `running` by a supervisor that died mid-job.

    A container restart kills its children, so nothing closes their rows: the run
    shows as running forever and no alert fires — spec section 8's invisible
    failure class, reintroduced by the row-at-spawn design itself.

    Ownership is decidable rather than guessed, because the advisory lock died
    with the child's connection: if we can take the lock, nobody is running that
    job.

    **The fire time stays consumed and the run is NOT retried.** `latest_
    scheduled_for` is status-agnostic, so marking the row `missed` does not make
    `decide` re-evaluate — and that is the behaviour we want. A collect killed
    halfway has already polled its batch and may have opened paper positions;
    re-running it blind is worse than not running it. The operator gets an alert
    naming the job and can re-run by hand.
    """
    rows = conn.execute(
        "SELECT id, job_name FROM job_runs WHERE status = 'running'"
    ).fetchall()
    reclaimed: list[str] = []
    for run_id, job_name in rows:
        with db.advisory_lock(conn, job_name) as nobody_owns_it:
            if not nobody_owns_it:
                continue  # a live process really is running this job
        db.finish_run(conn, run_id, EX_ORPHANED, status="missed")
        reclaimed.append(job_name)
        log.warning(f"[{job_name}] run {run_id} orphaned by a restart; marked missed")
    if reclaimed:
        telegram_alert(
            "Orphaned by a restart and NOT retried (re-run by hand if needed): "
            + ", ".join(sorted(set(reclaimed)))
        )
    return reclaimed


def _due_jobs(conn, now: datetime) -> list[tuple[scheduler.Schedule, datetime, str]]:
    """Decide every schedule, recording misses. Returns what should run now."""
    ready = []
    for spec in scheduler.SCHEDULES:
        last = db.latest_scheduled_for(conn, spec.job)
        decision = scheduler.decide(spec, now, last)
        if decision.action == "run":
            trigger = "catchup" if now > decision.scheduled_for else "scheduled"
            ready.append((spec, decision.scheduled_for, trigger))
        elif decision.action == "missed":
            log.warning(f"[{spec.job}] {decision.reason}")
            db.record_missed(conn, spec.job, decision.scheduled_for)
    return ready


def serve() -> int:
    """Entry point for `brief.py serve`."""
    log.info("=== SERVE (supervisor) ===")
    state = startup()

    stopping = threading.Event()

    def _stop(signum, _frame):
        log.info(f"Signal {signum}: shutting down")
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    residents: dict[str, Child] = {}
    failures: dict[str, int] = {}
    next_start: dict[str, float] = {}
    tracker = RestartTracker()
    jobs: dict[str, Child] = {}

    while not stopping.is_set():
        now_mono = time.monotonic()

        if state.residents_enabled:
            for mode in RESIDENT_MODES:
                child = residents.get(mode)
                if child is not None and child.running:
                    continue
                if child is not None:
                    code = child.wait(timeout=0)
                    log.warning(f"[{mode}] exited with {code}; will restart")
                    failures[mode] = failures.get(mode, 0) + 1
                    if tracker.record(mode):
                        telegram_alert(
                            f"{mode} has restarted {CRASH_LOOP_LIMIT} times in "
                            f"{CRASH_LOOP_WINDOW_SECONDS // 60} minutes — crash loop"
                        )
                    next_start[mode] = now_mono + backoff_delay(failures[mode])
                if now_mono >= next_start.get(mode, 0.0):
                    residents[mode] = Child(mode)
                    residents[mode].start()
                    failures[mode] = 0

        for mode, child in list(jobs.items()):
            if child.running:
                continue
            code = child.wait(timeout=0)
            child.join_reader(timeout=5)
            del jobs[mode]
            _close_run(child.run_id, code)
            if code == EX_ALREADY_RUNNING:
                log.warning(
                    f"[{mode}] refused: another entry path holds the job lock. "
                    f"Recorded and not retried until the next fire time."
                )
            elif code not in (0, None):
                # The invisible failure class: a child killed by the OOM killer
                # or SIGKILL never reaches its own except block, so today this
                # is silence — indistinguishable from a quiet news day.
                log.error(f"[{mode}] exited with {code}")
                telegram_alert(f"{mode} exited with code {code}")

        if state.jobs_enabled:
            try:
                with db.connect() as conn:
                    for spec, scheduled_for, trigger in _due_jobs(
                        conn, datetime.now(timezone.utc)
                    ):
                        if spec.job in jobs:
                            log.warning(
                                f"[{spec.job}] still running from a previous fire "
                                f"time; skipping {scheduled_for.isoformat()}"
                            )
                            continue
                        # Write the row BEFORE spawning. This consumes the fire
                        # time immediately, so a child that dies before it can
                        # record anything — refused lock, missing env var,
                        # import error — is not respawned on every tick for the
                        # whole grace window. Writing it after the child took
                        # its lock would mean ~240 spawns and ~240 alerts across
                        # collect's two hours. It is also why job children need
                        # no backoff of their own: the fire time is already
                        # spent, so there is nothing to back off from.
                        run_id = db.start_run(conn, spec.job, scheduled_for, trigger)
                        child = Child(
                            spec.job,
                            env={
                                "NEWSBRIEF_SCHEDULED_FOR": scheduled_for.isoformat(),
                                "NEWSBRIEF_TRIGGER": trigger,
                                "NEWSBRIEF_RUN_ID": str(run_id),
                            },
                        )
                        child.run_id = run_id
                        try:
                            child.start()
                        except Exception as e:
                            # The fire time is already spent, so this job will
                            # not be retried until its next one — that must not
                            # be silent.
                            log.exception(f"[{spec.job}] failed to spawn")
                            telegram_alert(
                                f"{spec.job} failed to spawn: {type(e).__name__}: {e} "
                                f"— not retried until the next scheduled run"
                            )
                            _close_run(run_id, -1)
                            continue
                        jobs[spec.job] = child
            except Exception:
                log.exception("Scheduler tick failed; continuing")

        stopping.wait(scheduler.TICK_SECONDS)

    for child in list(jobs.values()) + list(residents.values()):
        child.terminate()
    log.info("Supervisor stopped")
    return 0

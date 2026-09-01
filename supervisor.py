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
from datetime import datetime, timedelta, timezone

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
# A resident alive this long has demonstrated it is not crash-looping; only
# then does its failure count reset (see ResidentPool._note_stability, which
# is where this is applied). Resetting at spawn instead cleared the count
# before a real crash loop could ever grow it past CRASH_LOOP_LIMIT, making
# the backoff ceiling unreachable.
RESIDENT_STABLE_SECONDS = CRASH_LOOP_WINDOW_SECONDS

# How long every child together gets to exit after the SIGTERM broadcast. It is
# a SHARED deadline, not a per-child one: terminating four job children serially
# at 20s of grace each took 80s, which no plausible stop_grace_period covers.
#
# **docker-compose.yml's `stop_grace_period` must exceed this**, with room for
# the ledger writes that follow — the comment there says so too, because the
# pair is only safe while both numbers are tuned together. Under Docker's 10s
# default the supervisor is SIGKILLed mid-shutdown, its `running` rows survive,
# and the next boot reports them as orphans: the Task 4 fix goes inert and every
# routine deploy fires the false alert that trains the operator to ignore the
# real one.
SHUTDOWN_BUDGET_SECONDS = 30.0
# A shared bound on draining the children's final output. Small on purpose: the
# log lines are worth having, but not at the cost of the ledger writes queued
# behind them, and a grandchild holding the pipe open would otherwise stall the
# drain for the whole remaining budget.
SHUTDOWN_DRAIN_SECONDS = 2.0
# Without this, closing a row against a stopped database blocks on libpq's own
# connect timeout — outside every budget above, which is how a "bounded"
# shutdown stops being bounded.
SHUTDOWN_DB_CONNECT_TIMEOUT_SECONDS = 5


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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
            # A strict decoder raises UnicodeDecodeError on one bad byte,
            # which kills this thread silently: the child then blocks
            # writing into a pipe nobody drains, `running` stays True
            # forever, and every future fire time is skipped with only a
            # warning (fix round 1, Important 4).
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        log.info(f"[{self.name}] started pid={self.proc.pid}")

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                log.info(f"[{self.name}] {line.rstrip()}")
        except Exception:
            # See the errors="replace" note above: this thread must not be
            # able to die without a trace (fix round 1, Important 4).
            log.exception(f"[{self.name}] output drain crashed")

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

    def signal_stop(self) -> None:
        """SIGTERM without waiting, so a fleet can be signalled all at once.

        Split out of `terminate` because waiting per child serialises the whole
        shutdown: the caller broadcasts this to every child, then waits once
        against a single deadline (see `shutdown`).
        """
        if not self.running:
            return
        assert self.proc is not None
        self.proc.terminate()

    def kill(self) -> bool:
        """SIGKILL if still running. Returns whether a kill was actually sent."""
        if not self.running:
            return False
        assert self.proc is not None
        log.warning(f"[{self.name}] did not exit in time; killing")
        self.proc.kill()
        return True

    def terminate(self, grace: float = 20.0) -> None:
        """SIGTERM, wait, then SIGKILL — so a host restart mid-run is safe."""
        if not self.running:
            return
        self.signal_stop()
        if self.wait(timeout=grace) is None and self.kill():
            self.wait(timeout=5)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


@dataclass
class ResidentPool:
    """Keeps the resident children up: at most one reap or one start per tick.

    Reaping and starting are deliberately different ticks. Doing both in one
    could not work: the backoff was measured from the same `now` it was then
    compared against, so the start never fired on the crash tick, and the dead
    Child stayed in the dict — every later tick re-entered the reap branch, the
    failure count climbed, the crash-loop alert re-fired, and the backoff was
    pushed further out each time. Past four failures the delay exceeded one
    tick and the start line became permanently unreachable: the Telegram bot,
    which is the operator's only control channel, stayed dead while an alert
    went out every 30 seconds forever (fix round 2, Critical 1).

    So a reap removes the child and only records when the next start becomes
    due; a later tick, observing the elapsed backoff against a fresher `now`,
    starts a new one.
    """

    tracker: RestartTracker = field(default_factory=RestartTracker)
    children: dict[str, Child] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    started_at: dict[str, float] = field(default_factory=dict)
    next_start: dict[str, float] = field(default_factory=dict)

    def tick(self, now_mono: float, spawn=None) -> None:
        """One pass over RESIDENT_MODES. `spawn` is injectable for tests."""
        spawn = spawn or Child
        for mode in RESIDENT_MODES:
            child = self.children.get(mode)
            if child is not None and child.running:
                self._note_stability(mode, now_mono)
                continue
            if child is not None:
                self._reap(mode, child, now_mono)
                continue
            if now_mono >= self.next_start.get(mode, 0.0):
                fresh = spawn(mode)
                fresh.start()
                self.children[mode] = fresh
                self.started_at[mode] = now_mono

    def _note_stability(self, mode: str, now_mono: float) -> None:
        """Clear the failure count only once a child has proved it stays up.

        Resetting at spawn instead cleared it before a real crash loop could
        ever grow it past CRASH_LOOP_LIMIT, so the backoff ceiling was
        unreachable (fix round 2, Minor 5).
        """
        if now_mono - self.started_at.get(mode, now_mono) >= RESIDENT_STABLE_SECONDS:
            self.failures[mode] = 0

    def _reap(self, mode: str, child: Child, now_mono: float) -> None:
        code = child.wait(timeout=0)
        # Drain what the child wrote on its way out; without this its last
        # lines — usually the traceback that says why it died — are dropped.
        child.join_reader(timeout=5)
        del self.children[mode]
        self.started_at.pop(mode, None)
        self.failures[mode] = self.failures.get(mode, 0) + 1
        delay = backoff_delay(self.failures[mode])
        self.next_start[mode] = now_mono + delay
        log.warning(f"[{mode}] exited with {code}; restarting in {delay:.0f}s")
        if self.tracker.record(mode):
            telegram_alert(
                f"{mode} has restarted {CRASH_LOOP_LIMIT} times in "
                f"{CRASH_LOOP_WINDOW_SECONDS // 60} minutes — crash loop"
            )


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

    The connection is always closed before returning (fix round 1, Critical 2):
    left open it sits idle-in-transaction, holding ACCESS SHARE on job_runs for
    the life of the process — which blocks any later ALTER TABLE indefinitely,
    in a system whose whole premise is that the schema gets revised.
    """
    migrate = migrate or db.run_migrations
    connect = connect or db.connect
    conn = None
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                # Cleanup must not be able to replace the real return value
                # with an exception of its own.
                log.exception("Failed to close the startup connection")


EX_ORPHANED = -2  # a run whose supervisor died before it could be closed


def _closing_status(exit_code: int | None) -> tuple[int, str]:
    """How a run is recorded from its exit code.

    The supervisor is authoritative for the children it spawned: it has the exit
    code even when the child was OOM-killed or SIGKILLed and could therefore
    write nothing itself — which is the failure class that is invisible today
    (spec section 8). A missing exit code is recorded as -1, not as success.

    A refusal closes as 'missed', not 'finished': the child never ran, and a
    'finished' row would let /jobs report work that did not happen.
    """
    code = -1 if exit_code is None else exit_code
    return code, ("missed" if code == EX_ALREADY_RUNNING else "finished")


def _close_run(run_id: int | None, exit_code: int | None) -> None:
    """Close one ledger row on its own connection."""
    if run_id is None:
        return
    code, status = _closing_status(exit_code)
    try:
        with db.connect() as conn:
            db.finish_run(conn, run_id, code, status=status)
    except Exception:
        log.exception(f"Could not close run {run_id}")


def _close_runs(outcomes: list[tuple[int | None, int | None]]) -> None:
    """Close every row a shutdown owns, on ONE connection with a bounded connect.

    `_close_run` per child would open a connection per child, and each
    `db.connect()` blocks on libpq's own connect timeout — *outside* any wait
    budget. With the database slow or gone, four children meant four
    unbounded stalls, so a shutdown that is bounded on paper is not bounded at
    all. One connection, one bound.

    A row that cannot be closed is left to the next boot's `reclaim_orphans`,
    which is what it is for; the failure is logged so the resulting alert is
    attributable rather than mysterious.
    """
    pending = [(rid, code) for rid, code in outcomes if rid is not None]
    if not pending:
        return
    try:
        with db.connect(connect_timeout=SHUTDOWN_DB_CONNECT_TIMEOUT_SECONDS) as conn:
            for run_id, exit_code in pending:
                code, status = _closing_status(exit_code)
                try:
                    db.finish_run(conn, run_id, code, status=status)
                except Exception:
                    # One bad row must not cost the others their close.
                    log.exception(f"Could not close run {run_id}")
    except Exception:
        log.exception(
            "Could not close runs "
            + ", ".join(str(rid) for rid, _ in pending)
            + " on shutdown; the next boot will reclaim them as orphans"
        )


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
            # `decide` returns previous_fire(), which is by construction <= now,
            # so a bare `now > scheduled_for` labelled EVERY run a catchup and
            # the ledger never recorded a 'scheduled' one — the column stopped
            # distinguishing anything (fix round 2, Important 2). A polled
            # scheduler always observes a fire time a few seconds late, so the
            # honest threshold is the poll interval that made it late.
            lateness = now - decision.scheduled_for
            trigger = (
                "catchup"
                if lateness > timedelta(seconds=scheduler.TICK_SECONDS)
                else "scheduled"
            )
            ready.append((spec, decision.scheduled_for, trigger))
        elif decision.action == "missed":
            log.warning(f"[{spec.job}] {decision.reason}")
            db.record_missed(conn, spec.job, decision.scheduled_for)
    return ready


def shutdown(jobs: dict[str, Child], residents: dict[str, Child]) -> None:
    """Stop every child and close the ledger rows this supervisor owns.

    Terminating a job child without closing its row left it `running` for the
    next boot's `reclaim_orphans`, so every ordinary `compose down` produced an
    "Orphaned by a restart and NOT retried" alert — which trains the operator to
    ignore the one alert that matters (fix round 2, Important 3). A planned stop
    must leave no orphans.

    The row closes as `finished` carrying the signal-derived exit code (-15 for
    SIGTERM on Linux), not as `missed`: the child really did run, and the
    non-zero code says how it ended. `missed` stays reserved for work that never
    started at all. No alert fires here — a stop we asked for is not a failure.

    Two things about the ordering are deliberate, and both come from the fact
    that we may ourselves be SIGKILLed part-way through:

    * **Broadcast, then wait once.** Signalling and waiting per child made the
      worst case the sum of every child's grace; against a shared deadline it is
      the longest one.
    * **The ledger rows close BEFORE the escalation to SIGKILL.** The rows are
      the part of this function that nothing else will do for us; killing the
      children is something container teardown does anyway. Work ordered by what
      survives being interrupted, not by what is tidy.
    """
    children = list(jobs.values()) + list(residents.values())
    for child in children:
        child.signal_stop()

    deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
    for child in children:
        child.wait(timeout=_remaining(deadline))

    # Drain only the children that have exited: their pipes are at EOF, so this
    # returns at once. Joining one still running would spend the budget the
    # ledger writes need, on output that is about to be killed anyway.
    drain_deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    for child in children:
        if not child.running:
            child.join_reader(timeout=_remaining(drain_deadline))

    outcomes: list[tuple[int | None, int | None]] = []
    for mode, child in list(jobs.items()):
        code = child.wait(timeout=0)
        del jobs[mode]
        log.info(f"[{mode}] stopped for shutdown with {code}")
        outcomes.append((child.run_id, code))

    _close_runs(outcomes)

    killed = [child for child in children if child.kill()]
    reap_deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    for child in killed:
        child.wait(timeout=_remaining(reap_deadline))
        child.join_reader(timeout=_remaining(reap_deadline))


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

    pool = ResidentPool()
    jobs: dict[str, Child] = {}

    while not stopping.is_set():
        now_mono = time.monotonic()

        if state.residents_enabled:
            pool.tick(now_mono)

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

    shutdown(jobs, pool.children)
    log.info("Supervisor stopped")
    return 0

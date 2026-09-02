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

import claim_store
import config
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
# **docker-compose.yml's `stop_grace_period` must exceed the WHOLE of shutdown**,
# not just this number — the comment there says so too, because the pair is only
# safe while both are tuned together. Under Docker's 10s default the supervisor
# is SIGKILLed mid-shutdown, its `running` rows survive, and the next boot
# reports them as orphans: the Task 4 fix goes inert and every routine deploy
# fires the false alert that trains the operator to ignore the real one.
#
# Every phase is bounded, so the worst case is the sum of the bounds below and
# is written out once, here, since four separate constants cannot be audited
# apart:
#
#     25  this budget           children exit after the broadcast
#   +  2  SHUTDOWN_DRAIN        final output of whichever children did exit
#   +  5  DB_CONNECT_TIMEOUT    opening the one connection the rows close on
#   +  5  DB_STATEMENT_TIMEOUT  5 schedules x 2 statements x 0.5s
#   +  2  SHUTDOWN_DRAIN again  reaping whatever had to be SIGKILLed
#   ----
#     39  plus up to 5s for a tick-path connect already in flight when the
#         signal arrived, so ~44s
#
# This row scales with len(scheduler.SCHEDULES) and is the ONLY line here
# that does. Adding a schedule costs 1s of worst case; the 60s grace
# absorbs it, but if this table is ever left behind by the schedule list
# the next person reconciles a budget that does not add up and concludes
# the wrong thing.
#
# A mixed fleet reaches it: one child exits late with a grandchild holding its
# pipe open (the first drain), another ignores SIGTERM entirely (the reap).
#
# **The 60s stop_grace_period is deliberately more than this sum, not equal to
# it.** Two things the sum does not model: statement_timeout does NOT abort a
# COMMIT already blocked in fsync, which is the wedged-disk case the timeout was
# added for; and closing a connection whose socket is wedged is unbounded too.
# The extra ~17s is cover for what cannot be bounded, not slack to be reclaimed
# by someone re-deriving this table and finding it generous.
SHUTDOWN_BUDGET_SECONDS = 25.0

# Every connect the main loop makes must be bounded, not only shutdown's. libpq
# with no connect_timeout falls back to the OS TCP timeout — roughly two minutes
# — so a SIGTERM arriving while the loop is blocked in `db.connect` would not be
# acted on until long after Docker's SIGKILL, and every job row would stay
# `running` for the next boot to report as an orphan. That is the precise
# failure the shutdown work exists to prevent, reached by the one path that had
# not been hardened.
DB_CONNECT_TIMEOUT_SECONDS = 5
# A shared bound on draining the children's final output. Small on purpose: the
# log lines are worth having, but not at the cost of the ledger writes queued
# behind them, and a grandchild holding the pipe open would otherwise stall the
# drain for the whole remaining budget.
SHUTDOWN_DRAIN_SECONDS = 2.0
# Without this, closing a row against a stopped database blocks on libpq's own
# connect timeout — outside every budget above, which is how a "bounded"
# shutdown stops being bounded.
SHUTDOWN_DB_CONNECT_TIMEOUT_SECONDS = 5
# And the handshake is only half of it: a database that ACCEPTS the connection
# and then stalls — lock contention, a wedged disk — passes the connect timeout
# and hangs on the UPDATE instead. Its contribution to the worst case is in the
# arithmetic beside SHUTDOWN_BUDGET_SECONDS above.
#
# It is a partial bound, and the arithmetic above says so: statement_timeout
# does not abort a COMMIT already blocked in fsync, so on the very wedged-disk
# case this was added for it can be exceeded. That is why the grace period sits
# above the sum rather than on it.
#
# Half a second is three orders of magnitude more than an indexed single-row
# UPDATE needs, and the failure direction is the right way round: a timed-out
# row is left for the next boot's reclaim_orphans, which is what that exists
# for, whereas being SIGKILLed part-way loses EVERY remaining row.
SHUTDOWN_DB_STATEMENT_TIMEOUT_MS = 500


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
    _in_episode: set[str] = field(default_factory=set)

    def record(self, name: str, at: float | None = None) -> bool:
        """Record a restart; True only on ENTERING a crash-loop episode.

        Returning True on every restart at or past the limit meant one episode
        alerted once per restart — four times as the backoff ramps, and again
        whenever the window refills. An alert that repeats for a condition the
        operator has already been told about is how the channel stops being
        read, which is the same failure as the false orphan alert, slower.

        The episode ends when a restart lands with the window no longer full,
        so a genuinely new crash loop later still alerts.
        """
        at = time.monotonic() if at is None else at
        events = [t for t in self._events.get(name, []) if at - t < self.window_seconds]
        events.append(at)
        self._events[name] = events
        if len(events) < self.limit:
            self._in_episode.discard(name)
            return False
        if name in self._in_episode:
            return False
        self._in_episode.add(name)
        return True


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
            # No child of this supervisor is interactive, and saying so is
            # load-bearing twice over. Left unset, Popen INHERITS the
            # supervisor's own stdin, which it must first duplicate — and a
            # handle the OS declines to duplicate turns an ordinary spawn into
            # an exception before the child exists (observed here as WinError
            # 50, intermittently, from the pytest runner's stdin). More
            # importantly a child that ever reads stdin would block forever on
            # an inherited handle nobody writes to, holding `running` in the
            # ledger and starving every later fire time: the exact hang the
            # interlock and the orphan reclaim are built to make impossible.
            # DEVNULL gives each child a fresh handle that reads EOF at once.
            stdin=subprocess.DEVNULL,
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

        Signalling and waiting are separate methods because doing both together
        serialises the whole shutdown: the caller broadcasts this to every
        child, then waits once against a single deadline (see `shutdown`).
        """
        if not self.running:
            return
        assert self.proc is not None
        self.proc.terminate()

    def kill(self) -> bool:
        """SIGKILL if still running. Returns whether a kill was actually sent.

        There is deliberately no `terminate(grace)` helper pairing these two.
        One existed and had no caller left once `shutdown` started broadcasting:
        an unreferenced "just stop this child" method sitting beside a shutdown
        path whose ORDERING is the whole point is the thing a future reader
        reaches for by mistake, and using it would serialise the budget again
        and close the ledger rows after the SIGKILL instead of before.
        """
        if not self.running:
            return False
        assert self.proc is not None
        log.warning(f"[{self.name}] did not exit in time; killing")
        self.proc.kill()
        return True

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
    spawn_alerted: set[str] = field(default_factory=set)

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
                self._start(mode, spawn, now_mono)

    def _start(self, mode: str, spawn, now_mono: float) -> None:
        """Start one resident. A spawn failure must not escape this method.

        `Popen` can fail for reasons that have nothing to do with the child —
        fork failing under memory pressure, the interpreter path gone after an
        image change. Unguarded, that exception propagated out of `tick` and out
        of `serve`, killing the supervisor with no alert and orphaning every
        running job child. Under `restart: unless-stopped` the container then
        comes straight back and does it again: a silent crash loop, in the
        process whose entire remit is making failures visible.
        """
        fresh = spawn(mode)
        try:
            fresh.start()
        except Exception as e:
            log.exception(f"[{mode}] failed to spawn")
            if mode not in self.spawn_alerted:
                # Once per episode, for the same reason RestartTracker alerts
                # once: the backoff means this would otherwise repeat forever.
                self.spawn_alerted.add(mode)
                telegram_alert(f"{mode} failed to spawn: {type(e).__name__}: {e}")
            self.failures[mode] = self.failures.get(mode, 0) + 1
            self.next_start[mode] = now_mono + backoff_delay(self.failures[mode])
            return
        self.spawn_alerted.discard(mode)
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
        # Identity before work: every job that delivers resolves its target
        # through `config`, so an unseeded database would fail each of them
        # separately and late. Inside the try on purpose — a failure here means
        # we do not know who this deployment serves, which is fail-closed on
        # work and fail-open on the bot, exactly like a failed migration.
        config.ensure_seeded(conn)
        config.import_settings_from_env(conn)
        config.import_sources_from_file(conn)
        config.import_preferences_from_file(conn)
        config.import_state_from_file(conn)
        claim_store.import_legacy(conn)
        try:
            reclaim_orphans(conn)
        except Exception:
            # Reclaim is hygiene, not a precondition: failing it must not stop
            # the supervisor from doing today's work.
            log.exception("Orphan reclaim failed; continuing")
        # Seeding is NOT hygiene and is deliberately not wrapped: if it fails,
        # the very next tick can run a second collect for a day host cron
        # already ran, down the live trading path. Letting it raise into the
        # handler below disables jobs and alerts, which is the fail-closed-on-
        # work half of this function's contract.
        seed_first_boot(conn)
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
EX_SHUTDOWN_KILLED = -3  # a run still going at the shutdown deadline, SIGKILLed
EX_QUEUE_EXPIRED = -4  # a manual request that went stale before it was claimed
EX_UNKNOWN_JOB = -5  # a queued row naming a job no schedule defines

# What a negative exit code means, in the words /jobs shows the operator. Kept
# beside the constants themselves: a copy in brief.py would be a second source
# of truth for numbers that only ever appear here.
EXIT_GLOSS = {
    EX_ALREADY_RUNNING: "refused, another run held the lock",
    EX_ORPHANED: "orphaned by a restart",
    EX_SHUTDOWN_KILLED: "killed at the shutdown deadline",
    EX_QUEUE_EXPIRED: "request expired before it was claimed",
    EX_UNKNOWN_JOB: "no such job",
    -1: "killed, or it never reported",
}

# How long a /run request may sit unclaimed before it is discarded instead of
# run. Long enough to outwait a collect that is already going (they run for
# minutes, and the request waits for the lock rather than racing it); short
# enough that a request made while jobs were disabled cannot surface hours later
# as a job nobody is expecting. There is no reading that makes this exact.
MANUAL_QUEUE_TTL_SECONDS = 15 * 60


def seed_first_boot(conn, now: datetime | None = None) -> list[str]:
    """Consume each job's current fire time, once, on its FIRST EVER boot.

    Without this the cutover double-runs. On first boot `job_runs` is empty, so
    `latest_scheduled_for` is None, `decide` never reaches its already-recorded
    branch, and EVERY schedule still inside its grace window fires immediately.
    Deploy between 06:00 and 08:00 UTC and the supervisor runs a second collect
    on top of the one host cron already ran that morning; monitor's 15-minute
    grace means any deploy in the first quarter of ANY hour re-runs `monitor`,
    which calls `trading.sweep_live_exits` and
    `polygram_live.reconcile_live_book` — the live sell path, with real money.

    An empty ledger cannot distinguish "host cron already ran this" from
    "genuinely missed", so we assume the former exactly once, per job, and let
    ordinary scheduling take over from the next fire time. It is recorded
    through `record_missed` rather than a new writer, so the row is honest about
    what happened: the fire time passed and nothing ran.

    The check is per JOB, not on the table as a whole. Whole-table emptiness
    would break the day a fifth schedule is added — the table is not empty, the
    new job gets no seed, and this defect comes back for that job alone. Per-job
    also makes it idempotent if a crash lands mid-seed.

    It tests `latest_scheduled_for(...) is None` — the exact value `decide`
    consumes — and not merely "does a row exist". The two diverge for a job
    whose only history is manual runs, which record a NULL `scheduled_for`:
    row-existence calls that job touched and skips the seed, while `decide`
    still sees None and fires it. Seeding on the weaker predicate re-opens the
    hole it exists to close. The reverse error is cheap by comparison: a
    manual-only job looks untouched, gets seeded, and loses one catch-up.
    Whatever gates firing must be what gates seeding.

    One legitimate catch-up is suppressed: host down across 06:00, stack up at
    07:00, no collect. That is the safe direction, it is visible in the ledger
    as `missed`, and `docker compose run --rm newsbrief collect` recovers it.
    """
    now = now or datetime.now(timezone.utc)
    seeded: list[str] = []
    for spec in scheduler.SCHEDULES:
        if db.latest_scheduled_for(conn, spec.job) is not None:
            continue
        fire = scheduler.previous_fire(spec, now)
        db.record_missed(conn, spec.job, fire)
        seeded.append(spec.job)
        log.warning(
            f"[{spec.job}] first boot: recording {fire.isoformat()} as missed "
            f"rather than running it, in case another entry path already did"
        )
    return seeded


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
        with db.connect(connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
            db.finish_run(conn, run_id, code, status=status)
    except Exception:
        log.exception(f"Could not close run {run_id}")


def _close_runs(outcomes: list[tuple[int | None, int | None]]) -> None:
    """Close every row a shutdown owns, on ONE bounded connection.

    `_close_run` per child would open a connection per child, and each
    `db.connect()` blocks on libpq's own connect timeout — *outside* any wait
    budget. With the database slow or gone, four children meant four
    unbounded stalls, so a shutdown that is bounded on paper is not bounded at
    all. One connection, and both bounds on it: a database that accepts the
    connection and then stalls would otherwise hang here past the container's
    stop grace and be SIGKILLed, leaving exactly the `running` rows this
    function exists to prevent.

    A row that cannot be closed is left to the next boot's `reclaim_orphans`,
    which is what it is for; the failure is logged so the resulting alert is
    attributable rather than mysterious.
    """
    pending = [(rid, code) for rid, code in outcomes if rid is not None]
    if not pending:
        return
    try:
        with db.connect(
            connect_timeout=SHUTDOWN_DB_CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={SHUTDOWN_DB_STATEMENT_TIMEOUT_MS}",
        ) as conn:
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


def _manual_jobs(conn, now: datetime) -> list[tuple[scheduler.Schedule, int]]:
    """Queued /run requests worth claiming. Stale and bogus rows are closed here.

    /run writes a row rather than spawning, because the commands daemon is a
    child of this process and cannot fork a job. Two kinds of row must never
    reach a spawn: one naming a job no schedule defines, and one that has sat
    unclaimed past its TTL — a request made while jobs were disabled, or queued
    behind a long collect, which would otherwise fire hours later as a job
    nobody is expecting.

    Both close as `missed` rather than `finished`, so /jobs cannot report work
    that never happened, and they close with DIFFERENT exit codes: aging out a
    row for a job that does not exist would eventually stop it too, but it would
    tell the operator the request went stale when the truth is that he mistyped
    the name. A wrong reason is worse than a late one.
    """
    by_name = {spec.job: spec for spec in scheduler.SCHEDULES}
    ready: list[tuple[scheduler.Schedule, int]] = []
    for row in db.queued_runs(conn):
        run_id, job = row["id"], row["job_name"]
        spec = by_name.get(job)
        if spec is None:
            db.finish_run(conn, run_id, EX_UNKNOWN_JOB, status="missed")
            log.error(f"[{job}] queued run {run_id} names no known job; discarded")
            telegram_alert(
                f"A run was queued for '{job}', which is not a job. Discarded. "
                f"Known jobs: {', '.join(sorted(by_name))}"
            )
            continue
        waited = (now - row["created_at"]).total_seconds()
        if waited > MANUAL_QUEUE_TTL_SECONDS:
            db.finish_run(conn, run_id, EX_QUEUE_EXPIRED, status="missed")
            log.warning(
                f"[{job}] queued run {run_id} waited {int(waited)}s, past the "
                f"{MANUAL_QUEUE_TTL_SECONDS}s limit; discarded rather than run late"
            )
            telegram_alert(
                f"/run {job} sat unclaimed for {int(waited // 60)}m and was "
                f"discarded rather than started this long after it was asked for"
            )
            continue
        ready.append((spec, run_id))
    return ready


def _spawn_job(
    job: str,
    run_id: int,
    env: dict[str, str],
    jobs: dict[str, "Child"],
    not_retried: str,
) -> None:
    """Start one job child and register it, or close its row and say why not.

    The ledger row exists BEFORE this is called on both paths — written by
    `start_run` for a scheduled fire time, claimed out of `queued` for a manual
    one. So a spawn failure has already consumed the thing that would have
    caused a retry. That is deliberate: it is what stops a job that cannot start
    respawning on every tick for a whole grace window. It is also exactly why
    the failure cannot be left silent, hence `not_retried`, which says in the
    alert what the operator has lost.
    """
    child = Child(job, env=env)
    child.run_id = run_id
    try:
        child.start()
    except Exception as e:
        log.exception(f"[{job}] failed to spawn")
        telegram_alert(
            f"{job} failed to spawn: {type(e).__name__}: {e} — {not_retried}"
        )
        _close_run(run_id, -1)
        return
    jobs[job] = child


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
        # Still running at the deadline: we are about to SIGKILL it, and that is
        # a different fact from "we never learned the exit code". Recording both
        # as -1 left /jobs unable to tell a planned stop from an unknown
        # outcome, which is the distinction an operator reads the column for.
        outcomes.append((child.run_id, EX_SHUTDOWN_KILLED if code is None else code))

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
    # Fire times already alerted as skipped, per job. Keyed by fire time so a
    # hang spanning several of monitor's hours reports each one, and cleared
    # when the job finally exits so it cannot grow without bound.
    skipped_alerts: dict[str, set[str]] = {}

    while not stopping.is_set():
        now_mono = time.monotonic()

        if state.residents_enabled:
            pool.tick(now_mono)

        for mode, child in list(jobs.items()):
            if stopping.is_set():
                # Leave the rest to `shutdown`, which closes every remaining row
                # on ONE bounded connection. Carrying on here would spend a
                # connect timeout per child before shutdown is even reached, and
                # that time comes out of the container's stop grace.
                break
            if child.running:
                continue
            code = child.wait(timeout=0)
            child.join_reader(timeout=5)
            del jobs[mode]
            skipped_alerts.pop(mode, None)
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

        if state.jobs_enabled and not stopping.is_set():
            try:
                with db.connect(connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
                    for spec, scheduled_for, trigger in _due_jobs(
                        conn, datetime.now(timezone.utc)
                    ):
                        if spec.job in jobs:
                            # A job that outlives its own fire time is a hang,
                            # and it was log-only: the fire time is silently
                            # dropped, so a wedged collect looks exactly like a
                            # quiet news day. Once per skipped fire time, not
                            # once per 30-second tick.
                            stamp = scheduled_for.isoformat()
                            log.warning(
                                f"[{spec.job}] still running from a previous fire "
                                f"time; skipping {stamp}"
                            )
                            if stamp not in skipped_alerts.setdefault(spec.job, set()):
                                skipped_alerts[spec.job].add(stamp)
                                telegram_alert(
                                    f"{spec.job} is still running from an earlier "
                                    f"fire time, so {stamp} was skipped and will "
                                    f"not be retried"
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
                        _spawn_job(
                            spec.job,
                            run_id,
                            {
                                "NEWSBRIEF_SCHEDULED_FOR": scheduled_for.isoformat(),
                                "NEWSBRIEF_TRIGGER": trigger,
                                "NEWSBRIEF_RUN_ID": str(run_id),
                            },
                            jobs,
                            "not retried until the next scheduled run",
                        )

                    # Manual requests come after the due ones so an operator
                    # asking for a run can never starve a scheduled fire time.
                    for spec, run_id in _manual_jobs(conn, datetime.now(timezone.utc)):
                        if spec.job in jobs:
                            # Unlike a fire time, a manual request is not
                            # consumed by being skipped: the row stays queued
                            # and a later tick reconsiders it once the running
                            # child exits. The TTL is what stops it waiting
                            # forever, so this needs no alert of its own.
                            continue
                        if not db.claim_queued(conn, run_id):
                            # The row left `queued` between the read and here.
                            # Spawning anyway is the double run the status
                            # predicate on the UPDATE exists to prevent.
                            log.warning(
                                f"[{spec.job}] queued run {run_id} was no longer "
                                f"claimable; not spawning"
                            )
                            continue
                        _spawn_job(
                            spec.job,
                            run_id,
                            {
                                "NEWSBRIEF_TRIGGER": "manual",
                                "NEWSBRIEF_RUN_ID": str(run_id),
                            },
                            jobs,
                            "the request is discarded, not retried",
                        )
            except Exception:
                log.exception("Scheduler tick failed; continuing")

        stopping.wait(scheduler.TICK_SECONDS)

    shutdown(jobs, pool.children)
    log.info("Supervisor stopped")
    return 0

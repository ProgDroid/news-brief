"""Supervisor process handling, with fake commands rather than real modes.

Real modes need an API key, a network and hours; none of that tests spawn,
reap, drain, backoff or fail-open startup, which is all this module does.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import claim_store
import config
import supervisor


def _record_boot_steps(monkeypatch) -> list:
    """Stub every `config` (and `claim_store`) call `startup` makes, recording
    (name, conn) pairs.

    One helper rather than a stub per test: `startup` has gained a config step
    per phase-2 child, and each one broke these tests until it was stubbed
    individually. A test that only cares about `seed_first_boot` should not need
    editing again when the next importer lands — it should need editing only if
    the list below is what it is asserting about.

    Individual tests override an entry afterwards to make it raise.
    """
    calls: list = []
    for name in (
        ("ensure_seeded", "operator"),
        ("import_settings_from_env", "settings"),
        ("import_sources_from_file", "sources"),
        ("import_preferences_from_file", "preferences"),
        ("import_state_from_file", "state"),
    ):
        attr, label = name
        monkeypatch.setattr(
            config, attr, lambda c, _l=label: calls.append((_l, c)) or 0
        )
    monkeypatch.setattr(
        claim_store, "import_legacy", lambda c: calls.append(("claims", c)) or 0
    )
    return calls


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


def test_wait_times_out_then_signal_stop_stops_the_child():
    child = supervisor.Child("slow", _fake(0, sleep=30))
    child.start()
    assert child.wait(timeout=0.5) is None, "still running, so no exit code yet"
    child.signal_stop()
    assert child.wait(timeout=10) is not None
    assert child.kill() is False, "nothing to escalate to once the child is gone"


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
    """A Child stand-in: no subprocess, so a tick loop can be driven by hand.

    `stubborn` models a child that ignores SIGTERM — the case that decides
    whether shutdown fits inside the container's stop grace. `ops` records the
    call order into a shared list when ordering is what is under test.
    `wait_blocks` makes `wait` actually consume the timeout it is handed, the
    way a process that is not going to exit does; `waits` records those
    timeouts, which is how a shared deadline is told from a per-child one.
    """

    def __init__(
        self,
        name,
        argv=None,
        env=None,
        stubborn=False,
        ops=None,
        wait_blocks=False,
        waits=None,
    ):
        self.name = name
        self.run_id = None
        self.alive = True
        self.exit_code = 7
        self.started = False
        self.joined = 0
        self.signalled = 0
        self.killed = 0
        self.stubborn = stubborn
        self.ops = ops
        self.wait_blocks = wait_blocks
        self.waits = waits

    def _record(self, kind):
        if self.ops is not None:
            self.ops.append((kind, self.name))

    def start(self):
        self.started = True

    @property
    def running(self):
        return self.alive

    def wait(self, timeout=None):
        self._record("wait")
        if self.waits is not None:
            self.waits.append(timeout)
        if self.alive and self.wait_blocks and timeout:
            time.sleep(timeout)
        return None if self.alive else self.exit_code

    def join_reader(self, timeout=5.0):
        self.joined += 1

    def signal_stop(self):
        self.signalled += 1
        self._record("signal")
        if not self.stubborn:
            self.alive = False

    def kill(self):
        if not self.alive:
            return False
        self.killed += 1
        self._record("kill")
        self.alive = False
        return True


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
        supervisor, "_close_runs", lambda outcomes: closed.extend(outcomes)
    )
    job = _FakeChild("collect")
    job.run_id = 41
    job.exit_code = -15
    resident = _FakeChild("commands")
    jobs = {"collect": job}
    residents = {"commands": resident}

    supervisor.shutdown(jobs, residents)

    assert closed == [(41, -15)], "a planned stop must leave no orphan row"
    assert job.signalled == 1 and resident.signalled == 1
    assert job.joined == 1, "the child's final output must be drained, not dropped"
    assert job.killed == 0, "a child that honoured SIGTERM must not be SIGKILLed"
    assert jobs == {}, "a stopped job must not stay in the live set"


def test_shutdown_broadcasts_first_and_closes_rows_before_it_escalates(monkeypatch):
    """The ordering, pinned with children that ignore SIGTERM.

    Both other shutdown tests use children that die instantly, so neither ever
    reaches the deadline or the SIGKILL — a regression here would pass them
    both. Serially this was terminate(grace=20) + wait(5) + join_reader(5) per
    child, for up to four schedules: well past any plausible
    `stop_grace_period`, so Docker's SIGKILL landed BEFORE the ledger rows were
    closed and the next boot called an ordinary deploy an orphan.
    """
    ops = []
    monkeypatch.setattr(
        supervisor, "_close_runs", lambda outcomes: ops.append(("close", outcomes))
    )
    collect = _FakeChild("collect", stubborn=True, ops=ops)
    collect.run_id = 41
    submit = _FakeChild("submit", stubborn=True, ops=ops)
    submit.run_id = 42
    resident = _FakeChild("commands", stubborn=True, ops=ops)
    jobs = {"collect": collect, "submit": submit}

    supervisor.shutdown(jobs, {"commands": resident})

    kinds = [kind for kind, _ in ops]
    last_signal = max(i for i, k in enumerate(kinds) if k == "signal")
    assert kinds.index("wait") > last_signal, (
        "every child must be signalled before the first wait, or the budget is "
        "spent one child at a time"
    )
    assert kinds.index("close") < kinds.index("kill"), (
        "the ledger rows are what nothing else will do for us; killing children "
        "is what teardown does anyway"
    )
    K = supervisor.EX_SHUTDOWN_KILLED
    assert ops[kinds.index("close")][1] == [(41, K), (42, K)], (
        "a child still running when the budget expires is about to be SIGKILLed, "
        "which is its own fact — not -1's 'we never learned the exit code'"
    )
    assert collect.killed == 1 and submit.killed == 1 and resident.killed == 1
    assert jobs == {}


def test_the_shutdown_deadline_is_shared_across_children_not_per_child(monkeypatch):
    """Two children, because one child cannot tell the two apart.

    The ordering test above uses fakes whose `wait` returns instantly, and the
    interlock suite's stubborn-child test has a single child — so regressing
    `_remaining(deadline)` back to a flat SHUTDOWN_BUDGET_SECONDS per child
    passes both. This is the test that fails: the wall clock doubles, and the
    second child is handed a whole fresh budget instead of what is left of the
    shared one.
    """
    monkeypatch.setattr(supervisor, "SHUTDOWN_BUDGET_SECONDS", 1.0)
    monkeypatch.setattr(supervisor, "_close_runs", lambda outcomes: None)
    waits = []
    collect = _FakeChild("collect", stubborn=True, wait_blocks=True, waits=waits)
    submit = _FakeChild("submit", stubborn=True, wait_blocks=True, waits=waits)

    started = time.monotonic()
    supervisor.shutdown({"collect": collect, "submit": submit}, {})
    elapsed = time.monotonic() - started

    assert waits[0] >= 0.9, "the first child gets the budget, near enough all of it"
    assert waits[1] < 0.2, (
        "the second gets what is LEFT of the shared deadline, not a fresh budget"
    )
    assert elapsed < 1.6, "two children must not cost two budgets"


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


def test_a_crash_loop_alerts_once_per_episode_not_once_per_restart():
    """An alert that repeats for a condition already reported is how the channel
    stops being read. Past the limit every restart used to return True, so one
    episode alerted four times as the backoff ramped."""
    tracker = supervisor.RestartTracker(limit=3, window_seconds=60)
    now = time.monotonic()
    trips = [tracker.record("commands", now + i) for i in range(6)]
    assert trips == [False, False, True, False, False, False]

    # A restart landing with the window no longer full ends the episode, so a
    # genuinely new crash loop later is still reported.
    assert tracker.record("commands", now + 1000) is False
    later = [tracker.record("commands", now + 1000 + i) for i in range(1, 3)]
    assert later == [False, True]


def test_a_resident_that_cannot_be_spawned_alerts_instead_of_killing_serve():
    """Popen can fail for reasons unrelated to the child. Unguarded that
    propagated out of tick and out of serve, killing the supervisor with no
    alert and orphaning every running job child — and under
    restart: unless-stopped the container came back and did it again."""
    alerts = []
    pool = supervisor.ResidentPool()

    def explode(mode):
        child = _FakeChild(mode)
        child.start = lambda: (_ for _ in ()).throw(OSError("fork: cannot allocate"))
        return child

    original = supervisor.telegram_alert
    supervisor.telegram_alert = alerts.append
    try:
        pool.tick(0.0, spawn=explode)
        assert pool.children == {}, "a child that never started must not be tracked"
        assert len(alerts) == 1 and "failed to spawn" in alerts[0]

        # Backed off, and silent on the retry: one alert per episode.
        pool.tick(supervisor.backoff_delay(1) + 1, spawn=explode)
        assert len(alerts) == 1, "the backoff would repeat this alert forever"

        # And a spawn that finally works clears the episode.
        spawned = []
        pool.tick(1000.0, spawn=_spawner(spawned))
        assert len(spawned) == 1 and pool.children["commands"] is spawned[0]
        assert pool.spawn_alerted == set()
    finally:
        supervisor.telegram_alert = original


class _StopOnNthConnect:
    """Drives serve() for a fixed number of ticks, then trips its SIGTERM."""

    def __init__(self, handlers, stop_after):
        self.handlers = handlers
        self.stop_after = stop_after
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) >= self.stop_after:
            self.handlers[supervisor.signal.SIGTERM](supervisor.signal.SIGTERM, None)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve_harness(monkeypatch, stop_after=1, jobs_enabled=True):
    handlers = {}
    monkeypatch.setattr(
        supervisor.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn)
    )
    monkeypatch.setattr(
        supervisor,
        "startup",
        lambda: supervisor.StartupState(
            jobs_enabled=jobs_enabled, residents_enabled=False
        ),
    )
    monkeypatch.setattr(supervisor.scheduler, "TICK_SECONDS", 0.01)
    connect = _StopOnNthConnect(handlers, stop_after)
    monkeypatch.setattr(supervisor.db, "connect", connect)
    return connect


def test_the_tick_path_connects_with_a_bounded_timeout(monkeypatch):
    """Only the shutdown path was hardened. libpq with no connect_timeout falls
    back to the OS TCP timeout (~2 minutes), so a SIGTERM arriving while the
    loop is blocked in db.connect is not acted on until long after Docker's
    SIGKILL — and every job row stays `running` for the next boot to call an
    orphan, which is the exact failure the shutdown work exists to prevent."""
    connect = _serve_harness(monkeypatch)
    monkeypatch.setattr(supervisor, "_due_jobs", lambda conn, now: [])

    assert supervisor.serve() == 0
    assert connect.calls, "the tick opened no connection at all"
    assert all(c.get("connect_timeout") for c in connect.calls), (
        f"every tick-path connect must be bounded; got {connect.calls}"
    )


def test_a_job_still_running_at_its_next_fire_time_alerts_once(monkeypatch):
    """A hung job was log-only: the fire time is dropped silently, so a wedged
    collect looks exactly like a quiet news day. Once per skipped fire time —
    not once per 30-second tick, which is the same alert fatigue by another
    route."""
    import scheduler

    alerts = []
    connect = _serve_harness(monkeypatch, stop_after=4)
    monkeypatch.setattr(supervisor, "telegram_alert", alerts.append)
    monkeypatch.setattr(supervisor, "_close_runs", lambda outcomes: None)
    monkeypatch.setattr(supervisor.db, "start_run", lambda *a, **k: 7)
    monkeypatch.setattr(supervisor, "Child", _FakeChild)

    spec = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    fire = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        supervisor, "_due_jobs", lambda conn, now: [(spec, fire, "scheduled")]
    )

    assert supervisor.serve() == 0
    assert len(connect.calls) >= 3, "needs a spawn tick and two skip ticks"
    assert len(alerts) == 1, f"one alert per skipped fire time, got {alerts}"
    assert "still running" in alerts[0] and fire.isoformat() in alerts[0]


def test_startup_seeds_the_first_boot(monkeypatch):
    """Pins the CALL, not just the function.

    Every other seeding test invokes `seed_first_boot` directly, and the only
    other `startup` test raises inside `migrate` and returns before reaching it.
    Between them the call itself was unpinned: it could be deleted, reordered
    ahead of `migrate`, or wrapped in a swallowing `try` with the suite still
    green — and the first boot after that would run a second `collect` down the
    live trading path.
    """
    conn = object()
    calls = _record_boot_steps(monkeypatch)
    monkeypatch.setattr(
        supervisor, "reclaim_orphans", lambda c: calls.append("reclaim")
    )
    monkeypatch.setattr(
        supervisor, "seed_first_boot", lambda c: calls.append(("seed", c))
    )

    state = supervisor.startup(
        migrate=lambda c: calls.append("migrate"), connect=lambda: conn
    )

    assert state.jobs_enabled is True
    assert ("seed", conn) in calls, "startup must seed, on the connection it migrated"
    assert calls.index("migrate") < calls.index(("seed", conn)), (
        "seeding reads the table migrate creates"
    )
    # The operator seed is pinned here for the same reason and against the same
    # failure: unpinned, it could be deleted or reordered ahead of `migrate`
    # with the suite still green, and the first boot after that would leave
    # every delivering job with no target to resolve.
    assert ("operator", conn) in calls
    assert calls.index("migrate") < calls.index(("operator", conn)), (
        "the operator seed writes to the table migrate creates"
    )
    # Every first-boot importer is pinned the same way. They are the reason a
    # cutover keeps the operator's existing configuration instead of coming up
    # with an empty database that looks like a working one.
    for step in ("settings", "sources", "preferences", "state", "claims"):
        assert (step, conn) in calls, f"startup must import {step} on first boot"


def test_a_failed_seed_disables_jobs(monkeypatch):
    """Seeding is fail-CLOSED, unlike reclaim: it is the guard, not hygiene.

    If it is ever wrapped in the same forgiving try/except that protects orphan
    reclaim, the next tick runs the job the seed was meant to consume.
    """

    def boom(conn):
        raise RuntimeError("job_runs is gone")

    _record_boot_steps(monkeypatch)
    monkeypatch.setattr(supervisor, "reclaim_orphans", lambda c: None)
    monkeypatch.setattr(supervisor, "seed_first_boot", boom)
    monkeypatch.setattr(supervisor, "telegram_alert", lambda m: None)

    state = supervisor.startup(migrate=lambda c: None, connect=lambda: object())
    assert state.jobs_enabled is False
    assert "job_runs is gone" in state.reason


def test_a_failed_operator_seed_disables_jobs(monkeypatch):
    """Identity is fail-closed too, and for a sharper reason than the run ledger.

    Without an operator row every delivering job resolves its target and fails —
    separately, later, and one alert at a time. Failing the boot instead reports
    it once, in the place that knows why.
    """

    def boom(conn):
        raise RuntimeError("TELEGRAM_CHAT_ID is unset")

    alerts = []
    _record_boot_steps(monkeypatch)
    monkeypatch.setattr(supervisor, "reclaim_orphans", lambda c: None)
    monkeypatch.setattr(supervisor, "seed_first_boot", lambda c: [])
    monkeypatch.setattr(config, "ensure_seeded", boom)
    monkeypatch.setattr(supervisor, "telegram_alert", alerts.append)

    state = supervisor.startup(migrate=lambda c: None, connect=lambda: object())
    assert state.jobs_enabled is False
    assert "TELEGRAM_CHAT_ID is unset" in state.reason
    # Fail-open on the bot: the operator has to be able to hear about this.
    assert state.residents_enabled is True
    assert alerts and "TELEGRAM_CHAT_ID is unset" in alerts[0]


# ── Manual runs: the queue /run writes and the tick claims ───────────────────

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _queued(run_id, job, created_at):
    return {
        "id": run_id,
        "job_name": job,
        "scheduled_for": None,
        "trigger": "manual",
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "created_at": created_at,
    }


def _capture_closes(monkeypatch, closed):
    monkeypatch.setattr(
        supervisor.db,
        "finish_run",
        lambda conn, rid, code, status="finished": closed.append((rid, code, status)),
    )


def test_a_fresh_manual_request_is_offered_for_claiming(monkeypatch):
    monkeypatch.setattr(
        supervisor.db,
        "queued_runs",
        lambda conn: [_queued(1, "collect", NOW - timedelta(minutes=1))],
    )

    ready = supervisor._manual_jobs(None, NOW)

    assert [(spec.job, run_id) for spec, run_id in ready] == [("collect", 1)]


def test_a_manual_request_past_its_ttl_is_closed_rather_than_run(monkeypatch):
    """A request made while jobs were disabled, or behind a collect that ran
    long, must not fire hours later as a surprise. It is closed as `missed` —
    not `finished`, which would let /jobs claim work that never happened."""
    closed, alerts = [], []
    _capture_closes(monkeypatch, closed)
    monkeypatch.setattr(supervisor, "telegram_alert", alerts.append)
    monkeypatch.setattr(
        supervisor.db,
        "queued_runs",
        lambda conn: [_queued(1, "collect", NOW - timedelta(hours=3))],
    )

    ready = supervisor._manual_jobs(None, NOW)

    assert ready == []
    assert closed == [(1, supervisor.EX_QUEUE_EXPIRED, "missed")]
    assert len(alerts) == 1 and "collect" in alerts[0]


def test_the_ttl_boundary_still_runs(monkeypatch):
    monkeypatch.setattr(
        supervisor.db,
        "queued_runs",
        lambda conn: [
            _queued(
                1,
                "collect",
                NOW - timedelta(seconds=supervisor.MANUAL_QUEUE_TTL_SECONDS),
            )
        ],
    )

    assert len(supervisor._manual_jobs(None, NOW)) == 1


def test_a_queued_row_naming_an_unknown_job_is_closed_with_its_own_reason(monkeypatch):
    """Distinguishable from an expiry. Aging it out would work eventually, but
    the operator would be told the request went stale when in truth the job does
    not exist — a wrong reason is worse than a late one."""
    closed, alerts = [], []
    _capture_closes(monkeypatch, closed)
    monkeypatch.setattr(supervisor, "telegram_alert", alerts.append)
    monkeypatch.setattr(
        supervisor.db,
        "queued_runs",
        lambda conn: [_queued(1, "backfill", NOW - timedelta(minutes=1))],
    )

    ready = supervisor._manual_jobs(None, NOW)

    assert ready == []
    assert closed == [(1, supervisor.EX_UNKNOWN_JOB, "missed")]
    assert "backfill" in alerts[0]


class _RecordingChild(_FakeChild):
    """A _FakeChild that reports its spawn into a shared op list, so a claim can
    be shown to happen BEFORE the process exists."""

    ops = None

    def __init__(self, name, argv=None, env=None, **kw):
        super().__init__(name, argv, env, **kw)
        self.spawn_env = dict(env or {})

    def start(self):
        _RecordingChild.ops.append(("spawn", self.name, self.run_id, self.spawn_env))
        super().start()


def _manual_serve_harness(monkeypatch, ops, claim_result=True, stop_after=1):
    import scheduler

    connect = _serve_harness(monkeypatch, stop_after=stop_after)
    monkeypatch.setattr(supervisor, "telegram_alert", lambda msg: None)
    monkeypatch.setattr(supervisor, "_due_jobs", lambda conn, now: [])
    monkeypatch.setattr(supervisor, "_close_runs", lambda outcomes: None)
    monkeypatch.setattr(supervisor, "_close_run", lambda rid, code: None)
    monkeypatch.setattr(
        supervisor.db,
        "claim_queued",
        lambda conn, rid: (ops.append(("claim", rid)), claim_result)[1],
    )
    _RecordingChild.ops = ops
    monkeypatch.setattr(supervisor, "Child", _RecordingChild)
    spec = next(s for s in scheduler.SCHEDULES if s.job == "monitor")
    monkeypatch.setattr(supervisor, "_manual_jobs", lambda conn, now: [(spec, 42)])
    return connect


def test_a_queued_manual_run_is_claimed_before_the_child_is_spawned(monkeypatch):
    """Same ordering the scheduled path uses for start_run: the row moves out of
    `queued` first, so a tick that dies between the two leaves a row the orphan
    reclaim can close rather than a request that is spawned twice."""
    ops = []
    _manual_serve_harness(monkeypatch, ops)

    assert supervisor.serve() == 0
    assert [op[0] for op in ops] == ["claim", "spawn"]
    assert ops[0] == ("claim", 42)


def test_a_claimed_manual_child_carries_its_run_id_and_no_fire_time(monkeypatch):
    """scheduled_for is NULL on a manual row, so the child must not be handed
    one. brief.py already defaults it to None; this pins that the supervisor
    does not invent a value."""
    ops = []
    _manual_serve_harness(monkeypatch, ops)

    assert supervisor.serve() == 0
    _, name, run_id, env = ops[1]
    assert name == "monitor" and run_id == 42
    assert env["NEWSBRIEF_TRIGGER"] == "manual"
    assert env["NEWSBRIEF_RUN_ID"] == "42"
    assert "NEWSBRIEF_SCHEDULED_FOR" not in env


def test_a_row_that_lost_its_claim_is_not_spawned(monkeypatch):
    """claim_queued returning False means something else already took the row.
    Spawning anyway is the double-run this guard exists to prevent."""
    ops = []
    _manual_serve_harness(monkeypatch, ops, claim_result=False)

    assert supervisor.serve() == 0
    assert [op[0] for op in ops] == ["claim"]


def test_a_manual_run_waits_rather_than_racing_a_job_already_running(monkeypatch):
    """Unlike a scheduled fire time, a manual request is not consumed by being
    skipped: it stays queued until the running child exits, and its TTL is what
    stops it waiting forever. Skipping it silently would drop the request."""
    import scheduler

    ops = []
    connect = _serve_harness(monkeypatch, stop_after=3)
    monkeypatch.setattr(supervisor, "telegram_alert", lambda msg: None)
    monkeypatch.setattr(supervisor, "_close_runs", lambda outcomes: None)
    monkeypatch.setattr(supervisor.db, "start_run", lambda *a, **k: 7)
    monkeypatch.setattr(
        supervisor.db, "claim_queued", lambda conn, rid: ops.append(("claim", rid))
    )
    _RecordingChild.ops = ops
    monkeypatch.setattr(supervisor, "Child", _RecordingChild)

    spec = next(s for s in scheduler.SCHEDULES if s.job == "collect")
    fire = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    due = [[(spec, fire, "scheduled")]]
    monkeypatch.setattr(
        supervisor, "_due_jobs", lambda conn, now: due.pop() if due else []
    )
    monkeypatch.setattr(supervisor, "_manual_jobs", lambda conn, now: [(spec, 42)])

    assert supervisor.serve() == 0
    assert len(connect.calls) >= 2, "needs the spawn tick and at least one wait tick"
    assert [op[0] for op in ops] == ["spawn"], (
        f"collect was already running, so the queued row must not be claimed; got {ops}"
    )

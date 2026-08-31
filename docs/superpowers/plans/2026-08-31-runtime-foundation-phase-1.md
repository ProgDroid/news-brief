# Runtime Foundation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace five cron-invoked compose services with one application container running a supervisor, backed by a Postgres service, so that a stack redeploy no longer fires the pipeline.

**Architecture:** A new `serve` mode runs a supervisor that owns scheduling, spawns each job as a child OS process (`python brief.py <mode>`, reusing the existing seven-function dispatch unchanged), keeps the Telegram daemon as a resident child, and is the sole writer of the rotating log file. Scheduling state lives in a `job_runs` table; every entry path to a job — supervisor, `docker compose run`, a future `/run` — takes a Postgres advisory lock and writes its row, so the interlock is a property rather than a convention.

**Tech Stack:** Python 3.12, `psycopg[binary]` v3, PostgreSQL, plain numbered SQL migrations, `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-08-31-process-architecture-and-storage-design.md` (commit `b2c3521`)

**Beads:** `news-brief-0q0.1` … `news-brief-0q0.6` under epic `news-brief-0q0`. Claim each with `bd update <id> --claim` before starting and close it when its task is done.

## Global Constraints

- **Dependency budget.** The runtime dependency list is `feedparser`, `requests`, and — added by this plan — `psycopg[binary]`. **No SQLAlchemy, no Alembic, no APScheduler, no croniter.** All four were considered and rejected in spec §4.6 and §5.3. `psycopg[binary]` is the required spelling; the plain package needs `libpq-dev` and a compiler in a slim image.
- **A new top-level module is a three-place update**, or it either `ModuleNotFoundError`s at runtime or silently escapes CI lint: `Dockerfile` COPY line, the `paths:` filter in `.github/workflows/docker-publish.yml`, and **both** `ruff` file lists in that workflow (`ruff check` and `ruff format --check`).
- **Postgres major version:** confirm the current stable major at implementation time. Do not copy a version number out of this plan or the spec.
- **`DATABASE_URL` must be declared in the `&newsbrief` compose anchor.** Compose passes through only what the anchor declares; setting it on the host or in `.env` delivers nothing to the container.
- **Commit from the Bash tool, never PowerShell** — `git commit` via PowerShell prepends a UTF-8 BOM to the subject line.
- **`ruff format` owns style.** Run `ruff format` before committing and stage the reformatted files, or CI fails.
- **Tests must still run with no database present**, skipping loudly with a message naming the missing `DATABASE_URL` — never silently.
- **Full pre-push gate:** `ruff check <files> && ruff format --check <files> && pytest -q`. `pytest` alone is not the gate.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `db.py` | Connection, advisory locks, migration runner, `job_runs` read/write helpers. The only module that knows psycopg exists. |
| `scheduler.py` | Pure schedule arithmetic: schedule specs, previous fire time, and the run/skip/missed decision. No I/O, no database, no subprocess. |
| `supervisor.py` | Process lifecycle: resident children, job children, log draining, backoff, shutdown. No business logic. |
| `migrations/0001_runtime_foundation_up.sql` | `schema_migrations`, `users`, `settings`, `job_runs`. |
| `migrations/0001_runtime_foundation_down.sql` | Reverses 0001. |
| `tests/test_db.py` | Migration runner, idempotence, down-restores, advisory lock. |
| `tests/test_scheduler.py` | Table-driven decision tests, including redeploy inside and outside grace. |
| `tests/test_supervisor.py` | Child spawn/reap with fake commands, backoff, fail-open startup. |
| `tests/test_job_interlock.py` | The interlock exercised through the bypass path. |

**Modified:** `brief.py` (dispatch: `serve` mode + interlock wrapper), `common.py` (log handler selection), `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/docker-publish.yml`, `tests/conftest.py`.

`scheduler.py` is deliberately separate from `supervisor.py`: spec §9 designates the catch-up rule the piece whose failure is silent, and pure functions with no I/O are the only version of it that can be exhaustively tested.

---

## Task 1: Database module, migrations, and the test path

**Beads:** `news-brief-0q0.1`, `news-brief-0q0.6`

**Files:**
- Create: `db.py`, `migrations/0001_runtime_foundation_up.sql`, `migrations/0001_runtime_foundation_down.sql`, `tests/test_db.py`
- Modify: `requirements.txt`, `Dockerfile`, `.github/workflows/docker-publish.yml`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `db.connect() -> psycopg.Connection`; `db.run_migrations(conn, direction="up", target=None) -> list[str]`; `db.advisory_lock(conn, name) -> ContextManager[bool]`; `db.database_url() -> str`; `db.MIGRATIONS_DIR`.

- [ ] **Step 1: Add the dependency and the three-place module registration**

`requirements.txt` — append:

```
psycopg[binary]==3.2.10
```

> Confirm the current stable `psycopg` 3.x at implementation time and pin that exact version; the project pins every dependency.

`Dockerfile` — the COPY line becomes (adding `db.py`, `scheduler.py`, `supervisor.py` now so later tasks need no Dockerfile change, plus the migrations directory):

```dockerfile
COPY common.py trading.py polygram_live.py validation.py brief.py brief_memory.py claim_verify.py retention.py db.py scheduler.py supervisor.py .
COPY migrations/ ./migrations/
COPY enrichment/ ./enrichment/
```

`.github/workflows/docker-publish.yml` — add to the `paths:` filter, after `- 'validation.py'`:

```yaml
      - 'db.py'
      - 'scheduler.py'
      - 'supervisor.py'
      - 'migrations/**'
```

And in **both** ruff lines, insert `db.py scheduler.py supervisor.py` after `validation.py`:

```yaml
          ruff check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py enrichment scripts tests
          ruff format --check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py enrichment scripts tests
```

Add a Postgres service to the `test` job, immediately under `runs-on: ubuntu-latest`:

```yaml
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_PASSWORD: newsbrief
          POSTGRES_USER: newsbrief
          POSTGRES_DB: newsbrief_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
```

and set the URL on the test step:

```yaml
      - name: Test
        env:
          DATABASE_URL: postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test
        run: pytest -q
```

> Pin the same Postgres major here as in `docker-compose.yml`. `17-alpine` is a placeholder — confirm current stable.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_db.py`:

```python
"""Migration runner and advisory lock.

These tests need a real Postgres: the runner's whole job is to talk to one, and
a fake would test the fake. They skip loudly when DATABASE_URL is unset so a
missing database can never read as a pass.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.database_url(),
    reason="DATABASE_URL is not set: start a Postgres and export it, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:17-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing."""
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def test_up_creates_the_four_tables(conn):
    applied = db.run_migrations(conn)
    assert applied == ["0001_runtime_foundation"]
    assert {"schema_migrations", "users", "settings", "job_runs"} <= _tables(conn)


def test_up_is_idempotent(conn):
    db.run_migrations(conn)
    assert db.run_migrations(conn) == []


def test_down_restores_the_prior_schema(conn):
    before = _tables(conn)
    db.run_migrations(conn)
    db.run_migrations(conn, direction="down")
    after = _tables(conn) - {"schema_migrations"}
    assert after == before - {"schema_migrations"}


def test_advisory_lock_is_exclusive_across_connections(conn):
    db.run_migrations(conn)
    with db.connect() as other:
        with db.advisory_lock(conn, "collect") as first:
            assert first is True
            with db.advisory_lock(other, "collect") as second:
                assert second is False


def test_advisory_lock_releases_when_the_connection_closes(conn):
    db.run_migrations(conn)
    holder = db.connect()
    with db.advisory_lock(holder, "collect") as acquired:
        assert acquired is True
        holder.close()
    with db.connect() as taker:
        with db.advisory_lock(taker, "collect") as acquired:
            assert acquired is True


def test_different_job_names_do_not_collide(conn):
    db.run_migrations(conn)
    with db.connect() as other:
        with db.advisory_lock(conn, "collect") as a:
            with db.advisory_lock(other, "weekly") as b:
                assert a is True and b is True
```

- [ ] **Step 3: Run the tests and verify they fail**

Start a database first:

```bash
docker run --rm -d --name newsbrief-testdb -p 5432:5432 \
  -e POSTGRES_PASSWORD=newsbrief -e POSTGRES_USER=newsbrief \
  -e POSTGRES_DB=newsbrief_test postgres:17-alpine
export DATABASE_URL=postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test
pytest tests/test_db.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'db'`.

- [ ] **Step 4: Write the migration SQL**

Create `migrations/0001_runtime_foundation_up.sql`:

```sql
-- Runtime foundation: identity, configuration, and the scheduler's run ledger.
-- schema_migrations is created by the runner itself, not here.

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    telegram_chat_id TEXT       NOT NULL,
    timezone        TEXT        NOT NULL DEFAULT 'UTC',
    delivery_time   TIME        NOT NULL DEFAULT '06:00',
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Scoped global (user_id IS NULL) or per-user. Values are TEXT with coercion at
-- read time: typed columns would need a type discriminator plus a cast per read,
-- and every current knob arrives from the environment as a string anyway.
CREATE TABLE settings (
    key        TEXT   NOT NULL,
    user_id    BIGINT NULL REFERENCES users(id) ON DELETE CASCADE,
    value      TEXT   NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX settings_key_global ON settings (key) WHERE user_id IS NULL;
CREATE UNIQUE INDEX settings_key_user ON settings (key, user_id) WHERE user_id IS NOT NULL;

-- One row per attempted run, from EVERY entry path. status and trigger exist
-- from the first migration so on-demand runs never require retrofitting a queue
-- into the table the /jobs command teaches the operator to trust.
CREATE TABLE job_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT        NOT NULL,
    scheduled_for TIMESTAMPTZ NULL,
    trigger       TEXT        NOT NULL CHECK (trigger IN ('scheduled', 'catchup', 'manual')),
    status        TEXT        NOT NULL CHECK (status IN ('queued', 'running', 'finished', 'missed')),
    started_at    TIMESTAMPTZ NULL,
    finished_at   TIMESTAMPTZ NULL,
    exit_code     INTEGER     NULL
);

-- The catch-up rule asks one question on every tick: what is the latest
-- scheduled_for already recorded for this job?
CREATE INDEX job_runs_job_scheduled ON job_runs (job_name, scheduled_for DESC);
CREATE INDEX job_runs_job_started ON job_runs (job_name, started_at DESC);
```

Create `migrations/0001_runtime_foundation_down.sql`:

```sql
DROP TABLE IF EXISTS job_runs;
DROP TABLE IF EXISTS settings;
DROP TABLE IF EXISTS users;
```

- [ ] **Step 5: Write `db.py`**

```python
#!/usr/bin/env python3
"""Postgres access: connections, advisory locks, migrations, run ledger.

The only module that imports psycopg. Everything else takes a connection or
calls a helper here, which keeps the driver swap in spec section 5.1 to one file.
"""

import os
import hashlib
from pathlib import Path
from contextlib import contextmanager

import psycopg

from common import log

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def database_url() -> str:
    """Read at call time, not import time, so tests can set it after import."""
    return os.environ.get("DATABASE_URL", "")


def connect() -> psycopg.Connection:
    url = database_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is unset. In the container it must be declared in the "
            "&newsbrief compose anchor: setting it on the host or in .env alone "
            "delivers nothing through compose."
        )
    return psycopg.connect(url, autocommit=False)


def _lock_key(name: str) -> int:
    """Map a job name onto the signed 64-bit integer pg_advisory_lock wants."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def advisory_lock(conn: psycopg.Connection, name: str):
    """Try to take a session-scoped lock on `name`; yield whether we got it.

    Session-scoped rather than transaction-scoped on purpose: a job runs for
    minutes to hours across many transactions, and a lock tied to the session
    dies with the connection — so a SIGKILLed child cannot strand it.
    """
    key = _lock_key(name)
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
    try:
        yield got
    finally:
        if got and not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            conn.commit()


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    conn.commit()


def applied_versions(conn: psycopg.Connection) -> list[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [r[0] for r in rows]


def _available(direction: str) -> list[tuple[str, Path]]:
    suffix = f"_{direction}.sql"
    found = sorted(p for p in MIGRATIONS_DIR.glob(f"*{suffix}"))
    return [(p.name[: -len(suffix)], p) for p in found]


def run_migrations(conn: psycopg.Connection, direction: str = "up") -> list[str]:
    """Apply pending migrations in order; return the versions applied.

    Each migration runs in its own transaction, so a failure leaves the versions
    before it applied and the failing one absent — a resumable state rather than
    a half-applied one. Raises on failure; the caller decides what that means
    (supervisor: block job children, still start the bot).
    """
    _ensure_migrations_table(conn)
    done = set(applied_versions(conn))
    pending = [(v, p) for v, p in _available(direction) if (v not in done) == (direction == "up")]
    if direction == "down":
        pending.reverse()

    changed: list[str] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute(sql)
            if direction == "up":
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            else:
                conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception(f"Migration {version} ({direction}) failed")
            raise
        changed.append(version)
        log.info(f"Migration {version} {direction} applied")
    return changed
```

- [ ] **Step 6: Make `conftest.py` tolerate a database-less run**

Append to `tests/conftest.py`:

```python
# DB-backed tests skip when DATABASE_URL is unset (see tests/test_db.py), and say
# how to get one. They must never skip in CI: the workflow sets DATABASE_URL, so
# a skip there means the services block is broken, not that the test is optional.
```

- [ ] **Step 7: Run the tests and verify they pass**

```bash
pytest tests/test_db.py -v
```

Expected: 6 passed.

Then verify the loud-skip path actually skips rather than errors:

```bash
DATABASE_URL= pytest tests/test_db.py -v
```

Expected: 6 skipped, each showing the `docker run` hint.

- [ ] **Step 8: Commit**

```bash
ruff format db.py tests/test_db.py
ruff check db.py tests/test_db.py
git add db.py migrations/ tests/test_db.py tests/conftest.py requirements.txt Dockerfile .github/workflows/docker-publish.yml
git commit -m "feat(db): Postgres access, advisory locks, reversible migrations"
bd close news-brief-0q0.1 news-brief-0q0.6
```

---

## Task 2: The schedule decision, as pure functions

**Bead:** `news-brief-0q0.3` (first half — the arithmetic; the tick loop lands in Task 4)

**Files:**
- Create: `scheduler.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: nothing. `scheduler.py` imports no project module and performs no I/O.
- Produces: `scheduler.Schedule(job, kind, at, every_minutes, grace_minutes)`; `scheduler.previous_fire(spec, now) -> datetime`; `scheduler.decide(spec, now, last_scheduled_for) -> Decision`; `scheduler.Decision(action, scheduled_for, reason)` where `action` is `"run" | "skip" | "missed"`; `scheduler.SCHEDULES: tuple[Schedule, ...]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler.py`:

```python
"""The catch-up rule.

Spec section 4.5 concedes this complexity is self-inflicted by moving the
scheduler inside the container, and section 9 designates it the piece whose
failure is silent. Hence: pure functions, exhaustive table.
"""

from datetime import datetime, timedelta, timezone

import pytest

import scheduler

UTC = timezone.utc


def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


DAILY = scheduler.Schedule(
    job="collect", kind="daily", at="06:00", every_minutes=None, grace_minutes=120
)
EVERY_30 = scheduler.Schedule(
    job="capture", kind="interval", at=None, every_minutes=30, grace_minutes=10
)


@pytest.mark.parametrize(
    "now,expected",
    [
        (dt(2026, 8, 31, 6, 0), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 1), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 5, 59), dt(2026, 8, 30, 6, 0)),
        (dt(2026, 8, 31, 23, 59), dt(2026, 8, 31, 6, 0)),
    ],
)
def test_previous_fire_daily(now, expected):
    assert scheduler.previous_fire(DAILY, now) == expected


@pytest.mark.parametrize(
    "now,expected",
    [
        (dt(2026, 8, 31, 6, 0), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 29), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 30), dt(2026, 8, 31, 6, 30)),
        (dt(2026, 8, 31, 0, 5), dt(2026, 8, 31, 0, 0)),
    ],
)
def test_previous_fire_interval(now, expected):
    assert scheduler.previous_fire(EVERY_30, now) == expected


def test_runs_when_due_and_nothing_recorded():
    d = scheduler.decide(DAILY, now=dt(2026, 8, 31, 6, 0), last_scheduled_for=None)
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_skips_when_this_fire_time_already_ran():
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 9, 0),
        last_scheduled_for=dt(2026, 8, 31, 6, 0),
    )
    assert d.action == "skip"


def test_redeploy_inside_the_grace_window_runs_the_missed_job():
    """The defining case: a deploy 30 seconds after the scheduled fire time."""
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 6, 0) + timedelta(seconds=30),
        last_scheduled_for=None,
    )
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_redeploy_outside_the_grace_window_does_not_resurrect_the_job():
    """The other defining case: a deploy at 14:00 must not produce a brief."""
    d = scheduler.decide(DAILY, now=dt(2026, 8, 31, 14, 0), last_scheduled_for=None)
    assert d.action == "missed"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_grace_boundary_is_inclusive():
    d = scheduler.decide(
        DAILY, now=dt(2026, 8, 31, 8, 0), last_scheduled_for=None
    )
    assert d.action == "run"


def test_one_second_past_grace_is_missed():
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 8, 0) + timedelta(seconds=1),
        last_scheduled_for=None,
    )
    assert d.action == "missed"


def test_coalesces_to_latest_never_replays_a_backlog():
    """Three days down: exactly one run, for the most recent fire time."""
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 9, 3, 6, 30),
        last_scheduled_for=dt(2026, 8, 31, 6, 0),
    )
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 9, 3, 6, 0)


def test_zero_grace_never_catches_up():
    weekly = scheduler.Schedule(
        job="weekly", kind="daily", at="21:00", every_minutes=None, grace_minutes=0
    )
    late = scheduler.decide(
        weekly,
        now=dt(2026, 8, 31, 21, 0) + timedelta(seconds=1),
        last_scheduled_for=None,
    )
    assert late.action == "missed"


def test_every_schedule_has_a_positive_or_zero_grace():
    for spec in scheduler.SCHEDULES:
        assert spec.grace_minutes >= 0
        assert (spec.at is None) != (spec.every_minutes is None)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_scheduler.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'scheduler'`.

- [ ] **Step 3: Write `scheduler.py`**

```python
#!/usr/bin/env python3
"""Schedule arithmetic and the catch-up decision.

Pure: no I/O, no database, no subprocess, no project imports. Spec section 4.5
records that this rule exists only because the scheduler moved inside the
container, and section 9 makes it the most heavily tested logic in the system —
which is affordable exactly because it is a function of its arguments.

Two trigger kinds cover every job we have, so there is no cron expression parser
(spec section 4.2).
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class Schedule:
    job: str
    kind: str  # "daily" | "interval"
    at: str | None  # "HH:MM", UTC, for kind="daily"
    every_minutes: int | None  # for kind="interval"
    grace_minutes: int  # how late a run may start and still happen


@dataclass(frozen=True)
class Decision:
    action: str  # "run" | "skip" | "missed"
    scheduled_for: datetime
    reason: str


# Times match the cron entries being retired, so the cutover changes when
# nothing. Grace is sized to the job: capture is cheap and frequent, so a stale
# catch-up is worthless; collect is the day's brief and worth an hour or two
# late; weekly is dated content that must never arrive on the wrong day.
SCHEDULES: tuple[Schedule, ...] = (
    Schedule("submit", "daily", "20:00", None, grace_minutes=60),
    Schedule("collect", "daily", "06:00", None, grace_minutes=120),
    Schedule("weekly", "daily", "21:00", None, grace_minutes=0),
    Schedule("monitor", "interval", None, 60, grace_minutes=15),
)


def previous_fire(spec: Schedule, now: datetime) -> datetime:
    """The most recent moment this schedule was due, at or before `now`."""
    if spec.kind == "daily":
        hh, mm = (int(x) for x in spec.at.split(":"))
        today = datetime.combine(now.date(), time(hh, mm), tzinfo=now.tzinfo)
        return today if today <= now else today - timedelta(days=1)

    if spec.kind == "interval":
        midnight = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
        elapsed = int((now - midnight).total_seconds() // 60)
        return midnight + timedelta(minutes=elapsed - (elapsed % spec.every_minutes))

    raise ValueError(f"unknown schedule kind: {spec.kind}")


def decide(spec: Schedule, now: datetime, last_scheduled_for: datetime | None) -> Decision:
    """Run at most once for the latest due fire time; never replay a backlog.

    `last_scheduled_for` is the greatest scheduled_for already recorded for this
    job in job_runs. Only the most recent fire time is ever considered, which is
    APScheduler's `coalesce="latest"` expressed as three lines rather than a
    dependency (spec section 4.6).
    """
    due = previous_fire(spec, now)

    if last_scheduled_for is not None and last_scheduled_for >= due:
        return Decision("skip", due, "already recorded for this fire time")

    lateness = now - due
    if lateness <= timedelta(minutes=spec.grace_minutes):
        return Decision("run", due, f"due {int(lateness.total_seconds())}s ago")

    return Decision(
        "missed",
        due,
        f"missed_start_deadline: {int(lateness.total_seconds())}s late, "
        f"grace is {spec.grace_minutes}m",
    )
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
ruff format scheduler.py tests/test_scheduler.py
ruff check scheduler.py tests/test_scheduler.py
git add scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): schedule arithmetic and the catch-up decision"
```

---

## Task 3: The job interlock, enforced in the mode dispatch

**Bead:** `news-brief-0q0.4`

**Files:**
- Modify: `db.py` (run-ledger helpers), `brief.py:3618-3648` (the `__main__` block)
- Test: `tests/test_job_interlock.py`

**Interfaces:**
- Consumes: `db.connect`, `db.advisory_lock`.
- Produces: `db.latest_scheduled_for(conn, job) -> datetime | None`; `db.start_run(conn, job, scheduled_for, trigger) -> int`; `db.finish_run(conn, run_id, exit_code) -> None`; `db.record_missed(conn, job, scheduled_for) -> None`; `brief.JOB_MODES: frozenset[str]`; `brief.EX_ALREADY_RUNNING = 75`.

**Why the dispatch and not the supervisor:** spec §4.4a. A guard living in the supervisor is exactly what the one caller bypassing the supervisor — `docker compose run --rm newsbrief collect`, which §3.6 deliberately preserves — evades. That call is how the double-collect this whole plan exists to prevent would come back.

- [ ] **Step 1: Add the run-ledger helpers to `db.py`**

Append to `db.py`:

```python
def latest_scheduled_for(conn: psycopg.Connection, job: str):
    """Greatest scheduled_for recorded for this job, or None. Feeds decide()."""
    row = conn.execute(
        "SELECT max(scheduled_for) FROM job_runs WHERE job_name = %s", (job,)
    ).fetchone()
    return row[0] if row else None


def start_run(conn: psycopg.Connection, job: str, scheduled_for, trigger: str) -> int:
    row = conn.execute(
        "INSERT INTO job_runs (job_name, scheduled_for, trigger, status, started_at) "
        "VALUES (%s, %s, %s, 'running', now()) RETURNING id",
        (job, scheduled_for, trigger),
    ).fetchone()
    conn.commit()
    return row[0]


def finish_run(conn: psycopg.Connection, run_id: int, exit_code: int) -> None:
    conn.execute(
        "UPDATE job_runs SET status = 'finished', finished_at = now(), exit_code = %s "
        "WHERE id = %s",
        (exit_code, run_id),
    )
    conn.commit()


def record_missed(conn: psycopg.Connection, job: str, scheduled_for) -> None:
    """A fire time that passed outside its grace window. Recorded, not run —
    so the next tick sees it as accounted for and does not re-evaluate it."""
    conn.execute(
        "INSERT INTO job_runs (job_name, scheduled_for, trigger, status) "
        "VALUES (%s, %s, 'scheduled', 'missed')",
        (job, scheduled_for),
    )
    conn.commit()
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_job_interlock.py`:

```python
"""The interlock, exercised through the path that bypasses the supervisor.

A test that drives the supervisor proves nothing about the case that motivated
the rule (spec section 4.4a): a SECOND entry path starting a job that is already
running. So this holds the lock the way the supervisor would, then invokes the
mode directly, as `docker compose run --rm newsbrief collect` does.
"""

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
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `pytest tests/test_job_interlock.py -v`
Expected: FAIL with `AttributeError: module 'brief' has no attribute 'run_job'`.

- [ ] **Step 4: Add `run_job` and rewrite the dispatch in `brief.py`**

Replace the `if __name__ == "__main__":` block at the foot of `brief.py` with:

```python
# ── Job entry ─────────────────────────────────────────────────────────────────
# Modes that mutate shared state and must never run twice concurrently. The
# guard lives HERE and not in the supervisor: the supervisor is not the only
# caller — `docker compose run --rm newsbrief collect` is a documented debug
# path (spec section 3.6), and a guard the bypass path skips is not a guard.
JOB_MODES = frozenset({"submit", "collect", "weekly", "monitor", "capture"})

# sysexits.h EX_TEMPFAIL: "try again later", which is exactly the situation.
EX_ALREADY_RUNNING = 75


def run_job(mode, fn, *, scheduled_for=None, trigger="manual") -> int:
    """Run `fn` under the job lock, recording the attempt in job_runs.

    Returns the exit code the caller should exit with. The lock is session
    scoped, so a SIGKILL of this process releases it when the connection drops.
    """
    import db

    with db.connect() as conn:
        with db.advisory_lock(conn, mode) as acquired:
            if not acquired:
                log.warning(
                    f"{mode} is already running (job lock held); refusing to start a "
                    f"second one. Check /jobs or job_runs for the holder."
                )
                return EX_ALREADY_RUNNING

            run_id = db.start_run(conn, mode, scheduled_for, trigger)
            try:
                fn()
                code = 0
            except Exception as e:
                log.exception(f"Mode '{mode}' crashed")
                telegram_alert(f"{mode} crashed: {type(e).__name__}: {e}")
                code = 1
            db.finish_run(conn, run_id, code)
            return code


if __name__ == "__main__":
    import sys

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dispatch = {
        "submit": mode_submit,
        "collect": mode_collect,
        "weekly": mode_weekly,
        "commands": mode_commands,
        "paper": mode_paper,
        "monitor": mode_monitor,
        "pgdiag": mode_pgdiag,
    }
    fn = dispatch.get(mode)
    if not fn:
        import supervisor

        if mode == "serve":
            sys.exit(supervisor.serve())
        print(
            "Usage: brief.py [serve|submit|collect|weekly|paper|commands|monitor|pgdiag]"
        )
        sys.exit(1)

    if mode in JOB_MODES:
        # Trigger is "manual" here by definition: the supervisor passes its own
        # scheduled_for via NEWSBRIEF_SCHEDULED_FOR when it spawns the child.
        scheduled_for = os.environ.get("NEWSBRIEF_SCHEDULED_FOR") or None
        trigger = os.environ.get("NEWSBRIEF_TRIGGER", "manual")
        sys.exit(run_job(mode, fn, scheduled_for=scheduled_for, trigger=trigger))

    try:
        fn()
    except Exception as e:
        # Last-resort alert for non-job modes: without this, an uncaught crash is
        # visible only in the log file and the reader silently gets no brief.
        log.exception(f"Mode '{mode}' crashed")
        telegram_alert(f"{mode} crashed: {type(e).__name__}: {e}")
        sys.exit(1)
```

- [ ] **Step 5: Run the tests and verify they pass**

```bash
pytest tests/test_job_interlock.py -v
pytest -q
```

Expected: 3 passed in the first; the full suite green (`serve` is not yet importable only if `supervisor.py` is missing — it is imported lazily inside the branch, so the suite is unaffected until Task 4).

- [ ] **Step 6: Commit**

```bash
ruff format db.py brief.py tests/test_job_interlock.py
ruff check db.py brief.py tests/test_job_interlock.py
git add db.py brief.py tests/test_job_interlock.py
git commit -m "feat(jobs): interlock every entry path with a lock and a job_runs row"
bd close news-brief-0q0.4
```

---

## Task 4: The supervisor

**Beads:** `news-brief-0q0.2`, `news-brief-0q0.3` (tick loop)

**Files:**
- Create: `supervisor.py`, `tests/test_supervisor.py`
- Modify: `common.py:33-46` (log handler selection)

**Interfaces:**
- Consumes: `db.connect`, `db.run_migrations`, `db.latest_scheduled_for`, `db.record_missed`, `scheduler.SCHEDULES`, `scheduler.decide`.
- Produces: `supervisor.serve() -> int`; `supervisor.Child`; `supervisor.spawn(mode, env) -> Child`; `supervisor.RESIDENT_MODES`.

- [ ] **Step 1: Make the log file single-writer**

Spec §3.2: several processes each holding a `RotatingFileHandler` on one file fight over the rotation rename. Children get the stream handler only; the supervisor owns the file.

In `common.py`, replace the body of `_log_handlers`:

```python
def _log_handlers() -> list[logging.Handler]:
    """Console always; the rotating file only for the process that owns it.

    Several processes each holding a RotatingFileHandler on one file fight over
    the rotation rename and lose lines. The supervisor is the single writer and
    sets NEWSBRIEF_LOG_FILE=1 for itself; children inherit an explicit 0 and log
    to stdout, which the supervisor drains into the file with their name.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if os.environ.get("NEWSBRIEF_LOG_FILE", "1") != "1":
        return handlers
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Rotate so the log can't grow unbounded over the container's lifetime:
        # 5 MB × 5 backups ≈ 30 MB ceiling.
        handlers.append(
            RotatingFileHandler(
                DATA_DIR / "newsbrief.log", maxBytes=5_000_000, backupCount=5
            )
        )
    except OSError:
        pass  # data dir unavailable (local run, tests): console logging still works
    return handlers
```

The default stays `"1"`, so a bare `docker compose run --rm newsbrief collect` still writes the file exactly as today — only supervisor-spawned children are switched off.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_supervisor.py`:

```python
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
```

- [ ] **Step 3: Run the tests and verify they fail**

Run: `pytest tests/test_supervisor.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'supervisor'`.

- [ ] **Step 4: Write `supervisor.py`**

```python
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
from common import log, telegram_alert

# Children that stay up. `commands` is the Telegram long-poll daemon and the
# ONLY getUpdates consumer (a second one 409s) — under the supervisor exactly
# one process owns that constraint, which a stray `compose up` used to violate.
RESIDENT_MODES = ("commands",)

TICK_SECONDS = 30
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

    def __init__(self, name: str, argv: list[str] | None = None, env: dict | None = None):
        self.name = name
        self.argv = argv or [sys.executable, "brief.py", name]
        self.env = env
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
        return StartupState(jobs_enabled=True, residents_enabled=True)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        log.exception("Migration failed; jobs are disabled, bot still starting")
        telegram_alert(
            f"Migration failed — jobs disabled, bot still up. {reason}"
        )
        return StartupState(jobs_enabled=False, residents_enabled=True, reason=reason)


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
            if code not in (0, None):
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
                        child = Child(
                            spec.job,
                            env={
                                "NEWSBRIEF_SCHEDULED_FOR": scheduled_for.isoformat(),
                                "NEWSBRIEF_TRIGGER": trigger,
                            },
                        )
                        child.start()
                        jobs[spec.job] = child
            except Exception:
                log.exception("Scheduler tick failed; continuing")

        stopping.wait(TICK_SECONDS)

    for child in list(jobs.values()) + list(residents.values()):
        child.terminate()
    log.info("Supervisor stopped")
    return 0
```

- [ ] **Step 5: Run the tests and verify they pass**

```bash
pytest tests/test_supervisor.py -v
pytest -q
```

Expected: 8 passed; full suite green.

- [ ] **Step 6: Commit**

```bash
ruff format supervisor.py common.py tests/test_supervisor.py
ruff check supervisor.py common.py tests/test_supervisor.py
git add supervisor.py common.py tests/test_supervisor.py
git commit -m "feat(supervisor): resident and job children, single-writer log, fail-open startup"
bd close news-brief-0q0.2 news-brief-0q0.3
```

---

## Task 5: The cutover

**Bead:** `news-brief-0q0.5`

**Files:**
- Modify: `docker-compose.yml`, `README.md`
- Create: `docs/runbooks/2026-08-31-supervisor-cutover.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing further consumes this.

**This phase is a flag day for scheduling** (spec §7.1). The host cron entries and the `newsbrief-commands` service must come out in the same change as the supervisor goes in, or you get double collects and a Telegram 409 from two `getUpdates` consumers.

- [ ] **Step 1: Write the rollback down BEFORE touching anything**

The rollback is a manual host operation that may happen while the Telegram bot is down, so it cannot be derived at the time. Create `docs/runbooks/2026-08-31-supervisor-cutover.md`:

```markdown
# Supervisor cutover — runbook

## Rollback (do this if the supervisor does not come up)

1. In the OMV compose file, replace the `newsbrief` service with the pre-cutover
   `newsbrief-commands` service:

       newsbrief-commands:
         <<: *newsbrief
         command: [commands]
         restart: unless-stopped

   and restore the four batch services:

       newsbrief-submit:   { <<: *newsbrief, command: [submit] }
       newsbrief-collect:  { <<: *newsbrief, command: [collect] }
       newsbrief-weekly:   { <<: *newsbrief, command: [weekly] }
       newsbrief-monitor:  { <<: *newsbrief, command: [monitor] }

2. Restore the host cron entries (UTC):

       0 20 * * *   docker compose run --rm newsbrief-submit
       0  6 * * *   docker compose run --rm newsbrief-collect
       0 21 * * 0   docker compose run --rm newsbrief-weekly
       0  *  * * *  docker compose run --rm newsbrief-monitor

3. `docker compose up -d newsbrief-commands`.

The Postgres service can stay up during a rollback: nothing in the pre-cutover
code path reads it.

## Cutover verification

- `docker compose ps` shows exactly two services: `newsbrief`, `postgres`.
- `SELECT job_name, scheduled_for, trigger, status FROM job_runs ORDER BY id;`
  shows no row with a `started_at` at deploy time — the point of the whole change.
- `/jobs` in Telegram answers.
- One `getUpdates` consumer only: the bot responds and the log shows no 409.
```

- [ ] **Step 2: Rewrite `docker-compose.yml`**

Replace the five services with two, and add `DATABASE_URL` to the anchor's `environment:` block (it is invisible in the container otherwise):

```yaml
    - DATABASE_URL=${DATABASE_URL:-postgresql://newsbrief:newsbrief@postgres:5432/newsbrief}

services:
  # One long-lived service. Its supervisor owns scheduling, spawns each job as a
  # child process, and keeps the Telegram daemon resident. A stack `up` therefore
  # starts no work: jobs run when they are due, and a redeploy runs only what the
  # catch-up rule says was actually missed.
  newsbrief:
    <<: *newsbrief
    command: [serve]
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy

  # Deliberately does NOT inherit the anchor: the official image manages its own
  # uid, and a `user:` override against an initialised data directory fails in a
  # way that reads as corruption.
  postgres:
    image: postgres:17-alpine
    environment:
      - POSTGRES_USER=${POSTGRES_USER:-newsbrief}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      - POSTGRES_DB=${POSTGRES_DB:-newsbrief}
    volumes:
      - newsbrief-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-newsbrief}"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  newsbrief-pgdata:
```

Also update the header comment block: delete the cron schedule (it is now in `scheduler.SCHEDULES`) and replace it with `docker compose up -d`, noting that `docker compose run --rm --entrypoint python newsbrief probe.py` remains the debug path.

- [ ] **Step 3: Update the README**

In `## 5. Schedule`, replace the cron block and the `up -d newsbrief-commands` line with:

```sh
docker compose up -d          # supervisor + postgres; jobs run on their own schedule
```

and note that schedules live in `scheduler.SCHEDULES`, that `/jobs` reports last run and next due, and that `POSTGRES_PASSWORD` is now required in `.env`.

- [ ] **Step 4: Verify the compose file is valid and starts nothing unexpected**

```bash
docker compose config --quiet && echo "compose OK"
docker compose up -d
sleep 20
docker compose ps
docker compose logs newsbrief | tail -40
```

Expected: two services up; the log shows `=== SERVE (supervisor) ===`, migrations applied, `[commands] started`, and **no** `=== COLLECT ===`.

Then confirm the interlock through the bypass path against the running stack:

```bash
docker compose exec -T postgres psql -U newsbrief -d newsbrief \
  -c "SELECT job_name, scheduled_for, trigger, status FROM job_runs ORDER BY id;"
```

Expected: no row whose `started_at` is the deploy time.

- [ ] **Step 5: Commit**

```bash
ruff format .
ruff check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py enrichment scripts tests
ruff format --check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py enrichment scripts tests
pytest -q
git add docker-compose.yml README.md docs/runbooks/
git commit -m "feat(compose): cut over to supervisor plus postgres, retire the batch services"
bd close news-brief-0q0.5
```

- [ ] **Step 6: Retire the host cron entries**

On the deploy host, delete the four `docker compose run --rm newsbrief-*` cron entries **in the same maintenance window as the deploy**. Leaving them running against a compose file that no longer defines those services produces a confusing failure per invocation; leaving them against an old file produces double collects.

---

## Self-Review

**Spec coverage:** §3.1 → Task 5 Step 2. §3.2 → Task 4 Steps 1, 4. §3.3 → Task 4 (`startup`, `RestartTracker`, no business logic). §3.4 → Task 4 (`Child.start` env-only, all coordination via `db`). §3.5 → Task 1 Step 1. §3.6 → preserved; Task 3 makes it safe. §4.1–4.4 → Task 2. §4.4a → Task 3. §4.6 → no APScheduler anywhere. §5.3 → Task 1 (up/down pairs, per-migration transaction). §5.4 → Task 1 (`psycopg[binary]`, `DATABASE_URL` in the anchor). §6.1 → `users` table created in 0001; **seeding and readers are phase 2 (`0q0.7`), out of scope here.** §7.1 → Task 5. §8 → Task 4 (`startup`, OOM alerting, healthcheck). §9 → all four test files.

**Known gaps, deliberate:** §5.5 (`pg_dump` backup job) and the `/jobs` Telegram command are not in this plan — both depend on the supervisor existing and belong to the phase that follows. **File follow-up beads for them under `news-brief-0q0` before closing the epic.** Success criteria 5 and 10 cannot be met until they land.

**Type consistency:** `Schedule(job, kind, at, every_minutes, grace_minutes)` and `Decision(action, scheduled_for, reason)` are used identically in Tasks 2 and 4. `db.start_run` returns the `int` that `db.finish_run` takes. `Child(name, argv=None, env=None)` matches every call site. `scheduled_for` crosses the process boundary as an ISO string in `NEWSBRIEF_SCHEDULED_FOR` and is inserted into a `TIMESTAMPTZ` column, which psycopg adapts.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-31-runtime-foundation-phase-1.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

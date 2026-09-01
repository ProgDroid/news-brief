#!/usr/bin/env python3
"""Postgres access: connections, advisory locks, migrations, run ledger.

The only module that imports psycopg. Everything else takes a connection or
calls a helper here, which keeps the driver swap in spec section 5.1 to one file.
"""

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from common import log

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


# The discrete variables compose passes through, and the libpq keyword each one
# fills. POSTGRES_PORT is deliberately absent: libpq's own 5432 default is the
# only one of these with a right answer that is not this stack's business.
_DISCRETE = (
    ("POSTGRES_HOST", "host"),
    ("POSTGRES_USER", "user"),
    ("POSTGRES_PASSWORD", "password"),
    ("POSTGRES_DB", "dbname"),
)


def conninfo() -> str:
    """The libpq connection string, built from the environment.

    DATABASE_URL wins when set — that is the escape hatch for a database outside
    this stack, and what CI and local test runs export. Otherwise the string is
    built from the discrete POSTGRES_* variables with `make_conninfo`, which
    escapes each value as a keyword/value pair.

    That difference is the whole point. Compose used to splice POSTGRES_PASSWORD
    into a `postgresql://` URI by plain substitution with no encoding, so the
    password had to survive a URI parse: `/` was read as the start of the port,
    `@` truncated it and corrupted the host, and `%cd` decoded silently to a
    DIFFERENT password. `openssl rand -base64` draws from [A-Za-z0-9+/=], so
    roughly two passwords in five carried a `/`. A keyword/value pair has no
    such grammar to fall foul of — the password is a value, not part of a path.

    One trap survives here and cannot be fixed in this file: compose eats a `$`
    in a .env value unless it is written `$$`, and it does so identically on the
    app and on the postgres service, so the stack comes up and works with a
    shorter password than the operator wrote. That is why the README still says
    to generate it with `openssl rand -hex 32`.

    Every variable is read at call time, not import time, so a test can set them
    after importing this module.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    values = {key: os.environ.get(name, "") for name, key in _DISCRETE}
    missing = [name for name, key in _DISCRETE if not values[key]]
    if missing:
        raise RuntimeError(
            f"The database is not configured: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} unset and DATABASE_URL is "
            "empty. In the container these must be declared in the &newsbrief "
            "compose anchor: setting them on the host or in .env alone delivers "
            "nothing through compose."
        )
    port = os.environ.get("POSTGRES_PORT", "")
    if port:
        values["port"] = port
    return make_conninfo(**values)


def is_configured() -> bool:
    """Whether `connect` has something to connect with.

    The DB-backed test modules skip on this rather than on DATABASE_URL alone,
    so the guard tests the exact predicate its consumer reads: a suite pointed
    at a database through the discrete variables must not report itself skipped.
    """
    try:
        conninfo()
    except RuntimeError:
        return False
    return True


def connect(
    connect_timeout: int | None = None, options: str | None = None
) -> psycopg.Connection:
    """Open a connection. Both arguments exist for callers working to a deadline.

    `connect_timeout` bounds the TCP/auth handshake; `options` carries libpq
    startup options, e.g. `-c statement_timeout=500`. Unbounded is the right
    default for the long-lived paths — a collect legitimately runs for minutes —
    but the two bounds are needed together, and neither substitutes for the
    other: a server that accepts the connection and then stalls (lock
    contention, a wedged disk) passes the handshake and hangs on the statement.
    Both stalls happen outside any wait budget the caller keeps for itself, which
    is how a bounded shutdown stops being bounded (see supervisor.shutdown).
    """
    dsn = conninfo()
    kwargs = {}
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    if options is not None:
        kwargs["options"] = options
    return psycopg.connect(dsn, autocommit=False, **kwargs)


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
            # A statement that failed inside the `with` block leaves the
            # transaction aborted; pg_advisory_unlock on an aborted transaction
            # raises InFailedSqlTransaction, which — unguarded — replaces the
            # real error with an unrelated one right as it propagates. Roll
            # back first so unlock can run, and swallow anything cleanup itself
            # raises: release bookkeeping must not be able to mask, or invent,
            # a failure.
            try:
                if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                conn.commit()
            except Exception:
                log.exception(f"Failed to release advisory lock for '{name}'")


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    conn.commit()


def applied_versions(conn: psycopg.Connection) -> list[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [r[0] for r in rows]


def _available(direction: str) -> list[tuple[str, Path]]:
    suffix = f"_{direction}.sql"
    found = sorted(p for p in MIGRATIONS_DIR.glob(f"*{suffix}"))
    return [(p.name[: -len(suffix)], p) for p in found]


def run_migrations(
    conn: psycopg.Connection, direction: str = "up", steps: int | None = None
) -> list[str]:
    """Apply migrations in order; return the versions changed.

    up:   every pending migration, ascending. `steps` caps how many.
    down: the most recently applied migration, descending — exactly ONE by
          default. `steps=0` means every applied migration and must be asked
          for explicitly, because it drops every table in the database. Spec
          section 5.3 exists because KB schema is learned rather than
          specified, so the expected use is "undo 0012", not "undo everything";
          a runner that defaults to the latter is a loaded gun in a codebase
          whose whole premise is that migrations get reverted.

    Each migration runs in its own transaction, so a failure leaves the versions
    before it applied and the failing one absent — a resumable state rather than
    a half-applied one. Raises on failure; the caller decides what that means
    (supervisor: block job children, still start the bot).
    """
    _ensure_migrations_table(conn)
    done = set(applied_versions(conn))
    available = _available(direction)

    if direction == "up":
        pending = [(v, p) for v, p in available if v not in done]
        limit = steps or None
    elif direction == "down":
        pending = [(v, p) for v, p in available if v in done]
        pending.reverse()
        limit = 1 if steps is None else (None if steps == 0 else steps)
    else:
        raise ValueError(f"unknown direction: {direction}")

    if limit is not None:
        pending = pending[:limit]

    changed: list[str] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute(sql)
            if direction == "up":
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            else:
                conn.execute(
                    "DELETE FROM schema_migrations WHERE version = %s", (version,)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception(f"Migration {version} ({direction}) failed")
            raise
        changed.append(version)
        log.info(f"Migration {version} {direction} applied")
    return changed


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


def finish_run(
    conn: psycopg.Connection, run_id: int, exit_code: int, status: str = "finished"
) -> None:
    """Close a run. `status` is a parameter because not every closed run ran:
    a refusal and a restart-orphan both end as 'missed', and recording those as
    'finished' would make /jobs claim work happened that did not."""
    conn.execute(
        "UPDATE job_runs SET status = %s, finished_at = now(), exit_code = %s "
        "WHERE id = %s",
        (status, exit_code, run_id),
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


# ── The /jobs read path and the manual-run queue ─────────────────────────────

_RUN_COLUMNS = (
    "id",
    "job_name",
    "scheduled_for",
    "trigger",
    "status",
    "started_at",
    "finished_at",
    "exit_code",
    "created_at",
)
_RUN_SELECT = ", ".join(_RUN_COLUMNS)


def _run_row(row) -> dict:
    return dict(zip(_RUN_COLUMNS, row))


def latest_runs(conn: psycopg.Connection, jobs) -> dict[str, dict]:
    """The most recent row for each named job, keyed by job name.

    Ordered by id, NOT by started_at. A `missed` row is inserted with started_at
    NULL, so ordering by started_at would skip past it to an older successful
    run and let a job that has stopped running keep reporting green — precisely
    the silence /jobs exists to break.

    A job with no history is absent from the mapping rather than present as
    None, so the caller can tell "never run" from "ran and told us nothing".
    """
    rows = conn.execute(
        f"SELECT DISTINCT ON (job_name) {_RUN_SELECT} FROM job_runs "
        "WHERE job_name = ANY(%s) ORDER BY job_name, id DESC",
        (list(jobs),),
    ).fetchall()
    return {row[1]: _run_row(row) for row in rows}


def enqueue_manual(conn: psycopg.Connection, job: str) -> int:
    """Record an on-demand request for the supervisor to claim.

    The commands daemon is a CHILD of the supervisor and cannot spawn a job
    itself, which is why `trigger='manual'` and `status='queued'` exist from the
    first migration: a manual run is an INSERT here and a claim on the next tick.

    scheduled_for stays NULL. latest_scheduled_for is max(scheduled_for), which
    ignores NULLs, so no manual run can consume a scheduled fire time.
    """
    row = conn.execute(
        "INSERT INTO job_runs (job_name, scheduled_for, trigger, status) "
        "VALUES (%s, NULL, 'manual', 'queued') RETURNING id",
        (job,),
    ).fetchone()
    conn.commit()
    return row[0]


def queued_runs(conn: psycopg.Connection) -> list[dict]:
    """Unclaimed manual requests, oldest first."""
    rows = conn.execute(
        f"SELECT {_RUN_SELECT} FROM job_runs WHERE status = 'queued' ORDER BY id"
    ).fetchall()
    return [_run_row(row) for row in rows]


def claim_queued(conn: psycopg.Connection, run_id: int) -> bool:
    """Move one queued row to running. False if it was no longer queued.

    The status predicate in the WHERE clause is the guard against a double
    spawn: the tick acts on a list it read a moment earlier, and only a row
    still in `queued` may be claimed.
    """
    cur = conn.execute(
        "UPDATE job_runs SET status = 'running', started_at = now() "
        "WHERE id = %s AND status = 'queued'",
        (run_id,),
    )
    conn.commit()
    return cur.rowcount == 1

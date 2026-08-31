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

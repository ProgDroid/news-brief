"""Scheduled logical backup: `pg_dump` into the appdata volume, retained N days.

Runs as an ordinary job — a `brief.py` mode in JOB_MODES, so it takes the same
advisory lock and writes the same `job_runs` row as every other job, and the
supervisor schedules it from `scheduler.SCHEDULES` with no special case.

Two properties are load-bearing and are why this is a module rather than four
lines in `mode_collect`:

**The version guard.** `pg_dump` is FORWARD compatible only. A client newer than
the server dumps it fine; a client OLDER than the server aborts and writes
nothing. The base image is Debian trixie, whose `postgresql-client` is 17, while
the stack runs postgres 18 — so the naive install line yields a job that fails
every single night. It fails quietly, too: "no dump file for today" is
indistinguishable from "the job has not run yet" unless something checks. The
guard compares the two majors and refuses loudly, so a future Postgres bump that
outruns the pinned client alerts instead of rotting.

**No password in argv.** `ps` inside the container would otherwise show it. The
connection is parsed out of `db.conninfo()` — which may be a DATABASE_URL or the
discrete POSTGRES_* variables — and the password is handed over as PGPASSWORD in
the child's environment while everything else goes as flags.

Retention is deliberately its own 14-day window rather than an entry in
`retention.py`'s `_families()`: that module runs a 90-day window, and adding
dumps to it would silently override this one. Pruning is fail-safe — any error
leaves files in place and never fails a backup that already succeeded.
"""

import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

import db
from common import DATA_DIR, log

DEFAULT_RETENTION_DAYS = 14
RETENTION_DAYS_ENV = "NEWSBRIEF_BACKUP_RETENTION_DAYS"

BACKUP_DIR_NAME = "backups"
DUMP_PREFIX = "newsbrief-"
DUMP_SUFFIX = ".dump"
DUMP_GLOB = f"{DUMP_PREFIX}*{DUMP_SUFFIX}"

# How long pg_dump may run before it is abandoned. Generous: the dump is small
# now, but a restore-worthy backup of a slow disk is worth waiting for. Bounded
# because a job child that never exits is a job that never reports.
DUMP_TIMEOUT_SECONDS = 30 * 60

# The real runner, named so a test can assert the injectable default is not a
# test double that shipped by accident.
_DEFAULT_RUNNER = subprocess.run

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_CLIENT_VERSION_RE = re.compile(r"(\d+)(?:\.\d+)*")


# ── versions ──────────────────────────────────────────────────────────────────


def _client_major(version_output: str) -> int | None:
    """Major version out of `pg_dump --version` output, or None if unreadable.

    Handles both the plain `pg_dump (PostgreSQL) 18.1` and Debian's
    `pg_dump (PostgreSQL) 17.6 (Debian 17.6-1.pgdg13+1)`. Unreadable is None
    rather than a guess: an unparseable version means the guard cannot do its
    job, which is a refusal, not a pass.
    """
    if not version_output:
        return None
    # Skip the leading "pg_dump" so a digit in the program name can never match.
    tail = version_output.split(")", 1)[-1] if ")" in version_output else version_output
    m = _CLIENT_VERSION_RE.search(tail)
    return int(m.group(1)) if m else None


def _server_major(version_num: int) -> int:
    """180003 -> 18. `server_version_num` is major*10000 + minor from PG10 on."""
    return int(version_num) // 10000


def version_refusal(client_major: int | None, server_major: int) -> str | None:
    """The reason this pair cannot produce a dump, or None if it can.

    Client newer than server is fine and common — that is the direction pg_dump
    supports. Only older-dumping-newer is refused, plus an unreadable client.
    """
    if client_major is None:
        return (
            "pg_dump is missing or its version is unreadable, so no backup can "
            "be taken. The image must install postgresql-client matching the "
            f"server major ({server_major})."
        )
    if client_major < server_major:
        return (
            f"pg_dump is version {client_major} but the server is "
            f"{server_major}; pg_dump refuses to dump a newer server, so this "
            "would write nothing. Install postgresql-client-"
            f"{server_major} in the image."
        )
    return None


def _probe_client_version() -> str:
    """`pg_dump --version` output, or "" if it cannot be run at all."""
    try:
        done = _DEFAULT_RUNNER(
            ["pg_dump", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"Backup: could not run pg_dump --version: {e}")
        return ""
    return (done.stdout or "").strip()


# ── the dump ──────────────────────────────────────────────────────────────────


def backup_dir() -> Path:
    """Resolved at call time so a test can relocate DATA_DIR."""
    return DATA_DIR / BACKUP_DIR_NAME


def dump_path(today: str) -> Path:
    return backup_dir() / f"{DUMP_PREFIX}{today}{DUMP_SUFFIX}"


def dump_command(parts: dict, target: Path) -> list[str]:
    """argv for pg_dump. The password is NOT here — see dump_env."""
    cmd = [
        "pg_dump",
        "--format=custom",
        # The restore target may be a different role and database name than the
        # source, which is exactly the case success criterion 10 exercises.
        "--no-owner",
        "--no-privileges",
        "--file",
        str(target),
    ]
    if parts.get("host"):
        cmd += ["--host", str(parts["host"])]
    if parts.get("port"):
        cmd += ["--port", str(parts["port"])]
    if parts.get("user"):
        cmd += ["--username", str(parts["user"])]
    if parts.get("dbname"):
        cmd += ["--dbname", str(parts["dbname"])]
    return cmd


def dump_env(parts: dict) -> dict:
    """A COPY of the environment carrying PGPASSWORD; os.environ is untouched."""
    env = dict(os.environ)
    password = parts.get("password")
    if password:
        env["PGPASSWORD"] = str(password)
    else:
        env.pop("PGPASSWORD", None)
    return env


def create_dump(parts: dict, target: Path, *, runner=None) -> int:
    """Dump to a `.part` file and rename on success. Returns the size in bytes.

    Writing under the final name would leave a truncated dump wearing today's
    date if the process died mid-write — and retention would then keep it for a
    fortnight as though it were good. The rename is the commit.
    """
    runner = runner or _DEFAULT_RUNNER
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        done = runner(
            dump_command(parts, part),
            env=dump_env(parts),
            capture_output=True,
            text=True,
            timeout=DUMP_TIMEOUT_SECONDS,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"pg_dump exited {done.returncode}: "
                f"{(done.stderr or '').strip() or 'no stderr'}"
            )
        if not part.exists() or part.stat().st_size == 0:
            # Exit 0 having written nothing is a lie the ledger must not record
            # as a successful backup.
            raise RuntimeError("pg_dump reported success but wrote no dump file")
        size = part.stat().st_size
        os.replace(part, target)
        return size
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError as e:
                log.warning(f"Backup: could not remove partial dump {part}: {e}")


# ── retention ─────────────────────────────────────────────────────────────────


def _resolve_days(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw is None or raw == "":
        return DEFAULT_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError:
        log.warning(
            f"Invalid {RETENTION_DAYS_ENV}={raw!r}; using {DEFAULT_RETENTION_DAYS}"
        )
        return DEFAULT_RETENTION_DAYS


def _file_date(name: str):
    """The YYYY-MM-DD in a dump filename as a date, or None if absent/invalid.
    None means 'undateable' -> never pruned."""
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def prune_dumps(today: str, days: int) -> int:
    """Delete dumps strictly older than (today - days). Returns how many went.

    Fail-safe throughout: this runs AFTER a dump that has already succeeded, and
    a retention error must never turn that success into a failed job.
    """
    if days <= 0:
        return 0
    try:
        cutoff = datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=days)
    except ValueError as e:
        log.warning(f"Backup retention: unusable date {today!r}: {e}")
        return 0
    deleted = 0
    try:
        for path in backup_dir().glob(DUMP_GLOB):
            d = _file_date(path.name)
            if d is not None and d < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                except OSError as e:
                    log.warning(f"Backup retention: could not delete {path}: {e}")
    except Exception as e:
        log.warning(f"Backup retention: skipped: {e}")
    return deleted


# ── the job ───────────────────────────────────────────────────────────────────


def run_backup(today: str | None = None, *, runner=None, days: int | None = None):
    """Take today's dump and prune old ones. Raises on any failure to dump.

    Raising is deliberate: `run_job` wraps every mode, logs the traceback and
    sends the Telegram alert, so a refusal here reaches the operator through the
    same path as any other job failure rather than inventing a second one.
    """
    today = today or datetime.utcnow().strftime("%Y-%m-%d")
    parts = conninfo_to_dict(db.conninfo())

    with db.connect() as conn:
        row = conn.execute("SELECT current_setting('server_version_num')").fetchone()
    server_major = _server_major(int(row[0]))

    client_major = _client_major(_probe_client_version())
    refusal = version_refusal(client_major, server_major)
    if refusal:
        raise RuntimeError(refusal)

    target = dump_path(today)
    size = create_dump(parts, target, runner=runner)
    pruned = prune_dumps(today, _resolve_days(days))

    summary = {"path": str(target), "bytes": size, "pruned": pruned}
    log.info(
        f"Backup: wrote {target.name} ({size:,} bytes), pruned {pruned} old dump(s)"
    )
    return summary

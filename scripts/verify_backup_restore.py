"""Exercise a backup dump by RESTORING it — Epic 7 success criterion 10.

`seed` writes known rows into a source database; `verify` reads the same shape
back out of a restored one and compares. Run between them:

    docker run --rm -v <dir>:/app/logs -e DATABASE_URL=<source> <image> backup
    docker run --rm -v <dir>:/app/logs --entrypoint pg_restore <image> \\
        --dbname=<target> --no-owner --no-privileges /app/logs/backups/<file>

An unexercised backup is a belief, not a property, which is why this lives in
the repo rather than in a session transcript: the next person to change the
schema, the client major, or the dump format can re-run it.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402  (path shim above must run first)

SEED_JOBS = [
    ("collect", "scheduled", "finished", 0),
    ("submit", "catchup", "finished", 0),
    ("monitor", "manual", "missed", None),
    ("backup", "scheduled", "running", None),
]


def seed():
    with db.connect() as conn:
        applied = db.run_migrations(conn, direction="up")
        conn.commit()
        print(f"migrations applied: {applied or '(already current)'}")
        for name, trigger, status, code in SEED_JOBS:
            conn.execute(
                "INSERT INTO job_runs (job_name, scheduled_for, trigger, status, "
                "exit_code) VALUES (%s, now(), %s, %s, %s)",
                (name, trigger, status, code),
            )
        conn.commit()
    print(f"seeded {len(SEED_JOBS)} job_runs rows")
    return 0


def _snapshot(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        ).fetchall()
    ]
    counts = {}
    for t in tables:
        counts[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    rows = conn.execute(
        "SELECT job_name, trigger, status, exit_code FROM job_runs ORDER BY id"
    ).fetchall()
    return tables, counts, rows


def verify():
    with db.connect() as conn:
        tables, counts, rows = _snapshot(conn)

    print(f"tables restored: {tables}")
    print(f"row counts: {counts}")

    problems = []
    for required in ("job_runs", "schema_migrations", "users", "settings"):
        if required not in tables:
            problems.append(f"table {required!r} is missing from the restore")

    got = [(r[0], r[1], r[2], r[3]) for r in rows]

    # A SUBSET check, not equality, and deliberately so. Two things legitimately
    # put rows in the dump that are not in SEED_JOBS:
    #
    #   1. whatever the source database already held (a test run, an earlier job);
    #   2. the backup job's OWN ledger row. run_job writes it before the dump
    #      starts, so pg_dump always captures that row as `running` -- it cannot
    #      observe itself finishing. Comparing the restore against a post-hoc
    #      snapshot of the source would therefore fail forever, because the
    #      source row flips to `finished` the instant the job returns.
    #
    # The stable property is that the seeded rows survive, in order, unaltered.
    missing = [r for r in SEED_JOBS if r not in got]
    if missing:
        problems.append(f"seeded rows missing from the restore: {missing}")
    else:
        order = [got.index(r) for r in SEED_JOBS]
        if order != sorted(order):
            problems.append(f"seeded rows restored out of order: {order}")
        else:
            print(f"all {len(SEED_JOBS)} seeded rows restored, in order")

    in_flight = [r for r in got if r[0] == "backup" and r[2] == "running"]
    if not in_flight:
        problems.append(
            "the backup job's own `running` row is absent -- the dump did not "
            "capture the in-flight run, so run_job's ledger write may have moved"
        )
    else:
        print("the backup job's own in-flight row is present, as expected")

    # The restored database must also be a database the app considers current:
    # a fresh `up` has nothing left to apply.
    with db.connect() as conn:
        pending = db.run_migrations(conn, direction="up")
        conn.commit()
    if pending:
        problems.append(f"restore was not schema-current; up applied {pending}")
    else:
        print("migration runner reports the restored schema is current")

    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nOK: restored database matches the source and the app accepts it")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "seed":
        sys.exit(seed())
    if action == "verify":
        sys.exit(verify())
    print("usage: verify_backup_restore.py [seed|verify]")
    sys.exit(2)

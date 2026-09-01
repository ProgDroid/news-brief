# Backup and restore — runbook

The `backup` job runs daily at **07:00 UTC**, after the 06:00 collect, so it captures
the day's largest write rather than yesterday's state. It is an ordinary job: it takes
the same advisory lock and writes the same `job_runs` row as every other one, so
`/jobs` shows it and `/run backup` forces one.

Dumps land in the appdata volume at `/app/logs/backups/newsbrief-YYYY-MM-DD.dump`
(`pg_dump --format=custom`), and **14** are kept. Override with
`NEWSBRIEF_BACKUP_RETENTION_DAYS`; `<= 0` prunes nothing. That variable must be
declared in the `&newsbrief` compose anchor to have any effect — setting it on the
host or in `.env` alone delivers nothing to the container.

Note the two volumes are different on purpose: dumps go to the **appdata** volume,
the database lives in **`newsbrief-pgdata`**. A backup sharing a volume with the
thing it backs up is not a backup.

---

## The version coupling — read this before bumping Postgres

`pg_dump` is **forward compatible only**. A newer client dumps an older server
fine; a client OLDER than the server aborts and writes nothing.

The image installs `postgresql-client-18` from **PGDG**, not Debian. Debian trixie's
`postgresql-client` is version **17**, and against the `postgres:18` server it would
produce a backup job that fails every single night — silently, because "no dump file
for today" looks exactly like "the job has not run yet".

So the client major in `Dockerfile` and the server major in `docker-compose.yml` are a
pair. **If you bump one, bump the other.** `backup.version_refusal` compares them at
run time and refuses with an alert if they ever diverge, so the coupling fails loudly
rather than rotting — but it fails, and you get no backup until it is fixed.

---

## Restoring

The dump is `--no-owner --no-privileges`, so it restores into a different role and
database name than it came from. That is deliberate: in a real recovery you are
rarely restoring into an identically-named world.

```bash
# 1. Stop the app so nothing writes while you restore. Postgres stays up.
docker compose stop newsbrief

# 2. Restore into a FRESH database, never over a live one.
docker compose exec -T postgres createdb -U newsbrief newsbrief_restored
docker compose exec -T newsbrief pg_restore \
    --host=postgres --username=newsbrief --dbname=newsbrief_restored \
    --no-owner --no-privileges /app/logs/backups/newsbrief-YYYY-MM-DD.dump

# 3. Check it before you trust it.
docker compose exec -T postgres psql -U newsbrief -d newsbrief_restored \
    -c "SELECT job_name, status, started_at FROM job_runs ORDER BY id DESC LIMIT 10;"

# 4. Swap only once step 3 looks right.
docker compose exec -T postgres psql -U newsbrief -d postgres \
    -c "ALTER DATABASE newsbrief RENAME TO newsbrief_broken;" \
    -c "ALTER DATABASE newsbrief_restored RENAME TO newsbrief;"
docker compose start newsbrief
```

Keep `newsbrief_broken` until the next brief has been delivered from the restored
database. Dropping it is the step you cannot undo.

---

## The restore has been exercised — 2026-09-01

Success criterion 10 requires that a dump taken by the scheduled job actually
restores and that the application starts against it. **An unexercised backup is a
belief, not a property**, so this was run for real rather than reasoned about:

1. Seeded a source Postgres 18 with four known `job_runs` rows.
2. Ran the **real `backup` mode in the real built image** against it —
   `Backup: wrote newsbrief-2026-09-01.dump (9,650 bytes), pruned 0 old dump(s)`.
   The image's client reported `pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg13+2)`.
3. `pg_restore`d that dump into a **second, empty** database owned by a *different*
   role (`restoreuser`/`restored`, not `newsbrief`/`newsbrief_test`).
4. Verified: all four tables present, both migrations recorded, the four seeded rows
   restored in order, and `db.run_migrations(up)` reporting nothing left to apply —
   i.e. the app considers the restored schema current.

Re-run it with `scripts/verify_backup_restore.py` (`seed`, then the dump and restore,
then `verify`); the file's docstring carries the exact commands.

**One expected artefact, so it is not mistaken for corruption:** every dump contains
the backup job's own `job_runs` row with `status = running`. `run_job` writes that row
before the dump begins, so `pg_dump` can never observe the backup finishing. A restored
database therefore always shows one `running` backup row that no process owns; the next
boot's `reclaim_orphans` closes it. `verify` asserts this row is present rather than
filtering it out, because its absence would mean the ledger write had moved.

# Supervisor cutover — runbook

The cutover replaces five cron-invoked services with one supervised container plus
Postgres. It is a **flag day for scheduling**: the host cron entries and the
`newsbrief-commands` service must come out in the same maintenance window that the
supervisor goes in. Leave either behind and you get double collects, or a Telegram
409 from two `getUpdates` consumers.

Read the rollback first. It is written down rather than derived at the time because
the situation it is for is one where the Telegram bot — the only control channel —
is down.

---

## Before you start

1. `POSTGRES_PASSWORD` must be set in the stack `.env`. The compose file fails fast
   with `set POSTGRES_PASSWORD in .env` rather than silently starting an
   unauthenticated database.
2. `DATABASE_URL` is optional; it defaults to
   `postgresql://newsbrief:newsbrief@postgres:5432/newsbrief`. If you set
   `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` away from their defaults you
   must set `DATABASE_URL` to match — the default is not derived from them.
3. Keep a copy of the pre-cutover `docker-compose.yml` on the host. The rollback
   below reconstructs it, but a copy is faster.

---

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

Stop the supervisor first (`docker compose stop newsbrief`) so only one process
polls `getUpdates`; a second consumer 409s and the bot goes deaf.

The Postgres service can stay up during a rollback: nothing in the pre-cutover
code path reads it. Leaving it up also preserves the `job_runs` ledger for the
retry, and the `newsbrief-pgdata` volume survives a `docker compose down` either
way (it is a named volume, so only `down -v` destroys it).

---

## Cutover

1. In the same maintenance window, deploy the new compose file and pull the image:

       docker compose pull
       docker compose up -d

2. **Delete the four `docker compose run --rm newsbrief-*` cron entries on the
   host.** This is not optional and it is not deferrable to later in the day:
   against the new compose file those services no longer exist, so each invocation
   fails confusingly; against an old file still lying around they produce a second
   collect on top of the supervisor's.

---

## Cutover verification

    docker compose ps
    docker compose logs newsbrief | tail -40
    docker compose exec -T postgres psql -U newsbrief -d newsbrief \
      -c "SELECT job_name, scheduled_for, trigger, status, started_at FROM job_runs ORDER BY id;"

Expected:

- `docker compose ps` shows exactly two services: `newsbrief`, `postgres`.
- The log shows `=== SERVE (supervisor) ===`, migrations applied, and
  `[commands] started`. It does **not** show `=== COLLECT ===`.
- `job_runs` has no row whose `started_at` is the deploy time — a stack `up`
  starts no work, which is the point of the whole change. Jobs appear when they
  are due, and a redeploy runs only what the catch-up rule says was actually
  missed.
- `/jobs` in Telegram answers (phase 2 — `news-brief-0q0.9`; until it lands, use
  the psql query above).
- One `getUpdates` consumer only: the bot responds and the log shows no 409.

---

## Shutdown budget

`docker compose stop|down|restart` sends SIGTERM and then waits
`stop_grace_period` (45s) before SIGKILL. The supervisor's own budget is
`supervisor.SHUTDOWN_BUDGET_SECONDS` (30s) for the children to exit, after which
it closes its `job_runs` rows and only then escalates to SIGKILL.

**Those two numbers are a pair.** If the container is killed before the ledger
writes land, the rows stay `running`, and the next boot's `reclaim_orphans` fires
"Orphaned by a restart and NOT retried" on what was an ordinary deploy — the false
alert that trains an operator to ignore the real one. If you tune either number,
tune both, and keep the grace period comfortably above the budget.

A clean stop is therefore expected to take up to ~35s when a job is running. That
is not a hang.

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

   **Generate it with `openssl rand -hex 32`. Avoid `$`.**
   The password is no longer spliced into a URI — compose passes the discrete
   `POSTGRES_*` variables through and `db.conninfo` builds the libpq connection
   string with psycopg's `make_conninfo`, which escapes each value. `/`, `%` and
   `@` are safe in a password now (they were not before). `$` is the survivor,
   and it is compose's trap rather than libpq's:

   | Character | What happens |
   |---|---|
   | `$` | eaten by compose interpolation unless written `$$` — and it truncates identically on the app *and* on the postgres service's own `POSTGRES_PASSWORD`, so the stack comes up and works, silently, with a shorter password than you wrote |

   `-hex` output cannot contain a `$`, which is the whole reason to prefer it.

2. `DATABASE_URL` is optional and now defaults to **empty**: the app reaches the
   bundled database through `POSTGRES_HOST`/`PORT`/`USER`/`PASSWORD`/`DB`, so
   setting the password alone is enough and changing the user or database name
   needs no second edit. Set `DATABASE_URL` yourself only to point at a database
   outside this stack — it wins over the five when non-empty, and being a URI it
   brings the percent-encoding rules back for its own password. Note that
   `POSTGRES_PASSWORD` is still required in that case: the bundled `postgres`
   service starts regardless and demands it, even when nothing is using it.

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
- **You should see one row per schedule, `status = missed`, `started_at` NULL,
  and a `first boot: recording ... as missed` line per job in the log.** That is
  the first-boot seed, and it is correct: an empty ledger cannot tell "host cron
  already ran this today" from "genuinely missed", so each job's current fire
  time is consumed once without running it. Without the seed, a cutover between
  06:00 and 08:00 UTC would run a second `collect` on top of the morning's, and
  a cutover in the first 15 minutes of any hour would re-run `monitor` — which
  goes down the live sell path.
- `job_runs` has **no row whose `started_at` is the deploy time** — a stack `up`
  starts no work, which is the point of the whole change. The seed rows above
  have a NULL `started_at`, so this stays true as written. Jobs appear when they
  are next due.
- The seed suppresses exactly one legitimate case: if the host was down across
  06:00 and you bring the stack up at 07:00, there is no catch-up `collect`. It
  is visible in the ledger as `missed`, and
  `docker compose run --rm newsbrief collect` recovers it.
- `/jobs` in Telegram answers (phase 2 — `news-brief-0q0.9`; until it lands, use
  the psql query above).
- One `getUpdates` consumer only: the bot responds and the log shows no 409.

---

## Shutdown budget

`docker compose stop|down|restart` sends SIGTERM and then waits
`stop_grace_period` (60s) before SIGKILL. The supervisor gives its children
`supervisor.SHUTDOWN_BUDGET_SECONDS` (25s) to exit, then closes its `job_runs`
rows, and only then escalates to SIGKILL itself.

Every phase is bounded, and the worst case is the sum of the bounds:

| Phase | Bound |
|---|---|
| a tick-path connect already in flight when the signal arrived | 5s |
| children exit after the SIGTERM broadcast | 25s |
| drain the final output of whichever children did exit | 2s |
| open the one connection the rows close on | 5s |
| the ledger writes (4 schedules x 2 statements x 0.5s) | 4s |
| reap whatever had to be SIGKILLed | 2s |
| **worst case** | **43s** |

The 60s `stop_grace_period` is deliberately more than that, not equal to it. The
remaining ~17s is **not slack to reclaim**: it is cover for the two things the
sum cannot bound — `statement_timeout` does not abort a COMMIT already blocked
in fsync, and closing a connection whose socket is wedged is unbounded too.
Grace costs nothing when it is not used, because Docker waits only until the
process exits.

**The two numbers are a pair.** If the container is killed before the ledger
writes land, the rows stay `running`, and the next boot's `reclaim_orphans` fires
"Orphaned by a restart and NOT retried" on what was an ordinary deploy — the false
alert that trains an operator to ignore the real one. If you tune either number,
tune both.

A clean stop can therefore take up to ~43s when a job is running. That is not a
hang. In practice it is about a second: the job modes install no SIGTERM handler,
so they exit on the signal and nothing else in the list is reached.

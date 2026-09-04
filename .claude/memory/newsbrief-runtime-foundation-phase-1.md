---
name: newsbrief-runtime-foundation-phase-1
description: "Epic 7 phases 1 AND 2 BUILT and DEPLOYED (2026-09-01, 2026-09-02) — supervisor + Postgres, 5 containers to 2, internal scheduling, and configuration out of the environment into `settings` rows. Epic 3 (KB core) is unblocked. Read before touching scheduling, the cutover, or any config knob."
metadata: 
  node_type: memory
  type: project
  originSessionId: 3e4e66d4-de2c-4575-996b-51b70fecedf6
  modified: 2026-09-02T09:52:02.786Z
---

**2026-08-31 / 2026-09-01. Epic 7 phase 1 BUILT, reviewed, and DEPLOYED on 2026-09-01.**
27 commits on `main` (`b2c3521`..`ee37216`), 1101 tests, ruff clean. All pushed. Everything earlier on
`main` is pushed — see [[newsbrief-kb-architecture-2026-08-29]], whose unpushed note was
corrected the same day.

Spec: `docs/superpowers/specs/2026-08-31-process-architecture-and-storage-design.md` (decides
`bqa.1` process architecture and `bqa.2` storage).
Plan: `docs/superpowers/plans/2026-08-31-runtime-foundation-phase-1.md`.
Runbook: `docs/runbooks/2026-08-31-supervisor-cutover.md`.
**Read the spec before re-opening either decision** — §11 is the red-team record and §12 lists
the positions I reversed under argument.

## What it is

Five containers become **two**: one application container running a `supervisor` process that
owns resident children (the Telegram `commands` daemon) and job children (`submit`, `collect`,
`weekly`, `monitor`), plus **Postgres**. Scheduling moves inside the supervisor, so a redeploy
no longer fires the pipeline out of order — that defect was the trigger for the whole epic.

Chosen against a single-container + SQLite option **with the argument for it in front of him**;
he took the two-container shape. Multi-user is deliberately kept as *option value* (his words:
"It's genuinely just an option I want open"), which is why config tables carry `user_id` and KB
tables do not — the world is shared, the reading of it is personal.

## DEPLOY STATE — CUTOVER DONE 2026-09-01

**Deployed. The hold is OVER; do not re-apply it.** He pushed `main` (tip `ee37216`,
27 commits ahead at the time) and redeployed on the host the same evening. CI run
`33546104364` green, `build-and-push` ran (48s) so the image really published — a green run
alone would not have proved that, the `paths:` filter can skip the job.

Confirmed by him: **the four host cron entries are deleted**, and **`/jobs` responds** — which
jointly prove one `getUpdates` consumer (no 409), the commands daemon resident under the
supervisor, and the daemon's read path to Postgres.

**VERIFIED END TO END the same evening.** `/jobs` showed `submit` and `monitor` both ticked
after their 20:00 and hourly fires, so the supervisor genuinely FIRES jobs — not merely seeds
them. That was the last open question, and it is closed: read path (`/jobs` answering), write
path (`/run`), and the tick loop spawning + reaping a job child are all confirmed live. Nothing
about Phase 1 is outstanding. Host state is still never knowable from this repo — see
[[live-state-on-deploy-host]] — so ask him rather than infer for anything NEW.

Pre-registered seed expectation, for anyone reconstructing that boot: four rows, `status=missed`,
`started_at` NULL — submit 2026-08-31 20:00, collect 2026-09-01 06:00, weekly 2026-08-30 21:00,
monitor 2026-09-01 HH:00. The alarming-looking output IS the success condition: the seed burns
each job's current fire time because an empty ledger cannot tell "host cron already ran this"
from "genuinely missed". Without it a 19:00 cutover re-fires `monitor` down the live sell path.

The deploy applied **two** migrations: `0001` plus `0002` (`job_runs.created_at` — a queued
manual run had no timestamp to age; `scheduled_for` stays NULL and `started_at` is not set until
the claim).

**The coupling that mattered is now spent.** First-boot seeding protected only the FIRST boot
against a surviving cron entry; with cron retired that risk is closed, and it does not return.

## The rule this epic converged on

**A guard must test the exact predicate its consumer reads.** First-boot seeding skipped a job
when any `job_runs` row existed, while `decide` read `latest_scheduled_for`. Those agree until a
manual run writes a NULL `scheduled_for` — then the seed calls the job "touched", skips it, and
`decide` still sees `None` and fires it. The guard re-opened the hole it existed to close.
Both now call the same function, so there is no second implementation left to drift.

Sibling rule from the same epic, already in the KB memory: **quarantine is the default for an
unmeasured field; measurement lifts it.**

**How to apply:** when you write a guard, find the line that consumes the thing being guarded
and call *that*. Two predicates that "mean the same" are a latent divergence.

## Process notes worth reusing

- Subagent-driven development worked here, but **three findings were defects in MY OWN plan**,
  not in the implementers' work. Review the brief when a reviewer flags the implementation.
- One implementer died on a session limit mid-fix-round; reading its uncommitted diff, verifying
  it green and committing it beat discarding the round. See [[subagent-review-stalls]].
- A plan step told an agent to run `docker compose up -d` locally, which would have started a
  second Telegram `getUpdates` consumer and 409'd the live bot. **Local verification of this
  stack is `docker compose config` only.**
- Verify a new test by making it FAIL first. All three tests added at the end were checked
  against a deliberate mutant before being trusted.
- An intermittent test failure turned out to be a real production bug, not flake — full
  write-up in `learnings/python-subprocess-inherits-stdin-winerror-50.md` and
  `learnings/probe-failures.md` §16.

## Open follow-ups (all filed in bd)

`0q0.7` phase-2 config into the DB · `0q0.12` job children have no max runtime ·
`0q0.13` HTTP block covers `requests` only · `0q0.14` a permanently dead resident
eventually goes quiet.

**Epic 7 is 12/15 as of 2026-09-02** (phase 2 closed, see below). **The deploy has happened, so the
pre-deploy freeze no longer applies.** What is left is `0q0.12` / `0q0.13` / `0q0.14`, all P3–P4
hardening; `0q0.12` wants real production timings (true collect durations, to size a timeout that
will not kill working runs).

**`0q0.8` CLOSED 2026-09-01 (`7e474d6`) — backup job BUILT; DEPLOYED 2026-09-02 with phase 2.** Daily 07:00 UTC
`pg_dump`, 14 dumps kept, an ordinary job so it takes the lock and shows in `/jobs`. The
non-obvious part, and the reason it needed a design rather than four lines: **Debian trixie ships
`postgresql-client` 17 while the stack runs postgres 18, and `pg_dump` is FORWARD compatible only**
— the Debian package would have produced a job failing every night, indistinguishable from one
that had not run. The image installs `postgresql-client-18` from PGDG, and `backup.version_refusal`
compares client vs server major at run time so a future Postgres bump alerts instead of rotting.
**The Dockerfile client major and the compose server major are a PAIR; bump both.** Restore
exercised for real (criterion 10): real mode, real image, restored into a second EMPTY database
owned by a different role. Runbook `docs/runbooks/2026-09-01-backup-restore.md`; re-run with
`scripts/verify_backup_restore.py`. Every dump contains the backup's own `job_runs` row as
`running` — `run_job` writes it before the dump starts, so pg_dump cannot see itself finish; that
is expected, not corruption.

`bqa.1`, `bqa.2`, `0q0.1`–`0q0.6`, `0q0.8`–`0q0.11`, `0q0.15` are closed.

**`0q0.10` closed 2026-09-01 — HE did it, in `ee37216` (+17 lines).** The rule stands and is
worth keeping: `.env*` is denied to **both** the Bash and the Read tool, so no agent can read
or write one. Re-probed rather than assumed. The right move when this recurs is to hand over a
paste-ready block and refuse to commit the result blind — this repo is PUBLIC, and a file you
cannot read is how a real password ships dressed as a placeholder. See
[[live-state-on-deploy-host]].

**`0q0.9` + `0q0.15` closed 2026-09-01 (`c7ea24b`).** `/jobs` reads the ledger; `/run <job>`
queues a row the supervisor claims after the due jobs (15m TTL, then discarded with an
alert). `0q0.15` was split out of `0q0.9` — he asked for the write path too when offered it.
Three decisions worth not re-litigating: rows are read **`id DESC`, never `started_at`**
(a `missed` row has a NULL `started_at`, so that ordering walks past the miss to an older
green run); the view iterates **`SCHEDULES`, not the rows**, so a job that has NEVER run is
visible rather than absent; and an unreachable ledger renders as **UNKNOWN, not empty** —
four jobs that never ran is a confident wrong answer. `scheduler.next_fire` clamps to
midnight because `previous_fire` re-anchors there; with `every_minutes=60` the unclamped
version is indistinguishable, so it would have shipped green.

**`0q0.11` closed 2026-09-01 (`27aee22`).** Compose no longer splices the password into a
`postgresql://` URI: it passes `POSTGRES_HOST/PORT/USER/PASSWORD/DB` through the anchor as
themselves, `DATABASE_URL` defaults to empty and is now only the outside-the-stack escape hatch,
and `db.conninfo()` builds the libpq string with psycopg's `make_conninfo`. `/ % @` in a password
are safe now. **`$` still is not, and cannot be fixed in code** — compose interpolation eats it
identically on the app and on the postgres service, so the stack comes up *working* with a
shorter password than he wrote; `openssl rand -hex 32` is still the instruction. The DB-test skip
guards moved from `DATABASE_URL` to `db.is_configured()` — the same rule this epic converged on,
applied again. The SDD workspace at
`.superpowers/sdd/2026-08-31-runtime-foundation-phase-1/` (gitignored) still holds the reviewer
reports and the full ruling ledger.

## PHASE 2 — CLOSED AND DEPLOYED 2026-09-02 (`0q0.7`, 6/6). Epic 7 now 12/15.

Seven commits (`7e474d6`..`f1ab987`) pushed together on 2026-09-02; the backup job rode along.
Suite **1232 with a database configured, none skipped**. `users`, `settings`, `sources`,
`preferences` and `runtime_state` are all rows; the environment keeps only secrets, the Postgres
connection, and the deploy-scoped handful named below.

**This unblocks Epic 3 (`bqa`, KB core) — the store and the migration runner it depended on both
exist now.** Spec order from here is §7.2 phase 1.5 (Epic 2 capture, as a supervisor job child
writing a `captured_items` table — NOT the JSONL the issue was originally filed with) then §7.4
phase 3 (`bqa.3`–`bqa.6`).

### The knob seam, which is what you will actually touch

`common.KNOBS` is the single source of truth for three things at once: what `common.<NAME>`
resolves to, what the first-boot importer copies out of the environment, and what a typo is
checked against. A knob is **absent as a module constant on purpose** — that absence is what makes
PEP 562 `__getattr__` fire, and it is why `0q0.7.2` moved ~32 knobs with no call site changed.
`Knob.key()` is the settings key AND the env name, and differs from the attribute name only where
history left them apart (`MODEL` ⇒ `NEWSBRIEF_MODEL`).

**Adding a knob is three things, not one:** a `KNOBS` entry, deleting the module constant, and a
`- VAR=${VAR:-}` line in the compose anchor — see [[env-var-needs-compose-passthrough]], which
phase 2 makes SHARPER rather than obsolete.

**The boundary is written down in the `KNOBS` comment; read it before arguing a knob should move.**
Credentials stay in the environment (a row lands in every `pg_dump`). So does anything
**deploy-scoped**: `NITTER_BASE_URL` (names a compose-network service, interpolated into
`RSS_FEEDS` at import, so "changes without recreating a container" is not a property it can have),
`NEWSBRIEF_DATA_DIR` / `NEWSBRIEF_LOG_FILE` (read before a DB connection can exist), and
`NEWSBRIEF_SCHEDULED_FOR` / `_TRIGGER` / `_RUN_ID` (describe one run, not configuration).

### `0q0.7.6` — the tail, and the pattern worth reusing

The last 14 knobs lived in OTHER modules' own `os.environ.get` calls and were out of `0q0.7.2`'s
scope. Two corrections to how it was filed: there are **two** retention windows, not one
(`NEWSBRIEF_RETENTION_DAYS` 90d for dated artifacts, `NEWSBRIEF_BACKUP_RETENTION_DAYS` 14d for
dumps — folding them together silently gives dumps 90 days), and `NITTER_BASE_URL` was **not** a
lazy read, so it could not simply become a `common.X`.

**A knob whose default should track another knob defaults to `""`, not to a copy of the value.**
`SIGNALS_MODEL` and `CLAIM_VERIFY_MODEL` are empty-means-follow-`MODEL`, read as
`common.SIGNALS_MODEL or common.MODEL`. Duplicating the literal would strand both post-generation
calls on the old model the moment the operator moved `NEWSBRIEF_MODEL`, with nothing to say so —
the exact rot [[newsbrief-model-config]] warns about after a model bump. Proved by mutation: swap
in the literal and two DB-backed tests fail.

**Coercion moved, so parse-and-warn branches die.** `coerce_knob` already warns on a malformed
value and falls back to a default every knob has by construction, so both `_resolve_days`
functions lost their try/except. A hand-editable row DOES need normalisation the environment read
used to do inline: `ENRICHMENT_PROVIDER` was `.lower()`ed on the way out of `os.environ`, and
without moving that to the comparison in `get_provider()` a row reading `Fixture` silently selects
the null provider.

**How to apply:** when moving a knob, check what the old `os.environ.get(...)` chain did AFTER the
read — `.strip()`, `.lower()`, `int()`, a default-bearing second argument. `coerce_knob` covers
strip and the type cast. It does not cover `.lower()`, and nothing warns you.

## 2026-09-04 — EPIC 7 IS CLOSED, 15/15. The KB chain is genuinely unblocked now.

The last three were hardening, and they were the only thing standing between the project and its
whole product direction: `bqa` (Epic 3) depended on `0q0`, and Epics 4, 5 and 6 depended on `bqa`.
Thirteen issues were blocked behind three P3/P4 beads. **`bd ready` now offers `bqa.4` (the
comprehension pipeline), `bqa.5`, `bqa.6` and `bqa.8`.**

- **`0q0.14`** — a permanently dead resident went quiet. `RestartTracker.record` returns True only
  on ENTERING an episode, and once the backoff ceiling spaces restarts past the 600s window the
  episode ends and can never be re-entered. For `commands`, the only control channel, that silence
  looks exactly like health. Now an hourly reminder, cleared on **stability** rather than on a
  successful spawn — a resident that comes up and dies every thirty seconds is still down, and
  clearing at spawn would reset the clock on every flap and never remind at all.
- **`0q0.12`** — a wedged job child ran forever. SIGTERM, then SIGKILL a grace later, on a LATER
  tick rather than a sleep: the loop is single-threaded and blocking it would stall every other
  child's reap and every resident's restart.
- **`0q0.13`** — see [[test-network-guard]].

**`JOB_MAX_RUNTIME_MINUTES` is a knob at a deliberately useless default (240).** It clears the
widest schedule grace — `backup`'s 180, NOT `collect`'s 120, which is what I assumed until my own
test caught it — so it can only ever catch a genuinely stuck job. **`news-brief-uk5` holds the
query to size it properly from `job_runs`; it is a row edit, not a deploy.** Do not lower it from
a guess: a cap below a legitimate collect kills working runs, which is worse than the bug.

**A test should pin the RELATION, not the number.** `test_the_default_limit_clears_the_longest_job
_grace` asserts the cap exceeds `max(s.grace_minutes for s in scheduler.SCHEDULES)`. Retiming any
job re-checks the cap automatically, and the test told me my first default was wrong — which a
test asserting `== 240` never could have.

**`supervisor.py` needed `import common` added** to read the cap: it had only
`from common import log, telegram_alert, EX_ALREADY_RUNNING`, and a knob MUST be reached as
`common.X`. See [[newsbrief-flag-access-module-attr]].

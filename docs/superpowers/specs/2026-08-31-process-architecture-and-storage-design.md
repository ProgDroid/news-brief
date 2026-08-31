# Process Architecture and Storage — Design

**Date:** 2026-08-31
**Status:** Design, revised after red-team review, awaiting approval
**Decides:** `news-brief-bqa.1` (process architecture), `news-brief-bqa.2` (storage engine)
**Amends:** §5 of `2026-08-29-knowledge-base-architecture-design.md`, which deferred storage and
framed the engine choice as downstream of process architecture

---

## 1. Why

The knowledge-base spec deferred this decision in a single paragraph and treated it as a narrow
technical question — one database server versus one file. Two forcing problems decide it, neither
of which appears in that spec.

### 1.1 A deploy runs the pipeline

The deployment host runs OpenMediaVault's Docker Compose plugin, which acts on a whole compose
file at a time. Making a change live and starting everything in the file are the same gesture.

`docker-compose.yml` declares five services off one YAML anchor, differing only in `command:`.
Four — `submit`, `collect`, `weekly`, `monitor` — are **one-shot batch jobs** meant to be invoked
as `docker compose run --rm <mode>` from cron. Nothing marks them as templates rather than as
things to run, because Compose has no vocabulary for that beyond `profiles:`, which is unused
here. `restart: "no"` stops them looping; it does not stop them running once.

So every image update fires a `collect`: a brief generated off-schedule, out of order relative to
its `submit`, and paper positions re-opened. The project notes record the symptom — "the host
re-runs the whole pipeline on every image pull" — without naming the mechanism.

The compose file is doing double duty as a **service manifest** and a **job catalogue**, and `up`
cannot tell them apart.

### 1.2 Operating five containers has a running cost

Stated by the operator and treated here as a first-class requirement: five services for one
application has been painful to manage, and reducing that count is a goal in itself, not a
side effect. The design target is **one application container plus its database**, with the
seams to split later if a specific failure ever argues for it (§3.4).

**This requirement appears to be in tension with §5.1, and the tension is narrower than it looks.**
A SQLite branch would land the design in *one* container — review argued exactly that — but only
by dropping off-host querying, since serving that on SQLite needs a shim container of its own and
arrives back at two. The real choice is **one container without off-host access, or two with it**,
and it was made deliberately in favour of the latter by explicit operator decision (§5.1). Anyone
revisiting this should weigh it as a purchase, not an oversight.

### 1.3 Multi-user is an option being kept open, not a requirement

A parallel thread wants news-brief to become adoptable by other people. **This is explicitly
option-value, not a scheduled requirement** — the operator's own framing is "genuinely just an
option I want open." It is recorded here so that later readers do not mistake it for demand.

What it buys, at the cost of one column on three tables plus a settings table that is
independently justified (§6.3):

- State grows a user dimension. Files cannot carry one; `sources.json` and `feedback.json` are
  singletons on a volume (`common.py:29`).
- Configuration leaves the environment, which is per-deployment by construction.
- The KB/render split becomes the tenancy boundary: the world is shared, the reading is personal.

**What it does not buy, and must not be used to justify:** an internal scheduler. An earlier draft
of this spec argued that ten users with different delivery times "is not forty cron entries, it is
a scheduler that reads a users table." That was a strawman and is struck. One external cron entry
at `*/5` running a user-iterating render mode would serve multi-user perfectly well. The case for
internal scheduling rests on §1.1 and §1.2, not on tenancy.

### 1.4 The coupling asserted in the KB spec does not hold

§5 of the KB spec asserted: several containers ⇒ a database server ⇒ Postgres; one consolidated
service ⇒ SQLite suffices. The premise is the table row *"multi-container writes over a Docker
volume are a known hazard."*

That hazard is a **network-filesystem** hazard — NFS, SMB, and the virtualised mounts of Docker
Desktop on Windows and macOS — where POSIX advisory locks are unreliable or absent. Several
containers on one host writing a local bind mount share a kernel and an inode; SQLite's locking
works as designed, and at ~120 writes/day contention is not a factor. **The deploy host is local
disk on Linux (confirmed with the operator).** The coupling is void.

The engine choice therefore stands or falls on its own merits — see §5.1, which is deliberately
written as an honest ledger rather than an advocacy case.

---

## 2. Decisions

| | Decision |
|---|---|
| **Process architecture** (`bqa.1`) | **Two containers: one application, one database.** The application container runs a resident supervisor; the Telegram daemon and all batch modes are its children. Not a container per mode. |
| **Job isolation** | Jobs run as **child OS processes**. A crash, OOM or SIGKILL in a two-hour collect cannot take down capture or the bot. |
| **Scheduling** | **Internal, user-aware, catch-up-aware**, owned by the supervisor against a `job_runs` table. Host cron is retired. |
| **Storage engine** (`bqa.2`) | **PostgreSQL**, arriving with phase 1 because `job_runs` needs a store and Epic 3 needs one regardless. |
| **Availability** | **Fail closed on work, fail open on observability.** A failed migration blocks job children and still starts the bot. |
| **Tenancy** | **Shape now, machinery later.** `user_id` columns, config in the database, a user-iterating scheduler. No auth, sessions, quotas or billing. |
| **Trading** | Deliberately **un-tenanted**. Live credentials are per-deployment and real money. |

---

## 3. Process architecture

### 3.1 Two containers

`newsbrief` runs `command: [serve]` with `restart: unless-stopped`, inheriting the existing
`&newsbrief` anchor. The four batch services are **deleted** from the compose file, not hidden
behind a profile — with scheduling internal, they have no reason to exist as services.

`postgres` runs an explicitly pinned major on its own named volume and **deliberately does not
inherit the anchor**: no `user: "${PUID}:${PGID}"` override — the official image manages its own
uid, and an override against an already-initialised data directory fails in a way that reads as
corruption — and none of the application environment.

> **Version discovery:** pin an explicit Postgres major and confirm the current stable major at
> implementation time rather than copying a number out of this document.

### 3.2 The supervisor

A new top-level module dispatched as a mode, so `ENTRYPOINT ["python", "brief.py"]` is unchanged
and the compose command stays a single word. It carries **no business logic** — that constraint is
what keeps its own crash surface small, and it is the mitigation for the risk in §3.3.

**Resident children.** `commands` today; `web` and `mcp` later. Restarted on exit with capped
exponential backoff.

**Job children.** `capture`, `submit`, `collect`, `weekly`, `monitor`, and later a per-user
`render`, spawned at their due time as `python brief.py <mode>`, reaped, exit code recorded. The
dispatch at `brief.py:3627-3635` is already seven zero-argument functions and is reused unchanged.

**Log ownership.** `common.py:39-43` installs a `RotatingFileHandler` on the shared volume.
Several processes each holding their own handler on one file fight over the rotation rename — a
latent defect today, since the `commands` daemon already overlaps the cron modes. Fix: children
get a stream handler only, and the **supervisor is the sole writer** of the rotating file, also
echoing to its own stdout so `docker logs` and OMV stay useful. Single writer removes the race by
construction, keeps the volume archive that dropping the file handler would lose, and buys
per-child attribution in a log that currently interleaves anonymously.

**Shutdown.** SIGTERM propagates to children, waits with a timeout, then kills stragglers, so a
host restart mid-run is safe rather than merely survivable.

**One policy:** a job still running when it next comes due is skipped, not doubled, with a counter
that alerts if skipping becomes chronic. A collect that consistently overruns its window is a
signal, not a nuisance.

### 3.3 The risk this accepts, and how it is bounded

Consolidation means a supervisor crash takes the Telegram bot down with it — the operator's only
control channel — and a failed schema migration could do the same. This is the strongest objection
to a single container and it is accepted deliberately, bounded three ways:

1. **Fail open on observability** (§8). A failed migration blocks job children and starts the bot
   anyway. The control channel must outlive the thing most likely to break it.
2. **No business logic in the supervisor.** Scheduling, spawning, reaping, logging. Nothing that
   parses a feed or calls an API.
3. **`restart: unless-stopped`** plus the crash-loop alert in §8.

Residual risk: a bug in the supervisor itself. Section 9 puts the testing weight there for that
reason.

### 3.4 The seams, stated as invariants

Splitting a child into its own container later must remain a compose edit, never a refactor. Three
invariants make that true, and they are testable:

1. **A child receives nothing but `argv` and environment.** No inherited objects, no file
   descriptors carrying state, no fork-shared memory.
2. **All coordination goes through Postgres.** `job_runs`, settings and per-user rows are the only
   channel between supervisor and children. No in-memory queues, no pipes carrying anything but
   log lines.
3. **No child reads supervisor-internal state**, and the supervisor reads no child's internals.

Given these, promoting `commands` to its own service is: add a service with `command: [commands]`,
remove it from the child list. Nothing else changes. **The container boundary stays cheap to move,
which is why it does not have to be decided correctly now.**

### 3.5 What this costs

A new top-level module is a documented three-place update in this repository: the `Dockerfile`
COPY line, the publish workflow's `paths:` filter, and **both** `ruff` file lists. Missing any of
them yields a runtime `ModuleNotFoundError` or silently escapes CI lint.

### 3.6 Debuggability is unaffected

`docker compose run` creates a *new* container from a service definition rather than attaching to
a running one, so it works against any service — including one that is crash-looping — and with
`--entrypoint python` it works when the application itself is broken. The documented in-place
probe technique survives intact.

**The per-mode services were never a debugging mechanism. They were purely a scheduling one.**

---

## 4. Scheduling

### 4.1 Two classes of job

**Global** — capture, submit, collect, weekly, monitor. One run serves every user.
**Per-user** — render and deliver, iterating the users table, which initially holds one row.

### 4.2 Two trigger kinds, plus a weekday filter

Daily-at-`HH:MM` and every-`N`-minutes, with an optional set of weekdays on the daily kind. No cron
expressions, no parser, no `croniter`.

**Correction:** an earlier draft claimed two kinds "cover every job in the system". They do not —
`weekly` runs `0 21 * * 0`, Sunday only, and a plain daily schedule would have produced seven
weekly reports a week, each marking the paper book to market. The weekday filter is the minimum
addition that covers the real schedule; it is not a step toward a cron parser.

**A grace window shorter than one scheduler tick is unsatisfiable.** The scheduler is polled, so a
fire time is observed some seconds after it passes and a zero-minute grace can never be met — the
job would be recorded `missed` forever. Every schedule's grace must exceed the tick interval, and
that is asserted in code rather than left to care.

### 4.3 `job_runs`

`job_name`, `scheduled_for`, `started_at`, `finished_at`, `exit_code`, **`trigger`**
(`scheduled | manual | catchup`), **`status`** (`queued | running | finished | missed`).

`scheduled_for` is the load-bearing column for catch-up; `trigger` and `status` are load-bearing
for §4.4a, and both exist from the first migration rather than being retrofitted later.

### 4.4a The interlock must be a property, not a belief

An earlier draft specified "a job still running when it next comes due is skipped" while §3.6
deliberately preserves `docker compose run` as the debug path. With the batch services deleted,
`docker compose run --rm newsbrief collect` still works by command override — and **wrote no
`job_runs` row**, making it invisible to the skip policy. The exact failure this document exists to
eliminate (a double collect, paper positions re-opened) would have returned through the sanctioned
debugging route. The existing `file_lock` guards the book and state files, never a mode, so nothing
in the code prevented it.

The rule, therefore:

**Every entry path to a job — the supervisor, `docker compose run`, and any future `/run` command —
acquires a lock keyed on `job_name` and writes its `job_runs` row.** With Postgres that is
`pg_advisory_lock` on a hash of the name; the lock is released when the connection closes, so a
killed child cannot strand it. A path that cannot take the lock does not run and says why.

This is enforced in the mode dispatch, not in the supervisor — otherwise it is exactly the guard
that the one caller bypassing the supervisor evades. It also makes on-demand triggering
expressible: a `/run` request is a `queued` row with `trigger = manual`, which the supervisor picks
up on its next tick. Without `trigger` and `status`, a future `/run` would require retrofitting a
queue into the table `/jobs` had already taught the operator to trust.

### 4.4 The catch-up rule

On startup and on each tick, compute the most recent due fire time. If no `job_runs` row exists
for it and `now - scheduled_for` falls within that job's grace window, run it **once**, coalescing
to the latest and never replaying a backlog. Otherwise record it as missed and continue.

Grace is per-job: minutes for capture, an hour or two for collect, zero for weekly.

Not "a deploy never runs anything", but **"a deploy runs exactly what cron would have run, at most
once."** A redeploy at 06:00:30 performs the 06:00 collect; a redeploy at 14:00 does not resurrect
it.

### 4.5 This complexity is self-inflicted, and that is a real cost

Recorded honestly rather than defended: host cron never needed a catch-up rule, because it does
not live inside the artifact being redeployed. Moving the scheduler in creates the misfire problem
that §4.4 then solves, and §9 designates that rule the piece whose failure is silent.

An earlier draft justified the cost by claiming the schedule was "invisible to git" and that this
is what let §1.1 go unnoticed. **Both claims are false and are struck.** The four cron lines are in
git, in the `docker-compose.yml` header comment, with their exact times; and §1.1 is caused by `up`
acting on job *definitions*, which cron never touches. That draft also leaned on adoption — an
adopter should not hand-install four cron lines — which §1.3 explicitly forbids as a justification
for internal scheduling.

Asked to name one thing host cron demonstrably failed at other than §1.1, the honest answer is
**nothing**. Drift between the host crontab and the committed comment is invisible, but no instance
of it is known — that is unknown, not a failure.

What is left is genuine but smaller than the discarded story: internal scheduling collapses the
compose file to a single application service (§1.2, an operator requirement), and it brings
`job_runs`, OOM visibility and `/jobs` without installing a wrapper script on the host. That is a
requirement-shaped justification, not a technical necessity, and it is recorded as such. Price:
roughly eighty lines and the most heavily tested rule in the system.

### 4.6 Vocabulary borrowed, library declined

APScheduler 4 provides these semantics directly — `misfire_grace_time`, `coalesce`, and a
persistent store surviving restarts (verified against its current documentation). Declined: it is
async-first, expects SQLAlchemy or asyncpg alongside, and its job model is *serialised Python
callables* where ours is *spawn a child process*. We would use roughly a tenth of it while
inheriting an async runtime into a project whose dependency list is `feedparser` and `requests`.

Vocabulary adopted verbatim — `coalesce`, `misfire_grace_time`, `missed_start_deadline` — on the
same reasoning that took gbrain's resolution vocabulary while rejecting gbrain.

---

## 5. Storage substrate

Scope: engine, migrations, connections, backup. The entity schema — events, claims, observations,
links — remains `bqa.3`.

### 5.1 Why Postgres — an honest ledger

Not the multi-writer argument, which §1.4 voids. What remains, with each reason weighted rather
than asserted:

| Reason | Weight |
|---|---|
| **Epic 3 needs a real store.** The KB replaces `brief_memory.json`, whose 25-row working set is a *prompt-budget* cap that the ledger's own storage layer inherited. Epics 4, 5 and 6 all depend on `bqa`. | **Strong and committed, but non-discriminating.** It rules out JSON files; it does not choose between SQLite and Postgres, both of which satisfy it. Recorded here because an earlier draft let this row carry weight it cannot bear. |
| **External reachability.** Querying from a GUI, a notebook, another machine. SQLite has no wire protocol. | **Deciding.** This is the one requirement SQLite cannot meet in-container, and the container-count comparison turns on it: SQLite *alone* is one container with no off-host access; SQLite *plus* Datasette or a similar shim is **two containers with read-only access**; Postgres is two containers with read/write access from any standard client. Once off-host access is required at all, Postgres is not the more expensive option — it is the same container count and strictly more capable. Against it: off-host reach means a port or tunnel plus credential management either way. |
| **Concurrent resident readers.** A web UI and MCP server holding sessions alongside capture. | **Void — struck.** Two drafts leaned on this and both were wrong. §3.2 places `web` and `mcp` *inside the application container* as supervisor children: they are local processes on local disk, not wire clients, and SQLite would serve them exactly as it serves `capture`. Separately, `nyy.2`, `nyy.4` and `nyy.5` are open, unstarted and mostly P3 inside an epic blocked behind `bqa` — metadata, not commitment. |
| **Per-user rows.** | **Contingent** on §1.3, which is option-value, not demand. |
| **Operator confidence.** A file-based store does not inspire confidence for an always-on accumulating system. | **Real but not evidence**, and labelled as such. Of the three concerns behind it, two are live (multi-process writers, external access) and one — corruption from a process killed mid-write — is the case SQLite is specifically hardened against. Recorded so it is not re-litigated as fact. |

**Honest summary.** At ~120 writes/day on one Linux host, SQLite would technically suffice, and
because `web` and `mcp` are in-container children it would also serve every committed surface. The
strong row does not discriminate. **What actually decides this is off-host access — a SQL client,
notebook or GUI pointed at the KB from another machine, which SQLite cannot provide without
running a component in front of it — together with a stated operator preference and the option on
mature `pgvector` should `bqa.3` want vectors in the primary store.**

Bought at the price of a pinned major, a healthcheck, the uid caveat, `psycopg`, a CI services
block, a local dev database, and `pg_dump` retention with a restore that must actually be
exercised.

**The second container is not, on inspection, part of that price.** Review framed Postgres as
costing the container §1.2 exists to remove, and that framing holds only if off-host access is
abandoned: satisfying it on the SQLite branch requires a shim container of its own, arriving at two
containers with strictly fewer capabilities. The §1.2 tension is therefore real but narrower than
§1.2 states — it is a choice between one container *without* off-host access and two *with* it, not
between one and two at equal capability.

The ledger is written plainly so a future reader can reverse it on evidence rather than rediscover
the argument. **Reversal is cheap by construction:** §5.3's numbered SQL and §5.4's driver-local
data access mean the SQLite branch is a driver swap plus a dialect pass, not a rewrite.

### 5.2 What moves and what does not

Rows go to Postgres; documents stay on the volume.

| Today | Lands |
|---|---|
| `brief_memory.json` (`brief_memory.py:16`) | Postgres — the KB seed; Epic 3's schema supersedes it |
| `sources.json`, `feedback.json` | Postgres — per-user configuration |
| ~30 environment knobs | Postgres `settings` — global or per-user |
| `batch_state.json` | Postgres — pending batch id and Telegram offset are coordination state |
| `briefs/`, `weekly/`, `debug/` | Volume — rendered documents and dumps, not rows |
| `theses.json`, `thesis_log.json`, `paper/`, `signals/`, `enrichment/` | Volume — Epic 6 is deferred to 2026-12-01; do not touch what is out of scope |

### 5.3 Migrations

Numbered SQL files applied in order, tracked in `schema_migrations`, run by the supervisor at
startup before any job child is spawned.

**KB-table migrations ship with a tested down-migration.** Epic 1 is the evidence for why:
`status` and `origin` shipped as write-then-quarantine fields, the quarantine was lifted only
after eight measured runs, and `jx9.9`'s filed premise was found wrong and re-scoped mid-flight.
That is a schema being *learned*, not specified, and forward-only migration is the wrong instrument
for it — every wrong turn otherwise becomes a new forward migration against production rows the
pipeline already wrote. Infrastructure tables (`users`, `settings`, `job_runs`) may be
forward-only; KB tables may not.

Alembic remains declined — SQLAlchemy for a runner of roughly forty lines — but the down-migration
requirement is not contingent on that: numbered `NNNN_up.sql` / `NNNN_down.sql` pairs satisfy it.

### 5.4 Connections

`psycopg` v3, no pool. Job children open a connection and exit; the supervisor holds one. A pool
arrives with the web server and belongs to that child alone.

`psycopg[binary]` is the required spelling — the plain package needs `libpq-dev` and a compiler in
a slim image.

`DATABASE_URL` must be declared in the `&newsbrief` anchor or it is silently invisible inside the
container. That is the exact footgun this design is partly intended to retire.

### 5.5 Backup

`pg_dump` into the existing appdata volume, scheduled on the same catch-up machinery as every
other job — no host cron — retaining N days.

Current JSON state has **no** backup beyond whatever the host does to the share. A logical dump is
verifiable in a way a file copy of a possibly-active database is not.

---

## 6. Tenancy shape

### 6.1 Users

A `users` table — id, display name, telegram chat id, timezone, delivery time, active — seeded with
exactly one row from the current `TELEGRAM_CHAT_ID` during the first migration.

### 6.2 Where `user_id` goes, and where it deliberately does not

Sources, preferences and deliveries carry `user_id`. The KB tables — events, claims, observations,
links — do not. **The world is shared; the reading of it is personal.**

An honest qualification: amortisation is strong for the shared core — the wires, the majors — and
weak for the tail. Comprehension is per-item and items come from sources, so cost scales with the
*union of distinct sources*, which for a long tail of niche per-user feeds grows roughly linearly
in user count. A hosted instance would need a per-user source budget. That is a product decision.

### 6.3 Configuration

`settings`, scoped global or per-user, read at runtime. The environment keeps only what belongs to
the deployment: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`. `TELEGRAM_CHAT_ID`
leaves the environment for the users table (`common.py:25`).

This is justified without §1.3: it retires the documented `env-var-needs-compose-passthrough`
class of bug, in which a knob set on the host or in `.env` is silently invisible inside the
container and a fail-closed flag then no-ops.

### 6.4 Not built

Auth, sessions, signup, quotas, billing, per-user API keys. The gate is a person other than the
operator wanting an account.

### 6.5 Trading stays un-tenanted

Live PolyGram credentials are per-deployment and real money. They belong to the operator, not to a
user row. Recorded as a decision so it is not later discovered as an oversight.

---

## 7. Transition

### 7.1 Phase 1 — supervisor, Postgres, and the cutover

Add the `postgres` service, the supervisor module, the migration runner, and four tables:
`schema_migrations`, `users`, `settings`, `job_runs`. Delete the four batch services. The
supervisor schedules the existing modes at exactly their current times and runs `commands` as a
resident child. Every mode still reads its JSON. No KB work.

**This phase is a flag day for scheduling, and calling it anything else would be false.** The host
cron entries and the `newsbrief-commands` service must come out in the same change, or you get
double collects and a Telegram 409 from two `getUpdates` consumers (`brief.py:3378-3381`). The
later phases are incremental; this one is not.

Because the rollback ("stop the service, restore the cron lines") is a manual host operation, it
must be **written down before the cutover, not derived during it** — the four cron lines and the
`newsbrief-commands` service definition, saved on the host, so recovery does not depend on the
Telegram bot being reachable.

**Verification** is `job_runs`: the expected schedule present, and no row fired at deploy time.

This phase resolves §1.1 and §1.2 together.

### 7.2 Phase 1.5 — capture

Epic 2's capture becomes a supervisor job child. One amendment to `b42.1` as filed: it specifies
"a dedup'd JSONL keyed on content hash", but Postgres exists as of phase 1, so it writes a
`captured_items` table directly rather than a JSONL that phase 3 migrates. That issue gets simpler.

### 7.3 Phase 2 — configuration into the database

`sources` and `preferences` tables carrying `user_id`, populated by an importer that runs only when
the table is empty. `/addsource`, `/focus` and `/mute` write rows instead of JSON.
`TELEGRAM_CHAT_ID` leaves the environment.

`batch_state.json` moves here rather than in phase 1: its Telegram offset is owned by the
`commands` child and its pending batch id by the `submit`/`collect` pair, so it touches two live
readers and belongs with the other state migrations — not in the phase whose value is that it
changes no business logic.

The importer's emptiness check makes it idempotent, so rollback means keeping the files rather than
restoring a backup.

### 7.4 Phase 3 — the knowledge base

Epic 3 proper (`bqa.3`–`bqa.6`), with `brief_memory.json` as a one-time seed import. Unblocks the
render path (Epic 4).

---

## 8. Error handling and observability

The supervisor catches a failure class that is currently **invisible**: a child killed by the OOM
killer or by SIGKILL never reaches its own `except` block, so today an OOM-killed collect produces
silence — indistinguishable from a quiet news day. The supervisor sees the exit code and alerts
with the child name, the code, and its last output lines.

**Fail closed on work, fail open on observability.** A failed migration blocks job children,
alerts with the migration name and error text, and **starts the resident children anyway** so the
bot can report what broke and answer questions while it is broken. The control channel must
outlive the thing most likely to break it.

- Resident children: capped backoff, plus a crash-loop alert above K restarts in a window.
- Postgres: healthcheck plus `depends_on: condition: service_healthy`.
- `job_runs` is the observability surface. A `/jobs` command reports last run, exit code and next
  due per job.

That last item is small and matters more than it looks: a scheduler that cannot be inspected is
precisely the "an empty log proves nothing until you have seen it grow" failure mode.

---

## 9. Testing and the dev loop

**The catch-up rule gets the most attention**, because it is the piece whose failure is silent and
because §4.5 concedes it is complexity this design created. Due-time computation and the run/skip
decision are pure functions of `(now, schedule spec, last job_runs row)` and get table-driven
tests, including the two cases that define the feature: a redeploy inside the grace window and one
outside it.

**Supervisor behaviour** is tested with fake commands rather than real modes — exit codes, timeout
kill, output draining, crash-loop backoff, and the fail-open path where a migration fails and the
resident child still starts. The three seam invariants of §3.4 are assertable and get tests.

**The §4.4a interlock is tested through the bypass path, not the supervisor.** A test that only
exercises the supervisor proves nothing about the case that motivated the rule — a second entry
path taking the lock while a job is running. The test acquires the lock as the supervisor would,
then invokes the mode directly and asserts it refuses, names the holder, and writes no second
`running` row.

**Migrations** are applied to a scratch database, asserting the resulting schema, that re-running
is a no-op, and — for KB tables — that `down` restores the prior schema.

**The dev loop.** Docker runs on this machine (operator-confirmed), so DB-backed tests run locally
against a disposable Postgres container; CI gains a `services: postgres:` block on the test job.
Where no database is reachable, DB-backed tests **skip loudly with a message naming the missing
`DATABASE_URL`** — never silently, since a skipped test that reads as a pass is the exact failure
class this project has documented repeatedly. CI always has the database, so a skip can never hide
a regression there.

The existing suite runs against zero infrastructure (`conftest.py` points `NEWSBRIEF_DATA_DIR` at
a tempdir) and stays green through phase 1 untouched. From phase 2, the test files touching
`sources.json`, `feedback.json` and `batch_state.json` become DB-backed. That is a real cost and is
stated rather than glossed.

Not tested: that Postgres works. Only our logic against it.

---

## 10. Rejected and deferred

| | Decision | Reason |
|---|---|---|
| **SQLite** | Rejected, narrowly | See the §5.1 ledger. Technically sufficient at this volume and adequate for every in-container surface. Rejected on **off-host access** — the one thing it cannot serve without a shim container that erases its container-count advantage — plus a stated operator preference. Not on concurrency (§1.4 voids it), not on Epic 3's needs (which do not discriminate), and not on resident readers (struck). The closest call in this document. |
| **A container per mode** | Rejected | The direct cause of §1.1, and five services for one application is a stated operating cost (§1.2). |
| **Splitting the bot into its own container** | Rejected | Proposed by review as a blast-radius fix; declined on §1.2. The risk is instead bounded by fail-open startup, a logic-free supervisor, and the §3.4 seams that make the split a compose edit if it ever proves necessary. |
| **`profiles:` on the batch services** | Rejected as the fix | A correct four-line patch for §1.1 alone. It leaves five containers, leaves the schedule on the host and outside the image, and delivers nothing for §1.2 or Epic 3. Retained as the fallback if phase 1 stalls. |
| **APScheduler** | Rejected | Async-first, expects SQLAlchemy or asyncpg, job model is serialised callables rather than child processes. Vocabulary adopted. |
| **Alembic** | Rejected | SQLAlchemy dependency for a ~40-line runner. Does not exempt KB tables from down-migrations (§5.3). |
| **Forward-only migrations for KB tables** | Rejected | Epic 1 shows the schema is being learned, not specified. |
| **DuckDB / graph-first / bitemporal engines** | Rejected | Already rejected in KB spec §5; unchanged. |
| **Auth, quotas, billing** | Deferred | Gate: a person other than the operator wanting an account. |
| **Trading tenancy** | Rejected | Real-money credentials are per-deployment. |

---

## 11. Red-team review: what was accepted and what was not

A hostile review was run from fresh context against the first draft. Its brief was to find why this
is the wrong thing to build, so its findings and its verdict carry different weight.

**Accepted, and repaired in this revision:**

- §5.1 described the web UI and MCP server as "already committed" when the issues are open,
  unstarted and mostly P3 inside a blocked epic — metadata read as state. Now a weighted ledger.
- The "forty cron entries" argument for internal scheduling was a strawman. Struck (§1.3).
- The catch-up rule is complexity this design creates. Now stated as a cost (§4.5).
- A failed migration would have taken down the control channel. Now fail-open (§8).
- KB migrations must be reversible (§5.3).
- The first draft claimed "no flag day" while phase 1 required a simultaneous cutover (§7.1).
- The log-rotation race is better solved by a single writer than by more machinery (§3.2).

**Rejected, with reasons:**

- *Verdict: hold Postgres until a second user exists or `nyy.2`/`nyy.4` start.* The gate cannot
  fire: `nyy` depends on `bqa`, so those issues cannot start until the KB exists, and the KB is
  what needs the store. The real gate is Epic 3, which is committed.
- *"A simpler design gets 80% of the value."* It gets ~80% of the deploy fix and none of Epic 3,
  configuration, or §1.2. The denominator was the one presenting symptom, not the work.
- ~~*"A read-only Datasette is the same unit of cost."* Read-only does not cover a web UI that
  writes configuration or an MCP server.~~ **This rejection was itself wrong and is retracted** —
  see the second review below. `web` and `mcp` are in-container children, so no wire client is
  involved and the rebuttal answered a proposal this architecture never has to make.
- *"The dev box cannot carry a database."* Based on a documented-fragile Docker integration and a
  probe of mine that had merely found the daemon stopped. Docker runs on this machine; the
  objection is void (§9).
- *Split the bot into its own container.* Declined on §1.2; mitigated instead (§3.3, §3.4).

### Second review, against this revision

**Accepted, and repaired above:**

- **The interlock was a belief, not a property.** `docker compose run` bypassed `job_runs`
  entirely, resurrecting the double-collect this document exists to prevent, through the debug path
  §3.6 deliberately preserves. Fixed in §4.4a, with `trigger` and `status` present from the first
  migration so a future `/run` needs no retrofit. **This was the single most valuable finding of
  either review.**
- **§4.5's justification for internal scheduling was factually false** — the cron lines are in git,
  and the deploy bug is caused by `up`, not by cron's visibility. Struck and replaced with the
  smaller true claim.
- **The "concurrent resident readers" row in §5.1 is void**, because this design's own §3.2 puts
  `web` and `mcp` inside the application container. Struck, and the first review's Datasette
  rejection retracted with it.
- **The strong row in §5.1 does not discriminate** between SQLite and Postgres. Now labelled.

**Rejected, with reasons:**

- *Verdict: drop Postgres from phase 1 until `bqa.3` names a need SQLite cannot meet.* The
  strongest argument in either review, and the SQLite branch would indeed land the design in one
  container instead of two. Declined by explicit operator decision, taken with this argument in
  front of it, on off-host access and stated preference (§5.1). Recorded as a purchase, not an
  oversight (§1.2), and made cheap to reverse.
- *Build the supervisor for process supervision alone — no ticker, no `job_runs`.* That reduces
  the supervisor to a restart policy for a single child, which `restart: unless-stopped` already
  provides. It would leave five service definitions and the schedule on the host, delivering
  neither §1.2 nor the run history.

---

## 12. Positions reversed during design

- **SQLite was recommended first on ops-cost grounds, then reversed.** The original argument was
  answering the wrong question; once the committed surfaces are counted, the components needed to
  give SQLite a wire protocol approach the cost of the service being avoided. The reversal is
  recorded because §5.1 is now weaker than the case first made for it, not stronger.
- **The multi-container hazard was accepted from the KB spec, then challenged.** It is a
  network-filesystem property and the host is local disk. The conclusion held on other grounds;
  the premise did not.
- **Consolidation was first argued to be a large refactor, then found cheap** — the dispatch is
  already seven zero-argument functions.
- **Multi-user was first written as a forcing requirement, then demoted to option-value** (§1.3).
- **Amortisation across users was overstated** as `O(world)`. Corrected in §6.2.
- **The case against SQLite was argued twice and was wrong both times.** First on concurrency
  (§1.4 voids it), then on concurrent resident readers (§5.1 — `web` and `mcp` are in-container
  children). Postgres was chosen anyway, but on one narrow surviving reason plus preference, and
  the record says so. A reader who concludes the ledger no longer justifies the second container
  is reading this document correctly and should reverse it.

---

## 13. Consequences for existing issues

- **`bqa.1`** — decided by §2 and §3.
- **`bqa.2`** — decided by §5.1. No longer blocked on `bqa.1`; both are settled here.
- **`b42.1`** — amended by §7.2: writes a table, not a JSONL.
- **`nyy.4`** (web UI, configuration first) — its rationale, killing the env-var passthrough
  footgun, is delivered by §6.3 in phase 2. The UI becomes presentation over settings that already
  exist.
- **Not yet filed:** supervisor, migration runner with down-migrations, the §4.4a job interlock
  (lock plus `trigger`/`status` columns, enforced in the mode dispatch), config importers, the CI
  Postgres service, and the written-down cutover rollback (§7.1).

---

## 14. Open questions

1. **Grace windows per job.** §4.4 gives ranges, not values. Set them at implementation and record
   the reasoning.
2. **Crash-loop threshold `K`** and the backoff ceiling for resident children.
3. **Backup retention `N`**, and whether a restore is ever exercised — an untested backup is a
   belief, not a property.
4. **Postgres major version** — confirm current stable at implementation time (§3.1).
5. **Whether `render` becomes a distinct mode** or stays inside `collect` until Epic 4 splits it.
6. **Whether the `settings` table needs typed values or plain text** with coercion at read time.

---

## 15. Success criteria

1. A stack redeploy produces no brief, no paper position, and no `job_runs` row for a fire time
   that was not actually due.
2. A redeploy within a job's grace window runs that job exactly once.
3. A child killed by the OOM killer produces a Telegram alert naming the child and its exit code.
4. A failed migration leaves the bot answering and reporting the failure.
5. `/jobs` answers "when did collect last run, and did it succeed" without an SSH session.
6. The stack is two containers, and `docker compose up` starts no work that was not due.
7. Adding a second user is a row plus a delivery time — no schema change, no new container.
8. Configuration changes take effect without editing the compose anchor or recreating a container.
9. A `docker compose run --rm newsbrief collect` issued while a scheduled collect is running
   refuses to start, names the lock holder, and leaves exactly one row in `job_runs` — the
   interlock verified through the path that bypasses the supervisor, not only through the
   supervisor itself.
10. A `pg_dump` taken by the scheduled backup job restores into an empty database and the
    application starts against it. An unexercised restore does not satisfy this.

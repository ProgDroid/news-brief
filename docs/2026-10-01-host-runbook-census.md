# Host runbook: the event census (KB sub-project 0)

**Date:** 2026-09-29. Nothing here runs before **2026-10-01** (step 3 refuses earlier).
**Beads:** `news-brief-vd9` (epic; stays open until the census is labelled), `news-brief-vd9.10`
(this runbook). Related: `news-brief-115` (see "Before you start"), `news-brief-uh0` (its
retention must skip held items).
**Spec:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` (§4.2–4.6, §6, §7, §9).
**Plan:** `docs/superpowers/plans/2026-09-28-event-census.md`.

The census code is committed and green: migration `0017_census` (the census tables and the
retention hold), `census_prepare` (a `brief.py` mode), the `labeller` compose service, the
Telegram `/label`, `/label reset` and the morning nudge, `scripts/census_grants.py` and
`scripts/census_report.py`. Every step below is a **host** action, run in this order.

Placeholders only. This repository is public, so no real address, hostname or secret goes in
this file or in any commit. `<LAN IP>` is the host's address on the home LAN. `<psql>` is
defined in "Before you start".

## Status

| step | state |
|---|---|
| 1. Deploy; confirm `0017_census` is applied | outstanding |
| 2. Confirm the `c439ade` deploy date | outstanding |
| 3. `census_prepare`, on or after 2026-10-01; record the gap band | outstanding |
| 4. `.env` and the host compose file | outstanding |
| 5. Check the password reaches the container; apply the grants | outstanding |
| 6. Recreate `newsbrief`; start `labeller`; read its log | outstanding |
| 7. `/label` from Telegram | outstanding |
| 8. The readout (repeat after every window) | ongoing |
| 9. Go/no-go after windows 1 and 2 | outstanding |
| 10. After the census: stop and remove `labeller` | outstanding |
| 11. Release the hold (only when sub-projects 1 and 2 are done with the census) | outstanding |

---

## Before you start

**`<psql>`: a psql session on the production database.** Pick the line for this host:

```sh
# the stack's own postgres service (the default compose file):
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# the host sets DATABASE_URL (a database outside the stack); the image has psql 18:
docker compose run --rm --entrypoint sh newsbrief -c 'psql "$DATABASE_URL"'
```

**The scripts need `scripts/` inside the image.** The Dockerfile copies `scripts/` only as a
TEMPORARY measure (`news-brief-115`, which deletes that line once the comprehension gate has
run). Steps 5 and 8 run `scripts/census_grants.py` and `scripts/census_report.py` from the image.
If `news-brief-115` lands before the census is over, those steps stop working. Keep the line
until step 10 is done, or run both scripts from a checkout with `DATABASE_URL` exported.

**Shell variables.** `.env` feeds compose interpolation. It is **not** exported to your shell.
Step 5 checks this explicitly rather than assuming it.

---

## Step 1: deploy, and confirm `0017_census` is applied

Deploy the image that contains the census commits, then recreate the stack's `newsbrief`
container on it (the supervisor applies pending migrations at start):

```sh
docker compose pull newsbrief && docker compose up -d newsbrief
```

Confirm the migration by its row, then by its effect:

```sql
-- in <psql>
SELECT version, applied_at FROM schema_migrations WHERE version = '0017_census';   -- 1 row
SELECT tgname FROM pg_trigger WHERE tgname IN ('census_hold_row_trg', 'census_hold_truncate_trg');  -- 2 rows
```

Two rows from the second query mean the hold's triggers exist. They do nothing until step 3
writes the `census_block` row.

## Step 2: the `c439ade` deploy date, by recognition

`c439ade` (the Reuters proxy move, committed 2026-09-25) may have reached the host inside the
census block. `census_prepare` records its deploy date so the readout can print a table by
block half (spec §4.2 item 3). No field records a deploy, so you confirm one timestamp from the
evidence below.

**a. Which local images contain `c439ade`.** For each local image, print its revision and its
build time:

```sh
for id in $(docker image ls -q ghcr.io/progdroid/news-brief); do
  docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{.Created}}' "$id"
done
```

For each revision, from any checkout: `git merge-base --is-ancestor c439ade <revision> && echo
contains`. The earliest-built image that contains `c439ade` is the candidate. **`Created` is the
build time, not the deploy time.** The image was deployed at or after it.

**b. When that image first ran.** In `<psql>`, with `<Created>` from (a):

```sql
SELECT min(started_at) FROM job_runs WHERE started_at > '<Created>';
```

This is the first job the supervisor ran after that build. It is evidence, not proof: if the
host pulled late, the old image can have run it.

**c. You confirm one timestamp.** It must be ISO-8601 **with a timezone**, for example
`2026-09-26T09:00:00+00:00`. If the old image has been pruned, say so in the step-3 record and
confirm from the `job_runs` evidence alone.

## Step 3: `census_prepare`, on or after 2026-10-01

```sh
docker compose run --rm -e CENSUS_C439ADE_DEPLOYED_AT=<confirmed> newsbrief census_prepare ; echo REAL_EXIT=$?
```

Success prints one line and exits 0:

```
prepared block <start> to <end>: 16 windows drawn plus the repeat, <n> skipped, gap band <band> (<pairs> pairs, <windows> windows)
```

**Record the gap band** in the spec's §11 ("Gap check"), with the pairs and windows it rests on.
Also record the confirmed timestamp and, if it applies, that the old image was pruned. The band
decides what happens next (spec §4.3):

| band printed | meaning |
|---|---|
| `proceed` | below 40%: go on to step 4 |
| `within_6h_only` | 40–50%: go on, and the headline is labelled everywhere as covering confirmation **within 6 hours only** |
| (refusal, exit 2) `gap split share … exceeds 50%` | stop. Bring the window length back to the operator. Nothing was written |

**Refusals.** Each one exits 2, prints its reason and writes nothing:

- `CENSUS_C439ADE_DEPLOYED_AT is missing or unparseable` or `… has no timezone offset`: a naive
  timestamp is refused on purpose (ruling R9). Add the offset.
- `block would span only <n> days (minimum 10); the earliest valid preparation date is
  2026-10-01`: run it on or after that date.
- `gap check has no merged cross-outlet pairs; cannot evaluate` (ruling R3): production has
  merged no cross-outlet pair into one event, so the check is unanswerable, not passing. Bring
  it to the operator.
- `stratum <s> has only <n> eligible windows (needs 4)`: the pool is too thin to draw the
  order. Bring it to the operator.
- `census_prepare is already running`: another run holds the advisory lock.

**Re-running is safe.** Once prepared, it prints `already prepared: block <start> to <end>` and
changes nothing. From this moment the retention hold is armed: any delete of an item captured
before `block_end`, and any `TRUNCATE items`, raises and names this runbook.

## Step 4: `.env` and the host compose file

Add to the host's `.env`:

```sh
CENSUS_LABELLER_PASSWORD=<output of: openssl rand -hex 32>
LABELLER_BIND=<LAN IP>                 # the host address the port binds to; default 127.0.0.1
LABELLER_BASE_URL=http://<LAN IP>:8765 # exactly what the phone will type
LABELLER_IMAGE=<digest>                # see below; unchanged for the whole census
# LABELLER_PORT=8765                   # only if 8765 is taken; then use it in LABELLER_BASE_URL too
```

- **`LABELLER_BASE_URL` must match what the phone sends, byte for byte.** The labeller answers
  only when `Host` equals the URL's `host:port`, and a POST only when `Origin` equals the URL.
  Anything else gets a 403 whose body reads `forbidden: origin` (the page then says
  "blocked: page address does not match LABELLER_BASE_URL"). A missing or expired session
  reads `forbidden: session` (the page says "session expired"). If you set `LABELLER_PORT`, the URL carries that port.
- **`LABELLER_IMAGE` is a digest, not a tag,** so a deploy mid-census cannot change the tool.
  Take it from the image deployed in step 1:

  ```sh
  docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/progdroid/news-brief:latest
  ```

**The host compose file is ahead of the repo's** (memory `env-var-needs-compose-passthrough`),
so copy these two pieces into it from the repo's `docker-compose.yml`:

1. the anchor's `- LABELLER_BASE_URL=${LABELLER_BASE_URL:-}` line (under `x-newsbrief`'s
   `environment:`). The `/label` link is built by the `newsbrief` daemon from this variable.
   Without it, `/label` replies that `LABELLER_BASE_URL is not set` and mints nothing;
2. the whole `labeller:` service (`profiles: [census]`, `entrypoint: ["python", "labeller.py"]`,
   its `environment:`, `ports:`, `restart: unless-stopped` and `depends_on`).

**If the host sets `DATABASE_URL`** (a database outside the stack): in the host's `labeller:`
service, replace `POSTGRES_HOST`, `POSTGRES_PORT` and `POSTGRES_DB` with that database's
discrete values, and remove `depends_on: postgres` if the stack has no such service. Keep
`POSTGRES_USER=census_labeller` and `POSTGRES_PASSWORD=${CENSUS_LABELLER_PASSWORD:-}`.
**Never give the labeller `DATABASE_URL`**: it overrides the discrete values (`db.conninfo`),
so the labeller would connect as the main role and the privilege split would be gone.

## Step 5: check the password reaches the container, then apply the grants

```sh
docker compose run --rm -e CENSUS_LABELLER_PASSWORD --entrypoint sh newsbrief -c 'test -n "$CENSUS_LABELLER_PASSWORD"; echo REAL_EXIT=$?'
```

Expected: `REAL_EXIT=0`. If it prints `REAL_EXIT=1`, the variable did not reach the container:
`export CENSUS_LABELLER_PASSWORD=<the .env value>` in this shell and re-run the check.

Then create the role and apply its grants:

```sh
docker compose run --rm -e CENSUS_LABELLER_PASSWORD --entrypoint python newsbrief scripts/census_grants.py ; echo REAL_EXIT=$?
```

Expected: `grants applied`, `REAL_EXIT=0`. `CENSUS_LABELLER_PASSWORD is empty` (exit 2) means
the check above was skipped or failed.

The script is idempotent: it creates `census_labeller` if absent, sets its password (a SCRAM
verifier computed client-side, so the plaintext never appears in SQL), and applies every grant
in `census.LABELLER_GRANTS`. **Re-run it after any database restore** (roles survive
`DROP SCHEMA`, grants do not) **and after changing the password.**

## Step 6: recreate `newsbrief`, start `labeller`, read its log

```sh
docker compose up -d newsbrief                    # picks up LABELLER_BASE_URL for /label
docker compose --profile census up -d labeller
docker compose logs labeller
```

Expected in the log: `labeller listening on 0.0.0.0:8765 for http://<LAN IP>:8765`. The
labeller checks every grant on its own connection before it binds:

| in the log | exit | meaning |
|---|---|---|
| `missing grant: <grant>` (one line each) | 3 | step 5 did not run, or ran against another database |
| `database unreachable: …` | 3 | wrong host, port, database or password (step 4) |
| `CENSUS_LABELLER_PASSWORD is unset` | 3 | step 4's password did not reach the service |
| `LABELLER_BASE_URL is required` | 2 | step 4's variable did not reach the service |
| a bind error from `up` | — | `LABELLER_BIND` is not an address this host owns |

`restart: unless-stopped` brings the labeller back after a host or Docker restart, and a
crash-loop (a missing grant, say) shows in `docker compose --profile census ps -a labeller`
and `logs labeller`. If the page ever stops loading, repeat this step. A labeller failure
affects only this service.

## Step 7: `/label` from Telegram

Send `/label`. It replies with the progress line (`🏷 Session <n>/17`, with no item count, which would
reveal the repeat window) and a one-time link, `<LABELLER_BASE_URL>/open?t=<token>`. The link:

- is valid for 10 minutes and works once. Only its SHA-256 is stored;
- sets a 30-day session cookie on the phone, so later visits need no new link.

Open it on the phone, on the home LAN.

**Phone save check, before window 1 (mandatory).** Only a POST proves the page can save, and
the link itself is a GET. On the actual phone, with the link opened, select two items, make
one group, then reload the page. The group must still be there. **If saves fail** (the page
shows a banner such as "blocked: page address does not match LABELLER_BASE_URL", or the group
is gone after reload), **stop and report it; do not start window 1.** Check
`LABELLER_BASE_URL` against the address the phone types (step 4); if it matches and the
banner persists, the phone's browser is not sending the expected `Origin`.

Other replies: `not prepared yet` (step 3 has not
run), `stopped at the go/no-go` (step 9), `Nothing to label yet: the repeat of window 2 opens
<date> UTC`, and `complete`.

`/label reset` revokes every outstanding link and session, for a lost phone or a leaked link.

At morning-brief delivery, one line `🏷 Session <n>/17 ready · /label` is added while a window
is waiting. While the census is stopped at the go/no-go (step 9), the line is
`🏷 Census stopped at go/no-go · /label` instead. It is fail-safe (an error means no line,
never a failed brief), stays silent when the census is waiting, complete or not prepared, and
stops after session 17.

## Step 8: the readout

```sh
docker compose --profile census run --rm --entrypoint python labeller scripts/census_report.py
```

This runs from the **pinned `LABELLER_IMAGE`**, under the `census_labeller` role, so the
readout and its go/no-go use the same `census.py` and `census_metrics.py` as the page. (Do
not run it from the unpinned `newsbrief` image during the census.) The role has SELECT on
every table the readout reads; a test runs the readout on that role inside READ ONLY.

It is read-only and free: it runs inside `SET TRANSACTION READ ONLY` and makes no model call.
Before step 3 it prints `The census is not prepared.` and exits 2. It prints the gap check,
the block, every window (items, groups, singletons, unsure and NULL-`published_at` shares,
multi-outlet groups, active and wall-clock minutes, status), the go/no-go after window 2, and
after window 16 the achieved detectable difference at ρ = 0.05 / 0.1 / 0.3, blind precision,
consistency and the by-block-half table (descriptive only). Run it after every window.

## Census freeze: from step 3 to the end of step 10

**No semantic change to `census.py`, `census_metrics.py` or the `census_*` schema** between
`census_prepare` (step 3) and the end of the census (step 10). The page runs the pinned image
and the readout now does too, but `/label` and the morning nudge run in the **unpinned
`newsbrief` image** (`census.current_task`). A change would make them disagree with the page:
`/label` saying "complete" while the page has work, or a nudge for a session that is not
there. Sub-project 1 extends these files in new functions and new migrations only.

## Step 9: go/no-go after windows 1 and 2

The readout prints the go/no-go once windows 1 and 2 are done (spec §4.5): continue only if
the mean multi-outlet groups per window is at least **8** and the median active minutes per
window is at most **80**. Both window values are printed. **Active minutes are built from heartbeats, which measure
page-visible time, not interaction:** a tab left open and visible, or a phone set never to
lock, keeps accruing (only gaps over 5 minutes are dropped). That biases active minutes
upward, toward NO-GO; read a borderline NO-GO with that in mind. **Record it in the spec's §11
("Go/no-go").**

**If it fails,** `/label` answers `The census is stopped at the go/no-go: <reason>` and serves
nothing. Bring both measurements to the operator. **Only an explicit ruling** lifts the stop:

```sql
-- in <psql>, with the operator's words and the date
UPDATE census_block SET go_override_reason = '<ruling, date>';
```

An abandoned census still reports its detectable difference at the current K (step 8).

**After window 16,** record the achieved detectable difference in the spec's §11. There is no
extension.

## Step 10: after the census, stop and remove `labeller`

```sh
docker compose --profile census stop labeller && docker compose --profile census rm -f labeller
```

Then send `/label reset` to revoke any sessions still open. Leave `.env` and the host compose
entries as they are. They do nothing without `--profile census`.

## Step 11: release the hold, and only when sub-projects 1 and 2 are done with the census

The hold exists so sub-project 1's replay matches production (spec §7). Release it only when
sub-projects 1 and 2 no longer need the block's items:

```sql
-- in <psql>
DELETE FROM census_block;
```

This lifts the **time-based** hold: the delete trigger and the truncate trigger both stop
refusing. **It does not free everything,** by design:

- a row-level `DELETE` of a window's item stays refused by `census_window_items`' `ON DELETE
  RESTRICT` foreign key until the `census_*` tables themselves are dropped. **This is true for
  `DELETE` only. RESTRICT does not apply to `TRUNCATE`:** once the `census_block` row is
  deleted, `TRUNCATE items CASCADE` silently empties `census_window_items`,
  `census_assignments` and `census_adjudications`, wiping the labels. Nothing in production
  truncates `items`; a human or a future script would. Never truncate `items` after the
  release without exporting the census tables first;
- the down migration (`0017_census_down.sql`) **refuses while `census_assignments` has any
  row** (`the event census has labels; refusing to drop it`), so a migration reversal cannot
  destroy the labels.

That is deliberate friction: dropping the census is a separate decision, taken when the census
itself is retired.

**Limit, stated:** `SET session_replication_role = replica` bypasses both triggers. Never use
it against this database while the hold is armed.

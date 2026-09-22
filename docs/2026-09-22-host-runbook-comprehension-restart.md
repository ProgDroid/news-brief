# Host runbook — restarting comprehension for the Amendment 2 gate run

**Date:** 2026-09-22
**Beads:** `news-brief-bqa.27` (Amendment 2), `news-brief-bqa.11` (the gate run),
`news-brief-goq` (Nitter spacing), `news-brief-b42.5` (held, deliberately)
**Decisions this encodes:** the 2026-09-22 gate run is VOID not spent; a 7-day
accumulation window; `b42.5`'s per-feed intervals stay held until the cohort closes.

This exists because every remaining step is a **host** action. The code side is pushed
and green; nothing further happens in this repo until the flag is on.

---

## The trap this runbook is written around

`config.knob` resolves a knob from **a settings row or the code default, and never from
the environment.** `config.py:210` states it outright, and `config.py:278` records what
ignoring it already cost once: `COMPREHEND_ENABLED=1` in the host compose, no row, and
every pass logging *"disabled by COMPREHEND_ENABLED"* — a rollout window spent on a
setting that was accepted, readable and inert.

`import_settings_from_env` does not rescue this. It writes rows **only when the global
settings table is empty**, which on this host it has not been since Epic 7. So:

> **Editing the host compose file changes nothing on a running deployment.**
> A knob is changed by writing its row, or by changing its default in code and deploying.

Two consequences that shape the steps below:

- **`HOST_GAP_SECONDS` and `RECONCILE_TIMEOUT` need no host action.** Both are new
  knobs with no existing row, so each resolves to its new code default — 15.0 and 90 —
  the moment the pushed image is running. Adding them to compose is for a *fresh* seed,
  not for this deployment.
- **`COMPREHEND_ENABLED` does need a row.** Its code default is `False`
  (`common.py:265`), so an absent row means off, forever, whatever compose says.

---

## Step 0 — deploy

Both commits touch paths the publish workflow watches (`brief.py`, `common.py`,
`brief_memory.py`, `scripts/**`, `tests/**`), so the image builds on push:

- `e59315b` — four resolution bugs (`0p3`, `wa1`, `zp8`, `arm`) plus `goq`'s code half
- `0d215ce` — the `@LordPos3idon` editorial cut

**I have not verified how this host pulls a new image** — whether that is automatic or a
manual pull and restart. Confirm the running container is on the new image before
trusting any step below; every one of them assumes the new code.

## Step 1 — confirm the new defaults took effect, by effect

Do not read the rows back; there are no rows to read, which is the point.

```sql
-- Expect ZERO rows. A row here would OVERRIDE the new default and is the one
-- thing that would silently keep the old behaviour.
SELECT key, value, updated_at FROM settings
 WHERE user_id IS NULL AND key IN ('HOST_GAP_SECONDS', 'RECONCILE_TIMEOUT');
```

Then look for the effects:

- **`goq`:** the `RSS retry 1/2 ... 429` lines on the `@geo_papic` feed at :30 passes
  should stop. They were measured on 2026-09-10 at 04:30, 05:30 and 07:30, and not at
  any `:00` pass — so a clean run of several `:30` passes is the observation. **This is
  what closes `goq`; the code alone does not.**
- **`0p3`:** no reconcile read-timeout line in the logs. The value is a row precisely so
  that, if one appears, it can be widened without a redeploy.

The brief and the supervisor both call `config.warn_ignored_env_knobs()` on startup. If
the host compose sets either variable, that report will now name it as asking for
something not in effect — **believe the report, not the compose file.**

## Step 2 — flip `COMPREHEND_ENABLED`, capturing the cutover in the same statement

The cutover timestamp is a pre-registered gate parameter, so it must be the moment of
the flip itself — not a migration ledger's `applied_at`, which has already been measured
in this project predating a code cutover by 3h27m (`a-ledger-dates-what-it-records`).
Taking it from the flipping statement removes the question:

```sql
INSERT INTO settings (key, user_id, value)
VALUES ('COMPREHEND_ENABLED', NULL, 'true')
ON CONFLICT (key) WHERE user_id IS NULL
DO UPDATE SET value = EXCLUDED.value, updated_at = now()
RETURNING key, value, now() AS cutover;
```

**Record the returned `cutover` verbatim.** It is the `--cutover` argument in step 5 and
belongs in `bqa.11`'s notes the moment you have it.

*Verified 2026-09-22, not merely written:* this statement was run against a throwaway
`postgres:18-alpine` holding `migrations/0001`'s exact `settings` schema and a
pre-existing global row. `ON CONFLICT (key) WHERE user_id IS NULL` infers the partial
unique index correctly — it INSERTs on the first run and UPDATEs on the second rather
than erroring, leaving one global row per key. The `WHERE` clause is not optional; a
bare `ON CONFLICT (key)` cannot match a partial index.

Notes on the value: booleans are `text.lower() in {'1','true','yes','on'}`, and anything
else reads **False silently** — `banana` is not an error, it is `off`. The settings cache
has a **60-second TTL**, so no restart is needed; the next hourly pass after that minute
picks it up.

## Step 3 — verify by effect, within the hour

The acceptance criterion on `bqa.11` says the row is verified by **rows appearing**,
never by reading the row back:

```sql
SELECT 'item_triage' AS t, count(*), max(created_at) FROM item_triage
UNION ALL SELECT 'entities',   count(*), max(created_at) FROM entities
UNION ALL SELECT 'events',     count(*), max(created_at) FROM events
UNION ALL SELECT 'assertions', count(*), max(created_at) FROM assertions;
```

`max(created_at)` is the column that matters, not the counts: the corpus already holds
5,799 events from the 7–10 Sep window, so a count alone cannot distinguish "writing
again" from "still frozen". **A `max(created_at)` that has moved past the cutover is the
observation.** That is the same confusion `zp8` was filed for.

If the pass logs `Comprehend: disabled by COMPREHEND_ENABLED`, the row did not take —
go back to step 2 rather than editing compose.

## Step 4 — hold `b42.5`. Do nothing.

**Do not set `poll_every_minutes` on any feed until the cohort closes.** Phase-1 code is
shipped but fully inert — nothing declares the key, so the due-check runs its fail-open
path across the whole fleet and production behaviour is identical to before.

The reason for the hold is measurement, not risk: retiming 26 feeds *during* the
accumulation window would vary the cohort's input distribution mid-measurement, which is
precisely the confound Amendment 1 exists to describe — a corpus built under shifting
conditions, where a FAIL is attributable to the shift rather than the pipeline. Holding
costs only request budget, 1,248/day against a possible ~492, and nothing is bleeding.

## Step 5 — after 7 full days, run the gate. Once.

```bash
py scripts/score_comprehension.py --cutover '<the cutover from step 2>' --horizon-hours 6
```

**Record the output verbatim in the repo, PASS or FAIL.** The parameters are
pre-registered in Amendment 2 and are not open for adjustment after seeing the result.

Before believing a FAIL, read the header line the `zp8` fix added: it now prints
`last event <timestamp>` beside the cohort span on **every** run. If that timestamp has
not kept up with the window, the corpus stopped again and the run is `NOT MEASURABLE`
rather than a failure — the gate will now say so itself instead of leaving it to the eye.

### What must not happen

- **No threshold changes.** `CORROBORATION_FLOOR` stays `0.10`, the ceiling `0.60`.
- **No horizon change.** 6 hours, because 6 was chosen before the void run. 24h is more
  generous to corroboration, which is exactly why adopting it now would be using the
  void run's information.
- **No second void.** This run's verdict stands whatever it says. If it fails, it fails,
  and that is a result about the pipeline rather than about the instrument.

---

## Cost, so the window is entered with eyes open

From `docs/2026-09-10-comprehend-cost-handover.md`: 7–10 Sep cost ≈ $31, of which **57%
was one day** (8 Sep, the `items`-as-JSON-string failure, already fixed in `68688a0`).
The trend after it was 5.14M → 1.63M → 0.96M Sonnet tokens/day, and `h8p` (no-verdict
failures stop charging) and `19i` (the double-wrap recovery) both land *after* those
figures. So 7 days should come in **under the ~$23 that $3.25/day would imply**, likely
well under.

The dead ideas are recorded in that handover and should not be re-proposed: truncating
bodies (they average 154 characters) and caching the 777-token prefix (below Sonnet 5's
1,024-token minimum, where `cache_control` is accepted and silently does nothing).

---
name: env-var-needs-compose-passthrough
description: A new runtime env var is invisible in the container until docker-compose.yml declares it — the env-var sibling of the Dockerfile COPY allowlist chore
metadata: 
  node_type: memory
  type: project
  originSessionId: 5f544ef0-a044-4db3-becf-2fcd5cbae0f3
  modified: 2026-09-07T20:52:35.425Z
---

In news-brief, adding a new `os.environ` knob to `common.py` is only HALF the change. `docker-compose.yml` forwards **only** the variables enumerated in the `x-newsbrief` anchor's `environment:` block. Setting the variable on the host — exported in the shell, in systemd, or in a `.env` beside the compose file — delivers **nothing** to the process: compose reads `.env` purely for `${VAR}` *interpolation*, and with no matching `- VAR=${VAR:-}` line there is nothing to interpolate into.

Found 2026-07-27: all 16 `PG_*` live-trading knobs were missing from the repo's compose file. None of the three 2026-07-21 live-trading plans included a passthrough step, even though the earlier Phase-3 PolyGram plan and the Alpaca failover plan both had one explicitly. Fixed in 1e86f08.

**Symptom to recognise:** a feature that is flag-gated and *fail-closed* silently does nothing, with no error anywhere — because `_env_flag` reads an unset variable as `False` and every guard returns early. Identical presentation to "the feature is broken".

**Use the `${VAR:-}` empty-default style** for every added line. `common._env_flag` treats `""` as False and `common._env_float` falls back to its built-in default on `""`, so a declared-but-empty variable behaves exactly like an unset one — adding the lines is inert until they are populated.

**Two follow-on traps:**
- Add the lines to the **`x-newsbrief` anchor**, not to an individual service. YAML `<<` merge does not deep-merge — a service-level `environment:` key *replaces* the anchor's entire list.
- Cron modes (`docker compose run --rm`) build a fresh container and pick up changes immediately, but `newsbrief-commands` is a long-lived daemon. It keeps its old environment until `docker compose up -d` recreates it — so a Telegram command can report a feature "disabled" long after the cron path sees it enabled.

**Render the compose file WITHOUT leaking the real secrets into context (2026-09-01):** `docker compose config` reads the repo's `.env` automatically and would print live keys into the transcript — and `.env*` paths are denied to the Bash and Read tools anyway. Use a scratch env file instead, named so it does not match the guard's `.env` pattern:

```
docker compose --env-file "$SCRATCH/envtest.txt" config > "$SCRATCH/config.yml" 2>&1; echo "REAL_EXIT=$?"
```

That is the only safe local verification for this stack — `docker compose up -d` locally would start a second Telegram `getUpdates` consumer and 409 the live bot. It also proves the fail-fast half: drop a `:?`-required variable from the scratch file and compose exits 1 naming it, which is the *good* version of this bug class (loud, not silent).

**Verify what the process actually sees**, rather than what the compose file says:
```
docker compose run --rm --entrypoint sh newsbrief-collect -c 'env | grep -E "^PG_|^POLYGRAM_" | sort'
```

Same class as [[dockerfile-copy-allowlist]] (new top-level module ⇒ Dockerfile COPY + workflow path lists). Both are "the code is right but the container never got it" failures that no test can catch, because CI runs on a full checkout with its own environment. Bit us in [[polygram-live-trading-spec]].

## 2026-09-02 — phase 2 makes this SHARPER, not obsolete

The anchor is now the **seed for `settings` rows**, so a missing line has a worse consequence
than before and a shorter window to catch it. Previously a knob compose never named simply fell
back to its code default, every run, recoverably. Now the first boot with an empty `settings`
table copies the environment into rows **once** — so a missing line freezes a code default INTO
A ROW, and adding the compose line afterwards changes nothing, because the importer's emptiness
check has already been satisfied. The fix at that point is a `UPDATE settings`, not a redeploy.

Found while doing `0q0.7.6`: the anchor had **never** named `BRIEF_MEMORY_ENABLED`,
`CLAIM_VERIFY_ENABLED`, `NEWSBRIEF_RETENTION_DAYS`, `CHROMA_MCP_URL`, the `ENRICHMENT_*` family
or `BIGDATA_*` — invisible rather than broken until phase 2, since those modules read the
environment inside the container, found nothing, and used their defaults. Added in `f1ab987`.
**The deploy host's own compose already carried them** (confirmed by him — the repo copy was the
one that was behind, which is the reverse of the usual direction and worth remembering).

**Probe it, don't eyeball it — and use a positive control:**

```
git log --oneline -S"VAR_NAME" -- docker-compose.yml | wc -l     # 0 = never present
git log --oneline -S"PG_A_ENABLED" -- docker-compose.yml | wc -l  # must be >=1, or the probe is broken
```

### THE DIRECTION OF DRIFT IS THE OPPOSITE OF WHAT YOU WILL ASSUME

**The repo's `docker-compose.yml` is a stale reference template. The DEPLOY HOST'S copy is the
real one, and it is AHEAD.** Confirmed by him twice on 2026-09-02: the host compose already
carried the phase-2 knob lines, and **enrichment has been ON since it was implemented** — so the
host also carries `BIGDATA_API_KEY`, which the repo file has never named.

**I got this wrong the same day and it is the mistake to avoid here.** I filed `news-brief-qx4`
as a P2 claiming enrichment was silently running on `NullProvider` in production. The probe —
`git log -S BIGDATA_API_KEY -- docker-compose.yml`, zero commits, positive control passing — was
sound, and answered a question about **the repo's file**. I let it stand for a question about
**the running container**. Classic `the-probe-measured-the-wrong-layer` / `metadata-is-not-state`:
a compose file in git states INTENT, and on this project it is not even the current intent.
`qx4` is now re-scoped to P3 repo/host divergence, its premise marked superseded.

**How to apply:** a compose-file finding licenses a claim about a FRESH CLONE, never about
production. For anything the host actually runs, [[live-state-on-deploy-host]] applies — ask him.
The cost of asking is one message; the cost of a confident wrong "your feature is dark" is that he
goes looking for a bug that does not exist.

Still worth doing, at P3: add `BIGDATA_API_KEY=${BIGDATA_API_KEY:-}` to the repo anchor beside
`MODAL_PROXY_KEY` — inert on a host that does not set it, and it stops a fresh clone running
enrichment dark. `f1ab987` already added the `ENRICHMENT_*` and `BIGDATA_BASE_URL` seed lines.

**Rendering the file needs no scratch env file when the repo has no `.env`** (this dev machine
does not): `POSTGRES_PASSWORD=render-only docker compose config` renders clean and leaks nothing.
Confirm that first by running it BARE — if it exits 1 naming `POSTGRES_PASSWORD`, no `.env` was
picked up. If it renders, a `.env` exists and IS being read; fall back to the scratch-file recipe
above before printing anything.

## 2026-09-04 — VARIANT THREE: the line is there, correct, and names a variable nothing reads

The first two variants were "the knob never reached the container" and "the anchor line was
missing". This one is worse to spot, because from every angle it looks configured: the line
exists, compose interpolates it correctly, it crosses the container boundary, and **no code ever
looks it up**.

`docker-compose.yml` declared `NEWSBRIEF_CAPTURE_ENABLED`. `common.KNOBS` declares
`"CAPTURE_ENABLED": Knob(bool, False)` with **no `env=` override**, and `Knob.key()` returns
`self.env or name` — so the importer reads `os.environ.get("CAPTURE_ENABLED")` and the prefixed
variable is dead. The convention is visible in the siblings: `ENRICHMENT_ENABLED`,
`BRIEF_MEMORY_ENABLED`, `CLAIM_VERIFY_ENABLED` are unprefixed on both sides;
`NEWSBRIEF_RETENTION_DAYS` is prefixed on both because its knob declares `env=`. Capture was the
one that disagreed with itself. Fixed in `b140a43`.

**He hit this live**: set the flag on the host, recreated the container, capture stayed off.

**A BOOL KNOB CANNOT REPORT A BAD VALUE.** `coerce_knob` returns `text.lower() in _TRUTHY`
*before* the empty-string check and the try/except that other types go through. So `'ture'`,
`'yess'` and `''` all silently mean **False**, with **no warning logged** — unlike int/float,
which warn. Never verify a bool knob by reading its row back; verify the EFFECT (for capture:
`capture_runs.enabled = t` on the next fire).

**Now enforced, so this cannot be variant four:** `tests/test_packaging.py` derives the anchor's
variables from the `x-newsbrief` block and the consumed set from three places — `common.KNOBS`
keys, `os.environ` literals across the root modules, and `db._DISCRETE` — and fails on any
variable read by nothing. It carries a presence control (a variable that must appear on BOTH
sides), because an absence assertion is satisfied for free by a parser that returns nothing.
`tests/test_config.py` could never catch this: it exercises the importer with fabricated knobs
(`PG_A_ENABLED`, `PG_A_STAKE`) and never opens the real compose file.

**And the standing operator rule, which is the part most likely to be forgotten:** on an
established host, **no environment change can move a knob at all**. `import_settings_from_env`
runs only while `settings` is empty. The row is the only path — `INSERT … ON CONFLICT (key)
WHERE user_id IS NULL DO UPDATE`. `config.knob`'s own docstring states this: "An absent row means
the code default, NOT a lookup in the environment."

## 2026-09-07 — SECOND live occurrence, and I recommended the wrong route

`COMPREHEND_ENABLED=1` was set in the host's compose and the stack restarted. The 19:00 pass
logged `Comprehend: disabled by COMPREHEND_ENABLED; nothing read`. Nothing ran.

**The rule at the end of the previous section was already correct and I still walked into it** —
because the `bqa.11` bd notes from an earlier session recorded "he will edit the server's compose
himself" without carrying the caveat forward. A rule stated in memory does not survive into a task
note automatically. **When writing a bd note that names an operator action, restate the constraint
inline; the note is what the next session reads, not this file.**

Discriminating probe, three queries, no guessing:

```
SELECT count(*) FROM settings WHERE user_id IS NULL;                       -- non-empty => importer no-ops
SELECT key, value FROM settings WHERE user_id IS NULL AND key ILIKE '%X%'; -- row present?
docker compose exec -T newsbrief printenv | grep -i X                      -- env present?
```

Reading it: env set + no row = this bug. No row + no env = the edit landed nowhere. Row under a
PREFIXED name = right idea, wrong key.

**Ruled out during that investigation, so nobody re-checks them:** cache staleness (60s TTL, and
each job runs as a FRESH CHILD PROCESS so its settings cache starts empty — no restart is ever
needed for a row change to take effect), and value casing (`coerce_knob` does
`text.lower() in _TRUTHY`, so `True`/`TRUE`/`on`/`yes`/`1` all work; `t` and `enabled` do not).

The fix is one statement, no redeploy and no restart. **The `WHERE user_id IS NULL` in the
conflict clause is required**, not decoration — `settings_key_global` (`migrations/0001:23`) is a
PARTIAL unique index and Postgres needs the predicate to infer it:

```sql
INSERT INTO settings (key, user_id, value) VALUES ('COMPREHEND_ENABLED', NULL, '1')
ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value = EXCLUDED.value, updated_at = now();
```

Keep the compose line anyway: it seeds the row on a fresh database and `tests/test_packaging.py`
enforces anchor-to-KNOBS parity. Compose is right for a NEW deployment, the row for an ESTABLISHED
one. Filed `news-brief-5fc` to make this loud — a boot-time warning for any KNOBS key set in the
environment with no backing row, which would have caught it in the 18:16 startup log. Note both
arms matter: an absent row is what fired here, but a row DISAGREEING with the env var is the
nastier variant, where a row set months ago silently wins with an identical symptom.

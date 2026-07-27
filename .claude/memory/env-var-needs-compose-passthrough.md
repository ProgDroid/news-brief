---
name: env-var-needs-compose-passthrough
description: A new runtime env var is invisible in the container until docker-compose.yml declares it — the env-var sibling of the Dockerfile COPY allowlist chore
metadata: 
  node_type: memory
  type: project
  originSessionId: 5f544ef0-a044-4db3-becf-2fcd5cbae0f3
  modified: 2026-07-27T09:36:43.645Z
---

In news-brief, adding a new `os.environ` knob to `common.py` is only HALF the change. `docker-compose.yml` forwards **only** the variables enumerated in the `x-newsbrief` anchor's `environment:` block. Setting the variable on the host — exported in the shell, in systemd, or in a `.env` beside the compose file — delivers **nothing** to the process: compose reads `.env` purely for `${VAR}` *interpolation*, and with no matching `- VAR=${VAR:-}` line there is nothing to interpolate into.

Found 2026-07-27: all 16 `PG_*` live-trading knobs were missing from the repo's compose file. None of the three 2026-07-21 live-trading plans included a passthrough step, even though the earlier Phase-3 PolyGram plan and the Alpaca failover plan both had one explicitly. Fixed in 1e86f08.

**Symptom to recognise:** a feature that is flag-gated and *fail-closed* silently does nothing, with no error anywhere — because `_env_flag` reads an unset variable as `False` and every guard returns early. Identical presentation to "the feature is broken".

**Use the `${VAR:-}` empty-default style** for every added line. `common._env_flag` treats `""` as False and `common._env_float` falls back to its built-in default on `""`, so a declared-but-empty variable behaves exactly like an unset one — adding the lines is inert until they are populated.

**Two follow-on traps:**
- Add the lines to the **`x-newsbrief` anchor**, not to an individual service. YAML `<<` merge does not deep-merge — a service-level `environment:` key *replaces* the anchor's entire list.
- Cron modes (`docker compose run --rm`) build a fresh container and pick up changes immediately, but `newsbrief-commands` is a long-lived daemon. It keeps its old environment until `docker compose up -d` recreates it — so a Telegram command can report a feature "disabled" long after the cron path sees it enabled.

**Verify what the process actually sees**, rather than what the compose file says:
```
docker compose run --rm --entrypoint sh newsbrief-collect -c 'env | grep -E "^PG_|^POLYGRAM_" | sort'
```

Same class as [[dockerfile-copy-allowlist]] (new top-level module ⇒ Dockerfile COPY + workflow path lists). Both are "the code is right but the container never got it" failures that no test can catch, because CI runs on a full checkout with its own environment. Bit us in [[polygram-live-trading-spec]].

---
name: watch-and-pushed-files
description: "Operator-pushed private files (watch.json, theses_portfolio.json, both ON the host since 2026-11-19 via portfolio oaq) and the 8lp watch job: SCHEDULED 06:30 PROVISIONALLY 2026-11-19, ahead of gate 8lp.1 (deferred to 2026-12-03); replay needs --entrypoint python; TRIGGERED alerts RING (notify=True), everything else stays silent"
metadata:
  node_type: memory
  type: project
  originSessionId: 8be1e10c-5ebb-4071-9f1c-599a0c2b2743
  modified: 2026-11-22T20:13:19.583Z
---

Two files reach `DATA_DIR` (`/app/logs` in the container) by an operator push from the
user's **private** portfolio repo. Their contracts live in the 8lp/ajf/vap bead designs and
`docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md`. **Never commit either file
or a real row, and never log a trigger's text: this repo is public.**

- **The push overwrites in place and CANNOT create files (since 2026-11-20, portfolio bead
  7qu).** It runs as a restricted host user whose folder access is read+execute only. So
  news-brief must keep both files **present and group-writable by `users`**: deleting one, or
  replacing it with a write-temp-then-rename that drops group write, breaks every later push.
  Nothing here writes either file today (`watch.py` docstring: "never renamed or modified");
  keep it that way, and any cleanup or retention job must skip them.
- **`theses_portfolio.json` (ajf, closed 2026-11-18).** It's merged over `/thesis`'s
  `theses.json`, and the pushed line wins a clash. A **missing** file warns on Telegram on every
  `submit`. That's the user's explicit choice: a push that never lands must not go quiet.
- **`watch.json` → the `watch` job (8lp, open).** **Scheduled 06:30 daily on 2026-11-19,
  PROVISIONALLY, before its replay gate** `news-brief-8lp.1`, by user decision: an unvalidated
  watcher beats none. Until the gate passes, a quiet night means "probably nothing", NOT
  verified. When the gate is picked up, the user pastes headlines and the agent writes the rows
  and expected verdicts (the user only confirms events). Close 8lp only after the gate passes.
- **Replay on the host:**
  `docker compose run --rm --entrypoint python newsbrief scripts/watch_replay.py --as-of <ISO UTC> --rows /app/logs/<rows>.json`.
  The image's ENTRYPOINT is `python brief.py`, so without `--entrypoint python` you get
  brief.py's usage line and exit 1. The plan, the spec and the docstring all shipped that bug
  first, and the final review caught it. Bead 115 must keep `scripts/` in the image until the gate passes.
- **`vap` (price rows): BUILT 2026-11-19 as stage 2 of the same 06:30 job** (no separate job:
  red-team, user agreed). Code, not the model; same rules as the portfolio `watch_check.py`.
  Parity measured 29/29 agree on real data (`scripts/watch_price_parity.py`, run under the
  portfolio venv). It alerts on crossing, rings, and re-arms when a row stops firing. **Bars are
  dated in the exchange's timezone:** Yahoo stamps FX bars at London midnight, 23:00Z in
  summer, so UTC dating judged forming bars. That needs `tzdata` (pinned) because
  python:3.12-slim has no zone database; the Dockerfile smoke-checks it.

**Why:** these are the user's decisions from 2026-11-18. The judge reads captured items, not
the brief, so its links are real. It alerts once per row (re-alerting only on an edited trigger
or a stronger verdict). It uses a fixed 48 h window, because job_runs marks crashed runs
`finished`. A bad row is skipped while the rest are judged.

**How to apply:** before changing watch behaviour, read the spec's "Calls made by the agent"
list: those are open to revision, and the user decisions above are not. **Decided 2026-11-19:** TRIGGERED alerts **ring**
(`telegram_send(..., notify=True)`; it fires once per row, so a silent one can be missed
for good). UNCLEAR and the rows-not-judged report stay silent, like every other message,
which is the default. Deferred review minors are in
`news-brief-r1w`. Related: [[live-state-on-deploy-host]], [[telegram-send-long-convention]].

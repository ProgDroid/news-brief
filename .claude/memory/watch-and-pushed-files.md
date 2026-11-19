---
name: watch-and-pushed-files
description: "Operator-pushed private files (watch.json, theses_portfolio.json, both ON the host since 2026-11-19 via portfolio oaq) and the 8lp watch job: SCHEDULED 06:30 PROVISIONALLY 2026-11-19, ahead of gate 8lp.1 (deferred to 2026-12-03); replay needs --entrypoint python; open question: should TRIGGERED alerts ring"
metadata:
  node_type: memory
  type: project
  originSessionId: 8be1e10c-5ebb-4071-9f1c-599a0c2b2743
  modified: 2026-11-18T20:41:13.162Z
---

Two files reach `DATA_DIR` (`/app/logs` in the container) by an operator push from the
user's **private** portfolio repo. Their contracts live in the 8lp/ajf/vap bead designs and
`docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md`. **Never commit either file
or a real row, and never log a trigger's text: this repo is public.**

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
- **`vap` (price rows)** depends on 8lp. The loader already validates price rows, as
  `watch.PriceRow`.

**Why:** these are the user's decisions from 2026-11-18. The judge reads captured items, not
the brief, so its links are real. It alerts once per row (re-alerting only on an edited trigger
or a stronger verdict). It uses a fixed 48 h window, because job_runs marks crashed runs
`finished`. A bad row is skipped while the rest are judged.

**How to apply:** before changing watch behaviour, read the spec's "Calls made by the agent"
list: those are open to revision, and the user decisions above are not. **Open question for the
user:** news-brief sends with `disable_notification`, so a once-only TRIGGERED alert is
silent and can be missed. Ask before changing it. Deferred review minors are in
`news-brief-r1w`. Related: [[live-state-on-deploy-host]], [[telegram-send-long-convention]].

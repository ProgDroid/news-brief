---
name: telegram-send-long-convention
description: "Growth-prone Telegram content must use telegram_send_long (split-safe), not bare telegram_send; plus the monkeypatch-namespace gotcha that breaks tests of common-defined send wrappers"
metadata: 
  node_type: memory
  type: project
  originSessionId: 738ff289-e913-4911-81a2-fd6af8d0597d
---

2026-07-02 (commit d1159d1 → origin/main, deploy triggered): weekly mode's
2nd message — `performance_report(book)` (positions + marks) — 400'd
"message is too long" because it was sent with **bare `telegram_send`**,
which has NO length handling. `performance_report` grows unbounded (a line
per thesis_ref/ticker × dimension). Fix = new `common.telegram_send_long(text)`
(runs `split_html_message` → sends each chunk → throttles 0.4s → returns
False if any chunk failed). Routed 4 growth-prone sites through it: `mode_weekly`,
`mode_collect` daily trade update, `/positions`, `mode_monitor` alerts;
folded the already-split `/dig` + `/performance` loops in too. `deliver()`
keeps its OWN split loop (bespoke per-chunk failure logging + archive alert).

**Why:** the class-of-bug is "shared splitter exists, but a caller open-codes
a bare `telegram_send` and skips it." Weekly cron 400'd while the `/performance`
command (which always split) was fine — same string function, two paths, one unsafe.

**How to apply:**
- Any Telegram content that grows with data (reports, position/alert LISTS,
  model prose) → `telegram_send_long`. Bare `telegram_send` is ONLY for
  fixed-size acks (`🔇 Muted: …`, `No open positions.`). Telegram cap ~4096;
  repo targets `TELEGRAM_MAX_LEN = 4000`.
- **Test monkeypatch gotcha (bit me this session):** `brief.telegram_send` and
  `common.telegram_send` are SEPARATE names for the same object. A function
  defined in common.py (`telegram_send_long`) calls the *bare* name, which
  resolves in **common's** namespace. So a test patching only
  `brief.telegram_send` STOPS intercepting once a handler routes through a
  common-defined wrapper — the real API fires (404 in tests). Patch
  `common.telegram_send` (or both). Same trap for any future common-side wrapper
  of an imported function.

Distinct from the DEFERRED tag-split 400 in [[newsbrief-deferred-findings]]
(#2: splitter can cut between an open/close tag → invalid-HTML chunk) — that's
a different 400 cause, still open, low-prob.

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

## 2026-09-04 — there is a THIRD channel, and a capture helper that missed it

`telegram_send_buttons` is a separate send path from both `telegram_send` and
`telegram_send_long`, and it is how `/reset` and every wizard step answers. A test helper that
patches only `brief.telegram_send` therefore leaves it open.

That is not hypothetical: `_capture_sends` in `tests/test_delivery_and_state.py` patched exactly
that one name, so `test_handle_update_ignores_foreign_chat` asserted `sent == []` while `/reset`
actually answered a foreign chat over the real network. The empty list meant nothing. Widened to
patch `brief.telegram_send`, `common.telegram_send` and `brief.telegram_send_buttons`, and the
test gained a presence control asserting the OWNING chat does get answered — without which
"nothing was sent" is satisfied by a helper that simply cannot see the send.

**How to apply:** before writing any "nothing was sent" assertion, enumerate the channels — bare
send, send_long (resolves in `common`), send_buttons — and patch all of them. A silence assertion
is only as strong as the narrowest channel it watches.

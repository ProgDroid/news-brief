---
name: telegram-commands-text-parsed
description: Telegram commands work via text parsing (no setMyCommands); BotFather /setcommands is autocomplete-only
metadata: 
  node_type: memory
  type: project
  originSessionId: e9ab7554-6727-4032-aa2a-7d1b7ecb73a5
---

The bot has **no `setMyCommands` call anywhere** (grep confirms). Commands are handled
purely by parsing incoming message text in `_handle_telegram_update` (`text.startswith("/pin ")`,
`text == "/positions"`, …). So a command executes the moment its handler branch exists —
**registration with Telegram is NOT required for it to function**.

BotFather `/setcommands` (and the `setMyCommands` Bot API) only populate the **autocomplete
menu** that pops up when you type `/` in the chat. Pure UX/discoverability; zero effect on whether
a command runs.

**Why:** asked 2026-06-14 ("will new commands be added automatically or do I edit via BotFather?").
The answer is non-obvious without grepping for `setMyCommands` — easy to assume new commands need
registration.

**How to apply:** adding a new `/cmd` → add the handler branch in `_handle_telegram_update`,
a `HELP_TEXT` line, AND an entry in the `BOT_COMMANDS` list (`brief.py`). It works immediately;
the `/` autocomplete menu now self-syncs too.

**UPDATE 2026-06-19 — the self-sync IS built now.** `register_bot_commands_if_changed()` calls
`setMyCommands` on `commands`-daemon startup, hash-gated against `state['cmd_hash']` so it only
fires when `BOT_COMMANDS` changes. So new commands appear in autocomplete automatically after a
deploy — no BotFather paste. The bot ALSO now handles `callback_query` (inline-button taps), not
just text. See [[telegram-source-management-daemon]] for the daemon rewrite (the old 30-min
`commands` cron is gone) and [[multi-asset-trading-build]] for the command inventory.

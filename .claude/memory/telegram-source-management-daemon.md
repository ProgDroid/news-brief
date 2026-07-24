---
name: telegram-source-management-daemon
description: commands mode is now a real-time long-poll daemon; temp news sources managed via Telegram buttons + sources.json on the volume
metadata: 
  node_type: memory
  type: project
  originSessionId: a6e4b8fd-aaad-4013-8932-500dae7b6f55
---

SHIPPED 2026-06-19. Two coupled changes to the news-brief command surface:

**1. `commands` mode is now a long-running daemon, not a 30-min cron.** `mode_commands`
is a `while True` long-poll loop (`telegram_get_updates(timeout=30, allowed_updates=
["message","callback_query"])`). Run it as a persistent service: `docker compose up -d
newsbrief-commands` (compose service has `restart: unless-stopped`). It is the **ONLY**
`getUpdates` consumer — a second poller gets HTTP **409 Conflict**, so the old
`process_telegram_commands()` call was **removed from `mode_submit`** (that function is gone;
its testable batch logic lives in `_drain_update_batch`). `telegram_get_updates` returns
`None` on error (incl. 409) so the daemon backs off instead of hot-looping.

**Why:** user wanted instant commands + a guided button UX, and the cron model couldn't do
multi-turn or buttons. Long-poll is the standard bot architecture; the cron was the odd choice.

**2. Temporary news sources, Telegram-managed.** Always-on feeds stay hardcoded in `RSS_FEEDS`
(baked into the image). Temp sources live in `sources.json` on the persistent volume
(`DATA_DIR/sources.json` → `${APPDATA_DIR}/news-brief/sources.json`), merged into `RSS_FEEDS`
at `submit` time (`load_temp_sources()`). So they change **without an image rebuild or a
container restart** — the next brief reads the file fresh. This was the whole point: the user
wanted to add a source for a flaring story without redeploying, and without the env-var path
(which still needs container recreation + is fragile single-line JSON).

**How to apply / key facts:**
- `/addsource` = in-memory button wizard (`_WIZARD` dict, keyed by chat_id): tap category →
  tap kind (`wire|analyst|regional|primary`) → paste a domain OR full URL → confirm (with
  optional ✏️ Rename). Bare domain → Google News `site:` feed via `build_google_news_url`;
  full `http(s)://` → used as-is. Wizard state is in-memory only (lost on restart mid-setup =
  just re-tap; acceptable per user).
- `/sources` lists temp sources each with a 🗑 button; `/removesource <name>` is the text
  fallback. Buttons key by `_source_id` = first 10 hex of sha1(url) (callback_data is 64-byte
  capped, so the URL can't go in it).
- `load_temp_sources()` is defensive: non-list / missing fields / bad kind → dropped+logged,
  never raises. A broken `sources.json` degrades to "no temp sources", never breaks the brief.
- Store funcs (`load/add/remove_temp_source`, `build_google_news_url`, `_source_id`) live in
  `brief.py` (NOT a new module — avoids the [[dockerfile-copy-allowlist]] trap). Telegram API
  helpers (`telegram_send_buttons`, `telegram_edit_text`, `telegram_answer_callback`,
  `telegram_set_my_commands`) are in `common.py`.
- Every `callback_query` MUST be answered (`answerCallbackQuery`) or the button spins forever.

**Button UX extended to the rest of the command surface (same session, no deferrals):**
`/close`, `/unwatch`, `/unpin` now each take a no-arg form that renders the live list as
tappable buttons (text forms still work); `/reset` now asks [Yes]/[No] before clearing.
Buttons key by `_short_id` (sha1[:10]) over a stable field and re-resolve against the live
list on tap. `_handle_callback_query(cb, fb)` now takes+returns `fb` (threaded by
`_handle_update`) so button actions that change overrides (unpin, reset) stay consistent with
the daemon's in-memory copy. New callback prefixes: `close:`, `unwatch:`, `unpin:`, `reset:`
(plus existing `as:`, `rmsrc:`). Picker callbacks sit BEFORE the wizard-state guard in
`_handle_callback_query`. `_close_ticker` was factored out so text + button share it.

**DEPLOY GOTCHA — setMyCommands scope shadowing (hit + resolved on server 2026-06-19).**
After the daemon logged "Registered N bot commands" (so `setMyCommands` returned `ok:true`),
the `/` autocomplete in the private DM still showed the OLD list. Root cause: a pre-existing
**`all_private_chats`** command scope held stale commands, and scope precedence for a private
chat is `chat` > `all_private_chats` > `default`. Our code writes only the **default** scope,
so the stale `all_private_chats` scope shadowed it in the DM. Client cache (app restart) did
NOT fix it. Fix that worked: `deleteMyCommands` with scope `{"type":"all_private_chats"}`,
then restart the client. Diagnose with `getMyCommands` (default scope returns the NEW list,
proving registration worked) vs `getMyCommands?scope={"type":"all_private_chats"}` (returns the
stale list = the culprit). The code still registers default-scope only; if this recurs, the
durable fix is to register to `all_private_chats` (or delete that scope) on daemon startup —
offered, not built, since the stale scope was a one-time leftover now cleared.

Relates to [[telegram-commands-text-parsed]] (setMyCommands self-sync now built),
[[google-news-rss-recipe]] (the `when:2d site:` recipe `/addsource` reuses), and
[[brief-sources-and-edge-latency-thread]].

---
name: t212-401-diagnosis
description: "Trading212 401 — RESOLVED: auth is HTTP Basic base64(KEY_ID:KEY), not a raw key; root cause was a literal-placeholder bug"
metadata: 
  node_type: memory
  type: project
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
---

RESOLVED 2026-06-01. The Trading212 401 on `/api/v0/equity/positions` (and `/equity/metadata/instruments`) was a **code bug**, not a credential/scope/quote problem: every auth site base64-encoded the *literal placeholder bytes* `b"T212_API_KEY:T212_API_SECRET"` instead of the real credentials. Fixed in brief.py — single `t212_auth_header()` helper builds the header; both callers use it.

**Why (correct auth mechanism — this overrides the earlier "raw key, no Bearer" note which was wrong):** T212 uses **HTTP Basic** auth: `Authorization: Basic <base64(T212_API_KEY_ID:T212_API_KEY)>`. Confirmed by the user's working curl from the container. The two env vars are the Basic username:password pair. There is no `Bearer` and the key is NOT sent raw.

**How to apply:**
- Build the header in exactly one place via `t212_auth_header()` (brief.py). Format: `f"{T212_API_KEY_ID}:{T212_API_KEY}"` → base64 → prefix `Basic `.
- Use Python's `base64.b64encode` (never `encodebytes`): it emits no line breaks, so the token is always header-safe. The curl equivalent MUST use `base64 -w0` — GNU `base64` wraps at 76 cols by default and a wrapped newline inside the header causes a 401. This is why the user's curl only worked with `-w0`.
- The `.strip()` on `T212_API_KEY_ID`/`T212_API_KEY` (brief.py:75-76) still matters: a stray trailing `\n` from a Docker secret/.env would corrupt the base64 payload and resurrect the 401 from a different source.
- 401 still means the *credential value* is rejected (not scope — that's 403), but for this codebase always check the header-construction code first. See [[brief-local-run]].

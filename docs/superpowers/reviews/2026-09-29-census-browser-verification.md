# Event census labeller: recorded browser run (D2)

- **Date:** 2026-09-29 (run 19:28 to 20:03 UTC)
- **Commit under test:** `0801153`. The working tree was clean for `labeller.py`,
  `labeller_static/` and `census.py`. No source file was changed for this run.
- **Browser:** Chromium 154.0.8037.58 (UA `Chrome/154.0.0.0`), driven by the Playwright
  MCP server (`@playwright/mcp` 0.0.83 from the local npx cache, playwright-core
  1.64.0-alpha). `document.visibilityState` was `visible` throughout.
- **Scope:** four behaviours of `labeller_static/label.js` that the Python suite cannot
  execute. Each has steps, what was expected, what was observed, the evidence and a
  verdict.

Secrets are redacted. The session cookie, the login-link token and the throwaway role
password do not appear in this record.

## Setup

- **Database:** the test Postgres already running on `localhost:5432/newsbrief_test`,
  held exclusively for this run. pytest was not run.
- **Setup script:** a scratch script, outside the repo, run with `py` from the repo root
  as the main `newsbrief` role. It:
  1. ran `DROP SCHEMA public CASCADE` and `CREATE SCHEMA public`, then
     `db.run_migrations` (0001 to 0017);
  2. called `census_fixtures.prepared(conn, today=2026-10-01, now=<wall clock>)`. The
     result served `blind` on window 1, session 1/17, 45 items;
  3. called `census.apply_labeller_grants` with a random throwaway password;
  4. called `census.mint_link` with the wall clock.

  Every later state change also went through that script and the public census API,
  using the wall clock. The labeller uses the same clock (`labeller._now` is not pinned
  in a real process).
- **Labellers:** two processes of `py labeller.py`, both started as `census_labeller`
  through `POSTGRES_*` with `DATABASE_URL` unset. Both passed the startup self-check:
  each logged `labeller listening ...`, with no missing or surplus grant.
  - **L1:** `LABELLER_BASE_URL=http://127.0.0.1:18765`, bound to 127.0.0.1:18765. Used
    for behaviours 1, 2, 3a and 4.
  - **L2:** `LABELLER_BASE_URL=https://127.0.0.1:18766`, bound to 127.0.0.1:18766 and
    reached over plain `http://`. Used for behaviour 3b. The only difference from L1 is
    the scheme in the base URL, which is a realistic misconfiguration (see 3b).
- **Login:** the browser opened `/open?t=<redacted>`, got a 303 and landed on `/` with
  the session cookie. Cookies are scoped by host, not port, so the same cookie reached
  L2.
- **Clock:** `page.clock.install()` ran before the first navigation. The fake clock is
  context-wide, so page timers (the 60 s heartbeat and the 3 s retry) fire only when the
  run calls `clock.fastForward`. That is what makes "no further heartbeat" provable
  instead of a matter of waiting.
- **Evidence capture:** Playwright `request` and `response` listeners, with bodies from
  `response.text()` and headers from `request.allHeaders()`. Also used: the MCP console
  log, the MCP network list, the labeller's own access log (`labeller <METHOD> <path>
  <status>`), and DOM or accessibility-snapshot reads of `#unsaved`, the banner.
- **Screenshots:** saved in the session scratchpad, not committed:
  `d2-b1-heartbeat-stopped.png`, `d2-b2-callback-error.png`,
  `d2-b3a-session-expired.png`, `d2-b3b-wrong-address.png` and
  `d2-b4-stale-precision-400.png`.
- **Background noise:** every page load logs one console error, `Failed to load
  resource: ... 404 ... /favicon.ico`. It is unrelated and is left out of the evidence
  below.

---

## 1. The heartbeat stops after a 400: PASS

**Steps**

1. Loaded the blind page on L1. `#data` held `kind=blind`, `window_id=1`,
   `heartbeat_seconds=60`, 45 rows, and the banner was hidden.
2. **Control:** advanced the clock 60 s. One heartbeat went out and returned 204. This
   proves that `fastForward` drives the heartbeat interval and that the listener sees
   it.
3. From Python, finished window 1 with `census_fixtures.label(conn, w1, <2
   cross-outlet groups>, now)`. The window went to `blind_done` and the census served
   `precision` on window 1 (2 pairs).
4. Advanced the clock 60 s, then 60 s three more times, then 600 s: 13 minutes of page
   time in total after the 400.

**Expected:** one heartbeat returns 400 and no heartbeat follows. There is no banner,
because label.js stops the heartbeat quietly on a 400. The server's route is
`record_event`, which raises `BadWrite` for a window in `_DONE`.

**Observed:** as expected.

**Evidence**

Listener output, per clock step:

```
+60s  (control, before the finish)  POST /api/heartbeat 204
+60s  (1st after the finish)        REQ POST /api/heartbeat -> POST /api/heartbeat 400
+60s  #1                            (none)
+60s  #2                            (none)
+60s  #3                            (none)
+600s                               (none)
```

MCP network list for the page:

```
5. [POST] http://127.0.0.1:18765/api/heartbeat => [204] No Content
6. [POST] http://127.0.0.1:18765/api/heartbeat => [400] Bad Request
```

L1's access log. There is no later heartbeat line anywhere in the log, including the
rest of the run:

```
20:29:18,423 INFO labeller POST /api/heartbeat 204
20:29:36,494 INFO labeller POST /api/heartbeat 400
```

Console: `[ERROR] Failed to load resource: the server responded with a status of 400
(Bad Request) @ http://127.0.0.1:18765/api/heartbeat`. This is the browser's own
network log line; label.js logs nothing on this path.

Banner after the steps: `hidden=true`, text `""`, and `body.className` was empty. The
accessibility snapshot shows no banner node. The page stays interactive: Finish was
still enabled.

**Note:** by design the page now looks live. The operator learns the window is over
only on their next queued action, which is the 409 "window closed — reload" path. That
path was not exercised here.

---

## 2. A callback exception stops the queue with an error, not a silent stall (m11): PASS

label.js runs inside an IIFE, so `pump`, `stop` and the op callbacks cannot be patched
through `evaluate`. The exception was provoked at the response instead. A Playwright
`page.route('**/api/groups')` passed the request to the real server with
`route.fetch()`, which returned `{"group_id": 3}` (the group really was created). The
route then answered the page with `200`, `application/json` and body `null`. With a 200
status, `result.ok` is true, `op.done(null)` runs, and `groups.get(key).serverId =
json.group_id` throws. That is a real exception thrown in the real callback, inside the
real `pump`.

**Steps**

1. After behaviour 4, the census served blind on window 2. Reloaded L1 and got
   `kind=blind`, `window_id=2`, session 2/17.
2. With the route installed, clicked rows 0 and 1, then **New group**. This queues
   `/api/groups` and, behind it, `/api/assign`.
3. Removed the route. Clicked rows 2 and 3.
4. Read the state of the bar buttons.
5. Called `.click()` from script on the disabled **New group** button.
6. Advanced the clock 5 minutes, past the 3 s retry and five heartbeat periods.
7. The bar has `pointer-events: none` when `body.stopped`, so a pointer click cannot
   reach it. Focused **Finish blind pass ✓** and pressed Enter, then accepted the
   confirm dialog.
8. Advanced the clock another 10 s.

**Expected:**

- banner `not saved: unexpected page error — reload` (label.js line 163), with class
  `fatal`;
- `console.error("census label callback failed", err)`;
- the queued `/api/assign` never sent;
- no later action, retry or heartbeat reaching the network.

**Observed:** as expected. One UI detail is noted after the evidence.

**Evidence**

Listener output:

```
RESP GET / 200
REQ  POST /api/groups {"window_id":2}
RESP POST /api/groups 200            (page saw body `null`; server's real body {"group_id": 3})
console[error]: census label callback failed TypeError: Cannot read properties of null (reading 'group_id')
    at Object.done (http://127.0.0.1:18765/static/label.js:475:43)
    at http://127.0.0.1:18765/static/label.js:159:29
--- further actions after the stop ---
(no request: row clicks, programmatic New group click, +5 min clock, keyboard Finish + confirm, +10 s clock)
```

Banner right after the exception: `hidden=false`, class `fatal`, text **`not saved:
unexpected page error — reload`**, and `body.className = "stopped"`. The banner was
unchanged at the end of step 8.

After step 3 the count read `Selected: 0`: row clicks are ignored once the page has
stopped.

Button state after step 3:

| Button | Disabled |
|---|---|
| New group | true |
| Ungroup | true |
| Unsure | true |
| Abandon… | **false** |
| Finish blind pass ✓ | **false** |

The computed `pointer-events` of `.bar` was `none`.

In step 7 the confirm dialog did appear. After it was accepted, `finishing` locked the
bar (Finish became disabled), but no `/api/finish` was sent: `enqueue` returns early
when the page has stopped.

L1's access log, from the reload onward:

```
21:00:48,791 INFO labeller GET / 200
21:00:48,981 INFO labeller POST /api/groups 200
(next line is the 3a reload at 21:02:43 — no /api/assign, no /api/finish, no heartbeat in between)
```

The census state afterwards: window 2 still `prepared`, and blind on window 2 still
served.

**Detail (a finding, not a failure):** `stop()` does not call the blind page's
`refresh()`. That leaves **Finish** and **Abandon…** enabled in the DOM after a stop,
and in fact after any `stop()`. Pointer input is blocked only by the CSS rule `body.stopped
.bar { pointer-events: none }`. Keyboard input still reaches the buttons: Finish can be
focused and activated, it raises its confirm dialog, and it then does nothing, so the
action is dropped silently beyond the banner that is already shown. Nothing is sent and
nothing is lost, and the fatal banner already says reload, so this is a small UX
inconsistency rather than a stall.

The group row created server-side (`census_groups` id 3) was left with no assignment.
That is the expected leftover of this failure path and has no effect on metrics.

---

## 3a. 403, session expired: PASS

**Steps**

1. Loaded the blind page on L1 (window 2) with the banner hidden.
2. From Python, called `census.revoke_all(conn, now)`, which revoked 1 row.
3. Clicked rows 6 and 7, then **New group**.
4. Advanced the clock 5 minutes.

**Expected:** `/api/groups` returns 403 with body `forbidden: session`, and the banner
reads `session expired — open a new /label link` (label.js line 94).

**Observed:** as expected.

**Evidence**

```
REQ  POST /api/groups Origin=http://127.0.0.1:18765 cookie=[present, redacted]
RESP POST /api/groups 403 body="forbidden: session"
console[error]: Failed to load resource: the server responded with a status of 403 (Forbidden)
--- after +5 min of page clock --- (no further request)
```

L1's access log: `21:02:59,550 INFO labeller POST /api/groups 403`, followed by no
further requests.

The banner as the accessibility snapshot shows it:

```yaml
- banner:
  - heading "Session 2/17" [level=1]
- generic: session expired — open a new /label link
```

Banner class `fatal`, `body.className = "stopped"`.

---

## 3b. 403, wrong address (Origin mismatch): PASS

**How it was reached.** The Host check and the Origin check are separate
(`labeller._host_ok`, `_origin_ok`). A plain browser sends a Host that matches the
address it loaded, so a wrong *host* is refused on the GET with a plain-text 403 before
any page loads, and label.js never runs. The Origin-only mismatch is reachable when
`LABELLER_BASE_URL` names a different **scheme** from the one the operator uses. L2 was
configured with `https://127.0.0.1:18766` and reached over `http://127.0.0.1:18766`.
Two things follow:

- `Host: 127.0.0.1:18766` is in `expected_hosts`, because the port is non-default and
  the netloc is compared without the scheme. So the GET serves the page.
- The browser's `Origin: http://127.0.0.1:18766` is not in `expected_origins`
  (`{"https://127.0.0.1:18766"}`).

This is a real, not crafted, request from the real page. It is also a realistic
deployment mistake, for example a base URL written for a TLS proxy while the phone is
using the plain LAN address.

**Steps**

1. Navigated to `http://127.0.0.1:18766/`. The GET returned 200 with blind on window 2.
2. Clicked rows 4 and 5, then **New group**.
3. Reloaded and repeated step 2 with `allHeaders()`, to record the Origin actually sent.

**Expected:** 403 with body `forbidden: origin`, and the banner `blocked: page address
does not match LABELLER_BASE_URL` (label.js line 93).

**Observed:** as expected, on both attempts.

**Evidence**

```
REQ  POST /api/groups Host=127.0.0.1:18766 Origin=http://127.0.0.1:18766
RESP POST /api/groups 403 body="forbidden: origin"
console[error]: Failed to load resource: the server responded with a status of 403 (Forbidden)
```

L2's access log:

```
21:02:25,031 INFO labeller GET / 200
21:02:26,178 INFO labeller POST /api/groups 403
21:02:36,934 INFO labeller GET / 200
21:02:38,064 INFO labeller POST /api/groups 403
```

Banner: `hidden=false`, class `fatal`, text **`blocked: page address does not match
LABELLER_BASE_URL`**, and `body.className = "stopped"`. There were no further requests
after the clock was advanced 5 minutes.

On the first attempt, Playwright's provisional `request.headers()` reported no Origin.
`allHeaders()` on the second attempt shows what went on the wire, which was
`http://127.0.0.1:18766`.

---

## 4. D1 guard: a write to a window no longer served gets 400 and the page says reload: PASS

**How it was reached:** legitimately, with two tabs, and no crafted request produced the
refusal. A blind write to an unfinished window that is not served cannot be reached by
two tabs. Two tabs always load the same served task, and the only way to move the
census off a blind window is to finish or abandon it, which makes later writes to it a
409 (closed), not a 400. The precision page does reach D1, because `save_precision` has
no closed-window check of its own and calls `_require_served` first.

**Steps**

1. After behaviour 1, the census served `precision` on window 1 with 2 pairs,
   `[[1441,1442],[1443,1444]]`.
2. Tab A: reloaded L1 and got the precision page, "Pair 1 of 2: the same occurrence?".
3. Tab B: opened L1 on the same precision page.
4. Tab B answered **Same** on both pairs. After the last answer label.js reloads, and
   tab B then showed `kind=blind`, `window_id=2`, session 2/17. Window 1 was now
   complete.
5. Tab A, which still showed pair 1 of 2, clicked **Same**.

**Expected:** `POST /api/precision` returns 400, because `_require_served` raises
`BadWrite` "window 1 is not served for precision ... reload". The page runs
`stopFor(400)`, and the banner reads `not saved: the server refused a change (400) —
reload` (label.js line 95).

**Observed:** as expected.

**Evidence**

```
tabA: GET / 200
tabB: GET / 200
tabB: POST /api/precision 204
tabB: POST /api/precision 204
tabB: GET / 200                       (label.js reload -> blind window 2)
tabA: POST /api/precision 400
tabA console[error]: Failed to load resource: the server responded with a status of 400 (Bad Request)
```

L1's access log:

```
21:00:22,280 INFO labeller POST /api/precision 204
21:00:23,111 INFO labeller POST /api/precision 204
21:00:23,167 INFO labeller GET / 200
21:00:23,297 INFO labeller POST /api/precision 400
21:00:34,485 INFO labeller POST /api/precision 400   <- the body-read replica below, not the page
```

Tab A's banner: `hidden=false`, class `fatal`, text **`not saved: the server refused a
change (400) — reload`**, and `body.className = "stopped"`. The **Different** button
was disabled.

**Which 400 it was.** The page's own response body was not captured, because the MCP
`browser_network_request` call hung (see below). To show that the 400 was the D1 guard,
tab A sent one replica request with the same path and body, `{"window_id":1,
"item_a":1441, "item_b":1442, "decision":"same"}`, the same cookie and the same
`mode: "cors"`. It returned:

```
400 {"error": "window 1 is not served for precision (the census serves blind on window 2); reload"}
```

Nothing else could have produced that text. In `save_precision` the `_require_served`
call runs before the "pair not offered" check. The replica wrote nothing.

---

## Concerns and notes

- **Behaviour 2, UI detail:** after any `stop()`, **Finish** and **Abandon…** stay
  enabled in the DOM, because `stop()` does not call `refresh()`. Only the CSS rule
  `body.stopped .bar { pointer-events: none }` blocks pointer input. Keyboard
  activation still raises Finish's confirm dialog and then drops the action silently.
  Nothing is sent. This is a minor follow-up candidate: disable the bar in `stop()`.
- **Behaviour 1, by design:** after a heartbeat 400 the page shows no banner and stays
  interactive. The operator learns the window closed only on their next action (a 409).
- **Behaviour 2 method:** the exception came from rewriting the response, not from a
  patched page function, because label.js is an IIFE with nothing to patch. The server
  side of the write was real.
- **Behaviour 3b** needed a second labeller configured with an `https://` base URL and
  reached over `http://`. On a single, correctly configured server the page cannot
  produce an Origin-only mismatch, because a wrong host fails the GET first.
- **Behaviour 4** used the precision page. A D1 400 on a *blind* write is not reachable
  with two tabs: closing a window makes later writes 409, and D1a keeps a loaded page
  from being pre-empted.
- **Tooling:** the Playwright MCP `browser_network_request` call (full request details)
  hung for 1800 s and was aborted. Response bodies were read with listeners in
  `browser_run_code_unsafe` instead.

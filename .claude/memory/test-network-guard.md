---
name: test-network-guard
description: "The suite blocks outbound sockets and FAILS any test that caused a blocked call, swallowed or not — what trips it, how to stub, and the marker for tests that trip it on purpose"
metadata: 
  node_type: memory
  type: project
  originSessionId: 09d3ac98-bf2b-4ca7-8fdc-394dccdc1bc1
  modified: 2026-09-04T13:04:21.987Z
---

Built 2026-09-04 (`88bad9c`, news-brief-0q0.13). `tests/conftest.py` now guards the network at
two layers and, crucially, **reports** rather than merely raising.

**Coverage.** The old block sat on `requests.sessions.Session.request` — every requests entry
point funnels there, and nothing else does. `feedparser` fetches through `urllib`, and so do
paths in `brief.py` and `backtest/`, so "the suite cannot reach the network" was true of one
library and simply false for the rest. The guard is now on `socket.socket.connect`, which all of
them reach eventually. **Loopback stays open** — the test Postgres is TCP on localhost, and
blocking it would turn the entire DB layer into a skip, which is a green suite that has stopped
looking. AF_UNIX addresses (non-tuple) are local by construction and pass.

**Silence was the real bug.** Raising is not reporting: `telegram_alert` wraps its send in
`except Exception`, so a blocked call left no trace and its test passed. Both layers now append
to `conftest.BLOCKED_ATTEMPTS`, and an autouse fixture **fails the test at teardown** if it
caused one. `BlockedNetwork` is a distinct exception type so a test can assert on it without
matching message text.

**If you see `Failed: this test reached for the network: …`** — that is this guard, and it is
almost always a missing stub, not a bad test. Stub at the seam, returning **what the swallowed
failure already returned** (`None` for a fetch, `None` for a login, nothing for an alert), so the
behaviour under test is unchanged and only the outbound call disappears. Do NOT reach for the
marker to make it quiet.

**`@pytest.mark.allow_blocked_network`** is only for tests whose POINT is tripping the block —
`tests/test_network_block.py` carries it file-wide, since a control that cannot fire is a claim,
not a control.

## What it found the moment it was switched on

**Nine tests were reaching the real internet and passing**: api.telegram.org ×5
(`test_supervisor`, `test_job_interlock` ×3, `test_delivery_and_state`), polygram.ink ×2
(`test_prediction`), Yahoo ×2 (`test_trading`). Eight were missing stubs. Two hid behind known
traps that this repo had already written down and which habit-based countermeasures had not
stopped — see [[newsbrief-flag-access-module-attr]] (a `from`-import freeze) and
[[telegram-send-long-convention]] (a third send channel).

**One was a genuine defect, not a stub gap.** `test_handle_update_ignores_foreign_chat` called
`_handle_telegram_update`, whose own docstring says *"Authorization is NOT done here"* — the gate
lives in `_handle_update`. So it never exercised the gate at all; `/reset` ran for a foreign
chat, and the test passed only because `/reset` answers through an unpatched channel. **The
lesson generalises past the network:** an assertion that *nothing happened* is worth exactly as
much as the narrowest channel it watches, and it will pass for free if it is pointed at the wrong
function. Every absence assertion needs a presence sibling — the same rule as
[[tests-asserting-less-than-their-name]], which is the dominant defect class in this repo.

## Writing a test for a teardown verdict

The check itself is tested by generating a one-test file and running `pytest` on it in a
**subprocess**, because the thing under test is a verdict issued after the test body finishes —
asserting on the mechanism in-process proves the check works while saying nothing about whether
it runs. It has a positive control (a probe that touches nothing must exit 0), or a broken
harness — wrong path, import error — returns non-zero for unrelated reasons and the real test
passes while proving nothing.

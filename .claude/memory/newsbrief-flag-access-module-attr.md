---
name: newsbrief-flag-access-module-attr
description: "Live-toggleable common.py flags must be read as common.X (module attr), never via `from common import X` — a from-copy freezes at import and defeats host env toggles + test monkeypatch"
metadata: 
  node_type: memory
  type: project
  modified: 2026-08-05T22:30:09.529Z
  originSessionId: 7b3fb708-5d48-4f6f-860d-386eaf7c243b
---

In news-brief, any **live-toggleable** config/kill-switch flag defined in `common.py` (e.g. `PG_LIVE_ENABLED`, `PG_A_ENABLED`, `PG_B_ENABLED`, the `PG_*_CAP` caps) must be read at the call site as a **module attribute** — `common.PG_LIVE_ENABLED` — NOT pulled in via `from common import PG_LIVE_ENABLED`.

**Why:** `from common import X` binds a *copy* of the value at import time. A bool/float copied that way can never change afterward — so (a) a host env var flipped between runs won't be seen, and (b) `monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)` in tests won't reach the copy. Reading `common.X` does a fresh attribute lookup every call, so host toggles and monkeypatches both take. This is why the Sleeve-B `/predict` wizard required adding `import common` to `brief.py` (2026-07-22, commit ce6acf1) — `brief.py` had only `from common import (...)` for functions and would have frozen the kill-switch flags. Sleeve A's `trading.py` already reads `common.PG_*` this way; `trading._sleeve_b_open_ok` reads `common.PG_B_POS_CAP`/`common.PG_B_TOTAL_CAP` for the same reason.

**The same trap applies to FUNCTIONS in tests, not just flags.** `polygram_live.py` does `from trading import polygram_login`, so `monkeypatch.setattr(trading, "polygram_login", …)` does not divert `polygram_live._pg_request` — it still calls the frozen copy. On 2026-08-05 that let a pgdiag test make a **real HTTP request to polygram.ink** (visible only as a stray `401 Unauthorized` in the captured log while the test otherwise passed). Stub the function on the module that *calls* it (`polygram_live`), or stub the wrapper that reaches the network (`polygram_live.list_positions`). Scan new tests' captured logs for real hostnames — a passing test that talks to production is silent otherwise.

**How to apply:** when adding a new module (or a first flag-read to an existing module like `brief.py`) that gates on a `common.py` flag, `import common` and reference `common.FLAG`. Functions/constants that never change at runtime (helpers, `telegram_send`, `MODEL` snapshot) are fine to `from common import`. If a test's `monkeypatch.setattr(common, "FLAG", …)` mysteriously has no effect, or a host env toggle doesn't take, suspect a frozen `from`-copy first. Relates to [[polygram-live-trading-spec]], [[newsbrief-model-config]] (host single-knob override), and [[tdd-plan-fixtures-drift-from-contracts]] (this was one of the Sleeve-B fixups).

## 2026-09-02 — the rule got STRONGER, and it fired again in a package `__init__`

Since Epic 7 phase 2 the knobs are `settings` ROWS, resolved by a module `__getattr__` on
`common` (PEP 562). So `common.X` is no longer merely the way to see a host toggle — it is the
only way the value is fetched at all, and a knob is deliberately **absent as a module constant**
so that `__getattr__` fires. See [[newsbrief-runtime-foundation-phase-1]].

**It fired again, in `enrichment/__init__.py`:** `from .config import ENRICHMENT_ENABLED as _ENABLED`,
with `is_enabled()` returning that copy. Frozen at import since the subsystem shipped; no host
toggle could ever have moved it. Fixed to a call-time read. **Look at package `__init__.py`
re-exports specifically** — they read as tidy API surface rather than as a config read, which is
why this one survived several passes over the same subsystem.

**The forwarding pattern, if you add another module of knobs.** `enrichment/config.py` keeps its
~10 `config.ENRICHMENT_X` call sites unchanged by forwarding through its OWN `__getattr__` to
`common`, against an explicit `_FORWARDED` set (not "anything in `common.KNOBS`", or
`config.PG_A_ENABLED` would stop being a typo). Two traps that come with it:

- **`monkeypatch.setattr` LEAKS through a `__getattr__` seam.** Its undo restores the resolved
  value as a REAL attribute, which then shadows `__getattr__` for the rest of the process and
  quietly freezes that knob for every later test. `tests/conftest.py` sweeps the leak at SETUP
  (not teardown — monkeypatch tears down after the fixture, so a teardown sweep is undone
  immediately by the very thing it fixes). **Any new forwarding module must be added to that
  sweep**, or the symptom is a test that passes alone and fails in suite order.
- `enrichment/config.py` importing `common` rather than the top-level `config.py` also sidesteps
  the name collision between the two `config` modules.

## 2026-09-04 — the `polygram_login` case recurred, and is now caught mechanically

The exact trap this memory named in 2026-08-05 fired again: `test_mode_paper_shares_one_matcher
_pass` stubbed `trading.polygram_login`, and `polygram_live.py:13` binds it with
`from trading import polygram_login`, so the live path (reached because the test sets
`PG_LIVE_ENABLED=True`) called the real one and POSTed to polygram.ink. Same symptom as before:
the test PASSED, with only a `WARNING PolyGram login failed` in the captured log.

**The advice "scan new tests' captured logs for real hostnames" did not work** — nobody scans
logs of passing tests, twice over. It is now enforced instead: `tests/conftest.py` records every
blocked call and **fails the test at teardown**, swallowed or not. See [[test-network-guard]].
That is the general lesson: a from-import freeze is invisible by construction, so the
countermeasure has to be a mechanism, not a habit.

**The same shape, on the Telegram side, in the same session:** `_capture_sends` in
`tests/test_delivery_and_state.py` patched only `brief.telegram_send`, leaving
`telegram_send_buttons` and the `common` namespace open — so a test asserting *nothing was sent*
passed while the message reached api.telegram.org. When writing a "nothing was sent" assertion,
enumerate every channel the handler can speak through first; see
[[telegram-send-long-convention]].

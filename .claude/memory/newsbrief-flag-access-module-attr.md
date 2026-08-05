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

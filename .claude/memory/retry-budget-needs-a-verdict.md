---
name: retry-budget-needs-a-verdict
description: "A counter that permanently retires work may only be charged by failures that obtained a verdict about that work — an outage charged to the item retires data nobody ever judged, and the obvious fix overshoots into an infinite retry"
metadata: 
  node_type: memory
  type: project
  originSessionId: e6d626fb-1862-4525-a333-1ae4323e8d37
  modified: 2026-09-08T06:15:20.904Z
---

**A counter that permanently retires a unit of work may only be charged by failures that actually
obtained a verdict about it.** An infrastructure fault is a statement about the host, never about
the work.

**Why:** measured 2026-09-08 in news-brief. `item_triage.integrate_attempts >= 3` is a one-way
door — no prompt-version bump brings a retired item back. A ~90s host DNS fault (it took out RSS
fetches, Telegram `getUpdates` and the Anthropic API simultaneously) failed three whole
integration batches, and the bare `except Exception` charged +1 to every item in them: ~15 items
spent a third of their lifetime budget on a fault that never let a model see them. Three unrelated
outages and an item is gone for good.

**The loss is not random, which is what makes it a measurement problem rather than an annoyance.**
The select is `ORDER BY i.id LIMIT n`, so failures always land on the *front* of the queue — the
oldest corpus. Whatever reads the surviving rows later sees a biased sample and has no way to tell
infrastructure from content.

**The obvious fix overshoots.** "Do not charge on failure" turns a permanently-malformed request
into an unbounded retry: a 4xx will be refused identically next hour, so the batch re-pays an
expensive generation every pass and nothing ever retires it — quieter than the bug it replaces,
and therefore worse. The discriminator is not *was it an error* but **did anything judge the
work**.

**How to apply:**

1. For every failure path that increments a permanent counter, ask: did this obtain a verdict
   about the unit? If not, defer — leave the counter alone and let the next pass re-select it.
2. With `requests`: `getattr(exc, "response", None) is None` separates DNS/connect/timeout from
   HTTP status errors; on top of that, **429 and 5xx are the server declining to answer**, which
   is likewise not a verdict. Everything else, including non-`RequestException`, charges.
3. **Count deferrals in their own field**, never folded into the failure counter. Folding them
   misattributes a host fault as poor output quality — the same confound as
   [[fail-closed-needs-status-not-count]], and in this project it was exactly what made a P3
   "follow-up" turn out to be a gate-validity bug.
4. Assert the property on the **database column**, not the tally: the column is the thing that is
   one-way. Then write a second test for the property the first cannot see — that a deferred unit
   is genuinely re-offered rather than quietly marked done.
5. **A one-way counter with no alerting is a stop-loss you have not built.** Filed as
   `news-brief-bqa.15`; the only signal today is a cumulative number inside an hourly log line.

Related: [[shared-helper-carries-first-callers-tuning]] (retry layers MULTIPLY — 3 item-level × 2
HTTP; put the retry where budget accounting lives), [[newsbrief-comprehension-pipeline]].

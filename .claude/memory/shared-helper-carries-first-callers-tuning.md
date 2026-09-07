---
name: shared-helper-carries-first-callers-tuning
description: "A shared helper's constants encode the FIRST caller's justification, and retry layers MULTIPLY — count them before adding one; the safe fix is keyword params defaulting to the original plus a positive-control test"
metadata: 
  node_type: memory
  type: project
  originSessionId: 884baeb1-9c4a-46f6-9caa-16b4bddd7912
  modified: 2026-09-07T12:57:56.788Z
---

Two failures with one root, both found in `news-brief-wvt` (2026-09-07).

**1. A shared helper's tuned constants are a property of its first caller, not of the helper.**
`brief._post_messages` hardcoded `SIGNALS_TIMEOUT = 90`, and the comment beside it justified the
value: *"generous: extraction runs AFTER delivery, so latency is free"*. True of `extract_signals`.
False of `comprehend`, which reuses the same function inside an hourly job against its own
`DEADLINE_SECONDS` and generates at `INTEGRATE_MAX_TOKENS = 8192` — four times the budget. The
second caller silently inherited a justification that did not transfer, and `comprehend`'s own
docstring even *praised* the inheritance ("already carries the retry and the generous timeout").

**Tell:** a constant whose comment explains *why this value is safe here*. That comment is scoped to
one caller. When a second caller appears, the reasoning must be re-derived, never inherited.

**2. Retry layers MULTIPLY, and nobody notices because each layer is locally reasonable.**
`comprehend.py` selects on `integrate_attempts < 3` and increments on failure, so a bad item is
already retried across hourly passes. Underneath it `_post_messages` retried twice. **3 × 2 = six
charged 8192-token generations for one persistently-bad item** — and the inner layer is the one
spending the pass's wall-clock deadline, so raising the timeout without touching attempts makes
throughput worse quadratically.

**How to apply:**
- **Before adding a retry, ask what already retries.** Put the retry at the layer that has *budget
  accounting* — here, the item-level ceiling, which persists across runs and is visible in a table.
  The HTTP layer had neither. Integration now makes exactly ONE HTTP attempt.
- **Widen a shared function with keyword params defaulting to the incumbent's values**, so the
  original caller is unchanged *by construction* rather than by inspection — then pin it with a
  **positive-control test** that asserts the old caller still gets the old numbers. Those two tests
  passed at RED and still pass at GREEN; that is exactly their job.
- **Make the new value a settings knob, not a constant**, when you are guessing it. Both timeouts
  ship as `COMPREHEND_INTEGRATE_TIMEOUT` (300) and `COMPREHEND_TRIAGE_TIMEOUT` (90) and a
  `_timed_post` wrapper logs each call's real elapsed seconds, so the host retunes from measurement
  instead of from a token-rate estimate. See [[env-var-needs-compose-passthrough]] — a knob is
  invisible in the container until the compose **anchor** declares it.
- **Argue the direction from asymmetry.** Too short silently destroys evidence *and* misattributes
  it as a model failure; too long only spends wall-clock the tally reports. So start generous,
  measure, tighten — never the reverse.

Fourth instance of the timeout-shaped bug here: [[signals-extraction-separate-call-followup]]
(timeout=30 copied from a Haiku call wiped a day's signals) and
[[signals-parse-error-is-truncation]] are the same family. Context:
[[newsbrief-comprehension-pipeline]].

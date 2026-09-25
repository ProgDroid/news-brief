---
name: snapshot-config-per-unit-of-work
description: "A setting read through a TTL cache at every use can change in the middle of one pass — resolve it ONCE per unit of work and thread it through; found only by reading the whole function, never by a per-task review"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 9ddd2146-00b2-42bb-a88a-0f15d3efd634
  modified: 2026-09-25T22:37:37.364Z
---

`comprehend.run()` checked that the model had a price, then called `_triage_model()` again at
nine other sites over a pass lasting up to 40 minutes. The settings cache TTL is 60 s. A settings-row
edit mid-pass (the whole point of rows is that they need no redeploy) therefore sent later calls to
a model that had never been checked. The final reviewer's probe switched to an unpriced model on
the second call: **0 ledger rows, a $0 debit, 3 items marked `failed`, and no abort.** The
`UnpricedModel` was raised by `record_spend` AFTER the paid call, and the generic `except` treated
it as a batch failure. Even a swap between two priced models would have priced the ledger row at the
wrong rate.

**Why:** every per-task review read a diff where `_triage_model()` was a harmless one-line helper.
Only the final whole-branch review read `run()` top to bottom as ONE function, and only that
showed the same value being fetched repeatedly across a long-lived unit of work.

**How to apply:**
- Anything read from `common.X` / `config.knob` that a pass USES AS AN INVARIANT (a model id, a price,
  a limit that sizes the work) is resolved once at the top of the unit of work and passed down.
  Use keyword args defaulting to the resolver, so direct callers keep working.
- Test it by changing the setting INSIDE the fake call, then asserting that the ledger or output
  carries the value the pass started with.
- This is not a rule against live knobs. A kill switch SHOULD be re-read. The question is whether
  a mid-pass change would break a check that has already been passed.
- Same family as [[newsbrief-flag-access-module-attr]]. That memory is about the opposite failure (a
  `from`-import freezing a value FOREVER). The right lifetime is one unit of work.

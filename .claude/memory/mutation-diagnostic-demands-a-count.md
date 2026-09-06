---
name: mutation-diagnostic-demands-a-count
description: "Require a subagent to break the code and report HOW MANY tests failed, not whether one did — it audits the tests, and it caught the controller's own mistakes four times"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 19b68250-ca61-4936-834b-18d6a595831c
  modified: 2026-09-05T19:22:43.932Z
---

When a fix round adds a test, require the worker to **break the code deliberately and report the
number of tests that failed**, not whether any did. Name the mutation precisely (which line, to
what), and ask which tests moved.

**Why:** a test written to catch a specific regression should be *shown* catching it. Asking
"did it fail?" invites a yes. Asking "how many, and which?" turns the diagnostic into an audit of
the tests themselves, and it repeatedly audited *me*:

- Four tests added for a `bool`-is-`int` guard, four sites fixed — **3 of 4 failed** on the
  reverted code. The fourth was mine and vacuous: it fed rows `{"id": True}` then `{"id": 1}` and
  asserted `== {1: True}`, but `hash(True) == hash(1)`, so both rows addressed the same dict slot
  and the second write always overwrote the first. My test for the collision was defeated *by* the
  collision. "It failed" would have hidden this completely.
- Two mutations in one round failed **zero** tests — two fixes that no test could distinguish from
  their absence. Fixes on trust, shipped green.
- A mutation failing *more* tests than expected is also a finding: the test measures something
  broader than its name. Ask for the extras to be named.

**How to apply:** name the mutation exactly ("change `return None` to `continue` in the entity
loop, keep the trailing empty-list check"), state which single test must fail, and require the
count plus any others. Run it **fail-first** where possible — against the broken code *before*
applying the fix; run after and it is reassurance, not evidence.

**Two traps in the instruction itself, both mine.** `git checkout <path>` restores the *fix* once
it is committed, so a revert diagnostic must name the commit to revert **to**. And a revert is the
wrong probe when the behaviour is original and correct — there, name a mutation instead.

Sibling technique to [[tests-asserting-less-than-their-name]]; the substitution form (swap in a
plausible *wrong* implementation) found the two worst defects in the comprehension build, both
invisible to three reading passes each. See [[newsbrief-comprehension-pipeline]].

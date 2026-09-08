---
name: mutation-diagnostic-demands-a-count
description: "Require a subagent to break the code and report HOW MANY tests failed, not whether one did — it audits the tests, and it caught the controller's own mistakes four times"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 19b68250-ca61-4936-834b-18d6a595831c
  modified: 2026-09-08T06:16:36.006Z
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
- **Failing FEWER than expected, while every mutation is still caught, is the subtlest finding.**
  2026-09-08, five mutations caught by 3/3/10/1/2 tests: I predicted 4 for one and got 3. A
  "deferred item is re-offered" test still passed under a mutation that charged the item, because
  the ceiling is 3 and a once-charged item is *still* re-selected. Neither test was redundant —
  one guards the charge, the other guards that the deferral path does not mark the item done — but
  a pass/fail check would have said "caught" and taught nothing. **Pre-register each count before
  running, and read every disagreement as information about which test does which work.**

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

## 2026-09-08 — two refinements to the pre-registered count, one of them a CORRECTION

Measured while shipping `news-brief-5fc` and the `b42.2` instrument, doing the mutations myself
rather than through a worker. Both are about the arithmetic of the prediction, which is the part
that decides whether a disagreement teaches anything.

**1. Subtract the silence-assertions.** Predicting "gut the detector, all 12 new tests fail" is
wrong by construction: a test asserting that nothing is reported is satisfied by an implementation
that reports nothing, ever. The honest prediction was 11 of 12, and 11 is what came back. So
before writing the number down, count the tests whose assertion an EMPTY or INERT implementation
already passes, and exclude them — then make sure each of those has a presence-sibling holding it
up, because they are exactly the tests that carry no weight under this mutation.

**2. When the count OVERSHOOTS, first check the mutation is the one you named.** The entry above
says a mutation failing more tests than expected means "the test measures something broader than
its name". That is one explanation and I reached for it first; it was wrong. Predicted 1 failure
for "empty window returns 0.0 instead of None", got 3 — because the mutation was applied with
`sed 's/^        return None$/        return 0.0/'` and that pattern matched the same line in
THREE functions. The tests were fine and each of the three `None` returns was independently held
by one test, which is a stronger result than intended but not the one I predicted.

**How to apply:** a regex-applied mutation must be verified by what it CHANGED, not by what it was
meant to change — `grep -n` the pattern first, or read the diff. An anchored pattern that looks
specific (`^        return None$`) is specific to an INDENTATION LEVEL, not to a function. Only
once the mutation is confirmed to be the named one does a surprising count license the
"broader than its name" reading.

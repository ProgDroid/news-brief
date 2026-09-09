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

## 2026-09-08 — COUNT THE LIST, DO NOT DERIVE THE NUMBER

Three pre-registrations lost in one session, all the same way, none of them about the code:

- "13 new tests" — arithmetic from a total minus a remembered baseline. It was 12.
- "10 in the store file plus 2 wrappers" — I had written 9 plus 2. It was 11.
- "3 scripts, 1 fails" — `scripts/` held 7, so the control was six-wide, not two.

Every one was a prediction about MY OWN artifact, derived by subtraction or memory rather than
read off the thing itself. The tests were right each time; the number was not. That is the
[[the-prediction-had-a-generator]] shape at its smallest — the generator was mental arithmetic,
and correcting the figure afterwards fixes nothing, because the next estimate is made the same way.

**How to apply:** before writing a count down, run `grep -c "^def test"` or list the names. A
pre-registration is only worth something if a disagreement is informative, and a prediction with a
sloppy generator turns every disagreement into "recount", which is exactly the noise the technique
exists to remove.

**The countermeasure that DID work, same session:** assert the mutation anchor matches exactly
once inside the patch script (`assert s.count(old) == 1`). Four mutations, four predictions of
"exactly one failing test", four hits — and unlike the earlier sed that silently hit three
functions, a wrong anchor now refuses to apply instead of quietly widening the experiment.

## An OVERSHOOT usually means you broke the code, not that the tests are strong (2026-09-09)

Two mutations reported **22** and **10** failing tests. That reads as excellent coverage and proved
nothing: both had deleted a `%s` placeholder while the params tuple still supplied it, so every
query raised before reaching an assertion. **A mutation that breaks the STATEMENT tests nothing
about its MEANING.**

- **A mutation must leave the code runnable.** Preserve arity, placeholder count, and types; change
  only the semantics. Rewritten to keep the placeholders in place, the same two scored 2 and 1.
- **Check the failure MODE, not just the count.** Assertion failures are signal; import errors,
  `TypeError`, and database exceptions mean the experiment did not run. A count without that check
  is `pipe-eats-the-exit-code` wearing different clothes.
- **A big number is a smell.** If a one-line change fails a fifth of the suite, suspect the harness
  before congratulating the tests.

**Predicting ZERO in advance is legitimate and worth doing.** One guard was short-circuit only —
`CROSS JOIN unnest('{}')` returns no rows with or without it — so the mutation could not fail
anything. Saying so before running turned a would-be embarrassment into a stated limit, and the
test still earns its place against a *future* fallback, which is the risk the docstring names.

**An UNDERSHOOT is the interesting one.** A prediction of 3 that scored 2 found a vacuous test
([[tests-asserting-less-than-their-name]]); the survivor was the headline assertion, passing with
the feature removed. **When fewer tests fail than predicted, suspect the tests, not the
prediction.** When more fail, suspect the mutation.


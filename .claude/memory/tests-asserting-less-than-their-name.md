---
name: tests-asserting-less-than-their-name
description: SEVEN tests in one run passed while asserting less than their names claimed — the dominant defect class here. None was found by a failing test; the technique that finds them is mentally deleting the code and asking whether the test would fail.
metadata:
  node_type: memory
  type: project
  originSessionId: c7b3e1ab-57c0-4dbc-8e99-58bd09ddc7f3
  modified: 2026-09-03T15:49:40.588Z
---

**The b42.1 capture run (2026-09-02/03) found SEVEN tests that passed while asserting materially less than their names and docstrings claimed.** Not one was found by a test failing. This is the dominant defect class in this repo, ahead of logic errors by a wide margin.

**The seven, with the mechanism each time:**

1. `test_the_rollback_assertion_can_actually_fail` (`test_kb_schema.py`) — a NEGATIVE CONTROL. `_copy_migrations` globs `*.sql`, so adding 0008 meant `steps=2` reverted 0008+0007 and never reached 0006's stripped down file. "Function survives" passed because 0006 was never rolled back. Second recurrence; see `news-brief-5db`.
2. The spec's disabled-capture test — `job_runs` row + exit 0 + no items, which is byte-identical to an ENABLED run on a day every feed 403s.
3. The roll-off positive test — backdating only the sighting left it behind its own poll, so the `EXISTS` clause was satisfied by that poll alone. **It would have passed with the third pass deleted entirely.**
4. The roll-off negative control — same fixture bug, would have failed outright.
5. `fetch_rss`'s characterization test — docstring claimed "pins byte-for-byte", asserted substrings. Would miss a date-format or spacing change, and it was the SOLE guard on "the brief's output is unchanged".
6. `test_an_entry_with_no_title_is_skipped_not_fatal` — a Python guard filtered the bad entry BEFORE any SQL, so the savepoint it was named for was never exercised. Deleting `with conn.transaction():` would not have failed it.
7. `test_a_sighting_is_created_then_advanced` — asserted against a value it had just backdated, hiding that both `record_sightings` calls ran in ONE transaction where `now()` is frozen and the timestamp cannot advance.

**Why:** each test was written to describe a PROPERTY ("output unchanged", "no roll-off", "rollback can fail", "the transaction survives") and implemented as the easiest observable PROXY for it. Substrings are easier than equality; "returns empty" is easier than "returns empty for the right reason". The proxy passes, so nothing objects. Three of the seven were in code I wrote myself and re-read several times.

**How to apply:**

- **The technique that works: mentally delete the code and ask whether the test still passes.** Hand mutation testing, ~30 seconds per test, and it caught every one of these. Reviewers asked "would this fail if the code were wrong?" found them; reviewers asked "is this correct?" did not.
- **Any test asserting ABSENCE needs a sibling proving the probe can produce PRESENCE.** #3 and #4 were only caught because a third test (`test_no_later_poll_means_nothing_has_rolled_off`) exists purely to prove the other two are not vacuous.
- **Prefer an independently-derived expected value over a hand-written one.** #5's fix extracted the expected string from the PRE-refactor commit and asserted equality — a value that cannot be quietly reshaped to match what the new code happens to emit.
- **Watch for defence in depth hiding the inner layer** (#6): an outer guard that prevents the inner mechanism from ever running means the inner one is untested and will rot. If you keep both, one test must bypass the outer deliberately.
- **When a signature changes under existing tests, widen assertions, never loosen them.** `(1, 0)` becomes `(1, 0, 0)`; quietly relaxing to `result[0] == 1` also goes green and silently drops coverage. Ask reviewers specifically which happened — the test count looks identical either way.
- **A test that breaks LOUDLY when the world moves is self-maintaining; one that breaks silently is worse than absent.** `tests/test_scheduler.py:259` parametrizes over `scheduler.SCHEDULES`, so adding a schedule extends coverage automatically and can never go stale — that is the working template `news-brief-5db` asks the migration tests to copy.

Related: [[tdd-plan-fixtures-drift-from-contracts]] (fixtures using fields the real function lacks — adjacent but distinct), [[subagent-review-stalls]] (dispatch practices), [[newsbrief-capture-feature]].

## 2026-09-09: THREE more in one session, and all three had the same shape

Every one was written by me, in the same session, minutes after writing the previous one. None was
found by reading. All three were found by mutation, immediately.

**The shape: a FALLBACK ordering that happened to agree with the correct answer.**

- A ranking test put the correct event at `pool[0]`, and the tie-break was original position. An
  arm that scored the empty string still returned the right id. Green.
- A second gave both events the same `days_ago`, so reverting the ranking to pure recency still
  passed via the `e.id DESC` tie-break. Green.
- A third asserted a duplicate was buried by the cap, but every candidate scored near zero, so the
  ordering was noise and the duplicate survived by luck — a FLAKY test that happened to be passing.

**The rule that prevents all three: when a test asserts an ordering, arrange the fixture so that
EVERY fallback ordering points at the WRONG answer.** Make the correct answer the oldest, or the
first inserted, or the lowest id — whatever the tie-breaks favour, give it to the decoy. If the
test can pass with the ranking deleted, it is testing the fixture.

**Fourth, distinct shape, same session: a test satisfied by a different predicate than the one it
names.** A window-anchor test asserted the empty case using a one-day-old event, which a separate
`created_at` filter excluded on its own. The anchor was never exercised. **Make the fixture
straddle the boundary of the ONE predicate under test**, and prove it by mutating that predicate
alone. See [[reconstruction-drifts-from-production]].

**Reading review saturates.** Three careful passes over these tests found nothing; the mutation
run found each in seconds. Budget the mutation, not the re-read.


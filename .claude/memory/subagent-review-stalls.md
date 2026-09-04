---
name: subagent-review-stalls
description: "Subagent-driven development on this setup — reviewer stalls are RARE (24/24 subagents completed on 2026-09-02; 4/4 on 2026-07-19). Default to dispatching. Plus the dispatch practices that make SDD runs work here, including the ones that produced 9 tasks with zero fix rounds."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 16477d45-1807-43f8-9742-e61da567fe06
  modified: 2026-09-03T15:50:30.976Z
---

**Default to dispatching subagents.** Stalls are an occasional harness flake, not a property of
this setup. Evidence has only strengthened: **2026-09-02 (later session), a further 15 of 15
completed** across the ledger cutover — 6 implementers, 6 task reviewers, a final reviewer on
opus, a fixer and a scoped re-reviewer — again with zero fix rounds. Earlier that day, **24 of
24 completed** (9 implementers,
9 task reviewers, 4 red-team reviewers, 1 fixer, 1 re-reviewer — sonnet and opus both), every one
returning a file:line-cited report. 2026-07-19 was 4/4 reviewers + 9/9 implementers. The original
framing of this memory ("reviewers stall, always fall back to inline") was **too strong and was
actively costing us**, and the user corrected it explicitly on 2026-07-19: *"it happened once or
a couple of times, try them again at some point and see what happens."*

**How to apply:** dispatch. If one *does* hang — no completion notification in a reasonable
window, or the user says it isn't running — don't wait; review that task's diff inline instead.
Keep the per-task **base SHA** before dispatching each implementer so an inline diff is always one
command away. Inline review is a full-quality substitute when needed, never the default.

## Dispatch practices that work here

**The shell-split advice in the older version of this memory is STALE.** It said python/pytest/ruff
must go through the PowerShell tool because Bash errors "stdin is not a tty". On 2026-09-02 all
nine implementers ran `ruff format`, `ruff check` and `pytest -q` **directly from the Bash tool**
without trouble, with `py -m pytest` documented as the fallback if a guard denies a bare `python`.
What is still true and still must be stated in every dispatch: **commit from Bash, never
PowerShell** — PowerShell prepends a UTF-8 BOM to the commit subject. See
[[python-via-powershell]] for the current `py`-first guidance.

**Give absolute expected test counts, never deltas.** A plan that says "count = baseline + 14"
is unambiguous to its author and ambiguous to a fresh subagent, which reads "baseline" as *its own*
starting count rather than the pre-run figure. Two implementers reported a phantom arithmetic error
before this was switched to "the suite is at 1246 now and must be at 1251 when you are done".
Pre-registering the absolute number also makes "did the tests actually run" checkable — a test that
silently doesn't run looks identical to one that passes.

**Say `git commit -F <file>` or repeated `-m` flags in the dispatch, up front.** The first
implementer reached for `git commit -m "$(cat <<'EOF' ...)"` and `windows-guard.sh` denied it. That
firing was **correct** — command substitution in a commit message is exactly what the guard exists
to catch — so there is nothing to fix, but naming the right form up front saves a round trip in
every dispatch. (Contrast [[fix-the-tooling-dont-route-around-it]], which applies to *wrong*
firings.)

**Tell each implementer the exact expected-failure set, and make it a two-sided check where you
can.** Beyond "these N tests are expected to fail and belong to task X, don't touch them": the
strongest version states which tests must FAIL *and* which must PASS before the implementation
lands. On 2026-09-02 the trigger task pre-registered "two of these four must fail, two must already
pass — if all four pass, stop and report BLOCKED", and the result matched exactly, which is
positive evidence the tests were non-vacuous rather than merely green.

**Sequence a shared-fixture update immediately after the model change, not late.** A reshaped
dataclass makes old-shape fixtures *crash on load* (`SomeModel(**old_keys)` → TypeError), breaking
every test that loads them, not just stale assertions.

**Use the skill's `scripts/task-brief` and `scripts/review-package` file handoffs** so briefs and
diffs never enter the controller's context, and keep the `.superpowers/sdd/<plan>/progress.md`
ledger current — it survives compaction; conversation memory does not. Record every judgment call
as a `Ruling:` line; the skill requires collecting them at finish, and they are the only place
decisions taken on the user's behalf reach them.

## What produced zero fix rounds across nine tasks

2026-09-02: nine tasks, every one approved on its first review, no fix loop entered. Two causes
worth reproducing:

- **The plan carried the complete code.** Implementation was transcription plus verification, not
  design — so the cheapest capable model suffices and there is nothing for it to get creatively
  wrong. Reserve judgment-heavy dispatches for work the plan genuinely cannot specify.
- **The plan was reviewed by fresh context before execution, twice**, and the second pass
  *executed* the plan's new code against a real database rather than reading it. That pass found
  three blocking mechanical defects no amount of author re-reading had caught. See
  [[newsbrief-kb-schema-0006]] and `learnings/probe-failures.md` §21.

**When a change adds a member to an enumerated set, grep for the tests that PIN that set — and
name them in the brief.** Two of six tasks in the 2026-09-02 cutover needed necessary
out-of-brief expansion of exactly this shape: adding migration 0007 broke tests asserting
"0006 is the latest", and adding a boot importer broke tests asserting the set of importers.
Neither was anticipated by the plan, and both are legitimate scope rather than creep — the gate
cannot be green without them. Worse, one of the tests in the first group was a NEGATIVE CONTROL
that went vacuous rather than failing (`learnings/probe-failures.md` §23). Budget for this when
writing the plan, and tell the implementer that fixing such tests is in scope.

**The plan is prose ABOUT code and is not checked BY the tooling that checks code.** Three
defects in the 2026-09-02 plan were lint-level: `import` lines written inside "append this to
the test file" blocks (E402 is enabled here), a test header importing modules only later tasks
use (F401, failing the FIRST task's own gate), and an "expected: 8 failed, 4 passed" that was
impossible because a missing module is a *collection* error — 0 pass. The countermeasure that
worked was piping a probe through `ruff check` to see which rules actually fire, rather than
reasoning about ruff's defaults. Do that before writing code blocks into a plan.

## From the b42.1 run (2026-09-03) — 7 tasks, 5 fix rounds

**The E402-in-plan-blocks trap above was ALREADY WRITTEN DOWN and I walked into it anyway**, in a
worse form: a preflight ruling told Task 4 to declare every import the module would ever need, so
Tasks 5-6 would not append any. That traded a non-existent E402 for three real F401s and left the
gate RED on main. **E402 constrains an import's POSITION, not when it is added** — the move that
satisfies both rules is editing the file's TOP import block whenever you need one. Reading the
memory is not the same as applying it; when a plan block contains an import, run `ruff check` on a
probe of that exact shape.

**Tell the implementer to REPORT rather than resolve, wherever you have made a non-obvious call.**
Task 4's implementer hit those F401s, had been told deleting the imports would break Task 6, and
surfaced the contradiction instead of running `--fix`. Had it silently fixed them, the gate would
have gone green and Task 6 would have failed two tasks later with an error that looked unrelated.
One sentence in the dispatch converts a silent downstream failure into an immediate visible one.

**The final whole-branch review earns its cost — it finds what per-task review structurally
cannot.** Its best find: `fail-closed-needs-status-not-count` was applied THREE times at the
producer (a failure kind on `FeedFetch`, splitting `failed` out of `already_present`, an
`outlet_conflict` breakdown increment) and the caller then unpacked both counts and read neither.
Every task was individually correct; the property none of them delivered was end-to-end. "Does
this number ever arrive anywhere" belongs to no single task. Dispatch it on the most capable model
and point it at cross-task coherence explicitly.

**Verify one suite count yourself, at the end.** Every reviewer is (correctly) told not to re-run
the suite, so each takes the number from the implementer whose work it measures — a chain with no
independent link. One controller-run gate at the end closes all of it for ~100 seconds.

**A subagent can die mid-task from a session limit.** Recovery is cheap and the work is not lost:
its edits are in the working tree uncommitted, so check `git status`, read the diff to see how far
it got, run the gate yourself, and dispatch a fresh agent only for what remains. Do NOT re-dispatch
the whole task.

**Reviewers and implementers can run in parallel; two implementers cannot.** A reviewer never
writes, so overlapping it with the next task's implementer is safe and saves real wall-clock —
but hold a fix round if it touches a file the live implementer is also editing.

**An extracted brief is a FROZEN COPY.** `scripts/task-brief` snapshots the plan; changing the plan
afterwards does not update briefs already written. Three times in one run a correction had to be
propagated to already-extracted briefs by re-running the script — an interface change, an import
rule, and a stale expected-test-count. Any upstream correction must explicitly re-extract the
downstream briefs.

**"Idle" is not "finished".** The agent roster reports a lifecycle field, not whether work
happened; idle covers finished-and-reported, finished-silently and never-started. Chase a child
that shows idle without reporting rather than assuming either way.

**One thing to keep out of the implementer's scope:** closing the tracking issue. It belongs to the
controller after the final whole-branch review, not inside the last task — an implementer closing
it marks the work done while a review that can still find blocking defects has not run.

See [[newsbrief-commit-to-main]] (solo repo → commit straight to main during these runs) and
[[user-runs-concurrent-sessions]] (stage explicit paths, never `git add -A`).

---
name: subagent-review-stalls
description: Subagent-driven development on this setup — reviewer stalls are INTERMITTENT (not systematic; corrected 2026-07-19 after 4/4 subagent reviews succeeded); plus the dispatch practices that make SDD runs work here
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c950c1f3-0f76-4fd8-a64a-850bb83ada91
  modified: 2026-07-20T07:24:13.085Z
---

**CORRECTED 2026-07-19 (user, explicitly):** *"subagent reviewers don't always stall — it happened once or a couple of times, try them again at some point and see what happens."* The earlier framing of this memory ("reviewers stall, always fall back to inline") was **too strong and was actively costing us** — it made me default to inline reviews unprompted. On the 2026-07-19 Bigdata-enrichment run, **4/4 dispatched subagent reviewers completed normally** (3 task reviews on sonnet + 1 whole-branch review on opus), returning thorough, file:line-cited reports. Implementer subagents were 9/9 reliable too.

**How to apply:** **Default to dispatching subagent reviewers.** Stalls are a real but occasional harness flake, not a property of this setup. If one *does* hang (no completion notification in a reasonable window, or the user says it isn't running), don't wait — review that task's diff inline instead. Keep the per-task **base SHA** before dispatching each implementer so an inline diff is always one command away. Inline review is a full-quality substitute when needed, not the default.

**SDD dispatch practices that worked here (2026-07-19, 9 tasks, 9 clean commits):**
- **Pass the shell split into EVERY dispatch**, don't assume it's inherited: *git commits via the Bash tool* (PowerShell prepends a UTF-8 BOM to commit subjects — see global CLAUDE.md) and *python/pytest/ruff via the PowerShell tool* (Bash errors "stdin is not a tty"). Stated explicitly in all 9 dispatches → all 9 commit subjects verified BOM-free (`git log -1 --format=%s <sha> | head -c3 | xxd -p` must not be `efbbbf`).
- **Tell each implementer the exact expected-failure set.** During a cross-cutting type reshape the suite is legitimately red for a few tasks; without an explicit "these N tests are expected to fail and belong to task X, don't touch them" the implementer either chases them or "helpfully" fixes another task's file and collides.
- **Sequence a shared-fixture update immediately after the model change, not late.** A reshaped dataclass makes old-shape fixtures *crash on load* (`SomeModel(**old_keys)` → TypeError), which breaks every test that loads them, not just stale assertions. On 2026-07-19 I pulled the fixture task forward from #7 to right after #2 to clear that landmine — worth doing at plan-writing time.
- Use the skill's `scripts/task-brief` + `scripts/review-package` file handoffs so briefs/diffs never enter the controller's context; keep the `.superpowers/sdd/progress.md` ledger current (it survives compaction; conversation memory doesn't).

See [[newsbrief-commit-to-main]] (solo repo → commit straight to main during these runs) and [[bigdata-enable-spec-plan]] (the run this was corrected on).

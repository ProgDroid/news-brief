---
name: brief-py-sibling-prompt-strings
description: "brief.py holds 3 large sibling triple-quoted prompt blocks — \"edit the prompt\" is ambiguous and mis-targets; match on unique surrounding text"
metadata: 
  node_type: memory
  type: project
  originSessionId: 107fb616-3522-4f48-a810-fb5f074898aa
---

brief.py contains at least THREE big triple-quoted prompt blocks near each other: module-level `SYSTEM_PROMPT`, module-level `WEEKLY_SYSTEM_PROMPT`, and a large f-string built inside `build_daily_prompt`. An instruction like "add a line to the system prompt" is ambiguous about which block.

**Why:** 2026-06-25, a Task-4 implementer subagent (perspective/state-funded tagging) first landed its SYSTEM_PROMPT edit inside the `build_daily_prompt` f-string by mistake, reverted, and re-did it correctly. Self-corrected, no harm shipped — but it cost a round-trip, and a subagent editing by `old_string` can't see the whole file's structure to disambiguate. This recurs because prompt-block edits are frequent here (signals, brief-claim-memory, this feature all touched prompts).

**How to apply:** When editing a prompt, name the EXACT symbol (`SYSTEM_PROMPT` vs `build_daily_prompt`'s f-string vs `WEEKLY_SYSTEM_PROMPT`) in the task, and match the Edit `old_string` on text UNIQUE to that block (e.g. a distinctive sentence), not on generic prompt phrasing that appears in more than one. Em-dashes in these prompts are real U+2014 — edit via the Edit tool, never PowerShell Get-Content/Set-Content (mojibake). See [[formatter-owns-style]], [[signals-delimiter-fragility]].

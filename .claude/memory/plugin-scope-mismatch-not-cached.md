---
name: plugin-scope-mismatch-not-cached
description: "/doctor \"plugin not cached\" warnings = install scoped to home-dir project, not user; fix in installed_plugins.json"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 7aee93f1-f779-4f7f-990b-47d314744bb9
---

`/doctor` "Plugin X not cached at ...\marketplaces\<mp> — run /plugins to refresh" does NOT mean cache files are missing. It means the install record in `~/.claude/plugins/installed_plugins.json` has `"scope": "project"` + `"projectPath": "C:\\Users\\Nando Ferreira"` (the home dir as a project), while `enabledPlugins` in `~/.claude/settings.json` enables it globally (user scope). Running from another project (e.g. G:\pythonDev\news-brief) → "enabled everywhere, installed only for a different project" → flagged. Same reason `/plugin` uninstall fails with a scope error, and why `/plugin` update no-ops ("already latest" — version string unchanged).

Confirmed trigger is SCOPE, not commit drift: obsidian's recorded gitCommitSha equalled the marketplace HEAD yet was still flagged; user-scoped plugins (security-guidance, qmd) were never flagged.

Fix (keeps all plugins, durable — installed_plugins.json is NOT inside a cache plugin dir, so plugin updates don't revert it): for each flagged entry change `"scope": "project"` → `"scope": "user"` and delete the `"projectPath"` line, matching the known-good user-scoped entries. Leave `disabled` plugins (enabledPlugins:false) alone — /doctor doesn't check them. Back up installed_plugins.json first; CC rewrites/normalizes this file live (it pruned a duplicate rust-analyzer entry mid-edit), so re-read before each edit. Takes effect on CC restart. Done 2026-06-19 for 14 plugins. If a future `/plugin` install/update from the home dir re-adds a project-scoped entry, the warning returns — re-apply the flip.

Separate but same root machine quirk: this turn also hit the unquoted-`${CLAUDE_PLUGIN_ROOT}` path bug again (see global CLAUDE.md) in semgrep plugin's `hooks/hooks.json` UserPromptSubmit/Pre/Post/SessionStart commands, and the semgrep guardian MCP `.mcp.json` pointing at `hook.sh` (a bash script CC can't spawn on native Windows) — fixed by quoting the path and repointing the MCP command to `hook-windows-amd64.exe`. Both are cache-dir edits = stopgaps overwritten on plugin update.

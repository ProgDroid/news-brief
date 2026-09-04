---
name: python-via-powershell
description: "Use `py` to run Python from the Bash tool — plain `python` is winpty-wrapped and errors \"stdin is not a tty\"; a .ps1 via powershell.exe is the fallback when you need PowerShell itself"
promoted_to: global
metadata: 
  node_type: memory
  type: project
  originSessionId: ddcce554-d0f6-44e0-a600-c46053e944b5
  modified: 2026-09-01T15:57:38.143Z
---

Invoking `python ...` through the **Bash** tool fails on this machine with `Exit code 1 / stdin is not a tty` (seen for both `python -m py_compile` and `python script.py`). The **PowerShell** tool runs the identical command fine.

**Why:** The Bash tool wrapper allocates no tty; the Python launcher here trips on it. PowerShell doesn't.

**How to apply:** For any Python execution (compile checks, throwaway verification scripts, running the app), use the PowerShell tool. Heredoc-writing a temp file via Bash is fine — just run it with `python "$env:TEMP\file.py"` from PowerShell. See also [[brief-local-run]].

**CORRECTION (2026-09-01): use `py`, and note there may be NO PowerShell tool in the toolset.**

**First line of defence is `py`, straight from the Bash tool.** Verified 2026-09-01: `py -c "..."` and `py script.py` run fine under Bash and return the real exit code — the winpty wrapper is on the `python` alias, not on the `py` launcher, and `windows-guard.sh` does not block `py`. This is the cheap answer and it is what the guard's own denial message tells you to do. The vault records the same fact from the other direction — see `python-winpty-alias` in the aegyptvault-notes memory dir, which found it for `node` too.

When you specifically need **PowerShell** (env-var setup, `Set-Location`, PS-only cmdlets) and there is no PowerShell tool:

1. **Write the `.ps1` with the Write tool**, not with a Bash heredoc. `windows-guard.sh` matches the token `python` anywhere in a Bash command *including inside heredoc content*, so `cat > run.ps1 <<'EOF' ... python -m pytest ... EOF` is denied at composition time. The Write tool is not guarded.
2. Run it as `powershell.exe -NoProfile -File "<Windows-form path>" > "$S/out.log" 2>&1`, then `grep` the log. Put `Write-Output "REAL_EXIT=$LASTEXITCODE"` as the last line of the script — `$LASTEXITCODE` is the *script's* real status, and grepping for `REAL_EXIT=` satisfies the pipe-eats-the-exit-code rule.
3. Set env vars for the run inside the `.ps1` (`$env:FOO = 'bar'`), which sidesteps every bash→powershell quoting problem for values containing `/ @ % $`.

**Gotcha (2026-06-20):** when a Python program writes to **stderr** (e.g. the `logging` module — brief.py's `log.info` goes to stderr), running it via the PowerShell tool with `... 2>&1` makes PS wrap each stderr line as a `NativeCommandError` / `RemoteException` and append a scary `At line:1 char:N + ... ~~~~` block — **even on exit 0**. This is NOT a failure; the script ran fine. Don't infer an error from it — check the actual exit/output. To avoid the noise, drop `2>&1` (stderr is shown anyway) or route logs elsewhere.

**Gotcha (2026-08-29, path form when calling PowerShell FROM Bash):** `powershell.exe -NoProfile -Command "python '$SP/script.py'"` with a Unix-form `$SP` (`/c/Users/...`) does not fail cleanly — Python resolves it against the current drive and reports `can't open file 'G:\c\Users\...'`. Pass the **Windows form** (`C:\Users\...`) in any string handed to `powershell.exe`, and keep a separate Unix-form variable for the Bash-side `cat`/heredoc. Related: heredocs over ~50 lines through the Bash tool fail with a bogus `unexpected EOF` and write nothing — use the Write tool for those (the windows-guard hook warns).

**Gotcha (2026-06-20, two more false-failure signals — verify the artifact, not the exit code):** (1) piping a long-running Python print to a truncating consumer like `python x.py 2>&1 | Select-Object -First 6` returns **exit 255** (broken pipe — the consumer closes the stream early) **even when the script fully succeeded and wrote its output file**. Don't infer failure — Read the artifact (e.g. the written report) to confirm. Use `Select-Object -Last N` (drains the stream) when you need a tail without the broken pipe. (2) Running an operator script that lives in a subpackage (`python backtest/pilot/run_pilot.py`) fails `ModuleNotFoundError: No module named 'backtest'` because `sys.path[0]` becomes the script's dir, not the repo root; fix is `$env:PYTHONPATH='.'; python backtest/pilot/run_pilot.py` from repo root (or run as `-m`).

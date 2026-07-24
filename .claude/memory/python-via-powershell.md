---
name: python-via-powershell
description: "Run Python through the PowerShell tool, not the Bash tool — Bash errors \"stdin is not a tty\" on this Windows box"
metadata: 
  node_type: memory
  type: project
  originSessionId: ddcce554-d0f6-44e0-a600-c46053e944b5
---

Invoking `python ...` through the **Bash** tool fails on this machine with `Exit code 1 / stdin is not a tty` (seen for both `python -m py_compile` and `python script.py`). The **PowerShell** tool runs the identical command fine.

**Why:** The Bash tool wrapper allocates no tty; the Python launcher here trips on it. PowerShell doesn't.

**How to apply:** For any Python execution (compile checks, throwaway verification scripts, running the app), use the PowerShell tool. Heredoc-writing a temp file via Bash is fine — just run it with `python "$env:TEMP\file.py"` from PowerShell. See also [[brief-local-run]].

**Gotcha (2026-06-20):** when a Python program writes to **stderr** (e.g. the `logging` module — brief.py's `log.info` goes to stderr), running it via the PowerShell tool with `... 2>&1` makes PS wrap each stderr line as a `NativeCommandError` / `RemoteException` and append a scary `At line:1 char:N + ... ~~~~` block — **even on exit 0**. This is NOT a failure; the script ran fine. Don't infer an error from it — check the actual exit/output. To avoid the noise, drop `2>&1` (stderr is shown anyway) or route logs elsewhere.

**Gotcha (2026-06-20, two more false-failure signals — verify the artifact, not the exit code):** (1) piping a long-running Python print to a truncating consumer like `python x.py 2>&1 | Select-Object -First 6` returns **exit 255** (broken pipe — the consumer closes the stream early) **even when the script fully succeeded and wrote its output file**. Don't infer failure — Read the artifact (e.g. the written report) to confirm. Use `Select-Object -Last N` (drains the stream) when you need a tail without the broken pipe. (2) Running an operator script that lives in a subpackage (`python backtest/pilot/run_pilot.py`) fails `ModuleNotFoundError: No module named 'backtest'` because `sys.path[0]` becomes the script's dir, not the repo root; fix is `$env:PYTHONPATH='.'; python backtest/pilot/run_pilot.py` from repo root (or run as `-m`).

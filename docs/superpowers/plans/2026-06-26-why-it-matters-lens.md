# "Why it matters" lens on TOP STORIES — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sharpen the daily brief's "why it matters" by teaching an invisible Context/Stakes/Connection analytical lens, scoped to the TOP STORIES bullets, with the third beat re-pointed to market transmission.

**Architecture:** Two surgical prompt edits to `brief.py` — one paragraph appended to `SYSTEM_PROMPT` (the analytical standard, no visible labels in output), and two line edits inside `build_daily_prompt`'s OUTPUT FORMAT block (point the lens at TOP STORIES; add a one-clause dedup nudge to WATCH/FORWARD). No code logic, no new functions, no data or schema changes. Guarded by prompt-substring regression tests, the same way perspective-tagging was tested.

**Tech Stack:** Python 3, pytest, ruff (formatter owns `brief.py` style — run `ruff format` after edits; do not hand-align).

**Spec:** `docs/superpowers/specs/2026-06-26-why-it-matters-lens-design.md`

## Global Constraints

- Pure prompt change: no new functions, no data/schema/logic changes.
- The lens is **invisible** — no `Context` / `Stakes` / `Connection` labels appear in brief output.
- Third beat = **market transmission mechanism** (asset/rate/currency + channel), not human-interest "daily life".
- Brief stays under 600 words; lens must not encourage padding ("only where warranted, never a checklist").
- Pre-push gate (run before any push): `ruff check .` + `ruff format --check .` + `pytest`. Stage every file ruff reformats or CI fails. See `brief-local-run` memory.
- Commit via the **Bash tool, not PowerShell** (PowerShell prepends a UTF-8 BOM to the commit subject). Solo repo — commit straight to `main`, no feature branch.
- End every commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Teach the lens in `SYSTEM_PROMPT`

**Files:**
- Modify: `brief.py:1654-1672` (the `SYSTEM_PROMPT` triple-quoted string — append one paragraph at the end, after "...weigh it on its merits.")
- Test: `tests/test_signals.py` (alongside `test_system_prompt_is_forward_tilted`, ~line 209)

**Interfaces:**
- Consumes: nothing.
- Produces: an extended `brief.SYSTEM_PROMPT` string containing the lens language. No signature change.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signals.py` (near `test_system_prompt_is_forward_tilted`):

```python
def test_system_prompt_teaches_why_it_matters_lens():
    sp = brief.SYSTEM_PROMPT.lower()
    # the three beats are taught...
    assert "non-obvious" in sp  # Context beat
    assert "timeframe" in sp  # Stakes beat (next move + when)
    assert "transmission" in sp  # Connection beat re-pointed to markets
    # ...but NOT as visible output labels
    assert "context / stakes / connection" not in sp
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell tool — Python via PowerShell here; see `python-via-powershell` memory):
`python -m pytest tests/test_signals.py::test_system_prompt_teaches_why_it_matters_lens -v`
Expected: FAIL — `assert "non-obvious" in sp` (lens not yet added).

- [ ] **Step 3: Add the lens paragraph to `SYSTEM_PROMPT`**

In `brief.py`, the `SYSTEM_PROMPT` string currently ends:

```python
Some sources carry a perspective tag (the vantage they speak from) and/or a STATE-FUNDED flag in their section header. When a tagged source makes a claim, attribute its framing to that vantage rather than stating it as neutral fact (e.g. "Beijing's read, via SCMP, is..."). Treat agreement across opposing perspectives — or a state-funded outlet corroborating an independent wire — as a stronger signal; treat divergence as a flag worth surfacing. An untagged source carries no vantage claim; weigh it on its merits."""
```

Append one paragraph *before* the closing `"""` (blank line, then the new paragraph). The result:

```python
Some sources carry a perspective tag (the vantage they speak from) and/or a STATE-FUNDED flag in their section header. When a tagged source makes a claim, attribute its framing to that vantage rather than stating it as neutral fact (e.g. "Beijing's read, via SCMP, is..."). Treat agreement across opposing perspectives — or a state-funded outlet corroborating an independent wire — as a stronger signal; treat divergence as a flag worth surfacing. An untagged source carries no vantage claim; weigh it on its merits.

When a development is genuinely significant, sharpen the "why it matters" — woven into prose, never as labelled beats: surface (1) the non-obvious context the reader does not already know, (2) the likely next move and a rough timeframe, and (3) the market transmission — which asset, rate, or currency it moves and through what mechanism. Do not force all three onto an item that does not warrant them, and never pad to hit them."""
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_signals.py::test_system_prompt_teaches_why_it_matters_lens -v`
Expected: PASS.

- [ ] **Step 5: Run the existing SYSTEM_PROMPT guard to confirm no regression**

Run: `python -m pytest tests/test_signals.py::test_system_prompt_is_forward_tilted tests/test_commands.py::test_system_prompt_teaches_perspective_tags -v`
Expected: PASS (both — the append doesn't disturb existing assertions).

- [ ] **Step 6: Format and commit**

```bash
ruff format brief.py
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(brief): teach "why it matters" lens in SYSTEM_PROMPT

Context/Stakes/Connection analytical standard, woven into prose with no
visible labels; third beat re-pointed to market transmission. Backlog #5a.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Point the lens at TOP STORIES + dedup WATCH/FORWARD in `build_daily_prompt`

**Files:**
- Modify: `brief.py:1779-1801` (the OUTPUT FORMAT block inside `build_daily_prompt`'s returned f-string — the TOP STORIES bullet line and the WATCH/FORWARD bullet line)
- Test: `tests/test_signals.py` (alongside `test_prompt_has_fixed_spine_and_dynamic_instruction`, ~line 187)

**Interfaces:**
- Consumes: `brief.build_daily_prompt(**kwargs)` — existing signature, unchanged. Test helper `_daily_kwargs()` already exists in `tests/test_signals.py:169`.
- Produces: a built daily prompt whose TOP STORIES guidance carries the lens and whose WATCH/FORWARD guidance carries the dedup clause. No signature change.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signals.py` (near `test_prompt_has_fixed_spine_and_dynamic_instruction`):

```python
def test_top_stories_carry_why_it_matters_lens_and_watch_dedupes():
    out = brief.build_daily_prompt(**_daily_kwargs()).lower()
    # lens pointed at TOP STORIES (the "so what" in-line)
    assert "so what" in out
    assert "market channel" in out
    # WATCH/FORWARD gains the anti-repetition guard
    assert "not already covered in-line" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_signals.py::test_top_stories_carry_why_it_matters_lens_and_watch_dedupes -v`
Expected: FAIL — `assert "so what" in out` (lens not yet added to the f-string).

- [ ] **Step 3: Edit the TOP STORIES line**

In `brief.py`, inside the f-string returned by `build_daily_prompt`, the TOP STORIES block currently reads:

```python
<b>🌍 TOP STORIES</b>
- [3–5 bullets, only genuinely significant developments]
```

Replace the bullet line so the block reads:

```python
<b>🌍 TOP STORIES</b>
- [3–5 bullets, only genuinely significant developments. For each, answer the "so what"
  in tight prose: the non-obvious context the reader lacks, where it is heading and on
  what timeframe, and the market channel it moves through. Only where warranted — never
  a checklist, never padding.]
```

- [ ] **Step 4: Edit the WATCH / FORWARD line**

In the same f-string, the WATCH/FORWARD block currently reads:

```python
<b>👁 WATCH / FORWARD</b>
- [2–4 forward-looking things to monitor in the next 24–72h that could move markets]
```

Replace the bullet line so the block reads:

```python
<b>👁 WATCH / FORWARD</b>
- [2–4 forward-looking things to monitor in the next 24–72h that could move markets.
  Surface catalysts not already covered in-line above; do not repeat a forward-look you
  have already stated in a TOP STORIES bullet.]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_signals.py::test_top_stories_carry_why_it_matters_lens_and_watch_dedupes -v`
Expected: PASS.

- [ ] **Step 6: Run the existing spine guard to confirm no regression**

Run: `python -m pytest tests/test_signals.py::test_prompt_has_fixed_spine_and_dynamic_instruction -v`
Expected: PASS (TOP STORIES / MARKET PULSE / WATCH headings and the dynamic-middle instruction are untouched).

- [ ] **Step 7: Format and commit**

```bash
ruff format brief.py
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(brief): point why-it-matters lens at TOP STORIES + dedup WATCH/FORWARD

TOP STORIES bullets now answer the in-line "so what" (non-obvious context,
next move + timeframe, market channel); WATCH/FORWARD gains a one-clause
anti-repetition guard so forward-looks aren't stated twice. Backlog #5a.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Full gate + push

**Files:** none (verification only).

- [ ] **Step 1: Run the full pre-push gate**

Run (PowerShell tool):
```
ruff check .
ruff format --check .
python -m pytest
```
Expected: ruff clean (exit 0 both), full suite green (~440 tests; the two new tests included). If `ruff format --check` flags `brief.py`, run `ruff format brief.py`, `git add`, and amend the relevant commit — never hand-fix style (`formatter-owns-style` memory).

- [ ] **Step 2: Read the rendered prompt once, by eye**

Run: `python -c "import brief; print(brief.SYSTEM_PROMPT); print('---'); print(brief.build_daily_prompt(feed_content='x', web_content='x', chroma_context='x', yesterday_brief='', weekly_summary='', fb={'focus':[],'mute':[],'notes':[]}, portfolio=''))"`
Confirm by eye: the lens paragraph reads naturally, no stray `Context/Stakes/Connection` labels leaked into the OUTPUT FORMAT spine, TOP STORIES + WATCH/FORWARD edits render cleanly. (Sanity check only — no assertion.)

- [ ] **Step 3: Push**

```bash
git push origin main
```
Note: a push to `main` triggers the Docker deploy — this is the user's call to make. Confirm before pushing if unsure. The change activates on the next `mode_submit` run (no flag-gate; it is unconditional prompt text).

---

## Self-Review

**1. Spec coverage:**
- SYSTEM_PROMPT lens (invisible, 3 beats) → Task 1. ✓
- Scope = TOP STORIES → Task 2 Step 3. ✓
- Third beat = market transmission → Task 1 paragraph ("market transmission"), Task 2 ("market channel"). ✓
- One-clause WATCH/FORWARD dedup, not a reframe → Task 2 Step 4. ✓
- Prompt-substring regression tests → Tasks 1 & 2 tests. ✓
- "Explicitly NOT doing" (no labels/new section/other-section lens/code change) → enforced by Global Constraints + the `"context / stakes / connection" not in sp` negative assertion. ✓

**2. Placeholder scan:** No TBD/TODO; all prompt text and test code is literal and complete. ✓

**3. Type consistency:** No new functions or signatures; `_daily_kwargs()` and `build_daily_prompt(**kwargs)` match `tests/test_signals.py:169`. Test names are unique and consistent across steps. ✓

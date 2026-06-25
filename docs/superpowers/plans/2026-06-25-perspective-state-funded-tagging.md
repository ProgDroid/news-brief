# Perspective / State-Funded Source Tagging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag news sources with an optional `perspective` (national/bloc vantage) and a binary `state_funded` flag, render them in the LLM-facing section header, and teach the model to attribute and triangulate framing accordingly.

**Architecture:** Two optional keys (`state_funded: bool`, `perspective: str | None`) added to source dicts. Validated in `load_temp_sources`, rendered by one shared `_source_header` helper used by both fetch paths, taught via a `SYSTEM_PROMPT` line, and captured through two new `/addsource` wizard steps. Tagging is opt-in per source — untagged sources render byte-identical to today.

**Tech Stack:** Python 3, pytest, ruff. Single module `brief.py`, tests in `tests/test_commands.py`.

## Global Constraints

- Absent `perspective` means "no vantage claim made", NOT "neutral". Never add a NEUTRAL enum value.
- Untagged sources MUST render byte-identical headers to the current code (regression guard).
- `perspective` is validated against `VALID_PERSPECTIVES`; unknown values degrade to absent (mirror the existing `kind` graceful-fallback).
- `state_funded` defaults `False` and is always present on a loaded source dict; `perspective` is omitted when absent.
- Verification gate before every commit: `ruff check .` + `ruff format --check .` + `pytest` all green. Stage every reformatted file (CI fails otherwise).
- Commit straight to `main` (solo repo, no branch).
- Run Python/pytest via the PowerShell tool, not Bash (Bash errors "stdin is not a tty"). Make git commits via the Bash tool (PowerShell prepends a BOM to commit subjects).

---

### Task 1: Schema — `VALID_PERSPECTIVES` constant + `load_temp_sources` validation

**Files:**
- Modify: `brief.py` (add constant near `VALID_KINDS` line 262; extend `load_temp_sources` ~305-319)
- Test: `tests/test_commands.py`

**Interfaces:**
- Produces: `brief.VALID_PERSPECTIVES: tuple[str, ...]`. `load_temp_sources()` returns dicts that now always carry `state_funded: bool` and carry `perspective: str` only when valid+present.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_commands.py` (after `test_load_temp_sources_accepts_and_normalizes_source_type`, ~line 298):

```python
def test_load_temp_sources_state_funded_and_perspective(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    (tmp_path / "sources.json").write_text(
        json.dumps(
            [
                {
                    "name": "Tagged",
                    "url": "https://a/feed",
                    "category": "geo",
                    "state_funded": True,
                    "perspective": "ARAB",
                },
                {
                    "name": "BadPersp",
                    "url": "https://b/feed",
                    "category": "geo",
                    "perspective": "MARTIAN",  # not in VALID_PERSPECTIVES → dropped
                },
                {"name": "Plain", "url": "https://c/feed", "category": "geo"},
            ]
        ),
        encoding="utf-8",
    )
    out = brief.load_temp_sources()
    assert out[0]["state_funded"] is True and out[0]["perspective"] == "ARAB"
    assert out[1]["state_funded"] is False  # absent → default
    assert "perspective" not in out[1]  # invalid value dropped, not coerced
    assert out[2]["state_funded"] is False and "perspective" not in out[2]
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell tool): `python -m pytest tests/test_commands.py::test_load_temp_sources_state_funded_and_perspective -v`
Expected: FAIL — `KeyError: 'state_funded'` (key not yet emitted).

- [ ] **Step 3: Add the constant**

In `brief.py` immediately after `VALID_SOURCE_TYPES = ("feed", "page")` (line 263):

```python
# National/bloc vantage a source speaks from. OPTIONAL and sparse: set only
# where it changes the read (regional/primary outlets), left off neutral wires
# and analysts. An ABSENT perspective means "no vantage claim made" — the model
# falls back on its own priors — NOT "this source is neutral". Do not add a
# NEUTRAL value: that would be a positive editorial claim, as contestable as
# picking a side.
VALID_PERSPECTIVES = (
    "WESTERN",
    "CHINESE",
    "RUSSIAN",
    "IRANIAN",
    "ISRAELI",
    "ARAB",
    "UKRAINIAN",
    "JAPANESE",
    "KOREAN",
    "INDIAN",
)
```

- [ ] **Step 4: Extend `load_temp_sources` validation**

In `brief.py`, replace the `out.append({...})` block (currently lines 311-319) with:

```python
        loaded = {
            "name": str(name),
            "url": str(url),
            "category": str(category).lower(),
            "kind": kind,
            "source_type": source_type,
            "state_funded": bool(entry.get("state_funded", False)),
        }
        perspective = entry.get("perspective")
        if perspective in VALID_PERSPECTIVES:
            loaded["perspective"] = perspective
        out.append(loaded)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py::test_load_temp_sources_state_funded_and_perspective -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite + lint**

Run: `python -m pytest tests/test_commands.py -q` then `ruff check .` then `ruff format --check .`
Expected: all pass. If `ruff format --check` flags `brief.py`, run `ruff format brief.py` and re-stage.

- [ ] **Step 7: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sources): validate state_funded + optional perspective on temp sources

VALID_PERSPECTIVES enum; absent perspective = no vantage claim (not NEUTRAL).
EOF
```

---

### Task 2: Render — `_source_header` helper wired into both fetch paths

**Files:**
- Modify: `brief.py` (new helper near `fetch_rss` ~line 1180; call sites at ~1207-1208 and ~1242-1245)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `brief._source_header(name: str, kind: str, category: str, perspective: str | None = None, state_funded: bool = False) -> str` returning `"\n### {name} [{BRACKET}] ({CATEGORY})"` where `BRACKET` is `KIND` plus `· PERSPECTIVE` (if given) plus `· STATE-FUNDED` (if flagged), all uppercased except `name`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_commands.py` (near the other source tests):

```python
def test_source_header_untagged_is_byte_identical():
    # Regression guard: untagged source must match the pre-feature format exactly.
    assert brief._source_header("Reuters World", "wire", "geo") == (
        "\n### Reuters World [WIRE] (GEO)"
    )


def test_source_header_perspective_only():
    assert brief._source_header("SCMP", "regional", "china", perspective="CHINESE") == (
        "\n### SCMP [REGIONAL · CHINESE] (CHINA)"
    )


def test_source_header_state_funded_only():
    assert brief._source_header("NHK", "regional", "japan", state_funded=True) == (
        "\n### NHK [REGIONAL · STATE-FUNDED] (JAPAN)"
    )


def test_source_header_both():
    assert brief._source_header(
        "Al Jazeera", "regional", "geo", perspective="ARAB", state_funded=True
    ) == ("\n### Al Jazeera [REGIONAL · ARAB · STATE-FUNDED] (GEO)")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -k source_header -v`
Expected: FAIL — `AttributeError: module 'brief' has no attribute '_source_header'`.

- [ ] **Step 3: Add the helper**

In `brief.py`, immediately before `def fetch_rss(` (currently ~line 1180), add:

```python
def _source_header(
    name: str,
    kind: str,
    category: str,
    perspective: str | None = None,
    state_funded: bool = False,
) -> str:
    """Build the LLM-facing section header for a source. The bracket carries the
    source's kind plus, when present, its perspective and a STATE-FUNDED flag.
    Untagged sources render exactly as before (kind only), so existing briefs are
    unchanged. `·` separates bracket fields."""
    parts = [kind.upper()]
    if perspective:
        parts.append(perspective.upper())
    if state_funded:
        parts.append("STATE-FUNDED")
    return f"\n### {name} [{' · '.join(parts)}] ({category.upper()})"
```

- [ ] **Step 4: Wire `fetch_rss`**

In `fetch_rss`, replace lines 1207-1208:

```python
        kind = feed.get("kind", "wire").upper()
        lines = [f"\n### {feed['name']} [{kind}] ({feed['category'].upper()})"]
```

with:

```python
        lines = [
            _source_header(
                feed["name"],
                feed.get("kind", "wire"),
                feed["category"],
                feed.get("perspective"),
                feed.get("state_funded", False),
            )
        ]
```

- [ ] **Step 5: Wire `fetch_web_source`**

In `fetch_web_source`, replace lines 1242-1245:

```python
        kind = source.get("kind", "regional").upper()
        return (
            f"\n### {source['name']} [{kind}] ({source['category'].upper()})\n{content}"
        )
```

with:

```python
        header = _source_header(
            source["name"],
            source.get("kind", "regional"),
            source["category"],
            source.get("perspective"),
            source.get("state_funded", False),
        )
        return f"{header}\n{content}"
```

- [ ] **Step 6: Run tests + lint**

Run: `python -m pytest tests/test_commands.py -k source_header -v` then `python -m pytest tests/test_commands.py -q` then `ruff check .` then `ruff format --check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sources): render perspective + state-funded in LLM section header

Shared _source_header helper for fetch_rss/fetch_web_source; untagged
sources render byte-identical to before (regression-tested).
EOF
```

---

### Task 3: Data — tag the five baked-in regional/primary feeds

**Files:**
- Modify: `brief.py` `RSS_FEEDS` (Al Jazeera ~190-196, Kyiv Independent ~197-203, Yonhap ~211-217, NHK World ~225-231, SCMP ~239-245)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `VALID_PERSPECTIVES` (Task 1) — assigned values must be members.
- Produces: tagged `RSS_FEEDS` entries.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py`:

```python
def test_baked_in_feed_perspective_assignments():
    by_name = {f["name"]: f for f in brief.RSS_FEEDS}
    expected = {
        "Al Jazeera": ("ARAB", True),
        "NHK World": ("JAPANESE", True),
        "Yonhap (English)": ("KOREAN", True),
        "SCMP": ("CHINESE", False),
        "Kyiv Independent": ("UKRAINIAN", False),
    }
    for name, (persp, sf) in expected.items():
        assert by_name[name].get("perspective") == persp, name
        assert by_name[name].get("state_funded", False) is sf, name
        assert persp in brief.VALID_PERSPECTIVES
    # Wires/analysts/think-tanks stay untagged (sample check).
    for name in ("Reuters World", "ISW Daily Assessment", "38 North"):
        assert "perspective" not in by_name[name], name
        assert by_name[name].get("state_funded", False) is False, name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_baked_in_feed_perspective_assignments -v`
Expected: FAIL — `AssertionError: Al Jazeera` (no perspective key yet).

- [ ] **Step 3: Add the fields to the five entries**

Add `"perspective"` / `"state_funded"` keys after the `"kind"` line of each entry:

Al Jazeera (after `"kind": "regional",` at ~195):
```python
        "perspective": "ARAB",
        "state_funded": True,
```
Kyiv Independent (after `"kind": "regional",` at ~202):
```python
        "perspective": "UKRAINIAN",
```
Yonhap (English) (after `"kind": "regional",` at ~216):
```python
        "perspective": "KOREAN",
        "state_funded": True,
```
NHK World (after `"kind": "regional",` at ~230):
```python
        "perspective": "JAPANESE",
        "state_funded": True,
```
SCMP (after `"kind": "regional",` at ~244):
```python
        "perspective": "CHINESE",
```

(Kyiv Independent and SCMP get no `state_funded` key — default False.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py::test_baked_in_feed_perspective_assignments -v`
Expected: PASS.

- [ ] **Step 5: Run suite + lint**

Run: `python -m pytest tests/test_commands.py -q` then `ruff check .` then `ruff format --check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sources): tag five regional feeds with perspective + state-funded

Al Jazeera/NHK/Yonhap state-funded; SCMP/Kyiv Independent perspective only.
Wires, analysts and US think-tanks left untagged by design.
EOF
```

---

### Task 4: Prompt — attribute + triangulate line in `SYSTEM_PROMPT`

**Files:**
- Modify: `brief.py` `SYSTEM_PROMPT` (~1459-1475)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces: extended `SYSTEM_PROMPT` string.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py`:

```python
def test_system_prompt_teaches_perspective_tags():
    p = brief.SYSTEM_PROMPT
    assert "STATE-FUNDED" in p
    assert "perspective" in p.lower()
    assert "attribute" in p.lower()  # attribution instruction present
    assert "untagged" in p.lower()  # absent-tag semantics taught
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_system_prompt_teaches_perspective_tags -v`
Expected: FAIL — `assert "STATE-FUNDED" in p`.

- [ ] **Step 3: Extend `SYSTEM_PROMPT`**

In `brief.py`, change the final line of `SYSTEM_PROMPT` from:

```python
or repeat. If nothing significant happened on a topic, say so in one line."""
```

to:

```python
or repeat. If nothing significant happened on a topic, say so in one line.

Some sources carry a perspective tag (the vantage they speak from) and/or a STATE-FUNDED flag in their section header. When a tagged source makes a claim, attribute its framing to that vantage rather than stating it as neutral fact (e.g. "Beijing's read, via SCMP, is..."). Treat agreement across opposing perspectives — or a state-funded outlet corroborating an independent wire — as a stronger signal; treat divergence as a flag worth surfacing. An untagged source carries no vantage claim; weigh it on its merits."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py::test_system_prompt_teaches_perspective_tags -v`
Expected: PASS.

- [ ] **Step 5: Run suite + lint**

Run: `python -m pytest tests/test_commands.py -q` then `ruff check .` then `ruff format --check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(brief): teach model to attribute + triangulate tagged-source framing
EOF
```

---

### Task 5: Wizard — `state_funded` + skippable `perspective` steps

**Files:**
- Modify: `brief.py` (new prompt fns near `_wizard_sourcetype_prompt` ~581; `_wizard_confirm_prompt` ~599; callback handler `as:kind:`/`as:confirm` ~881-907)
- Modify: `tests/test_commands.py` — three existing wizard tests change shape (the post-kind step is now `state_funded`, not `url`)

**Interfaces:**
- Consumes: `VALID_PERSPECTIVES` (Task 1). New callbacks `as:sf:0` / `as:sf:1`, `as:persp:<VALUE>` / `as:persp:_skip`.
- Produces: wizard flow `category → kind → state_funded → perspective → url → [source_type] → confirm`. Persisted entry gains `state_funded: bool` and (when not skipped) `perspective: str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py`:

```python
def test_addsource_wizard_captures_state_funded_and_perspective(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(brief.TELEGRAM_CHAT_ID)
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:regional"))
    assert brief._WIZARD[chat]["step"] == "state_funded"

    brief._handle_callback_query(_cb("as:sf:1"))
    assert brief._WIZARD[chat]["state_funded"] is True
    assert brief._WIZARD[chat]["step"] == "perspective"

    brief._handle_callback_query(_cb("as:persp:ARAB"))
    assert brief._WIZARD[chat]["perspective"] == "ARAB"
    assert brief._WIZARD[chat]["step"] == "url"

    brief._handle_telegram_update(_update("aljazeera.com"), _fb())
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert srcs[0]["state_funded"] is True and srcs[0]["perspective"] == "ARAB"


def test_addsource_wizard_perspective_skip(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(brief.TELEGRAM_CHAT_ID)
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:wire"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    assert brief._WIZARD[chat]["step"] == "url"
    assert "perspective" not in brief._WIZARD[chat]

    brief._handle_telegram_update(_update("example.com"), _fb())
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert srcs[0]["state_funded"] is False and "perspective" not in srcs[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -k "state_funded_and_perspective or perspective_skip" -v`
Expected: FAIL — after `as:kind:regional` the step is still `url`, so `assert ... == "state_funded"` fails.

- [ ] **Step 3: Add the two prompt functions**

In `brief.py`, immediately after `_wizard_sourcetype_prompt` (ends ~line 596), add:

```python
def _wizard_statefunded_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "state_funded"
    rows = [
        [
            {"text": "🏛 State-funded", "callback_data": "as:sf:1"},
            {"text": "🏷 Independent", "callback_data": "as:sf:0"},
        ],
        _CANCEL_ROW,
    ]
    telegram_edit_text(
        w["msg_id"],
        f"➕ <b>Add a source</b>\nCategory: <b>{html.escape(w['category'])}</b> · "
        f"Kind: <b>{w['kind']}</b>\n\n"
        "Is it state-funded?",
        rows,
    )


def _wizard_perspective_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "perspective"
    rows = _btn_rows(
        [(p, f"as:persp:{p}") for p in VALID_PERSPECTIVES], per_row=2
    ) + [
        [{"text": "⏭ Skip", "callback_data": "as:persp:_skip"}],
        _CANCEL_ROW,
    ]
    telegram_edit_text(
        w["msg_id"],
        f"➕ <b>Add a source</b>\nKind: <b>{w['kind']}</b> · "
        f"State-funded: <b>{'yes' if w.get('state_funded') else 'no'}</b>\n\n"
        "Whose vantage does it speak from? (Skip if it's a neutral wire — "
        "skipping makes no claim either way.)",
        rows,
    )
```

- [ ] **Step 4: Rewire the `as:kind:` callback to the new flow**

In the callback handler, change the `as:kind:` branch (currently lines 881-883) from:

```python
    elif data.startswith("as:kind:"):
        w["kind"] = data[len("as:kind:") :]
        _wizard_url_prompt(chat_id)
```

to:

```python
    elif data.startswith("as:kind:"):
        w["kind"] = data[len("as:kind:") :]
        _wizard_statefunded_prompt(chat_id)
    elif data.startswith("as:sf:"):
        w["state_funded"] = data[len("as:sf:") :] == "1"
        _wizard_perspective_prompt(chat_id)
    elif data.startswith("as:persp:"):
        val = data[len("as:persp:") :]
        if val in VALID_PERSPECTIVES:
            w["perspective"] = val
        _wizard_url_prompt(chat_id)
```

- [ ] **Step 5: Include the new fields in the confirmed entry**

In the `as:confirm` branch, change the `entry = {...}` dict (currently lines 893-899) to:

```python
        entry = {
            "name": w["name"],
            "url": w["url"],
            "category": w["category"],
            "kind": w["kind"],
            "source_type": w.get("source_type", "feed"),
            "state_funded": w.get("state_funded", False),
        }
        if w.get("perspective"):
            entry["perspective"] = w["perspective"]
```

- [ ] **Step 6: Surface the tags in the confirm prompt**

In `_wizard_confirm_prompt`, change the body (lines 611-615) to include a tag line:

```python
    tags = []
    if w.get("perspective"):
        tags.append(w["perspective"])
    if w.get("state_funded"):
        tags.append("state-funded")
    tag_line = f"\nTags: <b>{' · '.join(tags)}</b>" if tags else ""
    telegram_edit_text(
        w["msg_id"],
        "➕ <b>Add this source?</b>\n\n"
        f"Name: <b>{html.escape(w['name'])}</b>\n"
        f"Category: <b>{html.escape(w['category'])}</b> · Kind: <b>{w['kind']}</b> · "
        f"Type: <b>{w.get('source_type', 'feed')}</b>"
        f"{tag_line}\n"
        f"URL: <code>{html.escape(w['url'])}</code>",
        rows,
    )
```

- [ ] **Step 7: Update the three existing wizard tests for the new flow**

These tests drive the wizard past the kind step and currently assume kind → url. Insert `as:sf:` and `as:persp:_skip` calls after each `as:kind:` and fix the post-kind assertion.

In `test_addsource_wizard_quick_domain` (~370-371), replace:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
    assert brief._WIZARD[chat]["step"] == "url"
```
with:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    assert brief._WIZARD[chat]["step"] == "url"
```

In `test_addsource_wizard_full_url_asks_feed_or_page` (~394), replace:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
```
with:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
```

In `test_addsource_wizard_full_url_feed_choice` (~418), replace:
```python
    brief._handle_callback_query(_cb("as:kind:wire"))
```
with:
```python
    brief._handle_callback_query(_cb("as:kind:wire"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
```

In `test_addsource_wizard_rejects_garbage_url` (~436), replace:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
```
with:
```python
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
```

- [ ] **Step 8: Run the wizard tests + full suite + lint**

Run: `python -m pytest tests/test_commands.py -k addsource -v` then `python -m pytest -q` then `ruff check .` then `ruff format --check .`
Expected: all pass. If `ruff format --check` flags a file, run `ruff format <file>` and re-stage.

- [ ] **Step 9: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sources): wizard captures state-funded + skippable perspective

Two new /addsource steps after kind; skip makes no vantage claim.
Existing wizard tests updated for the new step order.
EOF
```

---

## Post-implementation

- [ ] Run the full gate once more from a clean state: `python -m pytest -q` + `ruff check .` + `ruff format --check .`.
- [ ] Update memory `external-geo-dashboards-backlog.md`: mark item #1 STATUS done (built + pushed), with a one-line learning (absent-perspective semantics; lean scope shipped). Add a pointer update in `MEMORY.md` if the hook line changes.

## Self-Review

**Spec coverage:** §1 schema → Task 1. §2 header render → Task 2. §3 prompt → Task 4. §4 wizard → Task 5. §5 assignments → Task 3. §6 tests → distributed across all tasks (validation T1, header T2, wizard T5, assignments T3, prompt T4). All covered.

**Placeholder scan:** No TBD/TODO; every code step shows full code; every run step shows exact command + expected result.

**Type consistency:** `_source_header(name, kind, category, perspective=None, state_funded=False)` signature identical across Task 2 definition and Task 2/3 call sites. `VALID_PERSPECTIVES` defined T1, consumed T3/T5. Callback names `as:sf:` / `as:persp:` consistent between handler (T5 Step 4) and tests (T5 Step 1/7). `state_funded` is always-present bool; `perspective` omitted-when-absent — consistent in load (T1), entry build (T5 Step 5), and assertions.

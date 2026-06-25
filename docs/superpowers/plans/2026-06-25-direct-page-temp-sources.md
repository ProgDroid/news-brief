# Direct-Page Temp Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a temp source be marked `source_type: "page"` so it is fetched as a scraped web page instead of an RSS feed, with wizard support and the situational Iran dashboard migrated off in-code `WEB_SOURCES`.

**Architecture:** Add an optional `source_type` field (`"feed"` default | `"page"`) to temp-source entries. `mode_submit` partitions temp sources by type — feeds to `fetch_rss`, pages to `fetch_web_source` — mirroring the existing baked-baseline-vs-volume split. The `/addsource` wizard gains a feed-or-page step for full URLs. The Iran dashboard moves from in-code `WEB_SOURCES` to a host-side temp source.

**Tech Stack:** Python 3, `feedparser`, `requests`, pytest. Telegram inline-keyboard wizard. Spec: `docs/superpowers/specs/2026-06-25-direct-page-temp-sources-design.md`.

## Global Constraints

- Backward compatibility: a temp-source entry with no `source_type` MUST behave exactly as today (treated as `"feed"`). No migration of existing `sources.json` entries is required.
- `VALID_SOURCE_TYPES = ("feed", "page")` — any missing/invalid value normalizes to `"feed"`.
- Run Python/pytest via the PowerShell tool (the Bash tool errors `stdin is not a tty` here); PowerShell wraps Python stderr as a scary `NativeCommandError` even on success — that is not a failure.
- Make git commits via the **Bash tool**, not PowerShell (PowerShell prepends a UTF-8 BOM to the commit subject). Commit messages contain no backticks/`$`.
- Pre-push gate is `ruff check` + `ruff format --check` + `pytest` — not pytest alone. Stage every file the formatter reflows or CI fails.
- No new top-level module is added, so no Dockerfile COPY / workflow allowlist changes are needed.
- The host `sources.json` edit (re-adding the Iran dashboard) is live state on the deploy volume — NOT committed to the repo.

---

### Task 1: Add `source_type` to the temp-source model

**Files:**
- Modify: `brief.py` — add `VALID_SOURCE_TYPES` near `VALID_KINDS` (`brief.py:265`); extend `load_temp_sources` (`brief.py:290-318`)
- Test: `tests/test_commands.py` (alongside the existing `load_temp_sources` tests, ~`:228-261`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `load_temp_sources()` returns dicts that now include `"source_type"` (always present, `"feed"` or `"page"`). `VALID_SOURCE_TYPES = ("feed", "page")` module constant.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py`:

```python
def test_load_temp_sources_defaults_source_type_to_feed(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    (tmp_path / "sources.json").write_text(
        json.dumps(
            [{"name": "NoType", "url": "https://x/feed", "category": "iran"}]
        ),
        encoding="utf-8",
    )
    out = brief.load_temp_sources()
    assert out[0]["source_type"] == "feed"  # absent → default


def test_load_temp_sources_accepts_and_normalizes_source_type(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    (tmp_path / "sources.json").write_text(
        json.dumps(
            [
                {
                    "name": "Page",
                    "url": "https://x/dash",
                    "category": "us",
                    "source_type": "page",
                },
                {
                    "name": "Bad",
                    "url": "https://y/dash",
                    "category": "us",
                    "source_type": "weird",
                },
            ]
        ),
        encoding="utf-8",
    )
    out = brief.load_temp_sources()
    assert out[0]["source_type"] == "page"  # valid kept
    assert out[1]["source_type"] == "feed"  # invalid coerced to default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_load_temp_sources_defaults_source_type_to_feed tests/test_commands.py::test_load_temp_sources_accepts_and_normalizes_source_type -v`
Expected: FAIL with `KeyError: 'source_type'`.

- [ ] **Step 3: Add the constant**

In `brief.py`, immediately after the existing `VALID_KINDS` line (`brief.py:265`):

```python
VALID_KINDS = ("wire", "analyst", "regional", "primary")
VALID_SOURCE_TYPES = ("feed", "page")
```

- [ ] **Step 4: Extend `load_temp_sources`**

In `brief.py`, inside the loop in `load_temp_sources`, replace the `kind` block and the appended dict (`brief.py:307-317`) with:

```python
        kind = entry.get("kind", "regional")
        if kind not in VALID_KINDS:
            kind = "regional"
        source_type = entry.get("source_type", "feed")
        if source_type not in VALID_SOURCE_TYPES:
            source_type = "feed"
        out.append(
            {
                "name": str(name),
                "url": str(url),
                "category": str(category).lower(),
                "kind": kind,
                "source_type": source_type,
            }
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -k source_type -v`
Expected: PASS (both new tests).

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -m "feat(sources): add source_type field to temp sources"
```

---

### Task 2: Partition temp sources by type in `mode_submit`

**Files:**
- Modify: `brief.py` — add `_split_temp_sources` helper near `load_temp_sources` (after `remove_temp_source`, ~`brief.py:357`); change the routing block in `mode_submit` (`brief.py:2091-2098`)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `load_temp_sources()` dicts with `source_type` (Task 1).
- Produces: `_split_temp_sources(temp_sources: list[dict]) -> tuple[list[dict], list[dict]]` returning `(feeds, pages)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py`:

```python
def test_split_temp_sources_partitions_by_type():
    feed_default = {"name": "F", "url": "u1", "category": "us", "kind": "wire"}
    feed_explicit = {
        "name": "F2", "url": "u2", "category": "us", "kind": "wire",
        "source_type": "feed",
    }
    page = {
        "name": "P", "url": "u3", "category": "us", "kind": "regional",
        "source_type": "page",
    }
    feeds, pages = brief._split_temp_sources([feed_default, page, feed_explicit])
    assert [s["name"] for s in feeds] == ["F", "F2"]
    assert [s["name"] for s in pages] == ["P"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_split_temp_sources_partitions_by_type -v`
Expected: FAIL with `AttributeError: ... has no attribute '_split_temp_sources'`.

- [ ] **Step 3: Add the helper**

In `brief.py`, after `remove_temp_source` (`brief.py:357`):

```python
def _split_temp_sources(temp_sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition temp sources into (feeds, pages) by source_type. Feeds are
    fetched via fetch_rss; pages via fetch_web_source. A missing source_type
    counts as a feed, preserving pre-source_type behaviour."""
    feeds = [s for s in temp_sources if s.get("source_type", "feed") != "page"]
    pages = [s for s in temp_sources if s.get("source_type") == "page"]
    return feeds, pages
```

- [ ] **Step 4: Wire it into `mode_submit`**

In `brief.py`, replace the routing block (`brief.py:2091-2098`):

```python
    feed_temp, page_temp = _split_temp_sources(temp_sources)
    feed_content = (
        "\n".join(c for f in RSS_FEEDS + feed_temp if (c := fetch_rss(f)))
        or "(no RSS content)"
    )
    web_content = (
        "\n".join(c for s in WEB_SOURCES + page_temp if (c := fetch_web_source(s)))
        or "(no web content)"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py::test_split_temp_sources_partitions_by_type -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -m "feat(sources): route page temp sources to fetch_web_source"
```

---

### Task 3: Render `[KIND]` in the page-source header

**Files:**
- Modify: `brief.py` — `fetch_web_source` return line (`brief.py:1207`)
- Test: `tests/test_signals.py` (alongside `test_fetch_rss_header_includes_kind`, ~`:100`)

**Interfaces:**
- Consumes: a source dict with `kind` (page temp sources and `WEB_SOURCES` entries both carry it).
- Produces: `fetch_web_source` section header is now `### {name} [{KIND}] ({CATEGORY})`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signals.py`:

```python
def test_fetch_web_source_header_includes_kind(monkeypatch):
    html_page = (
        b"<html><head>"
        b'<meta name="description" content="Some analyst summary">'
        b"</head><body></body></html>"
    )

    class _Resp:
        text = html_page.decode()
        ok = True

        def raise_for_status(self):
            pass

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp())
    source = {
        "name": "BCA Dash",
        "url": "http://x",
        "category": "us",
        "kind": "regional",
    }
    out = brief.fetch_web_source(source)
    assert "[REGIONAL]" in out
    assert "BCA Dash" in out
    assert "Some analyst summary" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_signals.py::test_fetch_web_source_header_includes_kind -v`
Expected: FAIL — `[REGIONAL]` not in output (current header has no kind tag).

- [ ] **Step 3: Update the header**

In `brief.py`, replace the `fetch_web_source` return line (`brief.py:1207`):

```python
        kind = source.get("kind", "regional").upper()
        return (
            f"\n### {source['name']} [{kind}] "
            f"({source['category'].upper()})\n{content}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_signals.py::test_fetch_web_source_header_includes_kind -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -m "feat(sources): tag page-source header with kind for tilt"
```

---

### Task 4: Wizard feed-or-page step

**Files:**
- Modify: `brief.py` — add `_wizard_sourcetype_prompt` (after `_wizard_url_prompt`, ~`brief.py:567`); branch `_wizard_handle_text` `url` step (`brief.py:603-617`); add `as:stype:` callback branch (`brief.py:850-852`); include `source_type` in the confirm entry (`brief.py:858-864`) and confirm prompt (`brief.py:582-585`)
- Test: `tests/test_commands.py` (update `test_addsource_wizard_full_url_keeps_url` ~`:350`; update `test_addsource_wizard_quick_domain` ~`:342`; add a page-path test)

**Interfaces:**
- Consumes: `add_temp_source(entry)` with `entry` now including `source_type` (Task 1 model).
- Produces: wizard state `w["source_type"]`; callback data `as:stype:feed` / `as:stype:page`; new step name `"source_type"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_commands.py`, replace `test_addsource_wizard_full_url_keeps_url` (`:350-360`) with:

```python
def test_addsource_wizard_full_url_asks_feed_or_page(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(brief.TELEGRAM_CHAT_ID)
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:us"))
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_telegram_update(
        _update("https://www.bcaresearch.com/dashboard/x"), _fb()
    )
    w = brief._WIZARD[chat]
    assert w["url"] == "https://www.bcaresearch.com/dashboard/x"
    assert w["name"] == "www.bcaresearch.com"
    assert w["step"] == "source_type"  # full URL → asks feed-or-page

    brief._handle_callback_query(_cb("as:stype:page"))
    assert brief._WIZARD[chat]["source_type"] == "page"
    assert brief._WIZARD[chat]["step"] == "confirm"

    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert len(srcs) == 1 and srcs[0]["source_type"] == "page"


def test_addsource_wizard_full_url_feed_choice(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(brief.TELEGRAM_CHAT_ID)
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:wire"))
    brief._handle_telegram_update(_update("https://site.com/feed.xml"), _fb())
    brief._handle_callback_query(_cb("as:stype:feed"))
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert srcs[0]["source_type"] == "feed" and srcs[0]["url"] == "https://site.com/feed.xml"
```

Then extend `test_addsource_wizard_quick_domain` — after the existing `as:confirm` block (`:342-347`), add:

```python
    assert srcs[0]["source_type"] == "feed"  # bare domain → feed, no extra step
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -k "addsource_wizard" -v`
Expected: FAIL — the full-URL tests hit `as:stype:` which isn't handled, and `source_type` is absent from stored entries.

- [ ] **Step 3: Add the source-type prompt**

In `brief.py`, after `_wizard_url_prompt` (`brief.py:567`):

```python
def _wizard_sourcetype_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "source_type"
    rows = [
        [
            {"text": "📰 RSS feed", "callback_data": "as:stype:feed"},
            {"text": "📄 Page to scrape", "callback_data": "as:stype:page"},
        ],
        _CANCEL_ROW,
    ]
    telegram_edit_text(
        w["msg_id"],
        f"➕ <b>Add a source</b>\nURL: <code>{html.escape(w['url'])}</code>\n\n"
        "Is this an RSS/Atom <b>feed</b>, or a <b>page</b> to scrape?",
        rows,
    )
```

- [ ] **Step 4: Branch the URL text handler**

In `brief.py`, replace the `elif step == "url":` block (`brief.py:603-617`):

```python
    elif step == "url":
        raw = text.strip()
        if "://" in raw:
            w["url"] = raw
            w["name"] = urlsplit(raw).netloc or raw
            _wizard_sourcetype_prompt(chat_id)
        elif _DOMAIN_RE.match(raw):
            w["url"] = build_google_news_url(raw)
            w["name"] = raw
            w["source_type"] = "feed"
            _wizard_confirm_prompt(chat_id)
        else:
            telegram_send(
                "⚠️ That's neither a domain nor a URL. Send something like "
                "<code>timesofisrael.com</code> or "
                "<code>https://site.com/feed.xml</code>."
            )
```

- [ ] **Step 5: Handle the source-type callback**

In `brief.py`, after the `elif data.startswith("as:kind:")` block (`brief.py:850-852`):

```python
    elif data.startswith("as:kind:"):
        w["kind"] = data[len("as:kind:") :]
        _wizard_url_prompt(chat_id)
    elif data.startswith("as:stype:"):
        w["source_type"] = data[len("as:stype:") :]
        _wizard_confirm_prompt(chat_id)
```

- [ ] **Step 6: Persist `source_type` on confirm**

In `brief.py`, in the `as:confirm` branch, change the entry dict (`brief.py:859-864`):

```python
        entry = {
            "name": w["name"],
            "url": w["url"],
            "category": w["category"],
            "kind": w["kind"],
            "source_type": w.get("source_type", "feed"),
        }
```

- [ ] **Step 7: Show the type on the confirm screen**

In `brief.py`, in `_wizard_confirm_prompt`, change the category/kind line (`brief.py:584`):

```python
        f"Category: <b>{html.escape(w['category'])}</b> · Kind: <b>{w['kind']}</b> · "
        f"Type: <b>{w.get('source_type', 'feed')}</b>\n"
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -k "addsource_wizard" -v`
Expected: PASS (all wizard tests, including the domain and page paths).

- [ ] **Step 9: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -m "feat(sources): wizard asks feed-or-page for full URLs"
```

---

### Task 5: Migrate the Iran dashboard off in-code `WEB_SOURCES`

**Files:**
- Modify: `brief.py` — empty the `WEB_SOURCES` list and update its comment (`brief.py:248-255`)
- Modify: `README.md` — note that page sources are now temp sources (`README.md:303-304`)
- Test: `tests/test_signals.py` (`test_every_feed_has_a_kind` already iterates `WEB_SOURCES`; confirm green)

**Interfaces:**
- Consumes: nothing.
- Produces: `WEB_SOURCES == []` (kept as the always-on page baseline).

- [ ] **Step 1: Empty `WEB_SOURCES`**

In `brief.py`, replace the `WEB_SOURCES` definition (`brief.py:248-255`):

```python
# Always-on direct-page sources baked into the image, fetched via
# fetch_web_source. Empty by default: situational pages (e.g. a crisis
# dashboard) belong in temp sources (source_type="page") so they can be
# dropped without a redeploy. Parallel to RSS_FEEDS for feeds.
WEB_SOURCES: list[dict] = []
```

- [ ] **Step 2: Update the README note**

In `README.md`, replace the `WEB_SOURCES` line (`README.md:303-304`) with:

```markdown
- **Feeds:** edit `RSS_FEEDS` (any RSS/Substack/RSSHub source). For a direct
  page, add a temp source with `"source_type": "page"` (via `/addsource` →
  "Page to scrape", or by hand-editing `sources.json`); it is fetched with
  `fetch_web_source` (meta description, or first 800 chars). `WEB_SOURCES` is
  the always-on page baseline and is empty by default.
```

- [ ] **Step 3: Run the full suite + lint**

Run: `python -m pytest tests/test_signals.py::test_every_feed_has_a_kind -v && python -m pytest && python -m ruff check . && python -m ruff format --check .`
Expected: PASS / `All checks passed!`. Stage any file ruff reflows.

- [ ] **Step 4: Commit**

```bash
git add brief.py README.md
git commit -m "refactor(sources): move Iran dashboard out of in-code WEB_SOURCES"
```

- [ ] **Step 5: Re-add the Iran dashboard on the deploy host (manual, post-deploy)**

This is live state on the deploy volume — NOT a repo change. After deploying, either run `/addsource` (category `iran`, kind `regional`, paste the URL, choose **Page to scrape**), or append to `${APPDATA_DIR}/news-brief/sources.json`:

```json
{
  "name": "BCA Research — Iran Conflict Daily Dashboard",
  "url": "https://www.bcaresearch.com/collection/bcas-iran-conflict-daily-dashboard",
  "category": "iran",
  "kind": "regional",
  "source_type": "page"
}
```

Also add the original BCA midterm dashboard the same way (category `us`, kind `regional`, URL `https://www.bcaresearch.com/dashboard/us-midterm-election-dashboard`, **Page to scrape**).

---

## Self-Review

**Spec coverage:**
- §1 Data model (`source_type` field, default/normalize) → Task 1 ✓
- §2 Routing (split feed vs page) → Task 2 ✓
- §3 Wizard (feed-or-page step for full URLs; domain → feed) → Task 4 ✓
- §4 Iran migration (empty `WEB_SOURCES`; host re-add) → Task 5 ✓
- §5 Consistency fix (`[KIND]` in page header) → Task 3 ✓
- §6 Testing → covered per-task ✓

**Placeholder scan:** none — every code/test step shows full content.

**Type consistency:** `source_type` string field, `VALID_SOURCE_TYPES = ("feed", "page")`, `_split_temp_sources -> (feeds, pages)`, callback prefix `as:stype:`, step name `"source_type"`, wizard key `w["source_type"]` — consistent across Tasks 1, 2, 4. `fetch_web_source` header format matches `fetch_rss` (`[KIND]`).

# Source Mining — Perspective Matrix + Energy Starter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the brief's perspective enum (add Russian, Iranian, Israeli, Indian native feeds) and add a 2–3 feed energy/commodities starter, all live-validated and baked into `RSS_FEEDS`.

**Architecture:** Pure data addition to `RSS_FEEDS` in `brief.py` (a list of `{name, url, category, kind, perspective?, state_funded?}` dicts). A one-off scratchpad script validates each candidate feed live (native RSS first, Google-News `site:` proxy fallback, ≥3-entries gate) before any URL is baked. Two outcome regression tests lock the result.

**Tech Stack:** Python 3, `feedparser`, `requests` (all already used by `brief.py`); `pytest`; `ruff`.

## Global Constraints

- **Run Python via the PowerShell tool**, not Bash (Bash errors "stdin is not a tty"); PowerShell wraps Python stderr/logging as a scary `NativeCommandError` even on success — not a failure. See [[python-via-powershell]].
- **Make git commits via the Bash tool**, not PowerShell (PowerShell prepends a UTF-8 BOM to the commit subject). See CLAUDE.md.
- **Do NOT bulk-edit `brief.py` with PowerShell `-replace`** — use the Edit tool (ruff reflows on save; don't hand-align). See [[formatter-owns-style]].
- **Pre-push gate** = `ruff check` + `ruff format --check` + `pytest` (not pytest alone); stage all reformatted files or CI fails. See [[brief-local-run]].
- `kind` must be in `VALID_KINDS = ("wire", "analyst", "regional", "primary")`.
- `perspective`, when present, must be in `VALID_PERSPECTIVES` (the 10-value enum). Absent = "no vantage claim," never add a NEUTRAL value.
- `state_funded` defaults to `False`; set `True` only for state-controlled outlets.
- New feeds get a `# verified N entries 2026-06-26` comment matching the existing region-native block (brief.py ~lines 189–253).
- Energy feeds carry **no** `perspective`. Bake **3 energy feeds if all validate cleanly, else 2.**
- **Do NOT push.** The deploy is batched by the user with the unpushed #5a "why it matters" lens.

---

### Task 1: Validate candidate feeds live (scratchpad, no commit)

This task produces the verified feed list (URL, native-vs-proxy, entry count) that Tasks 2 and 3 bake. It is exploratory — its only artifact is a scratchpad script and its printed output. **No commit.**

**Files:**
- Create: `<scratchpad>/validate_feeds.py` (scratchpad dir, not the repo)

**Interfaces:**
- Consumes: `brief.fetch_rss(feed_dict)`, `brief.build_google_news_url(domain)` from `brief.py`.
- Produces: a printed table of `(slot, chosen_url, native|proxy, entry_count)` for the implementer to transcribe into Tasks 2–3. A slot that fails both native and proxy (and its alternate) is recorded as DROPPED.

- [ ] **Step 1: Write the validation script**

```python
# <scratchpad>/validate_feeds.py
"""One-off live validator for source-mining candidates. Not shipped.
Native RSS first; on <3 entries, fall back to the Google-News site: proxy.
Prints a table so survivors can be transcribed into RSS_FEEDS."""
import sys
sys.path.insert(0, r"G:\pythonDev\news-brief")
import feedparser
import requests
import brief

MIN_ENTRIES = 3
UA = {"User-Agent": "Mozilla/5.0 (compatible; newsbrief/1.0)"}

# (slot, native_url_or_None, proxy_domain_or_None, when_window)
CANDIDATES = [
    ("russia-official: TASS",        "https://tass.com/rss/v2.xml",                 "tass.com",          "2d"),
    ("russia-indep: Meduza",         "https://meduza.io/rss/en/all",                "meduza.io/en",      "2d"),
    ("iran-official: Press TV",      "https://www.presstv.ir/rss.xml",              "presstv.ir",        "2d"),
    ("iran-indep: IranWire",         "https://iranwire.com/en/feed/",               "iranwire.com/en",   "2d"),
    ("israel: Times of Israel",      "https://www.timesofisrael.com/feed/",         "timesofisrael.com", "2d"),
    ("india: The Hindu",             "https://www.thehindu.com/feeder/default.rss", "thehindu.com",      "2d"),
    ("energy: OilPrice",             "https://oilprice.com/rss/main",               "oilprice.com",      "2d"),
    ("energy: Reuters Commodities",  None,                                          "reuters.com/markets/commodities", "2d"),
    ("energy: EIA Today in Energy",  "https://www.eia.gov/rss/todayinenergy.xml",   "eia.gov",           "7d"),
]


def count(url: str) -> int:
    try:
        resp = requests.get(url, timeout=20, headers=UA)
        resp.raise_for_status()
        return len(feedparser.parse(resp.content).entries)
    except Exception as e:  # noqa: BLE001 - diagnostic script
        print(f"    fetch error: {e}")
        return 0


def main() -> None:
    print(f"{'SLOT':<32} {'SOURCE':<7} {'N':>4}  URL")
    for slot, native, domain, when in CANDIDATES:
        chosen, src, n = None, None, 0
        if native:
            n = count(native)
            if n >= MIN_ENTRIES:
                chosen, src = native, "native"
        if chosen is None and domain:
            # build_google_news_url hardcodes when:2d; build the proxy directly to honor `when`.
            from urllib.parse import quote_plus
            proxy = f"https://news.google.com/rss/search?q={quote_plus(f'when:{when} site:{domain}')}&hl=en-US&gl=US&ceid=US:en"
            n = count(proxy)
            if n >= MIN_ENTRIES:
                chosen, src = proxy, "proxy"
        status = chosen or "DROPPED — try alternate"
        print(f"{slot:<32} {src or '-':<7} {n:>4}  {status}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the validator live**

Run (PowerShell tool): `python "<scratchpad>/validate_feeds.py"`
Expected: a table; each slot shows `native` or `proxy` with `N >= 3`, or `DROPPED`.

- [ ] **Step 3: Resolve any DROPPED slots**

For any slot marked DROPPED, edit `CANDIDATES` to use the alternate from the spec table (Russia→RT / Moscow Times; Iran→Tehran Times / Radio Farda; Israel→Haaretz / Jerusalem Post; India→Indian Express) and re-run Step 2. Energy slots have no alternate — if one drops, the energy count falls to 2 (bake the 2 survivors).

- [ ] **Step 4: Record the survivor list**

Transcribe the printed `(slot, chosen_url, native|proxy, N)` rows into a scratch note for Tasks 2–3. No git commit (script lives in scratchpad).

---

### Task 2: Bake the perspective-matrix feeds + regression tests

**Files:**
- Modify: `brief.py` — append 6 dicts to `RSS_FEEDS` (after the existing region-native block, before the closing `]` at ~line 253).
- Create: `tests/test_sources.py`

**Interfaces:**
- Consumes: the verified URLs from Task 1; `brief.RSS_FEEDS`, `brief.VALID_KINDS`, `brief.VALID_PERSPECTIVES`.
- Produces: `tests/test_sources.py` with `test_perspective_matrix_filled`, `test_rss_feeds_well_formed` (the latter also covers Task 3's energy feeds).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sources.py
"""Outcome tests for the curated RSS_FEEDS registry: the perspective matrix is
complete and every feed dict is well-formed."""
import brief


def test_perspective_matrix_filled():
    sourced = {f.get("perspective") for f in brief.RSS_FEEDS if f.get("perspective")}
    for vantage in ("RUSSIAN", "IRANIAN", "ISRAELI", "INDIAN"):
        assert vantage in sourced, f"{vantage} has no source in RSS_FEEDS"


def test_rss_feeds_well_formed():
    for f in brief.RSS_FEEDS:
        assert f["name"], f"feed missing name: {f!r}"
        assert f["url"], f"feed missing url: {f['name']}"
        assert f["category"], f"feed missing category: {f['name']}"
        assert f.get("kind", "wire") in brief.VALID_KINDS, f"bad kind: {f['name']}"
        p = f.get("perspective")
        assert p is None or p in brief.VALID_PERSPECTIVES, f"bad perspective: {f['name']}"
        assert isinstance(f.get("state_funded", False), bool), f"bad state_funded: {f['name']}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell tool): `python -m pytest tests/test_sources.py -v`
Expected: `test_perspective_matrix_filled` FAILS (RUSSIAN/IRANIAN/ISRAELI/INDIAN not yet sourced). `test_rss_feeds_well_formed` passes (existing feeds are fine).

- [ ] **Step 3: Append the matrix feeds to RSS_FEEDS**

Using the Edit tool, insert these 6 dicts before the closing `]` of `RSS_FEEDS` (use the Task-1 verified URLs; the URLs below are the native candidates — replace any that validated only via proxy with the proxy URL Task 1 printed):

```python
    # ── Perspective-matrix completion (added 2026-06-26, borrow-backlog #4) ────
    {
        "name": "TASS",
        # verified N entries 2026-06-26  <- fill N from Task 1
        "url": "https://tass.com/rss/v2.xml",
        "category": "russia",
        "kind": "regional",
        "perspective": "RUSSIAN",
        "state_funded": True,
    },
    {
        "name": "Meduza",
        # verified N entries 2026-06-26
        "url": "https://meduza.io/rss/en/all",
        "category": "russia",
        "kind": "regional",
        "perspective": "RUSSIAN",
    },
    {
        "name": "Press TV",
        # verified N entries 2026-06-26
        "url": "https://www.presstv.ir/rss.xml",
        "category": "mideast",
        "kind": "regional",
        "perspective": "IRANIAN",
        "state_funded": True,
    },
    {
        "name": "IranWire",
        # verified N entries 2026-06-26
        "url": "https://iranwire.com/en/feed/",
        "category": "mideast",
        "kind": "regional",
        "perspective": "IRANIAN",
    },
    {
        "name": "Times of Israel",
        # verified N entries 2026-06-26
        "url": "https://www.timesofisrael.com/feed/",
        "category": "mideast",
        "kind": "regional",
        "perspective": "ISRAELI",
    },
    {
        "name": "The Hindu",
        # verified N entries 2026-06-26
        "url": "https://www.thehindu.com/feeder/default.rss",
        "category": "india",
        "kind": "regional",
        "perspective": "INDIAN",
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell tool): `python -m pytest tests/test_sources.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the full gate**

Run (PowerShell tool): `python -m ruff check . ; python -m ruff format --check . ; python -m pytest`
Expected: ruff clean, format clean, all tests pass. If `ruff format` reflowed `brief.py`, that's expected — stage it.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_sources.py
git commit -F - <<'EOF'
feat(brief): complete perspective matrix — add Russian/Iranian/Israeli/Indian feeds

Backlog #4. Fills the four vantages defined in VALID_PERSPECTIVES but
never sourced: TASS+Meduza (RUSSIAN, state+independent), Press TV+IranWire
(IRANIAN, state+independent), Times of Israel (ISRAELI), The Hindu (INDIAN).
Live-validated (>=3 entries, native or Google-News site: proxy) before baking.
New regression tests lock the matrix complete and assert feed well-formedness.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Bake the energy/commodities starter

**Files:**
- Modify: `brief.py` — append 2–3 dicts to `RSS_FEEDS` after the matrix block.
- Modify: `tests/test_sources.py` — add `test_energy_category_present`.

**Interfaces:**
- Consumes: the verified energy URLs from Task 1; `brief.RSS_FEEDS`.
- Produces: `test_energy_category_present` in `tests/test_sources.py`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sources.py`:

```python
def test_energy_category_present():
    energy = [f for f in brief.RSS_FEEDS if f["category"] == "energy"]
    assert len(energy) >= 2, "energy starter not added"
    assert all(f.get("perspective") is None for f in energy), "energy feeds carry no vantage"
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell tool): `python -m pytest tests/test_sources.py::test_energy_category_present -v`
Expected: FAIL (no energy-category feeds yet).

- [ ] **Step 3: Append the energy feeds to RSS_FEEDS**

Insert after the matrix block (bake only the slots that validated in Task 1 — 3 if all passed, else 2; replace native URLs with proxy URLs where Task 1 used the proxy):

```python
    # ── Energy / commodities starter (added 2026-06-26, borrow-backlog #4) ─────
    {
        "name": "OilPrice.com",
        # verified N entries 2026-06-26
        "url": "https://oilprice.com/rss/main",
        "category": "energy",
        "kind": "wire",
    },
    {
        "name": "Reuters Commodities",
        # verified N entries 2026-06-26 (Google-News site: proxy)
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fmarkets%2Fcommodities&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "energy",
        "kind": "wire",
    },
    {
        "name": "EIA Today in Energy",
        # verified N entries 2026-06-26
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "category": "energy",
        "kind": "primary",
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run (PowerShell tool): `python -m pytest tests/test_sources.py::test_energy_category_present -v`
Expected: PASS.

- [ ] **Step 5: Run the full gate**

Run (PowerShell tool): `python -m ruff check . ; python -m ruff format --check . ; python -m pytest`
Expected: ruff clean, format clean, all tests pass.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_sources.py
git commit -F - <<'EOF'
feat(brief): add energy/commodities starter feeds

Backlog #4. Adds OilPrice.com, Reuters Commodities (Google-News proxy),
and EIA Today in Energy under a new "energy" category — no vantage tag
(thematic, not national). Live-validated before baking. Closes the
energy gap flagged against the reader's portfolio exposure.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review

**Spec coverage:**
- Matrix completion (Russian/Iranian/Israeli/Indian) → Task 2. ✅
- Both state+independent for Russia/Iran; one feed for Israel/India → candidate dicts in Task 2. ✅
- Energy starter (2–3, no perspective) → Task 3. ✅
- Validate-then-bake, native→proxy, ≥3-entries gate → Task 1. ✅
- WESTERN left unused → no task adds it. ✅
- `# verified N entries 2026-06-26` comments → Tasks 2–3 step 3. ✅
- Matrix-completion + structural tests → Task 2; energy test → Task 3. ✅
- RADAR as discovery cross-check only (not load-bearing) → reflected: canonical URLs hardcoded in Task 1, RADAR not imported anywhere. ✅
- Do-not-push (batched deploy) → Global Constraints. ✅

**Placeholder scan:** The only intentional fill-ins are the `N` in `# verified N entries` comments and URL swaps for proxy-validated slots — both resolved by Task 1's live output, which is the correct and unavoidable source for those exact values. No vague "add error handling"-style placeholders.

**Type consistency:** `RSS_FEEDS` dict shape, `VALID_KINDS`, `VALID_PERSPECTIVES`, `fetch_rss`, `build_google_news_url` all match `brief.py`. Test function names are unique and consistent across Tasks 2–3.

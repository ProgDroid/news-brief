# Brief Sources & Edge-Latency Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily brief less "same-y" and less "priced-in" by replacing the rigid 5-country template with a user-controlled pinned-hybrid structure, diversifying sources toward region-native/forward-looking outlets tagged by `kind`, injecting pin-derived market-move context, and tilting the prompt forward.

**Architecture:** All changes live in `brief.py` (sources config, prompts, Telegram commands, feedback) and `trading.py` (market-pulse data, which reuses the existing Stooq/Kraken price + volume + book plumbing). The one-way import chain `common ← trading ← brief` is preserved: `build_market_pulse` lives in `trading.py`; `brief.py` calls it. No schema changes to `@@@SIGNALS@@@`, the weekly summary, or the paper/monitor trading logic.

**Tech Stack:** Python 3.11, `requests`, `feedparser`, `pytest`, `ruff`. Run Python/pytest/ruff via the **PowerShell** tool (Bash errors "stdin is not a tty"); make git **commits via the Bash** tool (PowerShell prepends a U+FEFF BOM to commit subjects).

**Spec:** `docs/superpowers/specs/2026-06-14-brief-sources-edge-latency-redesign-design.md`

**Pre-push gate (run before any push, per [[brief-local-run]]):**
`ruff check . ; ruff format --check . ; pytest -q` — all three must pass, and any file ruff reformats must be staged or CI fails.

**Testing convention:** when a test monkeypatches behaviour, patch it on the module whose function is *under test* (e.g. `brief.telegram_send`, `brief.load_book`), because `brief`'s functions resolve their own module-level names bound from `common`/`trading` at import. When a test just calls a pure function, either namespace works.

---

## File Structure

- `brief.py`
  - **Config (top):** `DEFAULT_PINS` constant; expanded `RSS_FEEDS` with a `kind` field; new region-native feeds.
  - **Feedback:** `resolved_pins(fb)` helper; `feedback_summary` gains a `Pinned:` line.
  - **Telegram:** `/pin`, `/unpin` handlers in `_handle_telegram_update`; `HELP_TEXT` updated.
  - **Prompts:** `SYSTEM_PROMPT` reframed; `build_daily_prompt` rewritten (pinned-hybrid template + `market_block` param + `kind`-weighting + forward tilt).
  - **Pipeline:** `mode_submit` builds market block + pins and passes them in; `fetch_rss` renders `kind`.
- `trading.py`
  - `fetch_daily_move(asset_class, instrument)` — open→last % move, single fetch.
  - `MARKET_SPINE`, `PIN_INSTRUMENTS` config; `build_market_pulse(pins)` — assembles the "what moved & why" block.
- `tests/test_commands.py` — `/pin`, `/unpin`, `/status` pins, default seeding.
- `tests/test_signals.py` (or `tests/test_delivery_and_state.py`) — `build_daily_prompt` template assertions.
- `tests/test_trading.py` — `fetch_daily_move`, `build_market_pulse`.
- `README.md`, `.env.example` — document new commands; no new env vars expected.

---

## Task 1: Pin resolution helper + default constant

**Files:**
- Modify: `brief.py` (add `DEFAULT_PINS` near `TOPICS`; add `resolved_pins` near `load_feedback` ~line 199)
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py
import brief


def test_resolved_pins_defaults_when_absent():
    # A feedback dict with no "pin" key resolves to the default five.
    assert brief.resolved_pins({"focus": [], "mute": [], "notes": []}) == [
        "ukraine",
        "iran",
        "korea",
        "japan",
        "china",
    ]


def test_resolved_pins_uses_explicit_list():
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "taiwan"]}
    assert brief.resolved_pins(fb) == ["china", "taiwan"]


def test_resolved_pins_empty_list_is_respected():
    # An explicit empty list means "no pins" — distinct from "key absent".
    fb = {"focus": [], "mute": [], "notes": [], "pin": []}
    assert brief.resolved_pins(fb) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_commands.py::test_resolved_pins_defaults_when_absent -q`
Expected: FAIL — `AttributeError: module 'brief' has no attribute 'resolved_pins'`.

- [ ] **Step 3: Add the constant and helper**

Add the constant just above `TOPICS` (after `WEB_SOURCES`, ~line 162):

```python
# Default pinned topics — always rendered (at least a one-liner) when the
# reader has not customised the pin set via /pin /unpin. Matches the legacy
# hardcoded country sections so day-one behaviour is unchanged.
DEFAULT_PINS = ["ukraine", "iran", "korea", "japan", "china"]
```

Add the helper just below `save_feedback` (~line 204):

```python
def resolved_pins(fb: dict) -> list[str]:
    """The active pin set: an explicit fb['pin'] list (even empty) wins;
    a missing key resolves to DEFAULT_PINS. Distinguishing absent-vs-empty
    lets /reset restore the defaults by dropping the key."""
    pins = fb.get("pin")
    return list(pins) if pins is not None else list(DEFAULT_PINS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_commands.py -q -k resolved_pins`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit** (Bash tool)

```bash
git add brief.py tests/test_commands.py
git commit -m "feat: resolved_pins helper + DEFAULT_PINS constant"
```

---

## Task 2: /pin and /unpin Telegram commands

**Files:**
- Modify: `brief.py` `_handle_telegram_update` (insert after the `/unwatch` branch, ~line 437; before `/positions`)
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py
import brief


def _update(text):
    return {"message": {"text": text, "chat": {"id": brief.TELEGRAM_CHAT_ID}}}


def test_pin_seeds_defaults_then_adds(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda m: sent.append(m))
    fb = {"focus": [], "mute": [], "notes": []}
    fb = brief._handle_telegram_update(_update("/pin taiwan"), fb)
    # First pin materialises the defaults, then appends the new topic.
    assert fb["pin"] == ["ukraine", "iran", "korea", "japan", "china", "taiwan"]
    assert "taiwan" in sent[-1].lower()


def test_pin_lowercases_and_dedupes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china"]}
    fb = brief._handle_telegram_update(_update("/pin China"), fb)
    assert fb["pin"] == ["china"]  # case-folded, no duplicate


def test_unpin_removes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "japan"]}
    fb = brief._handle_telegram_update(_update("/unpin japan"), fb)
    assert fb["pin"] == ["china"]


def test_unpin_default_member_materialises_then_removes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": []}  # no pin key → defaults active
    fb = brief._handle_telegram_update(_update("/unpin korea"), fb)
    assert "korea" not in fb["pin"]
    assert fb["pin"] == ["ukraine", "iran", "japan", "china"]
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_commands.py::test_pin_seeds_defaults_then_adds -q`
Expected: FAIL — pin key not created (handler falls through to "Unknown command").

- [ ] **Step 3: Implement the handlers**

Insert immediately after the `/unwatch` branch (after line 437, before `elif text == "/positions":`):

```python
    elif text.startswith("/pin "):
        topic = text[5:].strip().lower()
        fb.setdefault("pin", list(DEFAULT_PINS))
        if topic and topic not in fb["pin"]:
            fb["pin"].append(topic)
        telegram_send(
            f"📌 Pinned: <b>{html.escape(topic)}</b> — always shown.\n\n"
            f"{feedback_summary(fb)}"
        )

    elif text.startswith("/unpin "):
        topic = text[7:].strip().lower()
        fb.setdefault("pin", list(DEFAULT_PINS))
        if topic in fb["pin"]:
            fb["pin"].remove(topic)
            telegram_send(
                f"📍 Unpinned: <b>{html.escape(topic)}</b>.\n\n{feedback_summary(fb)}"
            )
        else:
            telegram_send(f"<b>{html.escape(topic)}</b> is not pinned.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_commands.py -q -k "pin"`
Expected: PASS (all pin/unpin tests).

- [ ] **Step 5: Commit** (Bash tool)

```bash
git add brief.py tests/test_commands.py
git commit -m "feat: /pin and /unpin Telegram commands"
```

---

## Task 3: Surface pins in /status + /help + README

**Files:**
- Modify: `brief.py` `feedback_summary` (~line 207); `HELP_TEXT` (~line 223)
- Modify: `README.md` (commands table)
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py
import brief


def test_feedback_summary_lists_pins():
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "iran"]}
    out = brief.feedback_summary(fb)
    assert "Pinned:" in out
    assert "china" in out and "iran" in out


def test_feedback_summary_shows_default_pins_when_absent():
    out = brief.feedback_summary({"focus": [], "mute": [], "notes": []})
    assert "Pinned:" in out
    assert "ukraine" in out  # defaults surfaced, not hidden
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_commands.py::test_feedback_summary_lists_pins -q`
Expected: FAIL — `"Pinned:"` not in output.

- [ ] **Step 3: Add the Pinned line to `feedback_summary`**

In `feedback_summary` (after the `notes` block, before the final `return`), add:

```python
    pins = resolved_pins(fb)
    if pins:
        lines.append("Pinned: " + ", ".join(html.escape(p) for p in pins))
```

Then update `HELP_TEXT` — insert after the `/unwatch` entry and before `/positions`:

```
/pin [topic]
  Always show a topic, even when quiet (at least a one-liner).
  e.g. <code>/pin taiwan</code>

/unpin [topic]
  Stop forcing a topic; it becomes dynamic again.
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_commands.py -q`
Expected: PASS (all command tests).

- [ ] **Step 5: Update README**

In `README.md`, add to the Telegram commands table (near `/watch`/`/unwatch`):

```
| `/pin <topic>` | Always show a topic, even when quiet (one-liner minimum). Default pins: ukraine, iran, korea, japan, china. |
| `/unpin <topic>` | Make a topic dynamic again. |
```

And add a sentence: "Pins are listed by `/status`. `/reset` restores the default pin set."

- [ ] **Step 6: Commit** (Bash tool)

```bash
git add brief.py tests/test_commands.py README.md
git commit -m "feat: surface pins in /status and /help; document in README"
```

---

## Task 4: Expand sources + add `kind` field

**Files:**
- Modify: `brief.py` `RSS_FEEDS` (~line 100), `WEB_SOURCES` (~line 155), `fetch_rss` (~line 533)
- Test: `tests/test_signals.py` (feed-config + fetch_rss header assertions)

> **Planning note — verify feeds at implementation:** before committing, fetch each new feed URL once and confirm it returns entries. Use native RSS where available; fall back to the Google News `site:` proxy
> (`https://news.google.com/rss/search?q=when:2d+site%3A<domain>&hl=en-US&gl=US&ceid=US%3Aen`) where not; drop any source that yields nothing and note it in the commit message. Candidate native feeds to try first:
> Al Jazeera `https://www.aljazeera.com/xml/rss/all.xml`; Kyiv Independent `https://kyivindependent.com/feed`; Yonhap (English) — `site:` proxy on `en.yna.co.kr`; 38 North `https://www.38north.org/feed/`; ISW — `site:` proxy on `understandingwar.org`; NHK World — `site:` proxy on `www3.nhk.or.jp`; SCMP `https://www.scmp.com/rss/91/feed`; BOJ — `site:` proxy on `boj.or.jp` statements.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py
import brief


def test_every_feed_has_a_kind():
    valid = {"wire", "analyst", "regional", "primary"}
    for f in brief.RSS_FEEDS:
        assert f.get("kind") in valid, f"{f['name']} missing/invalid kind"
    for s in brief.WEB_SOURCES:
        assert s.get("kind") in valid, f"{s['name']} missing/invalid kind"


def test_sources_diversified_beyond_reuters():
    names = " ".join(f["name"] for f in brief.RSS_FEEDS).lower()
    # At least the region-native additions are present.
    for needle in ("kyiv", "yonhap", "scmp", "nhk", "38 north", "isw", "al jazeera"):
        assert needle in names, f"expected source containing '{needle}'"


def test_fetch_rss_header_includes_kind(monkeypatch):
    sample = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>Hello</title><description>Body</description>"
        b"<pubDate>Mon, 01 Jan 2026</pubDate></item></channel></rss>"
    )

    class _Resp:
        content = sample
        ok = True

        def raise_for_status(self):
            pass

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp())
    feed = {"name": "Test Wire", "url": "http://x", "category": "geo", "kind": "wire"}
    out = brief.fetch_rss(feed)
    assert "WIRE" in out  # kind surfaced so the model can weight it
    assert "Test Wire" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_signals.py -q -k "kind or diversified"`
Expected: FAIL — existing feeds lack `kind`; new sources absent; header lacks kind.

- [ ] **Step 3: Add `kind` to existing feeds + the new sources**

Tag every existing `RSS_FEEDS` entry with `kind`: Reuters Markets/World → `"wire"`; Sinica, Un-Diplomatic, Observing Japan, Pinecone, Intersubjectively Transmissible, Marko Papic, Jacob Shapiro → `"analyst"`. Tag the BCA `WEB_SOURCES` entry → `"regional"`. Then append the new region-native feeds (use verified URLs from the planning note — `site:` proxy shown where native RSS is uncertain):

```python
    {
        "name": "Kyiv Independent",
        "url": "https://kyivindependent.com/feed",
        "category": "ukraine",
        "kind": "regional",
    },
    {
        "name": "ISW Daily Assessment",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aunderstandingwar.org&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "ukraine",
        "kind": "primary",
    },
    {
        "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "category": "geo",
        "kind": "regional",
    },
    {
        "name": "Yonhap (English)",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aen.yna.co.kr&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "korea",
        "kind": "regional",
    },
    {
        "name": "38 North",
        "url": "https://www.38north.org/feed/",
        "category": "korea",
        "kind": "primary",
    },
    {
        "name": "NHK World",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Awww3.nhk.or.jp%2Fnhkworld&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "japan",
        "kind": "regional",
    },
    {
        "name": "BOJ Statements",
        "url": "https://news.google.com/rss/search?q=when:7d+site%3Aboj.or.jp&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "japan",
        "kind": "primary",
    },
    {
        "name": "SCMP",
        "url": "https://www.scmp.com/rss/91/feed",
        "category": "china",
        "kind": "regional",
    },
```

- [ ] **Step 4: Render `kind` in `fetch_rss`**

In `fetch_rss`, change the section header line (currently `lines = [f"\n### {feed['name']} ({feed['category'].upper()})"]`) to include kind:

```python
        kind = feed.get("kind", "wire").upper()
        lines = [f"\n### {feed['name']} [{kind}] ({feed['category'].upper()})"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_signals.py -q`
Expected: PASS. (If a planning-verified feed was dropped, adjust `test_sources_diversified_beyond_reuters` to match what shipped.)

- [ ] **Step 6: Commit** (Bash tool)

```bash
git add brief.py tests/test_signals.py
git commit -m "feat: region-native sources + kind field on all feeds"
```

---

## Task 5: `fetch_daily_move` in trading.py

**Files:**
- Modify: `trading.py` (add near `fetch_stooq_volume`, ~line 184)
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading.py
import trading


def test_stooq_daily_move_open_to_last(monkeypatch):
    # CSV: Symbol,Date,Time,Open,High,Low,Close,Volume — open 100, close 110 → +10%
    csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-06-13,22:00:00,100,111,99,110,5000"

    class _R:
        text = csv

        def raise_for_status(self):
            pass

    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _R())
    assert trading.fetch_daily_move("equity", "aapl.us") == 10.0


def test_kraken_daily_move(monkeypatch):
    # open 200, last 190 → -5%
    data = {"error": [], "result": {"XXBTZUSD": {"o": "200", "c": ["190", "0.1"]}}}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _R())
    assert trading.fetch_daily_move("crypto", "XBTUSD") == -5.0


def test_daily_move_none_on_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr(trading.requests, "get", _boom)
    assert trading.fetch_daily_move("equity", "aapl.us") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_trading.py -q -k daily_move`
Expected: FAIL — `module 'trading' has no attribute 'fetch_daily_move'`.

- [ ] **Step 3: Implement `fetch_daily_move`**

Add after `fetch_stooq_volume`:

```python
def fetch_daily_move(asset_class: str, instrument: str) -> float | None:
    """Intraday percent move (open → last) for one instrument, single fetch.

    Equity → Stooq light quote (Open=col3, Close=col6); crypto → Kraken Ticker
    (o=today's open, c[0]=last). Returns the percent change rounded to 2dp, or
    None on any failure / non-positive open — callers render '—', never guess.
    """
    if asset_class == "crypto":
        return _kraken_daily_move(instrument)
    return _stooq_daily_move(instrument)


def _stooq_daily_move(stooq_symbol: str) -> float | None:
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq move fetch failed for {stooq_symbol}: {e}")
        return None
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")  # Symbol,Date,Time,Open,High,Low,Close,Volume
    if len(cols) < 7 or cols[3] in ("N/D", "") or cols[6] in ("N/D", ""):
        return None
    try:
        open_, close = float(cols[3]), float(cols[6])
    except ValueError:
        return None
    if open_ <= 0:
        return None
    return round((close - open_) / open_ * 100, 2)


def _kraken_daily_move(pair: str) -> float | None:
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken move fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        return None
    result = data.get("result") or {}
    if not result:
        return None
    entry = next(iter(result.values()))
    try:
        open_, last = float(entry["o"]), float(entry["c"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if open_ <= 0:
        return None
    return round((last - open_) / open_ * 100, 2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_trading.py -q -k daily_move`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit** (Bash tool)

```bash
git add trading.py tests/test_trading.py
git commit -m "feat: fetch_daily_move (open->last %) for equity and crypto"
```

---

## Task 6: `MARKET_SPINE` / `PIN_INSTRUMENTS` config + `build_market_pulse`

**Files:**
- Modify: `trading.py` (add config near the top constants; add `build_market_pulse` near `run_volume_monitor`, ~line 881)
- Test: `tests/test_trading.py`

> **Planning note — verify symbols at implementation:** before committing, call `fetch_daily_move` on each spine/pin symbol once and confirm it returns a number; drop any symbol that returns None (Stooq index/FX/commodity tickers vary) and note it in the commit. Starting candidates: spine `^spx`, `dx.f` (or `^dxy`), `xauusd`, BTC via Kraken `XBTUSD`; pins iran→`cb.f` (Brent), japan→`usdjpy`+`^nkx`, china→`^hsi`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading.py
import trading


def test_build_market_pulse_includes_spine_and_pinned(monkeypatch):
    # Deterministic moves; pin "iran" should pull its mapped instrument(s).
    monkeypatch.setattr(trading, "fetch_daily_move", lambda ac, inst: 1.5)
    monkeypatch.setattr(trading, "_watched_instruments", lambda: [])
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(trading, "run_volume_monitor_readonly", lambda: [], raising=False)
    monkeypatch.setattr(
        trading, "_load_json_or", lambda *a, **k: {}
    )  # empty volume history
    block = trading.build_market_pulse(["iran"])
    assert "MARKET PULSE" in block or "WHAT MOVED" in block
    assert "+1.5%" in block
    # Spine label always present
    assert "S&P 500" in block
    # Pinned-iran instrument present
    assert "Brent" in block


def test_build_market_pulse_skips_unresolvable(monkeypatch):
    monkeypatch.setattr(trading, "fetch_daily_move", lambda ac, inst: None)
    monkeypatch.setattr(trading, "_watched_instruments", lambda: [])
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(trading, "_load_json_or", lambda *a, **k: {})
    block = trading.build_market_pulse([])
    # No move resolved → each line shows the em-dash sentinel, never raises.
    assert "—" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_trading.py -q -k market_pulse`
Expected: FAIL — `build_market_pulse` undefined.

- [ ] **Step 3: Add config + implementation**

Add config near the top constants (after the `VOL_*` knobs / before functions):

```python
# Market-pulse instruments. Each entry: (label, asset_class, instrument).
# Tier 1 — macro spine, always fetched (universal risk-on/off pulse).
MARKET_SPINE = [
    ("S&P 500", "equity", "^spx"),
    ("US Dollar (DXY)", "equity", "dx.f"),
    ("Gold", "equity", "xauusd"),
    ("Bitcoin", "crypto", "XBTUSD"),
]
# Tier 2 — pin-derived: only fetched for currently pinned topics. Gaps allowed
# (a pin with no clean instrument simply contributes no market line).
PIN_INSTRUMENTS = {
    "iran": [("Brent crude", "equity", "cb.f")],
    "japan": [("USD/JPY", "equity", "usdjpy"), ("Nikkei 225", "equity", "^nkx")],
    "china": [("Hang Seng", "equity", "^hsi")],
}
```

Add `build_market_pulse` after `run_volume_monitor`:

```python
def build_market_pulse(pins: list[str]) -> str:
    """Assemble the 'what moved & why' block: macro spine + pin-derived
    instruments + open-position moves + current volume anomalies. Pure data —
    the model supplies the 'why'. Best-effort: a failed fetch renders '—' and
    never raises into the brief pipeline.
    """
    # Tier 1 + Tier 2, deduped by (asset_class, instrument), preserving order.
    seen: set[tuple[str, str]] = set()
    instruments: list[tuple[str, str, str]] = []
    for label, ac, inst in MARKET_SPINE:
        if (ac, inst) not in seen:
            seen.add((ac, inst))
            instruments.append((label, ac, inst))
    for topic in pins:
        for label, ac, inst in PIN_INSTRUMENTS.get(topic, []):
            if (ac, inst) not in seen:
                seen.add((ac, inst))
                instruments.append((label, ac, inst))

    def _fmt(move: float | None) -> str:
        return f"{move:+.1f}%" if move is not None else "—"

    lines = ["### MARKET PULSE — WHAT MOVED (open→last)"]
    for label, ac, inst in instruments:
        try:
            move = fetch_daily_move(ac, inst)
        except Exception as e:  # never let one symbol kill the block
            log.warning(f"Market pulse move failed for {inst}: {e}")
            move = None
        lines.append(f"- {label}: {_fmt(move)}")

    # Open-position moves (instrument already known; reuse the same pricer).
    pos_lines = []
    for p in load_book().get("positions", []):
        if p.get("status") != "open":
            continue
        inst = p.get("instrument")
        if not inst:
            continue
        ac = p.get("asset_class", "equity")
        try:
            move = fetch_daily_move(ac, inst)
        except Exception:
            move = None
        pos_lines.append(f"- {p.get('ticker', inst)}: {_fmt(move)}")
    if pos_lines:
        lines.append("\n### YOUR POSITIONS — TODAY'S MOVE")
        lines.extend(pos_lines)

    # Volume anomalies from the monitor history (read-only; no fetch here).
    hist = _load_json_or(VOLUME_HISTORY_FILE, {})
    anomalies = []
    for key, entry in hist.items():
        ts = entry.get("last_alert_ts")
        if ts:
            anomalies.append(f"- {key}: recent volume spike ({ts[:10]})")
    if anomalies:
        lines.append("\n### VOLUME ANOMALIES (last alerts)")
        lines.extend(anomalies)

    return "\n".join(lines)
```

> Note: the anomaly tier reads `last_alert_ts` recorded by `run_volume_monitor` — no network call, so the brief never re-sweeps. The test patches `_load_json_or` to `{}` (no anomalies).

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_trading.py -q -k market_pulse`
Expected: PASS (2 passed). (Remove the stray `run_volume_monitor_readonly` patch from Step 1's first test if it is not referenced — it is a no-op `raising=False` guard.)

- [ ] **Step 5: Commit** (Bash tool)

```bash
git add trading.py tests/test_trading.py
git commit -m "feat: build_market_pulse (spine + pin-derived + positions + anomalies)"
```

---

## Task 7: Rewrite `build_daily_prompt` (pinned-hybrid template + market block + forward tilt)

**Files:**
- Modify: `brief.py` `build_daily_prompt` (~line 826–948)
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py
import brief


def _base_kwargs():
    return dict(
        feed_content="(feeds)",
        web_content="(web)",
        chroma_context="(chroma)",
        yesterday_brief="",
        weekly_summary="",
        fb={"focus": [], "mute": [], "notes": [], "pin": ["iran", "japan"]},
        portfolio="",
    )


def test_prompt_includes_pinned_override_line():
    out = brief.build_daily_prompt(**_base_kwargs())
    assert "PINNED" in out
    assert "iran" in out and "japan" in out


def test_prompt_has_fixed_spine_and_dynamic_instruction():
    out = brief.build_daily_prompt(**_base_kwargs())
    assert "TOP STORIES" in out
    assert "MARKET PULSE" in out
    assert "WATCH" in out
    assert "@@@SIGNALS@@@" in out
    # Dynamic-middle instruction present (model picks significant unpinned topics)
    assert "significan" in out.lower()


def test_prompt_renders_market_block_when_supplied():
    out = brief.build_daily_prompt(market_block="### MARKET PULSE\n- S&P 500: +0.5%", **_base_kwargs())
    assert "S&P 500: +0.5%" in out


def test_prompt_defaults_pins_when_key_absent():
    kw = _base_kwargs()
    kw["fb"] = {"focus": [], "mute": [], "notes": []}
    out = brief.build_daily_prompt(**kw)
    assert "ukraine" in out  # default pins surfaced
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_signals.py -q -k "prompt"`
Expected: FAIL — no `PINNED` line / no `market_block` param.

- [ ] **Step 3: Rewrite `build_daily_prompt`**

Change the signature to add `market_block`:

```python
def build_daily_prompt(
    feed_content: str,
    web_content: str,
    chroma_context: str,
    yesterday_brief: str,
    weekly_summary: str,
    fb: dict,
    portfolio: str,
    perf_block: str = "",
    market_block: str = "",
) -> str:
```

After the existing `fb_lines` block (focus/mute/notes), add a pinned line:

```python
    pins = resolved_pins(fb)
    if pins:
        fb_lines.append(
            "PINNED — always include at least a one-line pulse for, in this order: "
            + ", ".join(pins)
        )
```

Replace the `## RSS / WEB SOURCE MATERIAL` … `## WEB SEARCH TOPICS` region and the `## OUTPUT FORMAT` region of the returned f-string. Insert a market block section after the podcast context, add a `kind`-weighting note, and replace the fixed 5-country output template with the pinned-hybrid template:

```python
    market_section = (
        f"\n## MARKET PULSE (what moved — you supply the why)\n{market_block}\n"
        if market_block
        else ""
    )

    return f"""Today is {today} (UTC). Produce the morning brief.
{feedback_block}

## SOURCE MATERIAL
Each source is tagged [WIRE|ANALYST|REGIONAL|PRIMARY]. Treat WIRE as the record of
what happened (anchor facts here), but lead your interpretation with ANALYST /
REGIONAL / PRIMARY material and the market action below. Compress recap; do not
echo a headline the market has already priced.
{feed_content}
{web_content}

## PODCAST ANALYST CONTEXT
Relevant excerpts from an indexed archive of geopolitical/macro podcast episodes.
Use for forward-looking analytical framing. Cite the show name if drawing on a specific insight.
{chroma_context}
{market_section}
## WEB SEARCH TOPICS
Search for current developments on each before writing. Anchor facts on Reuters; for
local colour and forward framing, prefer the region-native sources above.
{search_list}
{yesterday_block}{weekly_block}{portfolio_block}
{perf_block}

## OUTPUT FORMAT

Telegram HTML only. Allowed tags: <b>, <i>, <code>, <a href="...">
Use <b> for section headings. Bullets with •. No markdown. No # headers. No asterisks.
Output only the HTML — no preamble, no sign-off, no code fences.

Structure — a fixed spine with a dynamic middle:

<b>🌍 TOP STORIES</b>
- [3–5 bullets, only genuinely significant developments]

<b>📈 MARKET PULSE — WHAT MOVED</b>
[2–4 bullets: the notable moves above and the likely why. Flag any move NOT explained
by today's news as a potential early signal. Omit only if no market data was provided.]

[DYNAMIC TOPIC SECTIONS — your discretion:
• Render a section for EVERY pinned topic listed under READER OVERRIDES, even if quiet
  (a quiet pin collapses to: "No significant change — one sentence"). Never drop a pin.
• ALSO add a section for any UNPINNED topic that is materially significant today.
• Order by significance. Use a <b>flag + NAME</b> heading per section.]

<b>📊 MACRO SIGNAL</b>
[paragraph if material; omit entirely if nothing significant]

<b>📌 POSITION SIGNALS</b>
- [news that confirms or challenges a held position or thesis — name the ticker/thesis and
  the signal direction. Omit the section entirely if nothing is materially relevant.]

<b>👁 WATCH / FORWARD</b>
- [2–4 forward-looking things to monitor in the next 24–72h that could move markets]

After the WATCH / FORWARD section and a blank line, output the delimiter token below on its
own line, exactly as written — it is a literal parsing marker, NOT a section divider, so
reproduce it verbatim and do not shorten, restyle, or drop it:
@@@SIGNALS@@@
Then output a JSON array (and nothing else after it) capturing any position-relevant signals.
Empty array if none. Schema:
[
  {{
    "ticker": "the primary listing symbol — e.g. SHEL or BP for equities, BTC or ETH for crypto; null only for macro-level signals with no single tradable instrument",
    "asset_class": "equity | crypto — equity for stocks/ETFs, crypto for major coins; default to equity if unsure",
    "topic": "short topic label, e.g. hormuz-disruption",
    "direction": "bullish | bearish | neutral",
    "thesis_ref": "the held thesis this bears on, or null",
    "confidence": "low | medium | high",
    "rationale": "one sentence, no more",
    "provenance": "which source/feed/search this came from"
  }}
]

Keep the entire brief under 600 words."""
```

> The `@@@SIGNALS@@@` marker, the JSON schema, and the `<600 words` budget are unchanged from the original (per spec non-goals). Only the human-readable section template above the marker changed.

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `pytest tests/test_signals.py -q`
Expected: PASS (including the pre-existing signals-parsing tests, which are unaffected).

- [ ] **Step 5: Commit** (Bash tool)

```bash
git add brief.py tests/test_signals.py
git commit -m "feat: pinned-hybrid brief template + market block + forward tilt"
```

---

## Task 8: Reframe `SYSTEM_PROMPT` + wire pipeline in `mode_submit`

**Files:**
- Modify: `brief.py` `SYSTEM_PROMPT` (~line 810); `mode_submit` (~line 1366–1398)
- Test: `tests/test_signals.py` (system prompt assertion); existing `mode_submit` behaviour covered by the suite

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py
import brief


def test_system_prompt_is_forward_tilted():
    sp = brief.SYSTEM_PROMPT.lower()
    # Reuters still anchors facts...
    assert "reuters" in sp
    # ...but the forward/anticipatory tilt is explicit.
    assert "forward" in sp or "anticipat" in sp
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `pytest tests/test_signals.py::test_system_prompt_is_forward_tilted -q`
Expected: FAIL — no forward/anticipatory wording.

- [ ] **Step 3: Reframe `SYSTEM_PROMPT`**

Replace the last two instruction lines of `SYSTEM_PROMPT`:

```python
SYSTEM_PROMPT = """You are a senior geopolitical and macroeconomic analyst producing a concise daily briefing for an investor.

The reader:
- Is Portuguese, based in the UK
- Has financial exposure to multiple countries and regions
- Tracks geopolitics as a leading indicator for markets, not as an end in itself
- Is familiar with constraint-based analysis (Papic/BCA style)
- Does not need hedging language or excessive caveats — be direct
- Prefers Reuters as a primary news source for facts
- Trades equities and major cryptocurrencies (BTC, ETH, and other large-cap coins); surface directional crypto calls the same way as equities when news warrants

Your job is to synthesise the provided source material into a structured morning brief.
Anchor facts on Reuters/wire sources, but LEAD your interpretation with forward-looking,
anticipatory material — regional analysts, primary statements, podcast framing, and the
market action provided — over backward-looking wire recap. Use the web search tool to fill
gaps on the listed topics. Do not echo headlines the market has already priced. Do not pad
or repeat. If nothing significant happened on a topic, say so in one line."""
```

- [ ] **Step 4: Wire `build_market_pulse` + pins into `mode_submit`**

In `mode_submit`, after `perf_block = performance_prompt_block(load_book())` and before `prompt = build_daily_prompt(`:

```python
    market_block = build_market_pulse(resolved_pins(fb))
    log.info(f"Market pulse: {len(market_block)} chars")
```

Then pass it to the call:

```python
    prompt = build_daily_prompt(
        feed_content,
        web_content,
        chroma_context,
        yesterday_brief,
        weekly_summary,
        fb,
        portfolio,
        perf_block,
        market_block,
    )
```

Ensure `build_market_pulse` is imported from `trading` at the top of `brief.py` (add it to the existing `from trading import (...)` block).

- [ ] **Step 5: Run tests to verify they pass**

Run (PowerShell): `pytest -q`
Expected: PASS (full suite green — the new wiring does not break existing `mode_submit` tests).

- [ ] **Step 6: Commit** (Bash tool)

```bash
git add brief.py tests/test_signals.py
git commit -m "feat: forward-tilted SYSTEM_PROMPT + wire market pulse into mode_submit"
```

---

## Task 9: Docs + final pre-push gate

**Files:**
- Modify: `README.md` (sources section, market-pulse mention), `.env.example` (only if a new knob was added — none expected)
- No new tests.

- [ ] **Step 1: Update README sources/feature notes**

Document in `README.md`: the expanded source list (region-native + `kind` tags), the new "MARKET PULSE" brief section, and that the market pulse follows pinned topics. Confirm `.env.example` needs no new variables (the redesign adds no env config — `MARKET_SPINE`/`PIN_INSTRUMENTS` are code constants).

- [ ] **Step 2: Run the full pre-push gate** (PowerShell)

Run: `ruff check . ; ruff format --check . ; pytest -q`
Expected: all three pass. If `ruff format` reports files needing formatting, run `ruff format .` and stage the result (CI checks `--check`).

- [ ] **Step 3: Commit + done** (Bash tool)

```bash
git add README.md brief.py trading.py
git commit -m "docs: document region-native sources + market pulse"
```

- [ ] **Step 4: Update project memory**

Append a one-line pointer in `MEMORY.md` and write a memory file capturing that the sources/edge-latency redesign shipped (what changed: pinned-hybrid template, `/pin` `/unpin`, `kind`-tagged region-native sources, `build_market_pulse`, forward-tilted prompt) and the **revisit trigger** (after ~2 weeks of briefs, check `validation` hit_rate/mean_edge + read experience before any deeper edge pass).

---

## Self-Review

- **Spec coverage:**
  - §1 Structure → Tasks 1, 2, 3, 7 (helper, commands, status/help, template). ✓
  - §2 Sources → Task 4 (feeds + `kind` + `fetch_rss` rendering); §5 tilt → Tasks 7, 8 (`kind` weighting in body + `SYSTEM_PROMPT`). ✓
  - §3 Market injection (tiered/pin-derived) → Tasks 5, 6, 8 (`fetch_daily_move`, `build_market_pulse`, wiring). ✓
  - §4 Edge tilt → Tasks 7 (body) + 8 (`SYSTEM_PROMPT`). ✓
  - §`/status` lists pins → Task 3. ✓
  - §Evaluation revisit trigger → Task 9 Step 4 (memory). ✓
- **Placeholder scan:** the two "Planning note — verify at implementation" blocks (Tasks 4, 6) are deliberate verification steps with concrete starting URLs/symbols, not placeholders; every code step contains real code. ✓
- **Type consistency:** `resolved_pins(fb) -> list[str]` used identically in Tasks 1/2/3/7/8; `fetch_daily_move(asset_class, instrument) -> float | None` used in Tasks 5/6; `build_market_pulse(pins: list[str]) -> str` used in Tasks 6/8; `market_block` param name consistent in Tasks 7/8. ✓

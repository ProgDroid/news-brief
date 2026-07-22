# PolyGram Sleeve B — Discretionary Conviction Holds — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user open **real-money conviction-hold** prediction bets via a `/predict` Telegram wizard — held to settlement (or manual `/close`), contained by zero-able caps + no-DCA, every thesis logged as a resolution-dated calibration record that is scored on settlement and pruned by resolution-aware retention.

**Architecture:** A new `/predict` conversational wizard (built on the exact `_WIZARD`/`telegram_edit_text`/callback pattern that powers `/addsource`) captures a free-text thesis → searches PolyGram → the user picks market/side/stake/hold-mode/optional `p_hat` → confirm opens a live row via the foundation's `polygram_live.open_live_position(sleeve="B", …)` under a Sleeve-B cap + no-DCA guard, and appends a structured record to `thesis_log.json`. Manual `/close` is fixed to route live rows to a real venue sell. On settlement the record is scored (Brier vs. market-implied); resolution-aware retention keeps the payload until `resolve_by`+grace+scored, then collapses it to a compact scored summary.

**Tech Stack:** Python 3, `requests`, `pytest` (offline — Telegram/venue calls injected via monkeypatch). Depends on the foundation (`polygram_live.py`) and reuses Sleeve-A's `live_performance` split.

## Global Constraints

- **Depends on the foundation** (`polygram_live.py`: `open_live_position`, `close_live_position`, `list_positions`) and on Sleeve A's `validation.live_performance` (the `by_sleeve` split already reports Sleeve B).
- **Real money ⇒ fail-CLOSED.** A missing price/eventId, cap breach, or unreadable venue means the wizard refuses to open and says so — never a phantom row. Manual `/close` of a live row must **sell at the venue**, never paper-mark.
- **Kill-switch:** opening requires `common.PG_LIVE_ENABLED` **and** `common.PG_B_ENABLED` (both default OFF) **and** creds. Off ⇒ `/predict` replies that live trading is disabled and opens nothing.
- **Containment:** `PG_B_POS_CAP` per position (sized as money you can watch go to zero), `PG_B_TOTAL_CAP` across open Sleeve-B rows, and **NO DCA** — refuse a second Sleeve-B open on a market already held.
- **Sleeve B is hold-only:** its rows have `sleeve:"B"` and are **not** touched by Sleeve A's `sweep_live_exits` (which filters `sleeve=="A"`) nor by the weekly measurement path (which skips `execution=="live"`). They exit only via settlement (`reconcile_live_book` + backfill) or manual `/close`.
- **Every thesis is logged**, whether or not later machine-proposal auto-open is built — the record is the calibration corpus. Score `p_hat` **vs. market-implied-at-entry** (Brier), discrimination-first, on settlement.
- **Retention is resolution-aware:** keep a thesis payload until `today > resolve_by + PG_THESIS_GRACE_DAYS` **and** it is `scored`; then collapse to a compact scored summary (kept long-term). Keep-on-doubt: unparseable dates or unscored-past-grace records are kept (house style of `retention.py`).
- **Single-user daemon:** `_WIZARD` is in-memory, one entry per chat_id (matches `/addsource`). No persistence of half-built wizards.
- **Env & gate (`brief-local-run`):** pytest/ruff via **PowerShell**; git commits via **Bash** (BOM); quote spaced paths; full gate = `ruff check .` + `ruff format --check .` + `pytest`; commit straight to `main`; do NOT push (deploy = user's call). New top-level touch is only to existing modules (no new module ⇒ no Dockerfile/workflow allowlist change).

---

### Task 1: Thesis-log storage (shared, in `common.py`)

**Files:**
- Modify: `common.py` (path + load/save/append; placed in `common` so both `brief.py` and `retention.py` use it without a circular import)
- Test: `tests/test_common.py`

**Interfaces:**
- Produces: `common.THESIS_LOG_FILE`; `common.load_thesis_log() -> list`; `common.save_thesis_log(list)`; `common.append_thesis(record: dict)` (lock → load → append → atomic write).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_common.py (append)
import common


def test_append_thesis_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "THESIS_LOG_FILE", tmp_path / "thesis_log.json")
    common.append_thesis({"id": "t1", "market_id": "m", "p_hat": 0.8})
    common.append_thesis({"id": "t2", "market_id": "n", "p_hat": None})
    log = common.load_thesis_log()
    assert [r["id"] for r in log] == ["t1", "t2"]


def test_load_thesis_log_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "THESIS_LOG_FILE", tmp_path / "nope.json")
    assert common.load_thesis_log() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `python -m pytest tests/test_common.py::test_append_thesis_persists -q`
Expected: FAIL — `AttributeError: module 'common' has no attribute 'THESIS_LOG_FILE'`

- [ ] **Step 3: Implement**

```python
# common.py — near DATA_DIR / the other *_FILE consts
THESIS_LOG_FILE = DATA_DIR / "thesis_log.json"


def load_thesis_log() -> list:
    """The Sleeve-B conviction-thesis calibration corpus (list of records)."""
    data = _load_json_or(THESIS_LOG_FILE, [])
    return data if isinstance(data, list) else []


def save_thesis_log(records: list) -> None:
    _write_json_atomic(THESIS_LOG_FILE, records)


def append_thesis(record: dict) -> None:
    """Append one thesis record under the file lock (daemon + retention both touch it)."""
    with file_lock(THESIS_LOG_FILE):
        log_ = load_thesis_log()
        log_.append(record)
        save_thesis_log(log_)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_common.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add common.py tests/test_common.py
git commit -F - <<'EOF'
feat(sleeve-b): thesis_log storage in common (load/save/append, locked)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Sleeve B config + cap/no-DCA guard

**Files:**
- Modify: `common.py` (config), `trading.py` (`_sleeve_b_open_ok`)
- Test: `tests/test_prediction.py`

**Interfaces:**
- Produces: `common.PG_B_ENABLED: bool`, `PG_B_POS_CAP: float`, `PG_B_TOTAL_CAP: float`, `PG_THESIS_GRACE_DAYS: int`.
- Produces: `trading._sleeve_b_open_ok(book, market_id, outcome, amount) -> tuple[bool, str]` — `(True, "")` iff amount ≤ per-position cap, current Sleeve-B exposure + amount ≤ total cap, and no open Sleeve-B row already exists on `(market_id, outcome)` (no-DCA). Otherwise `(False, reason)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
import common


def test_sleeve_b_open_ok(monkeypatch):
    monkeypatch.setattr(common, "PG_B_POS_CAP", 10.0)
    monkeypatch.setattr(common, "PG_B_TOTAL_CAP", 25.0)
    book = {"positions": [
        {"execution": "live", "sleeve": "B", "status": "open",
         "instrument": "m1", "outcome": "No", "cost_basis": 20.0},
    ]}
    ok, _ = trading._sleeve_b_open_ok(book, "m2", "Yes", 10.0)
    assert ok is True                                        # within both caps, new market
    ok, why = trading._sleeve_b_open_ok(book, "m2", "Yes", 11.0)
    assert ok is False and "per-position" in why            # over per-position cap
    ok, why = trading._sleeve_b_open_ok(book, "m2", "Yes", 9.0)
    assert ok is False and "total" in why                   # 20+9 > 25 total cap
    ok, why = trading._sleeve_b_open_ok(book, "m1", "No", 3.0)
    assert ok is False and "already" in why                 # no-DCA on existing market
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prediction.py::test_sleeve_b_open_ok -q`
Expected: FAIL — `AttributeError: ... 'PG_B_POS_CAP'`

- [ ] **Step 3: Implement**

```python
# common.py — beside the PG_A_* block
PG_B_ENABLED = _env_flag("PG_B_ENABLED")
PG_B_POS_CAP = _env_float("PG_B_POS_CAP", 10.0)     # per conviction bet (money-you-can-zero)
PG_B_TOTAL_CAP = _env_float("PG_B_TOTAL_CAP", 40.0)  # across all open Sleeve-B rows
PG_THESIS_GRACE_DAYS = int(_env_float("PG_THESIS_GRACE_DAYS", 14))
```

```python
# trading.py — near open_sleeve_a_live
def _sleeve_b_open_ok(book, market_id, outcome, amount):
    """Sleeve-B guard: per-position cap, total-sleeve cap, and NO-DCA. Returns (ok, reason)."""
    if amount <= 0 or amount > common.PG_B_POS_CAP:
        return False, f"over per-position cap (${common.PG_B_POS_CAP:g})"
    exposure = sum(
        (p.get("cost_basis") or 0.0)
        for p in book.get("positions", [])
        if p.get("execution") == "live" and p.get("sleeve") == "B" and p.get("status") == "open"
    )
    if exposure + amount > common.PG_B_TOTAL_CAP:
        return False, f"over total Sleeve-B cap (${common.PG_B_TOTAL_CAP:g})"
    for p in book.get("positions", []):
        if (p.get("execution") == "live" and p.get("sleeve") == "B" and p.get("status") == "open"
                and p.get("instrument") == market_id and p.get("outcome") == outcome):
            return False, "a Sleeve-B position already open on this market (no DCA)"
    return True, ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add common.py trading.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-b): config + per-position/total cap + no-DCA open guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Route manual `/close` of live rows to a real venue sell (safety fix)

**Files:**
- Modify: `brief.py` — `_close_ticker` (route `execution=="live"` rows to `polygram_live.close_live_position`); `_close_picker_render` (unchanged behavior, but its list already includes live rows — fine once close routes correctly)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `polygram_live.close_live_position`.
- Produces: `_close_ticker` closes paper rows via `_close_position_at_market` (unchanged) and live rows via `polygram_live.close_live_position(p, "manual")`; a live row whose venue sell fails is left **open** (fail-closed), not paper-marked.

> **Why here:** live rows can exist the moment Sleeve A/B run; `/close`-ing one via the paper path orphans real capital. This fix makes `/close` execution-aware.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py (append)
def test_close_ticker_routes_live_to_venue_sell(monkeypatch, tmp_path):
    import brief, trading, polygram_live
    live = {"id": "L", "status": "open", "execution": "live", "sleeve": "B",
            "asset_class": "prediction", "instrument": "m", "ticker": "m", "outcome": "No"}
    book = {"positions": [live]}
    monkeypatch.setattr(brief, "load_book", lambda: book)
    monkeypatch.setattr(brief, "save_book", lambda b: None)
    monkeypatch.setattr(brief.trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(brief, "_pos_ticker", lambda p: "m")
    paper_called = []
    monkeypatch.setattr(brief, "_close_position_at_market",
                        lambda p, day, r: paper_called.append(p["id"]))
    live_called = []

    def fake_live_close(p, reason):
        live_called.append((p["id"], reason)); p["status"] = "closed"; return True

    monkeypatch.setattr(polygram_live, "close_live_position", fake_live_close)
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t))
    brief._close_ticker("m")
    assert live_called == [("L", "manual")]      # live routed to venue sell
    assert paper_called == []                    # paper path NOT used for the live row
    assert live["status"] == "closed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_close_ticker_routes_live_to_venue_sell -q`
Expected: FAIL — live row goes through `_close_position_at_market` (paper path), `live_called` empty.

- [ ] **Step 3: Implement**

In `brief.py`, `import polygram_live` (top), and change the close loop in `_close_ticker` to route by `execution`:

```python
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed_n = 0
        for p in matches:
            if p.get("execution") == "live":
                if polygram_live.close_live_position(p, "manual"):
                    closed_n += 1
            elif _close_position_at_market(p, day, "manual"):
                closed_n += 1
        if closed_n:
            save_book(book)
            telegram_send(
                f"✅ Closed {closed_n} position(s) for <b>{html.escape(tkr)}</b> (manual)."
            )
        else:
            telegram_send(f"⚠️ Couldn't close {html.escape(tkr)} — left open.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
fix(live): /close routes execution=live rows to a real venue sell (was paper-marking)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: `/predict` wizard — thesis → search → market → side selection

**Files:**
- Modify: `brief.py` — `BOT_COMMANDS` (+`predict`), `_handle_telegram_update` (`/predict` branch + text-step routing), `_handle_callback_query` (`pr:` branches), new `_predict_*` step functions
- Test: `tests/test_commands.py`

**Interfaces:**
- Produces (state machine in `_WIZARD[chat_id]` with `step` in `pr_thesis|pr_market|pr_side|pr_stake|pr_hold|pr_phat|pr_confirm`): `_predict_start`, `_predict_search_and_show`, `_predict_show_sides`. Callback namespace `pr:`.

> Mirrors `/addsource`: first step sends buttons via `telegram_send_buttons` (caches `msg_id`), later steps `telegram_edit_text(msg_id, …)`; the free-text thesis step is routed through `_wizard_handle_text`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py (append)
def test_predict_wizard_thesis_to_market(monkeypatch):
    import brief, common, trading
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    monkeypatch.setattr(brief, "telegram_send_buttons", lambda text, kb: 111)
    edits = []
    monkeypatch.setattr(brief, "telegram_edit_text", lambda mid, text, kb: edits.append((text, kb)))
    monkeypatch.setattr(trading, "_gather_pg_candidates", lambda s: [
        {"market_id": "2774056", "question": "Hormuz normal by Aug 31?",
         "yes_price": 0.13, "end_date": "2026-08-31", "event_id": "evt_h"}])
    brief._WIZARD.clear()
    brief._predict_start("42")
    assert brief._WIZARD["42"]["step"] == "pr_thesis" and brief._WIZARD["42"]["msg_id"] == 111
    # user replies with the thesis free-text → search + show market buttons
    brief._wizard_handle_text("42", "hormuz shipping stays disrupted")
    w = brief._WIZARD["42"]
    assert w["step"] == "pr_market" and w["thesis"] == "hormuz shipping stays disrupted"
    assert w["candidates"][0]["event_id"] == "evt_h"
    assert edits  # market buttons were rendered


def test_predict_disabled_says_so(monkeypatch):
    import brief, common
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", False)
    monkeypatch.setattr(common, "PG_B_ENABLED", False)
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t))
    brief._WIZARD.clear()
    brief._predict_start("42")
    assert "42" not in brief._WIZARD and sent and "disabled" in sent[0].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_predict_wizard_thesis_to_market -q`
Expected: FAIL — `AttributeError: module 'brief' has no attribute '_predict_start'`

- [ ] **Step 3: Implement**

Register the command — add to `BOT_COMMANDS` (brief.py:2821): `("predict", "Open a conviction prediction bet (guided)")`. In `_handle_telegram_update`, add `elif text == "/predict": _predict_start(chat_id)`. The existing wizard text-routing line already forwards non-`/` replies to `_wizard_handle_text` when `chat_id in _WIZARD`.

Step functions (brief.py, near the `_wizard_*` block):

```python
def _predict_start(chat_id: str) -> None:
    """Begin the /predict conviction-bet wizard, or refuse if live trading is disabled."""
    if not (common.PG_LIVE_ENABLED and common.PG_B_ENABLED):
        telegram_send("Live conviction trading is <b>disabled</b> (set PG_LIVE_ENABLED + PG_B_ENABLED).")
        return
    msg_id = telegram_send_buttons(
        "🎯 <b>Conviction bet</b>\n\nReply with your thesis (free text) — I'll find matching markets.",
        [[{"text": "❌ Cancel", "callback_data": "pr:cancel"}]],
    )
    _WIZARD[chat_id] = {"step": "pr_thesis", "msg_id": msg_id}


def _predict_search_and_show(chat_id: str, thesis: str) -> None:
    """After the thesis text: search PolyGram, stash candidates, render market buttons."""
    w = _WIZARD[chat_id]
    w["thesis"] = thesis
    cands = trading._gather_pg_candidates([{"topic": thesis}])[:6]
    if not cands:
        telegram_edit_text(w["msg_id"], "No matching markets found. Try /predict again.", [])
        _WIZARD.pop(chat_id, None)
        return
    w["candidates"] = cands
    w["step"] = "pr_market"
    rows = [[{"text": f"{c['question'][:55]} · {c['yes_price']:.2f}",
              "callback_data": f"pr:mkt:{i}"}] for i, c in enumerate(cands)]
    rows.append([{"text": "❌ Cancel", "callback_data": "pr:cancel"}])
    telegram_edit_text(w["msg_id"], "Pick the market:", rows)


def _predict_show_sides(chat_id: str, idx: int) -> None:
    """After market pick: fetch token ids/prices, stash, render YES/NO buttons."""
    w = _WIZARD[chat_id]
    c = w["candidates"][idx]
    m = trading.polygram_market(c["market_id"])
    parsed = trading._parse_pg_market(m) if m is not None else None
    if parsed is None or not c.get("event_id"):
        telegram_edit_text(w["msg_id"], "That market can't be traded right now. /predict to retry.", [])
        _WIZARD.pop(chat_id, None)
        return
    w.update({"market_id": c["market_id"], "event_id": c["event_id"],
              "question": parsed["question"], "prices": parsed["prices"],
              "token_ids": parsed["token_ids"], "step": "pr_side"})
    yes_p, no_p = parsed["prices"][0], parsed["prices"][1]
    rows = [[{"text": f"YES · {yes_p:.2f}", "callback_data": "pr:side:YES"},
             {"text": f"NO · {no_p:.2f}", "callback_data": "pr:side:NO"}],
            [{"text": "❌ Cancel", "callback_data": "pr:cancel"}]]
    telegram_edit_text(w["msg_id"], f"<b>{parsed['question']}</b>\n\nWhich side?", rows)
```

Wire the text step (in `_wizard_handle_text`, add a branch alongside the existing `category_text`/`url`/`rename` ones):

```python
    if w.get("step") == "pr_thesis":
        _predict_search_and_show(chat_id, text)
        return
```

Wire the callbacks (in `_handle_callback_query`, after the `as:*` block; `pr:cancel` must work without a live wizard, the rest gated on `_WIZARD`):

```python
    if data == "pr:cancel":
        w = _WIZARD.pop(chat_id, None)
        if w:
            telegram_edit_text(w["msg_id"], "❌ Cancelled.", [])
        return fb
    w = _WIZARD.get(chat_id)
    if w and data.startswith("pr:mkt:"):
        _predict_show_sides(chat_id, int(data.split(":")[2]))
        return fb
    if w and data.startswith("pr:side:"):
        _predict_show_stake(chat_id, data.split(":")[2])  # defined in Task 5
        return fb
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS (note: `pr:side:` handler references `_predict_show_stake` from Task 5 — for this task's tests it is not exercised; if the module must import cleanly, add a temporary `def _predict_show_stake(*a, **k): ...` stub that Task 5 replaces, OR land Tasks 4–5 together. Recommended: implement Task 5's stubs-to-real in the same review cycle.)

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sleeve-b): /predict wizard — thesis text -> market search -> side selection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: `/predict` wizard — stake → hold-mode → p_hat → confirm → open + log

**Files:**
- Modify: `brief.py` — `_predict_show_stake`, `_predict_show_hold`, `_predict_show_phat`, `_predict_confirm_prompt`, `_predict_commit`; callback + text-step wiring for `pr:stake/hold/phat/confirm`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `_sleeve_b_open_ok` (Task 2), `polygram_live.open_live_position`, `common.append_thesis`.
- Produces: the commit path builds the live row (`sleeve="B"`), enforces the cap/no-DCA, and appends the thesis record.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands.py (append)
def test_predict_commit_opens_and_logs(monkeypatch):
    import brief, common, trading, polygram_live
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    book = {"positions": []}
    monkeypatch.setattr(brief, "load_book", lambda: book)
    monkeypatch.setattr(brief, "save_book", lambda b: None)
    monkeypatch.setattr(brief.trading, "BOOK_FILE", "x")
    monkeypatch.setattr(brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(trading, "_sleeve_b_open_ok", lambda b, m, o, a: (True, ""))
    opened = {}

    def fake_open(book, **kw):
        opened.update(kw)
        row = {"execution": "live", "sleeve": "B", "instrument": kw["market_id"],
               "outcome": kw["outcome"], "entry_price": 0.62, "cost_basis": kw["amount"],
               "status": "open"}
        book["positions"].append(row)
        return row

    monkeypatch.setattr(polygram_live, "open_live_position", fake_open)
    logged = []
    monkeypatch.setattr(common, "append_thesis", lambda r: logged.append(r))
    monkeypatch.setattr(brief, "telegram_edit_text", lambda *a: None)
    brief._WIZARD["42"] = {
        "step": "pr_confirm", "msg_id": 1, "thesis": "oil stays bid",
        "market_id": "m", "event_id": "evt", "question": "Q?",
        "prices": [0.62, 0.38], "token_ids": ["tYes", "tNo"],
        "outcome": "Yes", "side_index": 0, "stake": 5.0, "hold_mode": "settle", "p_hat": 0.75,
        "end_date": "2026-09-01",
    }
    brief._predict_commit("42")
    assert opened["sleeve"] == "B" and opened["event_id"] == "evt"
    assert opened["token_id"] == "tYes" and opened["outcome"] == "Yes" and opened["amount"] == 5.0
    assert logged and logged[0]["p_hat"] == 0.75 and logged[0]["entry_price"] == 0.62
    assert logged[0]["resolve_by"] == "2026-09-01" and logged[0]["traded"] is True
    assert "42" not in brief._WIZARD


def test_predict_commit_blocked_by_cap(monkeypatch):
    import brief, common, trading, polygram_live
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(brief.trading, "BOOK_FILE", "x")
    monkeypatch.setattr(trading, "_sleeve_b_open_ok", lambda b, m, o, a: (False, "over total Sleeve-B cap"))
    calls = []
    monkeypatch.setattr(polygram_live, "open_live_position", lambda book, **k: calls.append(k))
    logged = []
    monkeypatch.setattr(common, "append_thesis", lambda r: logged.append(r))
    edits = []
    monkeypatch.setattr(brief, "telegram_edit_text", lambda mid, text, kb: edits.append(text))
    brief._WIZARD["42"] = {"step": "pr_confirm", "msg_id": 1, "thesis": "t", "market_id": "m",
                           "event_id": "e", "question": "Q", "prices": [0.6, 0.4],
                           "token_ids": ["a", "b"], "outcome": "Yes", "side_index": 0,
                           "stake": 99.0, "hold_mode": "settle", "p_hat": None, "end_date": "2026-09-01"}
    brief._predict_commit("42")
    assert calls == [] and logged == []                 # blocked before any order/log
    assert edits and "cap" in edits[-1].lower()
    assert "42" not in brief._WIZARD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands.py::test_predict_commit_opens_and_logs -q`
Expected: FAIL — `AttributeError: ... '_predict_commit'`

- [ ] **Step 3: Implement**

```python
# brief.py — the remaining wizard steps + commit
_PREDICT_STAKES = [2, 5, 10]  # preset USD buttons; "Other" → free-text


def _predict_show_stake(chat_id: str, side: str) -> None:
    w = _WIZARD[chat_id]
    w["outcome"] = "Yes" if side == "YES" else "No"
    w["side_index"] = 0 if side == "YES" else 1
    w["step"] = "pr_stake"
    row = [{"text": f"${s}", "callback_data": f"pr:stake:{s}"} for s in _PREDICT_STAKES]
    rows = [row, [{"text": "✏️ Other", "callback_data": "pr:stake:other"},
                  {"text": "❌ Cancel", "callback_data": "pr:cancel"}]]
    telegram_edit_text(w["msg_id"], "Stake (USD)? Money you can watch go to zero.", rows)


def _predict_show_hold(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "pr_hold"
    rows = [[{"text": "⏳ Hold to settlement", "callback_data": "pr:hold:settle"}],
            [{"text": "✋ Until I /close", "callback_data": "pr:hold:manual"}],
            [{"text": "❌ Cancel", "callback_data": "pr:cancel"}]]
    telegram_edit_text(w["msg_id"], "Exit mode?", rows)


def _predict_show_phat(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "pr_phat"
    rows = [[{"text": "⏭ Skip", "callback_data": "pr:phat:skip"}],
            [{"text": "❌ Cancel", "callback_data": "pr:cancel"}]]
    telegram_edit_text(
        w["msg_id"],
        "Your probability the bet resolves in your favour (0–1)? Reply a number, or Skip.",
        rows,
    )


def _predict_confirm_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "pr_confirm"
    ph = "—" if w.get("p_hat") is None else f"{w['p_hat']:.2f}"
    entry = w["prices"][w["side_index"]]
    summary = (f"<b>{w['question']}</b>\n"
               f"Side <b>{w['outcome']}</b> @ {entry:.2f} · ${w['stake']:g} · "
               f"{w['hold_mode']} · p̂={ph}\n<i>{html.escape(w['thesis'])}</i>")
    rows = [[{"text": "✅ Open bet", "callback_data": "pr:confirm"},
             {"text": "❌ Cancel", "callback_data": "pr:cancel"}]]
    telegram_edit_text(w["msg_id"], summary, rows)


def _predict_commit(chat_id: str) -> None:
    """Open the Sleeve-B live row under the cap/no-DCA guard and log the thesis. Fail-closed."""
    w = _WIZARD.get(chat_id)
    if not w:
        return
    with file_lock(trading.BOOK_FILE):
        book = load_book()
        ok, why = trading._sleeve_b_open_ok(book, w["market_id"], w["outcome"], w["stake"])
        if not ok:
            telegram_edit_text(w["msg_id"], f"⚠️ Not opened — {why}.", [])
            _WIZARD.pop(chat_id, None)
            return
        entry = w["prices"][w["side_index"]]
        row = polygram_live.open_live_position(
            book, sleeve="B", event_id=w["event_id"], market_id=w["market_id"],
            token_id=w["token_ids"][w["side_index"]], outcome=w["outcome"],
            side_index=w["side_index"], amount=w["stake"], topic=w["question"],
            source_id="user", source_kind="discretionary", source_perspective=None,
            live_exposure=0.0,  # cap already enforced by _sleeve_b_open_ok
        )
        if row is None:
            telegram_edit_text(w["msg_id"], "⚠️ Venue rejected the order — nothing opened.", [])
            _WIZARD.pop(chat_id, None)
            return
        if w.get("hold_mode") == "manual":
            row["hold_mode"] = "manual"
        save_book(book)
    common.append_thesis({
        "id": row["id"],
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "market_id": w["market_id"], "event_id": w["event_id"],
        "outcome": w["outcome"], "side_index": w["side_index"],
        "entry_price": entry, "p_hat": w.get("p_hat"),
        "resolve_by": w.get("end_date"), "question": w["question"],
        "thesis": w["thesis"], "source_ids": ["user"], "stake": w["stake"],
        "hold_mode": w.get("hold_mode", "settle"), "sleeve": "B",
        "traded": True, "scored": False, "outcome_result": None, "brier": None,
    })
    telegram_edit_text(w["msg_id"], f"✅ Opened <b>{w['outcome']}</b> @ {entry:.2f}. Logged.", [])
    _WIZARD.pop(chat_id, None)
```

Also stash `end_date` when the market is chosen — add `"end_date": c["end_date"]` to the `w.update({...})` in `_predict_show_sides` (Task 4).

Callback + text wiring (in `_handle_callback_query`, extend the `pr:` block; and `_wizard_handle_text` for the free-text stake/p_hat):

```python
    if w and data.startswith("pr:stake:"):
        val = data.split(":")[2]
        if val == "other":
            w["step"] = "pr_stake_text"
            telegram_edit_text(w["msg_id"], "Reply with the stake in USD (e.g. 7).", [])
        else:
            w["stake"] = float(val)
            _predict_show_hold(chat_id)
        return fb
    if w and data.startswith("pr:hold:"):
        w["hold_mode"] = data.split(":")[2]
        _predict_show_phat(chat_id)
        return fb
    if w and data == "pr:phat:skip":
        w["p_hat"] = None
        _predict_confirm_prompt(chat_id)
        return fb
    if w and data == "pr:confirm":
        _predict_commit(chat_id)
        return fb
```

```python
# _wizard_handle_text — add branches for the two free-text sub-steps
    if w.get("step") == "pr_stake_text":
        try:
            w["stake"] = float(text)
        except ValueError:
            telegram_edit_text(w["msg_id"], "Not a number — reply a USD amount (e.g. 7).", [])
            return
        _predict_show_hold(chat_id)
        return
    if w.get("step") == "pr_phat":
        try:
            v = float(text)
            w["p_hat"] = v if 0.0 <= v <= 1.0 else None
        except ValueError:
            w["p_hat"] = None
        _predict_confirm_prompt(chat_id)
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -F - <<'EOF'
feat(sleeve-b): /predict wizard commit — cap/no-DCA gated open + thesis-log record

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Score theses on settlement (Brier vs. market-implied)

**Files:**
- Modify: `trading.py` — `score_settled_theses()`; call it in `mode_monitor` after `backfill_settled`
- Test: `tests/test_prediction.py`

**Interfaces:**
- Produces: `trading.score_settled_theses() -> int` — for each unscored thesis whose live row is now `closed` with a known `realized_return`, set `outcome_result` (1 if the held side won else 0), `brier` = `(p_hat - outcome_result)**2` when `p_hat` is not None, and `scored=True`. Returns count scored. Held-side win is inferred from `realized_return > 0` (a favorite/hold that settled in the money).

> **Note:** market-implied comparison is the entry price already stored on the record (`entry_price`); Brier of `p_hat` vs `entry_price` is a *separate* calibration lens computed at review time from the logged fields — this task records the ground-truth `outcome_result` + the `p_hat` Brier, which is the minimum needed for the pre-registered gate. Records with `p_hat is None` get `outcome_result` + `scored=True` and `brier=None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
def test_score_settled_theses(monkeypatch):
    import trading, common
    thesis_store = [
        {"id": "L1", "p_hat": 0.8, "scored": False, "outcome_result": None, "brier": None},
        {"id": "L2", "p_hat": None, "scored": False, "outcome_result": None, "brier": None},
        {"id": "L3", "p_hat": 0.6, "scored": False, "outcome_result": None, "brier": None},
    ]
    monkeypatch.setattr(common, "load_thesis_log", lambda: thesis_store)
    saved = {}
    monkeypatch.setattr(common, "save_thesis_log", lambda r: saved.setdefault("r", r))
    monkeypatch.setattr(trading, "load_book", lambda: {"positions": [
        {"id": "L1", "status": "closed", "realized_return": 0.12},   # won
        {"id": "L2", "status": "closed", "realized_return": -1.0},   # lost
        {"id": "L3", "status": "open", "realized_return": None},     # not settled yet
    ]})
    n = trading.score_settled_theses()
    assert n == 2
    by_id = {r["id"]: r for r in saved["r"]}
    assert by_id["L1"]["outcome_result"] == 1 and abs(by_id["L1"]["brier"] - (0.8 - 1) ** 2) < 1e-9
    assert by_id["L2"]["outcome_result"] == 0 and by_id["L2"]["brier"] is None  # no p_hat
    assert by_id["L3"]["scored"] is False                                       # still open
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prediction.py::test_score_settled_theses -q`
Expected: FAIL — `AttributeError: ... 'score_settled_theses'`

- [ ] **Step 3: Implement**

```python
# trading.py
def score_settled_theses() -> int:
    """Grade unscored theses whose live row has settled. Returns count scored.

    outcome_result = 1 if the held side won (realized_return > 0) else 0.
    brier = (p_hat - outcome_result)**2 when p_hat is known, else None."""
    log_ = common.load_thesis_log()
    if not log_:
        return 0
    ret_by_id = {
        p["id"]: p.get("realized_return")
        for p in load_book().get("positions", [])
        if p.get("status") == "closed" and p.get("realized_return") is not None
    }
    n = 0
    for rec in log_:
        if rec.get("scored"):
            continue
        ret = ret_by_id.get(rec.get("id"))
        if ret is None:
            continue
        outcome = 1 if ret > 0 else 0
        rec["outcome_result"] = outcome
        rec["brier"] = None if rec.get("p_hat") is None else (rec["p_hat"] - outcome) ** 2
        rec["scored"] = True
        n += 1
    if n:
        common.save_thesis_log(log_)
    return n
```

Wire into `mode_monitor` (brief.py), after `backfill_settled`:

```python
            n_fill = polygram_live.backfill_settled(book)
            n_score = trading.score_settled_theses()
            if n_exit or n_rec or n_fill or n_score:
                save_book(book)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading.py brief.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-b): score settled theses (outcome + Brier vs p_hat) in the monitor

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: Resolution-aware thesis retention

**Files:**
- Modify: `retention.py` — `prune_scored_theses(today, grace_days)`; call in `run_retention`
- Test: `tests/test_retention.py`

**Interfaces:**
- Produces: `retention.prune_scored_theses(today, grace_days) -> int` — collapse each thesis record to a compact scored summary ONLY when `today > resolve_by + grace_days` **and** `scored is True`; keep-on-doubt otherwise (unparseable/missing `resolve_by`, or unscored). Returns count collapsed. Added to `run_retention`'s summary.

> The compact summary retains only `{id, sleeve, p_hat, entry_price, outcome_result, brier, resolve_by, scored, source_ids}` — the reliability-curve fields — dropping the verbose `thesis`/`question`/`topic` payload. Idempotent (a record already lacking the verbose keys is skipped).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retention.py (append)
import common, retention


def test_prune_scored_theses(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "THESIS_LOG_FILE", tmp_path / "thesis_log.json")
    common.save_thesis_log([
        {"id": "old", "sleeve": "B", "p_hat": 0.8, "entry_price": 0.85, "outcome_result": 1,
         "brier": 0.04, "resolve_by": "2026-06-01", "scored": True, "source_ids": ["user"],
         "thesis": "verbose text", "question": "Q?"},
        {"id": "recent", "resolve_by": "2026-07-30", "scored": True, "thesis": "keep",
         "p_hat": 0.7, "entry_price": 0.7, "outcome_result": None, "brier": None, "sleeve": "B",
         "source_ids": []},
        {"id": "unscored", "resolve_by": "2026-05-01", "scored": False, "thesis": "keep",
         "p_hat": None, "entry_price": 0.9, "sleeve": "B", "source_ids": []},
    ])
    n = retention.prune_scored_theses("2026-07-21", grace_days=14)
    assert n == 1  # only "old": past 2026-06-01+14 AND scored
    out = {r["id"]: r for r in common.load_thesis_log()}
    assert "thesis" not in out["old"] and out["old"]["brier"] == 0.04  # collapsed to summary
    assert out["recent"]["thesis"] == "keep"    # inside grace → kept verbose
    assert out["unscored"]["thesis"] == "keep"  # unscored past grace → kept (keep-on-doubt)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_retention.py::test_prune_scored_theses -q`
Expected: FAIL — `AttributeError: module 'retention' has no attribute 'prune_scored_theses'`

- [ ] **Step 3: Implement**

```python
# retention.py — import from common: load_thesis_log, save_thesis_log
from common import load_thesis_log, save_thesis_log  # add to existing common imports

_SUMMARY_KEYS = ("id", "sleeve", "p_hat", "entry_price", "outcome_result",
                 "brier", "resolve_by", "scored", "source_ids")


def prune_scored_theses(today: str, grace_days: int) -> int:
    """Collapse scored, past-grace thesis records to a compact summary. Keep-on-doubt.

    A record is collapsed only when its resolve_by parses, today > resolve_by + grace_days,
    and scored is True. Unparseable/missing dates and unscored records are kept verbatim."""
    log_ = load_thesis_log()
    if not log_:
        return 0
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    n = 0
    for i, rec in enumerate(log_):
        if not rec.get("scored"):
            continue
        rb = rec.get("resolve_by")
        try:
            rb_d = datetime.strptime(str(rb)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue  # undateable → keep verbose
        if today_d <= rb_d + timedelta(days=grace_days):
            continue  # inside grace → keep verbose
        if "thesis" not in rec and "question" not in rec:
            continue  # already collapsed (idempotent)
        log_[i] = {k: rec.get(k) for k in _SUMMARY_KEYS}
        n += 1
    if n:
        save_thesis_log(log_)
    return n
```

Wire into `run_retention` (retention.py) alongside the existing calls (uses `common.PG_THESIS_GRACE_DAYS` for grace, independent of the file-window `days`):

```python
        summary["deleted"] = prune_dated_files(today, resolved)
        summary["trimmed_lines"] = trim_signals_log(today, resolved)
        summary["theses_pruned"] = prune_scored_theses(today, common.PG_THESIS_GRACE_DAYS)
```

Add `"theses_pruned": 0` to the initial `summary` dict, `import common` if not already imported, and ensure `from datetime import datetime, timedelta` covers `timedelta` (it is already used by `_cutoff`).

- [ ] **Step 4: Run test to verify it passes + full gate**

Run: `python -m pytest tests/test_retention.py -q`
Then: `python -m ruff check . ; python -m ruff format --check . ; python -m pytest -q`
Expected: all pass (stage any file ruff reformats).

- [ ] **Step 5: Commit**

```bash
git add retention.py tests/test_retention.py
git commit -F - <<'EOF'
feat(sleeve-b): resolution-aware thesis retention (collapse scored past-grace records)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review

**Spec coverage (Sleeve B slice):**
- `/predict` conversational wizard (thesis → search → market → side → stake → hold-mode → p_hat → confirm), reusing the `_WIZARD`/`telegram_edit_text`/`pr:`-callback pattern — Tasks 4, 5 ✓.
- Live open via the foundation's `open_live_position(sleeve="B")`, cap + no-DCA + separate P&L (Sleeve-A `live_performance` `by_sleeve`) — Tasks 2, 5 ✓.
- Hold-to-settlement / manual `/close`, exempt from Sleeve-A auto-exits (its sweep filters `sleeve=="A"`) — hold-mode stored; `/close` fixed — Task 3 ✓.
- Every thesis logged as a resolution-dated calibration record; scored on settlement (Brier vs. `p_hat`) — Tasks 1, 5, 6 ✓.
- Resolution-aware retention (keep until `resolve_by`+grace+scored, then collapse) — Task 7 ✓.
- **The `/close` live-row safety bug** (Explore finding) — fixed — Task 3 ✓.
- **Deferred to spec #2** (correctly out of scope): machine-*proposed* theses (propose-then-confirm push) + earned auto-open; a `summarize_theses` calibration-review aggregate (trivial once records exist — the gate is reviewed manually against the logged fields).

**Placeholder scan:** none — every step has complete code. Task 4 Step 4 flags the one cross-task ordering point (the `pr:side:` handler calls Task 5's `_predict_show_stake`): land Tasks 4–5 in the same review cycle (or add the one-line stub noted), not a placeholder in the shipped code.

**Type consistency:** `append_thesis`/`load_thesis_log`/`save_thesis_log` (Task 1) are consumed by `_predict_commit` (Task 5), `score_settled_theses` (Task 6), and `prune_scored_theses` (Task 7) with the same record shape (`id`, `p_hat`, `resolve_by`, `scored`, `outcome_result`, `brier`, `entry_price`, `sleeve`, `source_ids`). `_sleeve_b_open_ok` returns `(bool, str)` as consumed in `_predict_commit`. `open_live_position` is called with the foundation's exact keyword signature. `/close` routes on the same `execution`/`sleeve` fields the other tasks write. Consistent.

## Execution Handoff

This is the **last** of the three plans. After it lands, the full two-sleeve live-trading feature is built (foundation + Sleeve A + Sleeve B), still gated OFF (`PG_LIVE_ENABLED`/`PG_A_ENABLED`/`PG_B_ENABLED` all default off) and unpushed. Go-live is then an operational step: push (Docker deploy), fund the polygram.ink account, set the caps/stake env vars, and flip the flags — Sleeve A first, Sleeve B once the `/predict` flow is validated. Spec #2 (machine-proposed theses + earned auto-open) is a separate future design gated on the accumulated thesis-log scorecard.

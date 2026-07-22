# PolyGram Sleeve A — Systematic Favorite-Fade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the (already-built) `polygram_live.py` rails into a real-money **systematic favorite-fade** strategy: match daily signals → buy the tight-spread favorite side of over-dramatized binaries → exit on repricing-take / stop / time-stop, checked hourly, with live rows kept out of the paper measurement path and reported separately.

**Architecture:** A new gated entry path in `mode_paper` (reusing the existing prediction matcher) opens live rows via `polygram_live.open_live_position` only when the favorite-side + price-band + live-orderbook-spread gates pass and an `eventId` is captured. A new hourly exit sweep in `mode_monitor` marks each open live row and closes it via `polygram_live.close_live_position` on take/stop/time-stop, then runs `reconcile_live_book` and a settled-proceeds backfill. The weekly `_mtm_prediction` measurement path skips `execution=="live"` rows, and `validation.aggregate_performance` splits paper (the go-live gate) from live.

**Tech Stack:** Python 3, `requests`, `pytest` (offline — HTTP/matcher injected via monkeypatch). Depends on the foundation module `polygram_live.py` (already on `main`).

## Global Constraints

- **Depends on the foundation** (`polygram_live.py`): `open_live_position`, `close_live_position`, `reconcile_live_book`, `list_positions`, `orderbook`, `wallet_balance`, `cap_ok` already exist and are tested. Do NOT reimplement them.
- **Real money ⇒ fail-CLOSED.** A missing price, unreadable orderbook, absent `eventId`, or any gate failure means **do not trade / do not close** — never guess. All new hooks wrapped so they never crash `mode_paper`/`mode_monitor`.
- **Master + sleeve kill-switch:** live entries require `common.PG_LIVE_ENABLED` **and** `PG_A_ENABLED` (both default OFF) **and** `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` set. Off ⇒ the existing paper path is byte-for-byte unchanged.
- **Spread gate uses the live orderbook:** reuse `trading._fetch_pg_half_spread(token_id)` (returns best-bid/ask **half**-spread as a fraction of mid, or None). Gate: open only when it is not None **and** ≤ `PG_A_SPREAD_GATE`. None (unreadable book) ⇒ skip (fail-closed).
- **Favorite side + band:** Sleeve A only buys the **held side priced in `[PG_A_BAND_LO, PG_A_BAND_HI]`** (default 0.75–0.92). The matcher's `side` names the outcome; the held-side price is `parsed["prices"][side_index]`.
- **Live rows never enter the weekly measurement path:** `_mtm_prediction` / `mark_to_market` must skip `execution=="live"`. Live exits run ONLY in the hourly monitor sweep. The 4-week (`PAPER_CLOSE_HORIZON`) and 182-day (`PG_MAX_HOLD_DAYS`) holds are paper-only.
- **Book lock:** every load→mutate→save of `book.json` holds `file_lock(BOOK_FILE, timeout=BOOK_LOCK_TIMEOUT)` (mirror `mode_paper`/`mode_weekly`).
- **Two live-verify points** (confirm against the real API the first time creds are live; code defends both): (1) the exact event-id key in a `/search` event wrapper — try `ev.get("id")` then `ev.get("eventId")`; (2) that `POST /trade/place`'s `marketId` accepts the numeric market id we store as `instrument`. Both are captured/passed defensively; a missing eventId simply skips the live open (fail-closed).
- **Env & gate (`brief-local-run`):** run pytest/ruff via the **PowerShell** tool; make git commits via the **Bash** tool (PowerShell prepends a BOM to commit subjects); quote spaced paths. Pre-push gate = `ruff check .` + `ruff format --check .` + `pytest`. Commit straight to `main` (solo repo). Do NOT push (push = Docker deploy = user's call).

---

### Task 1: Capture `eventId` into prediction candidates

**Files:**
- Modify: `trading.py` — `_gather_pg_candidates` (adds `event_id` to each candidate dict)
- Test: `tests/test_prediction.py`

**Interfaces:**
- Produces: each candidate dict gains `"event_id": str | None` (from the `/search` event wrapper). Consumed by Task 3's live open.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
def test_gather_candidates_captures_event_id(monkeypatch):
    ev = {
        "id": "evt_hormuz",
        "markets": [{
            "id": "2774056",
            "question": "Strait of Hormuz normal by Aug 31?",
            "outcomePrices": '["0.13", "0.87"]',
            "clobTokenIds": '["tokA", "tokB"]',
            "closed": False,
            "endDate": "2026-08-31",
            "umaResolutionStatus": "",
        }],
    }
    monkeypatch.setattr(trading, "polygram_search", lambda q: [ev])
    monkeypatch.setattr(trading, "_signal_search_terms", lambda s: ["hormuz"])
    cands = trading._gather_pg_candidates([{"topic": "hormuz"}])
    assert len(cands) == 1
    assert cands[0]["market_id"] == "2774056"
    assert cands[0]["event_id"] == "evt_hormuz"
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `python -m pytest tests/test_prediction.py::test_gather_candidates_captures_event_id -q`
Expected: FAIL — `KeyError: 'event_id'`

- [ ] **Step 3: Implement — thread the event wrapper's id through**

In `trading.py`, change `_gather_pg_candidates` so the event id is carried from the wrapper onto each market before parsing. Replace the search loop body:

```python
    seen: dict[str, dict] = {}
    for q in _signal_search_terms(signals):
        raw: list[dict] = []
        for ev in polygram_search(q) or []:
            if isinstance(ev, dict):
                ev_id = ev.get("id") or ev.get("eventId")  # live-verify key name
                for m in ev.get("markets", []):
                    if isinstance(m, dict):
                        m["_event_id"] = ev_id  # stash for parse/open
                        raw.append(m)
        raw.sort(key=_pg_market_volume, reverse=True)
        taken = 0
        for m in raw:
            if taken >= PG_PER_QUERY_CAP:
                break
            parsed = _parse_pg_market(m)
            if parsed is None or parsed["closed"]:
                continue
            if parsed["market_id"] not in seen:
                seen[parsed["market_id"]] = {
                    "market_id": parsed["market_id"],
                    "question": parsed["question"],
                    "yes_price": parsed["yes_price"],
                    "end_date": parsed["end_date"],
                    "event_id": m.get("_event_id"),
                }
                taken += 1
        if len(seen) >= PG_CANDIDATE_CAP:
            break
    return list(seen.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS (new test + existing prediction tests still green)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-a): capture eventId from the /search wrapper into prediction candidates

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Sleeve A config + entry gates (favorite side, band, spread)

**Files:**
- Modify: `common.py` (config), `trading.py` (gate helpers)
- Test: `tests/test_prediction.py`

**Interfaces:**
- Produces: `common.PG_A_ENABLED: bool`, `PG_A_STAKE: float`, `PG_A_BAND_LO/HI: float`, `PG_A_SPREAD_GATE: float`, `PG_A_TAKE/PG_A_STOP: float`, `PG_A_TIME_STOP_DAYS/PG_A_NEAR_DAYS: int`.
- Produces: `trading._sleeve_a_entry_ok(held_price, token_id) -> bool` — True iff `PG_A_BAND_LO ≤ held_price ≤ PG_A_BAND_HI` AND the live half-spread is readable and ≤ `PG_A_SPREAD_GATE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
import common


def test_sleeve_a_entry_ok_gates(monkeypatch):
    monkeypatch.setattr(common, "PG_A_BAND_LO", 0.75)
    monkeypatch.setattr(common, "PG_A_BAND_HI", 0.92)
    monkeypatch.setattr(common, "PG_A_SPREAD_GATE", 0.03)
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.01)
    assert trading._sleeve_a_entry_ok(0.85, "tok") is True     # in band, tight spread
    assert trading._sleeve_a_entry_ok(0.60, "tok") is False    # below band (longshot)
    assert trading._sleeve_a_entry_ok(0.99, "tok") is False    # above band (crumbs)
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.10)
    assert trading._sleeve_a_entry_ok(0.85, "tok") is False    # spread too wide
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: None)
    assert trading._sleeve_a_entry_ok(0.85, "tok") is False    # unreadable book → fail-closed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prediction.py::test_sleeve_a_entry_ok_gates -q`
Expected: FAIL — `AttributeError: module 'common' has no attribute 'PG_A_BAND_LO'`

- [ ] **Step 3: Implement config + gate**

```python
# common.py — beside the PG_LIVE_* block (reuse _env_flag/_env_float from Task-1 of the foundation)
# Sleeve A — systematic favorite-fade (real money). Default OFF.
PG_A_ENABLED = _env_flag("PG_A_ENABLED")
PG_A_STAKE = _env_float("PG_A_STAKE", 2.0)            # USD per fade
PG_A_BAND_LO = _env_float("PG_A_BAND_LO", 0.75)       # favorite-side entry band
PG_A_BAND_HI = _env_float("PG_A_BAND_HI", 0.92)
PG_A_SPREAD_GATE = _env_float("PG_A_SPREAD_GATE", 0.03)  # max half-spread (fraction of mid)
PG_A_TAKE = _env_float("PG_A_TAKE", 0.97)             # take-profit: held price repriced to ceiling
PG_A_STOP = _env_float("PG_A_STOP", 0.15)             # stop: absolute adverse drop from entry price
PG_A_TIME_STOP_DAYS = int(_env_float("PG_A_TIME_STOP_DAYS", 21))
PG_A_NEAR_DAYS = int(_env_float("PG_A_NEAR_DAYS", 10))   # ≤ this to settlement ⇒ hold, no time-stop
```

```python
# trading.py — near _fetch_pg_half_spread. Import common at top already present via `from common import ...`;
# add `import common` if the module does not already import it as a namespace.
def _sleeve_a_entry_ok(held_price: float, token_id: str) -> bool:
    """True iff the held-side price is in the favorite band AND the live half-spread
    is readable and within the gate. Unreadable orderbook ⇒ False (fail-closed)."""
    if not (common.PG_A_BAND_LO <= held_price <= common.PG_A_BAND_HI):
        return False
    half = _fetch_pg_half_spread(token_id)
    return half is not None and half <= common.PG_A_SPREAD_GATE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add common.py trading.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-a): config + favorite-side/band/live-spread entry gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Sleeve A live entry path (wired into `mode_paper`)

**Files:**
- Modify: `trading.py` — new `open_sleeve_a_live`; call it in `mode_paper` right after `_open_prediction_positions` (line 1655)
- Test: `tests/test_prediction.py`

**Interfaces:**
- Consumes: `_gather_pg_candidates` (with `event_id`), `run_prediction_matcher`, `polygram_market`/`_parse_pg_market`, `_sleeve_a_entry_ok`, `common.PG_A_*`, `polygram_live.open_live_position`, `polygram_live.list_positions`.
- Produces: `open_sleeve_a_live(book, signals, today) -> int` — count of live rows opened. No-op (returns 0) unless `PG_LIVE_ENABLED and PG_A_ENABLED` and creds present.

> **Design:** Sleeve A runs its OWN gather+match (a second ~2k-token matcher call — trivial cost) rather than refactoring the paper path, keeping the sleeves independent. Live exposure for the cap is computed from existing open live rows' `cost_basis`. Dedup: skip a market that already has an open live row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
import polygram_live


def test_open_sleeve_a_live_opens_gated_favorite(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_STAKE", 2.0)
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(trading, "_gather_pg_candidates", lambda s: [
        {"market_id": "2774056", "question": "Hormuz normal by Aug 31?",
         "yes_price": 0.13, "end_date": "2026-08-31", "event_id": "evt_h"}])
    monkeypatch.setattr(trading, "run_prediction_matcher", lambda s, c: [
        {"market_id": "2774056", "side": "NO", "play_type": "resolution",
         "similarity": 0.8, "target": None}])
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(trading, "_parse_pg_market", lambda m: {
        "market_id": "2774056", "prices": [0.13, 0.87], "yes_price": 0.13,
        "token_ids": ["tokA", "tokB"], "closed": False, "uma_status": "", "end_date": "x"})
    monkeypatch.setattr(trading, "_sleeve_a_entry_ok", lambda price, tok: True)
    opened_calls = []

    def fake_open(book, **kw):
        opened_calls.append(kw)
        row = {"execution": "live", "sleeve": "A", "cost_basis": kw["amount"],
               "instrument": kw["market_id"], "outcome": kw["outcome"], "status": "open"}
        book["positions"].append(row)
        return row

    monkeypatch.setattr(polygram_live, "open_live_position", fake_open)
    book = {"positions": []}
    n = trading.open_sleeve_a_live(book, [{"topic": "hormuz"}], "2026-07-21")
    assert n == 1
    kw = opened_calls[0]
    assert kw["sleeve"] == "A" and kw["event_id"] == "evt_h"
    assert kw["market_id"] == "2774056" and kw["token_id"] == "tokB"  # NO → side_index 1
    assert kw["outcome"] == "No" and kw["amount"] == 2.0


def test_open_sleeve_a_live_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", False)
    book = {"positions": []}
    assert trading.open_sleeve_a_live(book, [{"topic": "x"}], "2026-07-21") == 0
    assert book["positions"] == []


def test_open_sleeve_a_live_skips_when_gate_fails(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_A_ENABLED", True)
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(trading, "_gather_pg_candidates", lambda s: [
        {"market_id": "m", "question": "q", "yes_price": 0.1, "end_date": "x", "event_id": "evt"}])
    monkeypatch.setattr(trading, "run_prediction_matcher", lambda s, c: [
        {"market_id": "m", "side": "NO", "play_type": "resolution", "similarity": 0.8, "target": None}])
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(trading, "_parse_pg_market", lambda m: {
        "market_id": "m", "prices": [0.1, 0.9], "yes_price": 0.1,
        "token_ids": ["a", "b"], "closed": False, "uma_status": "", "end_date": "x"})
    monkeypatch.setattr(trading, "_sleeve_a_entry_ok", lambda price, tok: False)  # gate fails
    calls = []
    monkeypatch.setattr(polygram_live, "open_live_position", lambda book, **k: calls.append(k))
    book = {"positions": []}
    assert trading.open_sleeve_a_live(book, [{"topic": "x"}], "2026-07-21") == 0
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: FAIL — `AttributeError: module 'trading' has no attribute 'open_sleeve_a_live'`

- [ ] **Step 3: Implement the entry path + wire it in**

```python
# trading.py — add near _open_prediction_positions. `import polygram_live` at top of trading.py
# (new dependency; polygram_live imports FROM trading, so import it lazily INSIDE the function to
# avoid a circular import at module load).
def open_sleeve_a_live(book, signals, today) -> int:
    """Open real-money Sleeve-A favorite-fade positions. Creds+flag gated; returns count.

    Runs its own matcher pass, then for each match: buy the held favorite side only when
    it is in-band and tight-spread (via _sleeve_a_entry_ok) and an eventId is known, sized
    at PG_A_STAKE, deduped against open live rows. All opens route through the fail-closed
    polygram_live.open_live_position (kill-switch + cap enforced there)."""
    import polygram_live

    if not (common.PG_LIVE_ENABLED and common.PG_A_ENABLED):
        return 0
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        return 0
    candidates = _gather_pg_candidates(signals)
    if not candidates:
        return 0
    ev_by_mid = {c["market_id"]: c.get("event_id") for c in candidates}
    matches = run_prediction_matcher(signals, candidates)
    live_open = {
        (p.get("instrument"), p.get("outcome"))
        for p in book["positions"]
        if p.get("execution") == "live" and p.get("status") == "open"
    }
    exposure = sum(
        (p.get("cost_basis") or 0.0)
        for p in book["positions"]
        if p.get("execution") == "live" and p.get("status") == "open"
    )
    opened = 0
    for mt in matches:
        if mt["similarity"] < PG_SIMILARITY_FLOOR:
            continue
        mid, side = mt["market_id"], mt["side"]
        event_id = ev_by_mid.get(mid)
        if not event_id:
            log.warning(f"Sleeve A skip: no eventId for {mid}")
            continue
        outcome = "Yes" if side == "YES" else "No"
        if (mid, outcome) in live_open:
            continue
        m = polygram_market(mid)
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None or parsed["closed"]:
            continue
        side_index = 0 if side == "YES" else 1
        if len(parsed["prices"]) <= side_index or len(parsed["token_ids"]) <= side_index:
            continue
        held_price = parsed["prices"][side_index]
        token_id = parsed["token_ids"][side_index]
        if held_price is None or not _sleeve_a_entry_ok(held_price, token_id):
            continue
        row = polygram_live.open_live_position(
            book,
            sleeve="A",
            event_id=event_id,
            market_id=mid,
            token_id=token_id,
            outcome=outcome,
            side_index=side_index,
            amount=common.PG_A_STAKE,
            topic=parsed["question"],
            source_id=None,
            source_kind="unknown",
            source_perspective=None,
            live_exposure=exposure,
        )
        if row is not None:
            live_open.add((mid, outcome))
            exposure += common.PG_A_STAKE
            opened += 1
    return opened
```

Wire into `mode_paper` (trading.py:1655), inside the existing book lock, fail-safe so a live failure never breaks the paper run:

```python
        opened += _open_prediction_positions(book, signals, today, open_keys)

        try:
            opened += open_sleeve_a_live(book, signals, today)
        except Exception as e:  # live path is non-load-bearing for the paper run
            log.warning(f"Sleeve A live open failed: {e}")

        save_book(book)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS (3 new tests + existing prediction tests green)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-a): live favorite-fade entry path wired into mode_paper (flag/creds gated, fail-safe)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Live exit sweep (repricing-take / stop / time-stop)

**Files:**
- Modify: `trading.py` — new `_sleeve_a_exit_reason` (pure decision) + `sweep_live_exits`
- Test: `tests/test_prediction.py`

**Interfaces:**
- Produces: `_sleeve_a_exit_reason(held_price, entry_price, days_open, days_to_end) -> str | None` — `"take"` | `"stop"` | `"time_stop"` | None (hold). Near-dated (`days_to_end ≤ PG_A_NEAR_DAYS`) suppresses time_stop (ride to settlement).
- Produces: `sweep_live_exits(book, today) -> int` — mark each open live row from the live market and close via `polygram_live.close_live_position` when a reason fires. Count closed. Caller holds the book lock.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prediction.py (append)
def test_sleeve_a_exit_reason(monkeypatch):
    monkeypatch.setattr(common, "PG_A_TAKE", 0.97)
    monkeypatch.setattr(common, "PG_A_STOP", 0.15)
    monkeypatch.setattr(common, "PG_A_TIME_STOP_DAYS", 21)
    monkeypatch.setattr(common, "PG_A_NEAR_DAYS", 10)
    R = trading._sleeve_a_exit_reason
    assert R(0.98, 0.85, 3, 40) == "take"            # repriced to ceiling
    assert R(0.68, 0.85, 3, 40) == "stop"            # 0.85-0.68=0.17 ≥ 0.15 adverse
    assert R(0.86, 0.85, 25, 40) == "time_stop"      # stale, not near settlement
    assert R(0.86, 0.85, 25, 5) is None              # near settlement ⇒ ride it
    assert R(0.86, 0.85, 3, 40) is None              # healthy, hold


def test_sweep_live_exits_closes_on_take(monkeypatch):
    from datetime import date
    row = {"execution": "live", "sleeve": "A", "status": "open", "instrument": "m",
           "outcome": "No", "side_index": 1, "entry_price": 0.85, "cost_basis": 2.0,
           "entry_date": "2026-07-01", "end_date": "2026-09-01"}
    book = {"positions": [row]}
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"raw": mid})
    monkeypatch.setattr(trading, "_parse_pg_market", lambda m: {
        "market_id": "m", "prices": [0.02, 0.98], "yes_price": 0.02,
        "token_ids": ["a", "b"], "closed": False, "uma_status": "", "end_date": "2026-09-01"})
    closed = []

    def fake_close(r, reason):
        r["status"] = "closed"; r["close_reason"] = reason; closed.append(reason); return True

    import polygram_live
    monkeypatch.setattr(polygram_live, "close_live_position", fake_close)
    n = trading.sweep_live_exits(book, "2026-07-20")
    assert n == 1 and closed == ["take"] and row["status"] == "closed"


def test_sweep_live_exits_skips_paper_rows(monkeypatch):
    book = {"positions": [{"execution": "paper", "status": "open", "instrument": "m",
                           "asset_class": "prediction"}]}
    import polygram_live
    monkeypatch.setattr(polygram_live, "close_live_position",
                        lambda r, reason: (_ for _ in ()).throw(AssertionError("paper touched")))
    assert trading.sweep_live_exits(book, "2026-07-20") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: FAIL — `AttributeError: ... '_sleeve_a_exit_reason'`

- [ ] **Step 3: Implement**

```python
# trading.py (add near _mtm_prediction) — `from datetime import datetime, timezone` already imported
def _sleeve_a_exit_reason(held_price, entry_price, days_open, days_to_end):
    """Decide a live favorite-fade exit. take > stop > time_stop; near-dated holds to settlement."""
    if held_price >= common.PG_A_TAKE:
        return "take"
    if entry_price - held_price >= common.PG_A_STOP:
        return "stop"
    if days_to_end is not None and days_to_end <= common.PG_A_NEAR_DAYS:
        return None  # ride near-dated to settlement (reconcile handles it)
    if days_open >= common.PG_A_TIME_STOP_DAYS:
        return "time_stop"
    return None


def sweep_live_exits(book, today) -> int:
    """Hourly exit sweep for open live Sleeve-A rows. Marks from the live market and closes
    via polygram_live.close_live_position on take/stop/time-stop. Caller holds the book lock."""
    import polygram_live

    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    closed = 0
    for p in book.get("positions", []):
        if p.get("execution") != "live" or p.get("sleeve") != "A" or p.get("status") != "open":
            continue
        m = polygram_market(p["instrument"])
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None:
            log.warning(f"Sleeve A sweep: no price for {p.get('id')}; kept open")
            continue
        si = p["side_index"]
        if len(parsed["prices"]) <= si or parsed["prices"][si] is None:
            continue
        held = parsed["prices"][si]
        days_open = (today_d - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        days_to_end = None
        end = parsed.get("end_date") or p.get("end_date")
        if end:
            try:
                days_to_end = (datetime.strptime(str(end)[:10], "%Y-%m-%d").date() - today_d).days
            except ValueError:
                days_to_end = None
        reason = _sleeve_a_exit_reason(held, p["entry_price"], days_open, days_to_end)
        if reason and polygram_live.close_live_position(p, reason):
            closed += 1
    return closed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_prediction.py
git commit -F - <<'EOF'
feat(sleeve-a): hourly live exit sweep — repricing-take / stop / time-stop, near-dated holds

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Wire the monitor + keep live rows out of the weekly measurement path

**Files:**
- Modify: `trading.py` — `_mtm_prediction` (skip guard is at the `mark_to_market` dispatch; add an `execution=="live"` skip) ; `brief.py` — `mode_monitor`
- Test: `tests/test_prediction.py`, `tests/test_monitor.py`

**Interfaces:**
- Consumes: `sweep_live_exits`, `polygram_live.reconcile_live_book`.
- Produces: `mode_monitor` runs (under the book lock) `sweep_live_exits` + `reconcile_live_book` each hour; `mark_to_market` never marks/holds `execution=="live"` rows.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prediction.py (append) — live rows must be skipped by the weekly measurement path
def test_mark_to_market_skips_live_rows(monkeypatch):
    called = []
    monkeypatch.setattr(trading, "_mtm_prediction", lambda p, td, ts: called.append(p["id"]))
    monkeypatch.setattr(trading, "mark_to_market", trading.mark_to_market)  # ensure real fn under test
    book = {"positions": [
        {"id": "live1", "execution": "live", "sleeve": "A", "asset_class": "prediction",
         "status": "open", "side_index": 1, "entry_price": 0.85, "entry_date": "2026-07-01"},
    ]}
    trading.mark_to_market(book, "2026-07-20")
    assert "live1" not in called  # weekly measurement path never touches live rows
```

```python
# tests/test_monitor.py (append)
def test_mode_monitor_runs_live_exit_and_reconcile(monkeypatch):
    import brief, trading, polygram_live
    monkeypatch.setattr(brief, "run_volume_monitor", lambda: [])
    swept, reconciled = [], []
    monkeypatch.setattr(trading, "sweep_live_exits", lambda book, today: swept.append(True) or 0)
    monkeypatch.setattr(polygram_live, "reconcile_live_book", lambda book: reconciled.append(True) or 0)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    saved = {}
    monkeypatch.setattr(brief, "save_book", lambda b: saved.setdefault("b", b))
    brief.mode_monitor()
    assert swept == [True] and reconciled == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prediction.py::test_mark_to_market_skips_live_rows tests/test_monitor.py::test_mode_monitor_runs_live_exit_and_reconcile -q`
Expected: FAIL — live row is marked by `mark_to_market`; `mode_monitor` doesn't call the sweep.

- [ ] **Step 3: Implement**

In `mark_to_market` (trading.py:1720), at the top of the per-position loop, skip live rows so they never enter `_mtm_prediction`'s measurement-holds:

```python
    for p in book.get("positions", []):
        if p.get("status") != "open":
            continue
        if p.get("execution") == "live":
            continue  # live rows exit via the hourly sweep, never the weekly measurement path
        # ... existing equity/crypto/prediction dispatch unchanged ...
```

In `mode_monitor` (brief.py:2944), add the live sweep + reconcile under the book lock, after the volume alerts:

```python
def mode_monitor():
    """Hourly cross-asset volume-anomaly alerts + live-position exit sweep/reconcile."""
    log.info("=== MONITOR ===")
    alerts = run_volume_monitor()
    if alerts:
        telegram_send_long("\n\n".join(alerts))
    try:
        with file_lock(trading.BOOK_FILE, timeout=trading.BOOK_LOCK_TIMEOUT):
            book = load_book()
            n_exit = trading.sweep_live_exits(book, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            n_rec = polygram_live.reconcile_live_book(book)
            if n_exit or n_rec:
                save_book(book)
                log.info(f"Live sweep: {n_exit} exited, {n_rec} reconciled")
    except Exception as e:  # never let the live sweep break the monitor cron
        log.warning(f"Live exit sweep failed: {e}")
```

Ensure `brief.py` imports `polygram_live`, `file_lock`, and `datetime`/`timezone` (add any missing).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_prediction.py tests/test_monitor.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading.py brief.py tests/test_prediction.py tests/test_monitor.py
git commit -F - <<'EOF'
feat(sleeve-a): hourly monitor runs live exit sweep + reconcile; weekly MtM skips live rows

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Settled-proceeds backfill from `/trade/history`

**Files:**
- Modify: `polygram_live.py` — `trade_history()` + `backfill_settled(book)`; call in `mode_monitor` (brief.py)
- Test: `tests/test_polygram_live.py`, `tests/test_monitor.py`

**Interfaces:**
- Produces: `polygram_live.trade_history() -> list | None` (GET /trade/history; None on failed read); `backfill_settled(book) -> int` — for live rows `close_reason=="settled"` with `realized_return is None`, fill `realized_return` from the matching history entry's realized proceeds vs `cost_basis`. Returns count filled.

> **Note:** `reconcile_live_book` (foundation) flips vanished live rows to `closed`/`"settled"` but leaves `realized_return` unset. This closes that loop. Matching key: the venue history entry for the row's `market_id`/`outcome` (live-verify the exact history field names when creds are live; parse defensively, skip unmatched).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
def test_backfill_settled_fills_realized(monkeypatch):
    monkeypatch.setattr(polygram_live, "trade_history", lambda: [
        {"marketId": "m", "outcome": "No", "type": "settlement", "proceeds": 2.5}])
    row = {"execution": "live", "status": "closed", "close_reason": "settled",
           "instrument": "m", "outcome": "No", "cost_basis": 2.0, "realized_return": None}
    book = {"positions": [row]}
    assert polygram_live.backfill_settled(book) == 1
    assert abs(row["realized_return"] - 0.25) < 1e-9  # 2.5/2.0 - 1


def test_backfill_settled_skips_when_history_unreadable(monkeypatch):
    monkeypatch.setattr(polygram_live, "trade_history", lambda: None)
    row = {"execution": "live", "status": "closed", "close_reason": "settled",
           "instrument": "m", "outcome": "No", "cost_basis": 2.0, "realized_return": None}
    book = {"positions": [row]}
    assert polygram_live.backfill_settled(book) == 0
    assert row["realized_return"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: ... 'trade_history'`

- [ ] **Step 3: Implement**

```python
# polygram_live.py (append)
def trade_history():
    """Trade execution history via GET /trade/history. Returns the list, or None on failed read."""
    data = _pg_request("GET", "/trade/history")
    if isinstance(data, dict):
        items = data.get("history") or data.get("trades")
        return items if isinstance(items, list) else None
    return items if isinstance(data, list) else None


def backfill_settled(book):
    """Fill realized_return on settled live rows from /trade/history. None history ⇒ no-op.

    realized_return = proceeds / cost_basis - 1, matched by (marketId, outcome)."""
    pending = [
        p for p in book.get("positions", [])
        if p.get("execution") == "live"
        and p.get("close_reason") == "settled"
        and p.get("realized_return") is None
    ]
    if not pending:
        return 0
    hist = trade_history()
    if hist is None:
        return 0
    by_key = {}
    for h in hist:
        if isinstance(h, dict):
            by_key.setdefault((h.get("marketId"), h.get("outcome")), h)
    n = 0
    for p in pending:
        h = by_key.get((p.get("instrument"), p.get("outcome")))
        if not h:
            continue
        try:
            proceeds = float(h.get("proceeds"))
        except (TypeError, ValueError):
            continue
        cost = p.get("cost_basis") or 0.0
        p["realized_return"] = (proceeds / cost - 1.0) if cost else 0.0
        n += 1
    return n
```

Add `backfill_settled(book)` to the `mode_monitor` live block (Task 5), right after `reconcile_live_book`, contributing to the same save:

```python
            n_rec = polygram_live.reconcile_live_book(book)
            n_fill = polygram_live.backfill_settled(book)
            if n_exit or n_rec or n_fill:
                save_book(book)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polygram_live.py brief.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(sleeve-a): backfill realized_return on settled live rows from /trade/history

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: Reporting split — paper-only gate + live line

**Files:**
- Modify: `validation.py` — `aggregate_performance` (paper-only) + a live summary
- Test: `tests/test_validation.py`

**Interfaces:**
- Produces: `aggregate_performance(book)` counts only `execution!="live"` (paper) closed rows — the go-live gate stays a paper-calibration instrument. Produces `live_performance(book) -> dict` — realized stats over closed `execution=="live"` rows (n, mean net return, by `sleeve`), surfaced in `performance_report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validation.py (append)
import validation


def test_aggregate_performance_excludes_live():
    book = {"positions": [
        {"status": "closed", "asset_class": "prediction", "execution": "paper", "realized_return": 0.05},
        {"status": "closed", "asset_class": "prediction", "execution": "live", "sleeve": "A", "realized_return": -0.9},
    ]}
    agg = validation.aggregate_performance(book)
    assert agg["overall"]["n"] == 1  # live row excluded from the paper gate


def test_live_performance_reports_live_only():
    book = {"positions": [
        {"status": "closed", "execution": "live", "sleeve": "A", "realized_return": 0.1},
        {"status": "closed", "execution": "live", "sleeve": "A", "realized_return": -0.2},
        {"status": "closed", "execution": "paper", "realized_return": 0.5},
    ]}
    lp = validation.live_performance(book)
    assert lp["n"] == 2 and abs(lp["mean_return"] - (-0.05)) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_validation.py -q`
Expected: FAIL — live row counted in `aggregate_performance`; no `live_performance`.

- [ ] **Step 3: Implement**

In `aggregate_performance` (validation.py:57), restrict the closed set to paper:

```python
    closed = [
        p for p in book.get("positions", [])
        if p.get("status") == "closed" and p.get("execution", "paper") != "live"
    ]
```

Add a live summary (validation.py, near `aggregate_performance`):

```python
def live_performance(book: dict) -> dict:
    """Realized stats over closed live rows (real money), separate from the paper gate."""
    live = [
        p for p in book.get("positions", [])
        if p.get("status") == "closed"
        and p.get("execution") == "live"
        and p.get("realized_return") is not None
    ]
    if not live:
        return {"n": 0, "mean_return": 0.0, "by_sleeve": {}}
    rets = [p["realized_return"] for p in live]
    by_sleeve: dict = {}
    for p in live:
        by_sleeve.setdefault(p.get("sleeve", "?"), []).append(p["realized_return"])
    return {
        "n": len(live),
        "mean_return": sum(rets) / len(rets),
        "by_sleeve": {k: sum(v) / len(v) for k, v in by_sleeve.items()},
    }
```

Surface one line in `performance_report` (validation.py:208) — after the go-live gate block, if `live_performance(book)["n"]`:

```python
    lp = live_performance(book)
    if lp["n"]:
        lines.append(
            f"<b>💵 LIVE (real money)</b>: {lp['n']} closed, "
            f"mean net {lp['mean_return'] * 100:+.1f}%"
        )
```

- [ ] **Step 4: Run test to verify it passes + full gate**

Run: `python -m pytest tests/test_validation.py -q`
Then the full gate: `python -m ruff check . ; python -m ruff format --check . ; python -m pytest -q`
Expected: all pass (stage any file ruff reformats).

- [ ] **Step 5: Commit**

```bash
git add validation.py tests/test_validation.py
git commit -F - <<'EOF'
feat(sleeve-a): split reporting — paper-only go-live gate + separate live P&L line

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review

**Spec coverage (Sleeve A slice):**
- Favorite-side + band + live-orderbook spread gate — Tasks 2, 3 ✓ (reuses `_fetch_pg_half_spread`).
- `eventId` capture (the resolved integration unknown) — Task 1 ✓ (from the `/search` wrapper; defensive key + fail-closed skip).
- Live entry via the foundation's `open_live_position`, flag/creds gated, fail-safe in `mode_paper` — Task 3 ✓.
- Repricing-take / stop / time-stop exits, near-dated hold, hourly on the monitor — Tasks 4, 5 ✓; measurement-holds excluded (live rows skip `mark_to_market`) — Task 5 ✓.
- Reconcile + settled-proceeds backfill (foundation deferral closed) — Tasks 5, 6 ✓.
- Live/paper reporting split (paper-only gate) — Task 7 ✓.
- **Deferred to Sleeve B plan:** the `/predict` conviction-hold wizard, per-position/total sleeve-B caps, thesis-log + resolution-aware retention. **Fee-drag measurement** rides free — every fill already records `spread_fee`/`trade_fee` (foundation Task 5); a `summarize_live_fees` helper is a trivial follow-up once live data exists (noted, not built — YAGNI until there are fills).

**Placeholder scan:** none — every code step is complete. The two live-verify points (event-id key, `/trade/history` field names) are defended in code (fallback keys + fail-closed skips), not placeholders.

**Type consistency:** `_gather_pg_candidates` adds `event_id` (Task 1) consumed by `open_sleeve_a_live` (Task 3); `open_sleeve_a_live` calls `polygram_live.open_live_position` with the foundation's exact keyword signature; `_sleeve_a_exit_reason` returns the reason string `close_live_position` receives; `backfill_settled` matches `reconcile_live_book`'s `settled` rows; `live_performance` reads the `sleeve`/`execution`/`realized_return` fields Task 3/foundation write. Consistent.

## Execution Handoff

Next plan (after this lands): **Sleeve B** — `/predict` conversational conviction-hold wizard (reusing the `_wizard_*`/`_handle_callback_query` infra), per-position "money-I-can-zero" + total sleeve caps, no-DCA guard, and the thesis-log + resolution-aware retention (the calibration corpus).

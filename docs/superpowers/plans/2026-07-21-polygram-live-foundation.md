# PolyGram Live Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared real-money execution/funding/safety rails on polygram.ink — a `polygram_live.py` write layer, `execution`/`sleeve` book fields, kill-switch + exposure caps, and fill/reconcile plumbing — that both trading sleeves will later ride. No strategy logic here.

**Architecture:** A new `polygram_live.py` module wraps polygram.ink's `/trade/*` + `/wallet` + `/orderbook` endpoints behind a **fail-closed**, None-on-failure API, reusing the existing JWT/token-file auth from `trading.py`. Live positions are ordinary `book.json` rows tagged `execution:"live"` + `sleeve`, opened only after a pre-trade cap check and closed by mapping to the venue's `positionId`. A reconcile pass makes the venue authoritative over the book.

**Tech Stack:** Python 3, `requests` (already used), `pytest` (offline — HTTP injected via monkeypatch). No new dependencies.

## Global Constraints

- **Real money ⇒ fail-CLOSED** (opposite of the brief's fail-open): on any doubt, do **not** trade; never leave the book disagreeing with the venue. Every order path wrapped so it never crashes collect/monitor.
- **Master kill-switch:** nothing places a live order unless `PG_LIVE_ENABLED` is truthy AND `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` are set. Default OFF.
- **Base URL:** `https://polygram.ink/api` (existing `POLYGRAM_BASE`). Auth: `Authorization: Bearer <jwt>` via the existing `POLYGRAM_TOKEN_FILE` + `polygram_login()` refresh-on-401 pattern.
- **Units:** prices 0–1; `amount` is **USD** (min $1); a market fill returns `fillPrice`, `shares`, `spreadFee`, `tradeFee`, `totalFee`. Record fees **verbatim** from the fill (do not recompute).
- **None-on-failure:** every network helper returns `None` on any non-2xx/parse/network error (mirror `_polygram_get`). Callers treat `None` as "did not happen."
- **Persistence:** use `_write_json_atomic` / `_load_json_or` (from `common.py`) and hold `file_lock(BOOK_FILE, timeout=BOOK_LOCK_TIMEOUT)` across every load→mutate→save of the book (mirror `mode_paper`).
- **New top-level module chore (`dockerfile-copy-allowlist`):** `polygram_live.py` must be added to the Dockerfile `COPY` line, the GitHub workflow path lists, and the workflow ruff file lists — or it's a runtime `ModuleNotFound` that passes CI lint. Done in Task 2.
- **Pre-push gate (`brief-local-run`):** `ruff check .` + `ruff format --check .` + `pytest` must all pass; stage every reformatted file. Run `pytest` via the PowerShell tool (Bash tool = "stdin is not a tty"); commit via the Bash tool (PowerShell prepends a BOM to commit subjects).

---

### Task 1: Live-trading config + safety constants

**Files:**
- Modify: `common.py` (env config block, near the other `os.environ.get` reads ~line 88)
- Test: `tests/test_polygram_live.py` (new)

**Interfaces:**
- Produces: `common.PG_LIVE_ENABLED: bool`, `common.PG_LIVE_TOTAL_CAP: float`, `common.PG_LIVE_PER_TRADE_CAP: float` — imported by `polygram_live.py` and the sleeve plans.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py
import importlib
import common


def test_live_config_defaults_off(monkeypatch):
    monkeypatch.delenv("PG_LIVE_ENABLED", raising=False)
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is False
    assert common.PG_LIVE_TOTAL_CAP == 50.0
    assert common.PG_LIVE_PER_TRADE_CAP == 5.0


def test_live_config_reads_env(monkeypatch):
    monkeypatch.setenv("PG_LIVE_ENABLED", "1")
    monkeypatch.setenv("PG_LIVE_TOTAL_CAP", "120")
    monkeypatch.setenv("PG_LIVE_PER_TRADE_CAP", "3")
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is True
    assert common.PG_LIVE_TOTAL_CAP == 120.0
    assert common.PG_LIVE_PER_TRADE_CAP == 3.0
    monkeypatch.delenv("PG_LIVE_ENABLED", raising=False)
    importlib.reload(common)
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell): `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: module 'common' has no attribute 'PG_LIVE_ENABLED'`

- [ ] **Step 3: Implement the config**

```python
# common.py — add near the POLYGRAM_EMAIL/PASSWORD reads (~line 88)
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Live prediction trading (real money). Default OFF; funded/enabled per host.
PG_LIVE_ENABLED = _env_flag("PG_LIVE_ENABLED")
PG_LIVE_TOTAL_CAP = _env_float("PG_LIVE_TOTAL_CAP", 50.0)  # max USD across all live rows
PG_LIVE_PER_TRADE_CAP = _env_float("PG_LIVE_PER_TRADE_CAP", 5.0)  # max USD per order
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add common.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(live): PG_LIVE_ENABLED kill-switch + exposure-cap config (default off)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `polygram_live.py` module + authed request helper + read endpoints

**Files:**
- Create: `polygram_live.py`
- Modify: `Dockerfile` (COPY allowlist), `.github/workflows/*.yml` (path + ruff file lists)
- Test: `tests/test_polygram_live.py`

**Interfaces:**
- Consumes: `trading.POLYGRAM_BASE`, `trading.POLYGRAM_TOKEN_FILE`, `trading.polygram_login`, `common._load_json_or`, `common.log`.
- Produces: `polygram_live._pg_request(method, path, params=None, json_body=None) -> dict | None`; `wallet_balance() -> float | None`; `orderbook(token_id) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
import polygram_live


def test_wallet_balance_parses(monkeypatch):
    monkeypatch.setattr(
        polygram_live, "_pg_request",
        lambda *a, **k: {"balance": 1250.0, "currency": "USD"},
    )
    assert polygram_live.wallet_balance() == 1250.0


def test_wallet_balance_none_on_failure(monkeypatch):
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    assert polygram_live.wallet_balance() is None


def test_orderbook_spread_passthrough(monkeypatch):
    monkeypatch.setattr(
        polygram_live, "_pg_request",
        lambda *a, **k: {"bids": [], "asks": [], "spread": 0.02, "midpoint": 0.62},
    )
    ob = polygram_live.orderbook("0xabc")
    assert ob["spread"] == 0.02 and ob["midpoint"] == 0.62
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'polygram_live'`

- [ ] **Step 3: Create the module**

```python
# polygram_live.py
"""Real-money write layer for polygram.ink. Fail-closed, None-on-failure.

Reuses trading.py's JWT/token-file auth. Every network helper returns None on
any non-2xx / parse / network error; callers treat None as "did not happen".
"""

import requests

from common import _load_json_or, log
from trading import POLYGRAM_BASE, POLYGRAM_TOKEN_FILE, polygram_login

_TIMEOUT = 30


def _pg_request(method, path, params=None, json_body=None):
    """Authed request to a polygram.ink path; refresh the JWT once on 401.

    Returns parsed JSON dict on 2xx, else None (network error, non-2xx after a
    refresh attempt, or unparseable body). Mirrors trading._polygram_get.
    """
    token = (_load_json_or(POLYGRAM_TOKEN_FILE, {}) or {}).get("token") or polygram_login()
    if not token:
        return None
    url = f"{POLYGRAM_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = requests.request(
                method, url,
                headers={"Authorization": f"Bearer {token}"},
                params=params, json=json_body, timeout=_TIMEOUT,
            )
        except Exception as e:
            log.warning(f"PolyGram {method} {path} failed: {e}")
            return None
        if resp.status_code == 401 and attempt == 1:
            token = polygram_login()
            if not token:
                return None
            continue
        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"PolyGram {method} {path} failed: {e}")
            return None
    return None


def wallet_balance():
    """Current USD cash balance, or None on failure. GET /wallet."""
    data = _pg_request("GET", "/wallet")
    if not isinstance(data, dict) or "balance" not in data:
        return None
    try:
        return float(data["balance"])
    except (TypeError, ValueError):
        return None


def orderbook(token_id):
    """Live orderbook {bids, asks, spread, midpoint} for a token, or None. GET /orderbook/:id."""
    return _pg_request("GET", f"/orderbook/{token_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Update the deploy allowlists (or it ModuleNotFounds in prod)**

In `Dockerfile`, add `polygram_live.py` to the `COPY` line that lists the top-level modules (alongside `trading.py`, `validation.py`, etc.). In each `.github/workflows/*.yml` that lists Python files for path-filters or `ruff`, add `polygram_live.py` next to `trading.py`.

Verify: `git grep -n "trading.py" Dockerfile .github/workflows` — add `polygram_live.py` at every hit that enumerates modules.

- [ ] **Step 6: Commit**

```bash
git add polygram_live.py tests/test_polygram_live.py Dockerfile .github/workflows
git commit -F - <<'EOF'
feat(live): polygram_live module — authed request helper + wallet/orderbook reads

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Order primitives — place (market), sell, list positions

**Files:**
- Modify: `polygram_live.py`
- Test: `tests/test_polygram_live.py`

**Interfaces:**
- Consumes: `_pg_request`.
- Produces:
  - `place_market_order(event_id, market_id, token_id, outcome, amount) -> dict | None` → normalized fill `{"order_id", "fill_price", "shares", "spread_fee", "trade_fee", "total_fee", "status"}`.
  - `sell_position(position_id, shares=None) -> dict | None` → `{"shares_sold", "sale_price", "proceeds", "profit", "fee", "status"}`.
  - `list_positions() -> list[dict]` (empty list on failure — never None; reconcile must distinguish "no positions" from "couldn't read"; see note).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
def test_place_market_order_normalizes_fill(monkeypatch):
    captured = {}

    def fake(method, path, params=None, json_body=None):
        captured["path"] = path
        captured["body"] = json_body
        return {"success": True, "order": {
            "id": "ord_1", "fillPrice": 0.62, "shares": 161.29,
            "spreadFee": 1.5, "tradeFee": 0.5, "totalFee": 2.0, "status": "filled",
        }}

    monkeypatch.setattr(polygram_live, "_pg_request", fake)
    fill = polygram_live.place_market_order("evt_a", "mkt_b", "0xabc", "Yes", 100)
    assert captured["path"] == "/trade/place"
    assert captured["body"] == {
        "eventId": "evt_a", "marketId": "mkt_b", "tokenId": "0xabc",
        "outcome": "Yes", "amount": 100,
    }
    assert fill == {
        "order_id": "ord_1", "fill_price": 0.62, "shares": 161.29,
        "spread_fee": 1.5, "trade_fee": 0.5, "total_fee": 2.0, "status": "filled",
    }


def test_place_market_order_none_when_unfilled(monkeypatch):
    monkeypatch.setattr(polygram_live, "_pg_request",
                        lambda *a, **k: {"success": True, "order": {"status": "rejected"}})
    assert polygram_live.place_market_order("e", "m", "t", "Yes", 5) is None


def test_sell_position_full(monkeypatch):
    captured = {}

    def fake(method, path, params=None, json_body=None):
        captured["body"] = json_body
        return {"success": True, "sale": {
            "sharesSold": 161.29, "salePrice": 0.72, "proceeds": 116.13,
            "profit": 14.13, "fee": 1.16, "status": "completed"}}

    monkeypatch.setattr(polygram_live, "_pg_request", fake)
    r = polygram_live.sell_position("pos_1")
    assert captured["body"] == {"positionId": "pos_1"}
    assert r["proceeds"] == 116.13 and r["status"] == "completed"


def test_list_positions_empty_on_failure(monkeypatch):
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    assert polygram_live.list_positions() is None  # None = couldn't read (see note)
```

> **Note (fail-closed):** `list_positions()` returns `None` when the read *fails* and `[]` only when the venue genuinely reports no positions. Reconcile must never interpret a failed read as "all positions gone" — that would falsely settle everything. This distinction is load-bearing; keep it.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: module 'polygram_live' has no attribute 'place_market_order'`

- [ ] **Step 3: Implement the primitives**

```python
# polygram_live.py (append)
def place_market_order(event_id, market_id, token_id, outcome, amount):
    """Market buy via POST /trade/place. Returns a normalized fill or None.

    None when the request fails OR the venue did not report status 'filled'
    (fail-closed: no phantom position row is ever written on a non-fill).
    """
    data = _pg_request("POST", "/trade/place", json_body={
        "eventId": event_id, "marketId": market_id, "tokenId": token_id,
        "outcome": outcome, "amount": amount,
    })
    order = (data or {}).get("order") if isinstance(data, dict) else None
    if not isinstance(order, dict) or order.get("status") != "filled":
        log.warning(f"PolyGram place not filled for {market_id}/{outcome}: {data}")
        return None
    try:
        return {
            "order_id": order["id"],
            "fill_price": float(order["fillPrice"]),
            "shares": float(order["shares"]),
            "spread_fee": float(order.get("spreadFee") or 0.0),
            "trade_fee": float(order.get("tradeFee") or 0.0),
            "total_fee": float(order.get("totalFee") or 0.0),
            "status": order["status"],
        }
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"PolyGram fill parse failed for {market_id}: {e}")
        return None


def sell_position(position_id, shares=None):
    """Sell a live position via POST /trade/sell. Returns normalized sale or None."""
    body = {"positionId": position_id}
    if shares is not None:
        body["shares"] = shares
    data = _pg_request("POST", "/trade/sell", json_body=body)
    sale = (data or {}).get("sale") if isinstance(data, dict) else None
    if not isinstance(sale, dict) or sale.get("status") != "completed":
        log.warning(f"PolyGram sell not completed for {position_id}: {data}")
        return None
    try:
        return {
            "shares_sold": float(sale["sharesSold"]),
            "sale_price": float(sale["salePrice"]),
            "proceeds": float(sale["proceeds"]),
            "profit": float(sale["profit"]),
            "fee": float(sale.get("fee") or 0.0),
            "status": sale["status"],
        }
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"PolyGram sale parse failed for {position_id}: {e}")
        return None


def list_positions():
    """Open venue positions via GET /trade/positions.

    Returns the list on success (possibly empty), or None if the read FAILED.
    None must never be treated as 'no positions' — see the fail-closed note.
    """
    data = _pg_request("GET", "/trade/positions")
    if not isinstance(data, dict) or "positions" not in data:
        return None
    positions = data["positions"]
    return positions if isinstance(positions, list) else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polygram_live.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(live): order primitives — market place, sell, list positions (fail-closed)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Pre-trade cap check

**Files:**
- Modify: `polygram_live.py`
- Test: `tests/test_polygram_live.py`

**Interfaces:**
- Consumes: `wallet_balance`, `common.PG_LIVE_PER_TRADE_CAP`, `common.PG_LIVE_TOTAL_CAP`.
- Produces: `cap_ok(amount, live_exposure) -> bool` — True only if the order is within per-trade cap, within total cap given current live exposure, and covered by wallet cash. Fail-closed: unreadable balance ⇒ False.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
import common


def test_cap_ok_rejects_over_per_trade(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(6.0, live_exposure=0.0) is False


def test_cap_ok_rejects_over_total(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(5.0, live_exposure=48.0) is False


def test_cap_ok_rejects_when_balance_unreadable(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: None)
    assert polygram_live.cap_ok(5.0, live_exposure=0.0) is False


def test_cap_ok_allows_within_all_limits(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(5.0, live_exposure=10.0) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: ... 'cap_ok'`

- [ ] **Step 3: Implement**

```python
# polygram_live.py (append) — import common at top: `import common`
import common  # add to the import block at the top of the file


def cap_ok(amount, live_exposure):
    """True only if `amount` USD is within every guard. Fail-closed on unreadable cash.

    - per-trade: amount <= PG_LIVE_PER_TRADE_CAP
    - total:     live_exposure + amount <= PG_LIVE_TOTAL_CAP
    - funded:    amount <= wallet_balance() (None balance ⇒ reject)
    """
    if amount <= 0 or amount > common.PG_LIVE_PER_TRADE_CAP:
        return False
    if live_exposure + amount > common.PG_LIVE_TOTAL_CAP:
        return False
    bal = wallet_balance()
    if bal is None or amount > bal:
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polygram_live.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(live): pre-trade cap_ok gate (per-trade, total, funded; fail-closed)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Open a live position row (book schema: `execution` + `sleeve` + real fill)

**Files:**
- Modify: `polygram_live.py`
- Test: `tests/test_polygram_live.py`

**Interfaces:**
- Consumes: `place_market_order`, `cap_ok`, `common.PG_LIVE_ENABLED`.
- Produces: `open_live_position(book, *, sleeve, event_id, market_id, token_id, outcome, side_index, amount, topic, source_id, source_kind, source_perspective, live_exposure) -> dict | None` — appends and returns the new row, or None (kill-switch off, cap fail, or unfilled). The row carries `execution:"live"`, `sleeve`, the venue fill, and `fees`.

> **Design:** this is the shared open path both sleeves call. It does **not** decide *whether* to trade (that's sleeve strategy) — it enforces the kill-switch + cap, places the market order, and writes a truthful row from the real fill. Caller holds the book lock.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
def _fill():
    return {"order_id": "ord_1", "fill_price": 0.62, "shares": 8.06,
            "spread_fee": 0.07, "trade_fee": 0.03, "total_fee": 0.10, "status": "filled"}


def test_open_live_position_writes_truthful_row(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(polygram_live, "cap_ok", lambda *a, **k: True)
    monkeypatch.setattr(polygram_live, "place_market_order", lambda *a, **k: _fill())
    book = {"positions": []}
    row = polygram_live.open_live_position(
        book, sleeve="A", event_id="evt_a", market_id="mkt_b", token_id="0xabc",
        outcome="No", side_index=1, amount=5.0, topic="Hormuz normal by Aug 31?",
        source_id="OilPrice.com", source_kind="wire", source_perspective=None,
        live_exposure=0.0,
    )
    assert row is not None
    assert row["execution"] == "live" and row["sleeve"] == "A"
    assert row["asset_class"] == "prediction" and row["venue"] == "polygram"
    assert row["instrument"] == "mkt_b" and row["event_id"] == "evt_a"
    assert row["outcome"] == "No" and row["side_index"] == 1
    assert row["entry_price"] == 0.62 and row["shares"] == 8.06
    assert row["cost_basis"] == 5.0 and row["fees"]["total_fee"] == 0.10
    assert row["status"] == "open" and row["source_kind"] == "wire"
    assert book["positions"][-1] is row


def test_open_live_position_noop_when_killswitch_off(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", False)
    book = {"positions": []}
    assert polygram_live.open_live_position(
        book, sleeve="A", event_id="e", market_id="m", token_id="t", outcome="Yes",
        side_index=0, amount=5.0, topic="x", source_id=None, source_kind="unknown",
        source_perspective=None, live_exposure=0.0) is None
    assert book["positions"] == []


def test_open_live_position_noop_on_cap_fail(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(polygram_live, "cap_ok", lambda *a, **k: False)
    placed = []
    monkeypatch.setattr(polygram_live, "place_market_order",
                        lambda *a, **k: placed.append(1))
    book = {"positions": []}
    assert polygram_live.open_live_position(
        book, sleeve="A", event_id="e", market_id="m", token_id="t", outcome="Yes",
        side_index=0, amount=99.0, topic="x", source_id=None, source_kind="unknown",
        source_perspective=None, live_exposure=0.0) is None
    assert placed == []  # cap checked BEFORE any order is placed
    assert book["positions"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: ... 'open_live_position'`

- [ ] **Step 3: Implement**

```python
# polygram_live.py (append) — add `from datetime import datetime, timezone` at top
from datetime import datetime, timezone  # add to imports


def open_live_position(book, *, sleeve, event_id, market_id, token_id, outcome,
                       side_index, amount, topic, source_id, source_kind,
                       source_perspective, live_exposure):
    """Place a real market buy and append a truthful live row. None if not opened.

    Order of guards (all fail-closed): kill-switch → cap_ok → place. No order is
    placed unless the cap passes; no row is written unless the order fills.
    Caller must hold the book lock.
    """
    if not common.PG_LIVE_ENABLED:
        return None
    if not cap_ok(amount, live_exposure):
        log.warning(f"Live open rejected by cap: {market_id}/{outcome} ${amount}")
        return None
    fill = place_market_order(event_id, market_id, token_id, outcome, amount)
    if fill is None:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {
        "id": f"{today}:prediction:{market_id}:{outcome.upper()}:live",
        "opened": today,
        "asset_class": "prediction",
        "venue": "polygram",
        "execution": "live",
        "sleeve": sleeve,
        "ticker": market_id,
        "instrument": market_id,
        "event_id": event_id,
        "token_id": token_id,
        "outcome": outcome,
        "side_index": side_index,
        "play_type": "resolution",
        "direction": "bullish",  # always long the held side (long-sense return)
        "topic": topic,
        "rationale": f"live open (sleeve {sleeve})",
        "source_id": source_id,
        "source_kind": source_kind,
        "source_perspective": source_perspective,
        "order_id": fill["order_id"],
        "entry_price": fill["fill_price"],
        "shares": fill["shares"],
        "cost_basis": amount,
        "fees": {
            "spread_fee": fill["spread_fee"],
            "trade_fee": fill["trade_fee"],
            "total_fee": fill["total_fee"],
        },
        "entry_date": today,
        "status": "open",
        "close_reason": None,
        "closed_date": None,
        "checkpoints": {},
        "last_mark": None,
        "realized_return": None,
    }
    book["positions"].append(row)
    log.info(f"LIVE OPEN {sleeve} {market_id}/{outcome} ${amount} @ {fill['fill_price']}")
    return row
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polygram_live.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(live): open_live_position — kill-switch+cap gated, truthful row from real fill

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Close a live position + reconcile (venue authoritative)

**Files:**
- Modify: `polygram_live.py`
- Test: `tests/test_polygram_live.py`

**Interfaces:**
- Consumes: `list_positions`, `sell_position`.
- Produces:
  - `_match_position_id(venue_positions, market_id, outcome) -> str | None` — the `pos_…` id for a book row.
  - `close_live_position(row, reason) -> bool` — map to `positionId`, market-sell, stamp `realized_return`/`status:"closed"`. False (row untouched) if the venue can't be read or the sell fails.
  - `reconcile_live_book(book) -> int` — for each open live row, if the venue no longer lists it → mark `closed`/`settled`; return count reconciled. **Skips entirely if `list_positions()` is None** (failed read ⇒ never mass-settle).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polygram_live.py (append)
def _live_row(market_id="mkt_b", outcome="No"):
    return {"id": "r1", "execution": "live", "sleeve": "A", "asset_class": "prediction",
            "instrument": market_id, "outcome": outcome, "side_index": 1,
            "entry_price": 0.80, "cost_basis": 5.0, "status": "open",
            "realized_return": None, "closed_date": None, "close_reason": None}


def test_match_position_id():
    venue = [{"id": "pos_x", "marketId": "mkt_b", "outcome": "No"},
             {"id": "pos_y", "marketId": "mkt_b", "outcome": "Yes"}]
    assert polygram_live._match_position_id(venue, "mkt_b", "No") == "pos_x"
    assert polygram_live._match_position_id(venue, "mkt_z", "No") is None


def test_close_live_position_sells_and_stamps(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions",
                        lambda: [{"id": "pos_x", "marketId": "mkt_b", "outcome": "No"}])
    monkeypatch.setattr(polygram_live, "sell_position",
                        lambda pid, shares=None: {"proceeds": 6.0, "sale_price": 0.96,
                                                  "profit": 1.0, "fee": 0.05,
                                                  "shares_sold": 6.25, "status": "completed"})
    row = _live_row()
    assert polygram_live.close_live_position(row, "target") is True
    assert row["status"] == "closed" and row["close_reason"] == "target"
    # realized_return = proceeds/cost_basis - 1 = 6.0/5.0 - 1 = 0.20
    assert abs(row["realized_return"] - 0.20) < 1e-9


def test_close_live_position_false_when_unmatchable(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [])  # not on venue
    row = _live_row()
    assert polygram_live.close_live_position(row, "target") is False
    assert row["status"] == "open"  # untouched


def test_reconcile_settles_missing_positions(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [])  # venue empty
    row = _live_row()
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 1
    assert row["status"] == "closed" and row["close_reason"] == "settled"


def test_reconcile_skips_on_failed_read(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: None)  # read failed
    row = _live_row()
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 0
    assert row["status"] == "open"  # NEVER mass-settle on a failed read
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: FAIL — `AttributeError: ... '_match_position_id'`

- [ ] **Step 3: Implement**

```python
# polygram_live.py (append)
def _match_position_id(venue_positions, market_id, outcome):
    """Find the venue positionId for a book row by (marketId, outcome)."""
    for p in venue_positions or []:
        if p.get("marketId") == market_id and p.get("outcome") == outcome:
            return p.get("id")
    return None


def close_live_position(row, reason):
    """Market-sell a live row and stamp realized_return. False (untouched) on failure.

    realized_return is proceeds-relative: proceeds / cost_basis - 1 (net of fees,
    which the venue already deducts from proceeds). Caller holds the book lock.
    """
    venue = list_positions()
    if venue is None:
        log.warning(f"Live close skipped (positions unreadable): {row['id']}")
        return False
    pos_id = _match_position_id(venue, row["instrument"], row["outcome"])
    if pos_id is None:
        log.warning(f"Live close: {row['id']} not on venue; leaving to reconcile")
        return False
    sale = sell_position(pos_id)
    if sale is None:
        return False
    cost = row.get("cost_basis") or 0.0
    row["realized_return"] = (sale["proceeds"] / cost - 1.0) if cost else 0.0
    row["last_mark"] = {"date": row.get("closed_date"), "price": sale["sale_price"],
                        "proceeds": sale["proceeds"]}
    row["status"] = "closed"
    row["close_reason"] = reason
    row["closed_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"LIVE CLOSE {row['id']} reason={reason} proceeds={sale['proceeds']}")
    return True


def reconcile_live_book(book):
    """Make the venue authoritative: settle open live rows the venue no longer holds.

    Returns the count reconciled. If list_positions() FAILED (None), do nothing —
    a failed read must never be read as 'all positions gone'.
    """
    venue = list_positions()
    if venue is None:
        return 0
    live_keys = {(p.get("marketId"), p.get("outcome")) for p in venue}
    n = 0
    for row in book.get("positions", []):
        if row.get("execution") != "live" or row.get("status") != "open":
            continue
        if (row["instrument"], row["outcome"]) not in live_keys:
            row["status"] = "closed"
            row["close_reason"] = "settled"
            row["closed_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            n += 1
            log.info(f"LIVE RECONCILE settled {row['id']} (gone from venue)")
    return n
```

> **Note on settled realized_return:** `reconcile_live_book` marks a vanished position `settled` but cannot know the payout from `/trade/positions` alone. A follow-up (Sleeve-A plan) reads `GET /trade/history` / `/portfolio` to backfill the realized proceeds for settled rows. For this foundation, `settled` + `closed_date` is the correct, truthful state (status known, amount pending backfill).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_polygram_live.py -q`
Expected: PASS

- [ ] **Step 5: Run the full gate + commit**

Run: `python -m ruff check . ; python -m ruff format --check . ; python -m pytest -q`
Expected: all pass (stage any file ruff reformats).

```bash
git add polygram_live.py tests/test_polygram_live.py
git commit -F - <<'EOF'
feat(live): close_live_position + reconcile_live_book (venue authoritative, no mass-settle)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review

**Spec coverage (foundation slice of the spec):**
- `polygram_live.py` write layer — Tasks 2, 3, 5, 6 ✓ (place/sell/positions/wallet/orderbook/reconcile, market-orders-v0, real-fee recording).
- `execution:"live"` + `sleeve` book fields — Task 5 ✓.
- Kill-switch + per-trade + total caps, fail-closed, funded check — Tasks 1, 4, 5 ✓.
- Reconciliation, venue-authoritative, no mass-settle on failed read — Task 6 ✓.
- `eventId` capture + `positionId` mapping + settlement-by-disappearance — Tasks 5, 6 ✓.
- `dockerfile-copy-allowlist` chore — Task 2 ✓.
- **Deferred to later plans (correctly out of this foundation):** Sleeve A entry/spread-gate/exit strategy; Sleeve B `/predict` wizard; thesis-log + resolution-aware retention; live/paper reporting split in `validation.py`; settled-row proceeds backfill from `/trade/history`. Each is a named task in its own plan.

**Placeholder scan:** none — every code step is complete and runnable.

**Type consistency:** `place_market_order` returns the normalized fill consumed by `open_live_position` (`fill_price`/`shares`/`*_fee`); `list_positions` returns `list|None` consumed by `close_live_position`/`reconcile_live_book` with the None-vs-`[]` distinction honored in both; `cap_ok(amount, live_exposure)` signature matches its call in `open_live_position`. Consistent.

## Execution Handoff

Next plans (write after this one lands): **Sleeve A** (favorite-fade entry + live-orderbook spread/depth gate + reprice/stop/time-stop exits on the monitor cron + settled-proceeds backfill + live/paper reporting split) and **Sleeve B** (`/predict` conversational wizard + conviction-hold rows + per-position/total sleeve caps + thesis-log + resolution-aware retention).

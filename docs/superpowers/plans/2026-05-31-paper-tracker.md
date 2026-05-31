# Paper Trade Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pure-paper trade tracker to `brief.py` that opens notional positions from medium/high-confidence directional signals, prices them via Stooq, marks the book to market weekly at 1w/2w/4w horizons, supports manual `/close`, and reports a returns/hit-rate scorecard.

**Architecture:** A new `# ── Paper trading ──` section in the single file. Stooq CSV is the sole price source (entry + mark, for internal consistency); returns are FX-neutral ratios. T212 ticker→Stooq symbol mapping uses a cached `/equity/metadata/instruments` catalogue plus a manual override file, and **skips-and-logs** anything it can't price. Positions live in a rewritten `paper-book.json` (mutable). Opening is folded into `mode_collect`; marking + scoring into `mode_weekly`; `/close` into the Telegram command handler.

**Tech Stack:** Python 3 stdlib + `requests` (Stooq + T212) + `feedparser`. No additions. No test framework — verification uses the stubbed-exec harness, with `fetch_stooq_price` (or `requests.get`) stubbed so no network is required.

**Spec:** `docs/superpowers/specs/2026-05-31-paper-tracker-design.md`

**Harness preamble** (reused in every verify step):

```python
import sys, types, os, json, shutil
for name in ("feedparser", "requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py","r",encoding="utf-8").read()
src = src.replace('"/app/logs/newsbrief.log"','"_t/newsbrief.log"').replace('Path("/app/logs/','Path("./_t/')
os.makedirs("_t", exist_ok=True)
ns = {}; exec(compile(src,"brief.py","exec"), ns)
# ...test body...
shutil.rmtree("_t", ignore_errors=True)
```

To stub network in tests, override the module-global the function resolves at call time, e.g.
`ns["fetch_stooq_price"] = lambda sym: 100.0` or set `sys.modules["requests"].get = fake`.

---

### Task 1: Paper-trading config + price/symbol/storage helpers

**Files:**
- Modify: `brief.py` — add config paths near the other `PAPER_DIR`/`SIGNALS_DIR` definitions; add a `# ── Paper trading ──` section (place it where the current `get_price`/`mode_paper` live, inside/after the Trading212 section); **delete the `get_price` stub**.

This task adds pure helpers + `_signal_return`; it does not yet change `mode_paper` (still the old body) — the file keeps compiling.

- [ ] **Step 1: Add config constants** near `PAPER_DIR = Path("/app/logs/paper")`:

```python
PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"

PAPER_HORIZONS = {"1w": 7, "2w": 14, "4w": 28}  # days from entry_date
PAPER_CLOSE_HORIZON = "4w"  # closing the book once this checkpoint is recorded
```

- [ ] **Step 2: Write the failing verification**

```python
# after harness preamble
# Stooq CSV parse via a fake requests.get
class _Resp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass
sys.modules["requests"].get = lambda url, **k: _Resp(
    "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-05-29,22:00:19,311.7,315,309.5,312.06,70026752\n"
)
assert ns["fetch_stooq_price"]("aapl.us") == 312.06
sys.modules["requests"].get = lambda url, **k: _Resp(
    "Symbol,Date,Time,Open,High,Low,Close,Volume\nNONSENSE,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
)
assert ns["fetch_stooq_price"]("nonsense") is None

# Symbol resolution
cache = {"instruments": {
    "AAPL_US_EQ": {"isin": "US0378331005", "currencyCode": "USD"},
    "VOD_UK_EQ":  {"isin": "GB00BH4HKS39", "currencyCode": "GBX"},
    "SAP_DE_EQ":  {"isin": "DE0007164600", "currencyCode": "EUR"},
}}
res = ns["resolve_stooq_symbol"]
assert res("AAPL_US_EQ", cache, {}) == "aapl.us"
assert res("VOD_UK_EQ", cache, {}) == "vod.uk"
assert res("SAP_DE_EQ", cache, {}) == "sap.de"
assert res("WEIRD_XX_EQ", cache, {}) is None
assert res("WEIRD_XX_EQ", cache, {"WEIRD_XX_EQ": "weird.us"}) == "weird.us"  # override wins

# Return convention
assert abs(ns["_signal_return"]("bullish", 100.0, 110.0) - 0.10) < 1e-9
assert abs(ns["_signal_return"]("bearish", 100.0, 90.0) - 0.10) < 1e-9

# Book load/save round-trip
ns["save_paper_book"]({"positions": [{"id": "x"}]})
assert ns["load_paper_book"]()["positions"][0]["id"] == "x"
print("TASK1_PASS")
```

- [ ] **Step 3: Run to verify it fails**

Run harness + Step 2.
Expected: `KeyError: 'fetch_stooq_price'` (helpers not defined yet).

- [ ] **Step 4: Implement the helpers**

Add a new section after the Trading212 functions (and delete `get_price`):

```python
# ── Paper trading ──────────────────────────────────────────────────────────────

# Map a T212 instrument currency (and ISIN country for EUR) to a Stooq market suffix.
_STOOQ_SUFFIX = {"USD": "us", "GBP": "uk", "GBX": "uk"}
_STOOQ_EUR_BY_ISIN = {"DE": "de", "FR": "fr"}


def fetch_stooq_price(stooq_symbol: str) -> float | None:
    """Fetch the latest close price for a Stooq symbol (e.g. 'aapl.us').

    Returns None on network error or Stooq's 'N/D' not-found sentinel — callers MUST treat
    None as 'could not price' and skip, never substitute a guessed value.
    """
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq fetch failed for {stooq_symbol}: {e}")
        return None
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")  # Symbol,Date,Time,Open,High,Low,Close,Volume
    if len(cols) < 7 or cols[6] in ("N/D", ""):
        log.warning(f"Stooq returned no price for {stooq_symbol}")
        return None
    try:
        return float(cols[6])
    except ValueError:
        return None


def load_ticker_overrides() -> dict:
    """Manual T212-ticker -> Stooq-symbol overrides for instruments that don't map automatically."""
    if TICKER_MAP_FILE.exists():
        return json.loads(TICKER_MAP_FILE.read_text())
    return {}


def load_instruments_cache() -> dict:
    if INSTRUMENTS_CACHE_FILE.exists():
        return json.loads(INSTRUMENTS_CACHE_FILE.read_text())
    return {}


def refresh_instruments_cache(max_age_days: int = 14, force: bool = False) -> dict:
    """Refresh the T212 instrument metadata cache (ticker -> isin/currencyCode) if stale.

    One rate-limited call (1 req / 50s) returns the full catalogue. Returns the cache dict;
    returns the existing/empty cache unchanged when T212_API_KEY is unset or the call fails.
    """
    cache = load_instruments_cache()
    if not force and cache.get("fetched_at"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])
            if age < timedelta(days=max_age_days):
                return cache
        except ValueError:
            pass
    if not T212_API_KEY:
        return cache
    try:
        resp = requests.get(
            f"{T212_BASE_URL}/api/v0/equity/metadata/instruments",
            headers={"Authorization": T212_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        instruments = {
            i["ticker"]: {"isin": i.get("isin", ""), "currencyCode": i.get("currencyCode", "")}
            for i in resp.json()
            if i.get("ticker")
        }
    except Exception as e:
        log.warning(f"Instrument cache refresh failed: {e}")
        return cache
    cache = {"fetched_at": datetime.now(timezone.utc).isoformat(), "instruments": instruments}
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_CACHE_FILE.write_text(json.dumps(cache))
    log.info(f"Instrument cache refreshed: {len(instruments)} instruments")
    return cache


def resolve_stooq_symbol(ticker: str, cache: dict, overrides: dict) -> str | None:
    """Map a T212 ticker (e.g. 'AAPL_US_EQ') to a Stooq symbol (e.g. 'aapl.us').

    Override file wins; otherwise derive base.suffix from the cached instrument currency
    (and ISIN country for EUR). Returns None when no suffix can be determined.
    """
    if ticker in overrides:
        return overrides[ticker]
    base = ticker.split("_")[0].lower()
    meta = cache.get("instruments", {}).get(ticker, {})
    ccy = (meta.get("currencyCode") or "").upper()
    suffix = _STOOQ_SUFFIX.get(ccy)
    if suffix is None and ccy == "EUR":
        suffix = _STOOQ_EUR_BY_ISIN.get((meta.get("isin") or "")[:2].upper())
    if suffix is None:
        return None
    return f"{base}.{suffix}"


def load_paper_book() -> dict:
    if PAPER_BOOK_FILE.exists():
        return json.loads(PAPER_BOOK_FILE.read_text())
    return {"positions": []}


def save_paper_book(book: dict):
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_BOOK_FILE.write_text(json.dumps(book, indent=2))


def _signal_return(direction: str, entry: float, price: float) -> float:
    """Directional return ratio: +1 for bullish, -1 for bearish, FX/unit-neutral."""
    sign = 1.0 if direction == "bullish" else -1.0
    return sign * (price / entry - 1.0)
```

- [ ] **Step 5: Run Step 2 to confirm it passes** — Expected: `TASK1_PASS`.

- [ ] **Step 6: Compile** — `python -m py_compile brief.py && echo COMPILE_OK`.

- [ ] **Step 7: (offline-skippable) live Stooq sanity** — optional, network:
  `curl -s "https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv"` should show a `Close` value.

---

### Task 2: Rewrite `mode_paper` to open positions + add to dispatch

**Files:**
- Modify: `brief.py` — replace the existing `mode_paper` body; add `"paper": mode_paper` to the dispatch dict in `__main__`.

- [ ] **Step 1: Write the failing verification**

```python
# after harness preamble
import datetime as _dt
# fake a signals snapshot for "today"
today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
os.makedirs("_t/signals", exist_ok=True)
json.dump({"date": today, "signals": [
    {"ticker": "AAPL_US_EQ", "topic": "t", "direction": "bullish", "confidence": "high", "thesis_ref": None, "rationale": "r", "provenance": "p"},
    {"ticker": "VOD_UK_EQ",  "topic": "t", "direction": "bearish", "confidence": "medium", "thesis_ref": None, "rationale": "r", "provenance": "p"},
    {"ticker": None,          "topic": "macro", "direction": "bullish", "confidence": "high", "thesis_ref": None, "rationale": "r", "provenance": "p"},
    {"ticker": "ZZZ_XX_EQ",  "topic": "t", "direction": "bullish", "confidence": "high", "thesis_ref": None, "rationale": "r", "provenance": "p"},
    {"ticker": "LOWC_US_EQ", "topic": "t", "direction": "bullish", "confidence": "low", "thesis_ref": None, "rationale": "r", "provenance": "p"},
]}, open(f"_t/signals/signals-{today}.json","w"))

# stub network-dependent helpers
ns["refresh_instruments_cache"] = lambda *a, **k: {"instruments": {}}
ns["load_ticker_overrides"] = lambda: {"AAPL_US_EQ": "aapl.us", "VOD_UK_EQ": "vod.uk"}  # ZZZ unmapped
ns["resolve_stooq_symbol"] = lambda t, c, o: o.get(t)
ns["fetch_stooq_price"] = lambda sym: 100.0

ns["mode_paper"]()
book = ns["load_paper_book"]()
opened = {(p["ticker"], p["direction"]) for p in book["positions"]}
assert ("AAPL_US_EQ","bullish") in opened and ("VOD_UK_EQ","bearish") in opened, opened
assert len(book["positions"]) == 2, "null-ticker, unmapped (ZZZ), and low-confidence are skipped"
for p in book["positions"]:
    assert p["status"]=="open" and p["entry_price"]==100.0 and p["checkpoints"]=={} and p["last_mark"] is None

# dedup: running again opens nothing new
ns["mode_paper"]()
assert len(ns["load_paper_book"]()["positions"]) == 2, "dedup per (ticker,direction)"
print("TASK2_PASS")
```

- [ ] **Step 2: Run to verify it fails** — Expected: positions opened ≠ 2 / `KeyError`, because the old `mode_paper` writes to `paper-book.jsonl` and has no dedup/skip/price logic.

- [ ] **Step 3: Replace `mode_paper`** with:

```python
def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Each medium/high-confidence directional signal with a resolvable ticker opens one notional
    paper position (deduped per ticker+direction). Prices come from Stooq; unmappable tickers,
    Stooq 'N/D', and macro/null-ticker signals are skipped and logged. Marking-to-market and
    closing happen in the weekly job.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return

    signals = json.loads(snap_path.read_text()).get("signals", [])
    actionable = [
        s for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("confidence") in ("medium", "high")
        and s.get("ticker")
    ]
    if not actionable:
        log.info("No actionable signals today")
        return

    book = load_paper_book()
    open_keys = {
        (p["ticker"], p["direction"]) for p in book["positions"] if p["status"] == "open"
    }
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()

    opened = 0
    for s in actionable:
        ticker, direction = s["ticker"], s["direction"]
        if (ticker, direction) in open_keys:
            continue  # dedup: a position for this call is already open
        symbol = resolve_stooq_symbol(ticker, cache, overrides)
        if not symbol:
            log.warning(f"Paper skip: no Stooq symbol for {ticker}")
            continue
        price = fetch_stooq_price(symbol)
        if price is None:
            log.warning(f"Paper skip: no price for {ticker} ({symbol})")
            continue
        book["positions"].append({
            "id": f"{today}:{ticker}:{direction}",
            "opened": today,
            "ticker": ticker,
            "stooq_symbol": symbol,
            "direction": direction,
            "confidence": s.get("confidence"),
            "topic": s.get("topic"),
            "thesis_ref": s.get("thesis_ref"),
            "rationale": s.get("rationale"),
            "entry_price": price,
            "entry_date": today,
            "status": "open",
            "close_reason": None,
            "closed_date": None,
            "checkpoints": {},
            "last_mark": None,
        })
        open_keys.add((ticker, direction))
        opened += 1

    save_paper_book(book)
    log.info(f"Opened {opened} paper position(s)")
```

- [ ] **Step 4: Add `mode_paper` to dispatch.** In `__main__`, change the dispatch dict to include:

```python
        "paper": mode_paper,
```

(insert after the `"commands": mode_commands,` entry).

- [ ] **Step 5: Run Step 1 to confirm it passes** — Expected: `TASK2_PASS`.

- [ ] **Step 6: Compile + dispatch check**

```bash
python - <<'PY'
import re
src = open("brief.py",encoding="utf-8").read()
assert re.search(r'"paper":\s*mode_paper', src), "paper not in dispatch"
assert "def get_price" not in src, "old get_price stub should be removed"
print("DISPATCH_OK")
PY
python -m py_compile brief.py && echo COMPILE_OK
```
Expected: `DISPATCH_OK` then `COMPILE_OK`.

---

### Task 3: `mark_to_market` — weekly marks, horizon checkpoints, auto-close

**Files:**
- Modify: `brief.py` — add `mark_to_market` to the Paper trading section (after `mode_paper`).

- [ ] **Step 1: Write the failing verification**

```python
# after harness preamble
ns["fetch_stooq_price"] = lambda sym: 110.0  # +10% vs entry 100
def pos(entry_date, direction="bullish", checkpoints=None):
    return {"id":"i","opened":entry_date,"ticker":"AAPL_US_EQ","stooq_symbol":"aapl.us",
            "direction":direction,"confidence":"high","topic":"t","thesis_ref":None,"rationale":"r",
            "entry_price":100.0,"entry_date":entry_date,"status":"open","close_reason":None,
            "closed_date":None,"checkpoints":checkpoints or {},"last_mark":None}

mtm = ns["mark_to_market"]
# 8 days open -> records 1w only; bullish +10%
book = mtm({"positions":[pos("2026-05-01")]}, "2026-05-09")
p = book["positions"][0]
assert set(p["checkpoints"]) == {"1w"} and abs(p["checkpoints"]["1w"]["return"]-0.10) < 1e-9, p
assert p["status"] == "open" and abs(p["last_mark"]["return"]-0.10) < 1e-9

# 30 days open -> records 1w,2w,4w (all crossed) and CLOSES at 4w
book2 = mtm({"positions":[pos("2026-05-01")]}, "2026-05-31")
p2 = book2["positions"][0]
assert set(p2["checkpoints"]) == {"1w","2w","4w"}, p2["checkpoints"]
assert p2["status"]=="closed" and p2["close_reason"]=="horizon" and p2["closed_date"]=="2026-05-31"

# bearish return sign
ns["fetch_stooq_price"] = lambda sym: 90.0  # -10% price, bearish -> +10% return
book3 = mtm({"positions":[pos("2026-05-01","bearish")]}, "2026-05-09")
assert abs(book3["positions"][0]["checkpoints"]["1w"]["return"]-0.10) < 1e-9

# N/D at mark time -> position left open, no checkpoint
ns["fetch_stooq_price"] = lambda sym: None
book4 = mtm({"positions":[pos("2026-05-01")]}, "2026-05-09")
assert book4["positions"][0]["status"]=="open" and book4["positions"][0]["checkpoints"]=={}

# already-closed positions untouched
closed = pos("2026-04-01"); closed["status"]="closed"
book5 = mtm({"positions":[closed]}, "2026-05-09")
assert book5["positions"][0]["last_mark"] is None
print("TASK3_PASS")
```

- [ ] **Step 2: Run to verify it fails** — Expected: `KeyError: 'mark_to_market'`.

- [ ] **Step 3: Implement `mark_to_market`** (after `mode_paper`):

```python
def mark_to_market(book: dict, today_str: str) -> dict:
    """Mark every open position to market, record crossed horizon checkpoints, close at 4w.

    Mutates and returns the book. A position whose Stooq price can't be fetched is left open
    and retried next run. All crossed-but-unrecorded checkpoints are recorded in one pass
    (covers a missed weekly run); closing happens once the 4w checkpoint is recorded.
    """
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        price = fetch_stooq_price(p["stooq_symbol"])
        if price is None:
            log.warning(f"MtM kept open (no price): {p['ticker']} ({p['stooq_symbol']})")
            continue
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        for label, threshold in PAPER_HORIZONS.items():
            if label not in p["checkpoints"] and days_open >= threshold:
                p["checkpoints"][label] = {"date": today_str, "price": price, "return": ret}
        if PAPER_CLOSE_HORIZON in p["checkpoints"]:
            p["status"] = "closed"
            p["close_reason"] = "horizon"
            p["closed_date"] = today_str
    return book
```

- [ ] **Step 4: Run Step 1 to confirm it passes** — Expected: `TASK3_PASS`.

- [ ] **Step 5: Compile** — `python -m py_compile brief.py && echo COMPILE_OK`.

---

### Task 4: `paper_scorecard` — Telegram-HTML returns/hit-rate summary

**Files:**
- Modify: `brief.py` — add `paper_scorecard` to the Paper trading section (after `mark_to_market`).

- [ ] **Step 1: Write the failing verification**

```python
# after harness preamble
def closed_pos(direction, ret4w, conf="high", date="2026-05-31"):
    return {"id":"i","ticker":"T","direction":direction,"confidence":conf,"status":"closed",
            "close_reason":"horizon","closed_date":date,"checkpoints":{"4w":{"date":date,"price":1,"return":ret4w}}}
book = {"positions":[
    closed_pos("bullish", 0.10, "high"),
    closed_pos("bearish", -0.05, "high"),   # miss
    closed_pos("bullish", 0.02, "medium"),
    {"id":"o","ticker":"O","direction":"bullish","confidence":"high","status":"open",
     "close_reason":None,"closed_date":None,"checkpoints":{"1w":{"date":"d","price":1,"return":0.03}},"last_mark":{"return":0.03}},
]}
out = ns["paper_scorecard"](book)
assert "PAPER SIGNALS SCORECARD" in out
assert "67% of 3" in out          # 2 of 3 closed are hits
assert "Open: 1" in out and "Closed: 3" in out
print("TASK4_PASS")
```

- [ ] **Step 2: Run to verify it fails** — Expected: `KeyError: 'paper_scorecard'`.

- [ ] **Step 3: Implement `paper_scorecard`** (after `mark_to_market`):

```python
def paper_scorecard(book: dict) -> str:
    """Build a Telegram-HTML paper scorecard: hit-rate and mean returns (percentages only)."""
    positions = book.get("positions", [])
    closed = [p for p in positions if p["status"] == "closed" and "4w" in p.get("checkpoints", {})]
    open_ = [p for p in positions if p["status"] == "open"]

    def _hit_rate(ps):
        rets = [p["checkpoints"]["4w"]["return"] for p in ps]
        if not rets:
            return None
        hits = sum(1 for r in rets if r > 0)
        return 100.0 * hits / len(rets), len(rets)

    lines = ["<b>🧪 PAPER SIGNALS SCORECARD</b>"]
    overall = _hit_rate(closed)
    if overall:
        rate, n = overall
        lines.append(f"• Realized hit-rate (4w): {rate:.0f}% of {n}")
        for conf in ("high", "medium"):
            sub = _hit_rate([p for p in closed if p.get("confidence") == conf])
            if sub:
                lines.append(f"  – {conf}: {sub[0]:.0f}% of {sub[1]}")
    for label in PAPER_HORIZONS:
        rets = [p["checkpoints"][label]["return"] for p in positions if label in p.get("checkpoints", {})]
        if rets:
            lines.append(f"• Mean {label} return: {100.0 * sum(rets) / len(rets):+.1f}% (n={len(rets)})")
    lines.append(f"• Open: {len(open_)} | Closed: {len(closed)}")
    recent = sorted(closed, key=lambda p: p.get("closed_date") or "", reverse=True)[:5]
    if recent:
        lines.append("Recently closed:")
        for p in recent:
            r = p["checkpoints"]["4w"]["return"]
            lines.append(f"  • {p['ticker']} {p['direction']}: {100 * r:+.1f}% ({p['close_reason']})")
    return "\n".join(lines)
```

- [ ] **Step 4: Run Step 1 to confirm it passes** — Expected: `TASK4_PASS`.

- [ ] **Step 5: Compile** — `python -m py_compile brief.py && echo COMPILE_OK`.

---

### Task 5: Wire opening into collect, MtM+scorecard into weekly, `/close` command

**Files:**
- Modify: `brief.py` — `mode_collect` (call `mode_paper` after `save_signals`), `mode_weekly` (cache refresh + MtM + scorecard delivery), `process_telegram_commands` (`/close`), `HELP_TEXT`.

- [ ] **Step 1: Wire `mode_paper` into `mode_collect`.** After the `save_signals(signals, today, status=status, dropped=dropped)` line in `mode_collect`, add:

```python
        mode_paper()
```

(so opening uses the snapshot just written; place it before `clear_batch_state()`).

- [ ] **Step 2: Wire weekly mark-to-market + scorecard.** In `mode_weekly`, after the summary `deliver(...)` block (inside `if summary:`), add:

```python
        refresh_instruments_cache(force=True)
        book = mark_to_market(load_paper_book(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        save_paper_book(book)
        telegram_send(paper_scorecard(book))
        log.info("Paper book marked to market")
```

- [ ] **Step 3: Add the `/close` command.** In `process_telegram_commands`, add a branch (before the final `else:`):

```python
        elif text.startswith("/close "):
            tkr = text[7:].strip()
            book = load_paper_book()
            matches = [p for p in book["positions"] if p["status"] == "open" and p["ticker"] == tkr]
            if not matches:
                telegram_send(f"No open paper position for <b>{tkr}</b>.")
            else:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                closed_n = 0
                for p in matches:
                    price = fetch_stooq_price(p["stooq_symbol"])
                    if price is None:
                        continue
                    ret = _signal_return(p["direction"], p["entry_price"], price)
                    p["last_mark"] = {"date": day, "price": price, "return": ret}
                    p["status"] = "closed"
                    p["close_reason"] = "manual"
                    p["closed_date"] = day
                    closed_n += 1
                if closed_n:
                    save_paper_book(book)
                    telegram_send(f"✅ Closed {closed_n} paper position(s) for <b>{tkr}</b> (manual).")
                else:
                    telegram_send(f"⚠️ Couldn't price {tkr} — left open.")
```

- [ ] **Step 4: Document `/close` in `HELP_TEXT`.** Add this line after the `/note` block:

```python

/close [TICKER]
  Close an open paper position early at the current mark.
  e.g. <code>/close AAPL_US_EQ</code>
```

- [ ] **Step 5: Verify wiring + `/close` logic**

```bash
python - <<'PY'
import sys, types, os, json, shutil, re
for name in ("feedparser","requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py",encoding="utf-8").read()

# wiring present
assert re.search(r"def mode_collect.*?mode_paper\(\)", src, re.DOTALL), "mode_paper not called in collect"
assert re.search(r"def mode_weekly.*?mark_to_market\(.*?paper_scorecard", src, re.DOTALL), "weekly MtM/scorecard missing"
assert "/close" in src and "close_reason" in src, "/close handler missing"
print("WIRING_OK")
PY
python - <<'PY'
import sys, types, os, json, shutil
for name in ("feedparser","requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="123")
src = open("brief.py",encoding="utf-8").read().replace('"/app/logs/newsbrief.log"','"_t/newsbrief.log"').replace('Path("/app/logs/','Path("./_t/')
os.makedirs("_t/paper", exist_ok=True)
ns = {}; exec(compile(src,"brief.py","exec"), ns)
ns["fetch_stooq_price"] = lambda sym: 110.0
sent = []
ns["telegram_send"] = lambda msg: sent.append(msg) or True
ns["telegram_get_updates"] = lambda offset=0: [{"update_id":1,"message":{"text":"/close AAPL_US_EQ","chat":{"id":123}}}]
ns["save_paper_book"]({"positions":[{"id":"i","ticker":"AAPL_US_EQ","stooq_symbol":"aapl.us","direction":"bullish","confidence":"high","topic":"t","thesis_ref":None,"rationale":"r","entry_price":100.0,"entry_date":"2026-05-01","status":"open","close_reason":None,"closed_date":None,"checkpoints":{},"last_mark":None}]})
ns["process_telegram_commands"]()
book = ns["load_paper_book"]()
assert book["positions"][0]["status"]=="closed" and book["positions"][0]["close_reason"]=="manual", book
assert any("Closed 1" in m for m in sent), sent
shutil.rmtree("_t", ignore_errors=True)
print("TASK5_PASS")
PY
python -m py_compile brief.py && echo COMPILE_OK
ruff check brief.py 2>&1 | tail -3 || echo "ruff skipped"
```
Expected: `TASK5_PASS`, `COMPILE_OK`, ruff clean.

---

## Self-review

**Spec coverage:**
- Stooq price source + `N/D` skip → Task 1 (`fetch_stooq_price`). ✓
- Symbol mapping (override → currency/ISIN derive) + cache (lazy + weekly) → Task 1 (`resolve_stooq_symbol`, `refresh_instruments_cache`). ✓
- Open with dedup + skip null/unmapped + dispatch fix + collect wiring → Tasks 2, 5. ✓
- Weekly MtM, 1w/2w/4w checkpoints, auto-close at 4w → Task 3 + Task 5 wiring. ✓
- `/close` + HELP_TEXT → Task 5. ✓
- Scorecard (hit-rate + mean returns, percentages only) → Task 4 + Task 5 delivery. ✓
- `paper-book.json` mutable model replaces `.jsonl` → Tasks 1–3. ✓
- Privacy: no monetary amounts; Stooq public; T212 metadata only. Held throughout. ✓

**Placeholder scan:** none — every code step has full code; commands have expected output.

**Type consistency:** `fetch_stooq_price(str)->float|None`, `resolve_stooq_symbol(ticker,cache,overrides)->str|None`, `_signal_return(direction,entry,price)->float`, `load_paper_book()->dict {positions:[...]}`, `mark_to_market(book,today_str)->dict`, `paper_scorecard(book)->str` — names and shapes match across `mode_paper`, `mode_weekly`, `/close`, and tests. Position dict keys are identical in `mode_paper` (creation), `mark_to_market` (mutation), and `paper_scorecard` (read).

## Notes for the implementer

- **Privacy:** never call `/equity/orders/*`; never write a monetary amount into a paper file or prompt. Paper P&L is percentage-return only.
- **Commits:** user is on `main`, "commit when asked." Gate every commit step on explicit authorisation.
- **Formatter:** the ruff PostToolUse hook reformats `brief.py` after edits; re-read regions before subsequent edits if line numbers shift. Integration anchors in Task 5 are described by content, not line number, for this reason.
- **No new dependencies.** Stooq and T212 both go through `requests`.
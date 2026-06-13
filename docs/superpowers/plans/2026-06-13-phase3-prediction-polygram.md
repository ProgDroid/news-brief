# Phase 3: PolyGram Read Client + Claude Matcher + Prediction Lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add prediction markets (PolyGram) as the third paper-traded asset class — a read-only PolyGram client, a Claude matcher mapping the day's signals to live markets, and a prediction lifecycle (open → mark-to-market → close) that reuses the shared return math and forks only on the close trigger by `play_type` — wired into the live `collect` cron silently.

**Architecture:** Prediction reuses the *entire* polymorphic position model and return math (`_signal_return`, the `book.json` lifecycle). It adds one read client (`polygram_login`/`_polygram_get`/`polygram_search`/`polygram_market`/`_parse_pg_market`), one matcher (`_gather_pg_candidates`/`run_prediction_matcher`/`_parse_matches`), a `prediction` branch in `price_position`, a prediction open pass in `mode_paper`, and a prediction branch in `mark_to_market`. The pricer reads `GET /api/markets/:id → outcomePrices[side_index]` (one call yields both the held-side mark **and** the settlement status). All PolyGram I/O follows the existing None-on-failure posture; the whole stage is creds-gated and wrapped so a failure can never affect the brief.

**Tech Stack:** Python 3.12 (CI/Docker) / 3.14 (local), `requests`, `pytest`, `ruff`. No new dependencies. New env: `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD`. New state under `DATA_DIR`: `polygram_token.json`.

**Governing docs:** spec `docs/superpowers/specs/2026-06-13-phase3-prediction-polygram-design.md` (the real-API-grounded Phase 3 addendum) + `docs/superpowers/specs/2026-06-13-multi-asset-trading-polygram-design.md`.

**Testing convention (from `multi-asset-trading-build` memory):** when a test *monkeypatches* behaviour, patch it on the module whose function is *under test* (e.g. `trading.polygram_market`, `trading.POLYGRAM_EMAIL`, `brief.clear_batch_state`), because each module's functions resolve their own module-level names. When a test just *calls a pure function*, either namespace works.

**Pre-push gate (must match CI exactly — from `brief-local-run` memory):** all THREE, run via the **PowerShell tool** (the Bash tool errors `stdin is not a tty` on python):
- `python -m ruff check brief.py common.py trading.py tests`
- `python -m ruff format --check brief.py common.py trading.py tests`
- `python -m pytest tests -q`

`ruff format` edits files in place — after running it you MUST `git add` every reformatted file (an unstaged reformat passes local `--check` but fails CI on the committed tree). **Commit via the Bash tool, not PowerShell** (PowerShell 5.1 prepends a UTF-8 BOM to the commit subject). Deps: `pip install -r requirements.txt -r requirements-dev.txt` (ruff pinned 0.14.14).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `common.py` | Add `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` env reads (optional, like T212) | Modify |
| `trading.py` | PolyGram constants + read client + `_parse_pg_market`; matcher (`_gather_pg_candidates`/`run_prediction_matcher`/`_parse_matches`); `price_position` prediction branch; `_open_prediction_positions` + `mode_paper` restructure; `mark_to_market` prediction branch + `_record_checkpoints`/`_settle_prediction` helpers | Modify |
| `brief.py` | `mode_collect` ordering fix (trading stage try/except, after `clear_batch_state`) | Modify |
| `.env.example` | Document `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` | Modify |
| `docker-compose.yml` | Pass the two new env vars into the container | Modify |
| `tests/test_prediction.py` (new) | Client auth/refresh, `_parse_pg_market`, matcher parse/gate, pricer dispatch, both close triggers, prediction open | Create |
| `tests/test_brief.py` (or existing collect test) | Failure-isolation: trading stage raises → brief not duplicated | Modify/Create |

No new module → no `Dockerfile` `COPY` change; new state files live under `DATA_DIR`, not in the image.

---

## Task 0: Baseline green + reconfirm live API shapes

**Files:** none (verification only — no commit)

- [ ] **Step 1: Confirm the suite is green and ruff is clean**

Run (PowerShell tool): `python -m pytest tests -q` — record the passing count (the **baseline**, call it `N`). Then `python -m ruff check brief.py common.py trading.py tests` and `python -m ruff format --check brief.py common.py trading.py tests` — both must report no issues. If red, STOP and report.

- [ ] **Step 2: Reconfirm the PolyGram read shapes against the live API (read-only)**

The design doc records shapes probed on 2026-06-13. Before writing any parser, reconfirm they still hold (the API is third-party). Ensure `.env` has `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD`, then run a throwaway probe **from a temp dir outside the repo** (never commit it; redact the token). Confirm:
- `POST https://polygram.ink/api/auth/login` → `{user, token, twoFactorVerified}` (JWT under `token`).
- `GET /api/markets/:id` on an **open** market → `outcomes='["Yes", "No"]'`, `outcomePrices='["<y>", "<n>"]'` (JSON strings, index-aligned), `clobTokenIds` (JSON string of 2 ids), `closed` (bool), `umaResolutionStatus` (`"resolved"` when settled else `None`).
- `GET /api/search?q=<topic>` → array of events, each with nested `markets[]` of the same market shape.

If any field name changed, STOP and report — the parser in Task 2 depends on these exact names (`outcomePrices`, `clobTokenIds`, `closed`, `umaResolutionStatus`).

---

## Task 1: Credentials plumbing (env only — no behaviour)

Add the two optional env vars so the rest of the phase can read them. No trading logic yet.

**Files:**
- Modify: `common.py` (next to the T212 key reads)
- Modify: `.env.example`, `docker-compose.yml`
- Test: `tests/test_prediction.py` (create, with one import-smoke test)

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_prediction.py`:

```python
"""Prediction seam: PolyGram read client + Claude matcher + prediction lifecycle."""

import trading


def test_trading_exposes_polygram_creds_attrs():
    # The creds are read in common and re-exported via trading's import for gating.
    assert hasattr(trading, "POLYGRAM_EMAIL")
    assert hasattr(trading, "POLYGRAM_PASSWORD")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: FAIL — `AttributeError: module 'trading' has no attribute 'POLYGRAM_EMAIL'`.

- [ ] **Step 3: Add the env reads in `common.py`**

In `common.py`, immediately after the `T212_API_KEY = os.environ.get(...)` lines (the existing optional-credential block), add:

```python
# PolyGram (prediction markets) credentials — optional, like T212. Login is
# JWT-based; registration is manual/one-time and never in the cron path.
POLYGRAM_EMAIL = os.environ.get("POLYGRAM_EMAIL")
POLYGRAM_PASSWORD = os.environ.get("POLYGRAM_PASSWORD")
```

(If `import os` is not already at the top of `common.py`, it is — confirm; it reads all other env vars the same way.)

- [ ] **Step 4: Re-export into `trading.py`**

In `trading.py`, extend the `from common import (...)` block (lines 11-21) to also import the two creds and the Anthropic constants the matcher needs in Task 3 (add them now to avoid a second edit):

```python
from common import (
    DATA_DIR,
    SIGNALS_DIR,
    log,
    _write_json_atomic,
    _load_json_or,
    T212_API_KEY_ID,
    T212_API_KEY,
    T212_BASE_URL,
    t212_auth_header,
    MODEL,
    ANTHROPIC_HEADERS,
    POLYGRAM_EMAIL,
    POLYGRAM_PASSWORD,
)
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q` (Expected: PASS) then `python -c "import brief, trading, common; print('ok')"` (Expected: `ok`).

- [ ] **Step 6: Document the env vars**

In `.env.example`, after the T212 block (the `# T212_BASE_URL=...` line), add:

```
# PolyGram (prediction markets) login. Enables the prediction paper-trading seam
# (Claude matches signals to live markets; read-only — no orders are ever placed).
# Registration is manual/one-time at https://polygram.ink — not done by the app.
# POLYGRAM_EMAIL=
# POLYGRAM_PASSWORD=
```

In `docker-compose.yml`, in the `environment:` block of every service that already passes `ANTHROPIC_API_KEY`/`T212_API_KEY` (the newsbrief services), add the two passthroughs alongside the existing ones:

```yaml
      - POLYGRAM_EMAIL=${POLYGRAM_EMAIL:-}
      - POLYGRAM_PASSWORD=${POLYGRAM_PASSWORD:-}
```

(Match the exact indentation and `${VAR:-}` style of the surrounding lines. If the services use an `env_file:` or a YAML anchor instead of inline `environment:`, follow that pattern instead — read the file first and mirror what `T212_API_KEY` does.)

- [ ] **Step 7: ruff + commit**

Run: `python -m ruff check ...` + `python -m ruff format ...`; `git status`.

```bash
git add common.py trading.py tests/test_prediction.py .env.example docker-compose.yml
git commit -m "feat: add optional POLYGRAM_EMAIL/PASSWORD creds plumbing"
```

---

## Task 2: PolyGram read client + market parser

Auth (login + JWT persisted + 401-refresh), the two read endpoints, and `_parse_pg_market` (the defensive `json.loads` of the stringified arrays). All None-on-failure.

**Files:**
- Modify: `trading.py` (constants after `_KRAKEN_BASE`; client fns near the other fetchers)
- Modify: `tests/test_prediction.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_prediction.py`, append:

```python
import json
import pytest


# ── _parse_pg_market ──────────────────────────────────────────────────────────
def _raw_market(yes="0.30", no="0.70", closed=False, uma=None):
    return {
        "id": "2410562",
        "question": "Will X happen?",
        "endDate": "2026-07-20T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes}", "{no}"]',
        "clobTokenIds": '["71902280236980528007966111072910269163651886024599423678358797794246690742124", "17567294778637229825908271987925808954865907947626969581496912375435402975317"]',
        "closed": closed,
        "umaResolutionStatus": uma,
    }


def test_parse_pg_market_extracts_prices_and_status():
    p = trading._parse_pg_market(_raw_market(yes="0.30", no="0.70"))
    assert p["market_id"] == "2410562"
    assert p["question"] == "Will X happen?"
    assert p["prices"] == [0.30, 0.70]
    assert p["yes_price"] == 0.30
    assert p["closed"] is False
    assert p["uma_status"] is None
    assert len(p["token_ids"]) == 2


def test_parse_pg_market_reads_resolved_status():
    p = trading._parse_pg_market(_raw_market(yes="0", no="1", closed=True, uma="resolved"))
    assert p["prices"] == [0.0, 1.0]
    assert p["closed"] is True
    assert p["uma_status"] == "resolved"


def test_parse_pg_market_returns_none_on_garbage():
    assert trading._parse_pg_market({"id": "x"}) is None  # missing arrays
    assert trading._parse_pg_market({"id": "x", "outcomePrices": "not-json"}) is None


# ── login + _polygram_get (401 refresh) ───────────────────────────────────────
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_polygram_login_persists_token(tmp_path, monkeypatch):
    monkeypatch.setattr(trading, "POLYGRAM_TOKEN_FILE", tmp_path / "polygram_token.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    monkeypatch.setattr(
        trading.requests,
        "post",
        lambda url, json=None, timeout=None: _Resp(200, {"token": "JWT123", "user": {}}),
    )
    assert trading.polygram_login() == "JWT123"
    saved = json.loads((tmp_path / "polygram_token.json").read_text())
    assert saved["token"] == "JWT123"


def test_polygram_get_refreshes_on_401(tmp_path, monkeypatch):
    monkeypatch.setattr(trading, "POLYGRAM_TOKEN_FILE", tmp_path / "polygram_token.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    # Seed a stale token on disk.
    (tmp_path / "polygram_token.json").write_text('{"token": "STALE"}', encoding="utf-8")
    monkeypatch.setattr(trading, "polygram_login", lambda: "FRESH")

    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if headers.get("Authorization") == "Bearer STALE":
            return _Resp(401, {})
        assert headers.get("Authorization") == "Bearer FRESH"
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(trading.requests, "get", fake_get)
    assert trading._polygram_get("/markets/1") == {"ok": True}
    assert calls["n"] == 2  # one 401, one retry with the refreshed token


def test_polygram_get_returns_none_when_uncredentialed(monkeypatch):
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", None)
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", None)
    assert trading._polygram_get("/markets/1") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: FAIL — `_parse_pg_market`/`polygram_login`/`_polygram_get`/`POLYGRAM_TOKEN_FILE` undefined.

- [ ] **Step 3: Add the constants**

In `trading.py`, after the `_KRAKEN_BASE` dict (ends line 50) and the `PAPER_HORIZONS`/`PAPER_CLOSE_HORIZON` lines, add:

```python
# ── PolyGram (prediction markets) ─────────────────────────────────────────────
POLYGRAM_BASE = "https://polygram.ink/api"
POLYGRAM_TOKEN_FILE = PAPER_DIR / "polygram_token.json"
PG_CANDIDATE_CAP = 25  # max candidate markets fed to the matcher (prompt-size bound)
PG_SIMILARITY_FLOOR = 0.60  # open only matches at/above this matcher similarity
PG_MAX_HOLD_DAYS = 182  # ~26w backstop close for never-resolving resolution markets
```

And extend `_VENUE_BY_ASSET` (line 32) to include prediction:

```python
_VENUE_BY_ASSET = {"equity": "t212", "crypto": "kraken", "prediction": "polygram"}
```

- [ ] **Step 4: Add the read client + parser**

In `trading.py`, after `fetch_kraken_price` (before `fetch_price`), add:

```python
def _parse_pg_market(m: dict) -> dict | None:
    """Flatten a raw PolyGram market into the fields the seam needs.

    PolyGram mirrors Polymarket: `outcomes`, `outcomePrices`, and `clobTokenIds`
    are JSON-ENCODED STRINGS of index-aligned arrays (YES=index 0, NO=index 1).
    Returns None if the required arrays are missing or unparseable — callers skip.
    """
    try:
        prices = [float(x) for x in json.loads(m["outcomePrices"])]
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (KeyError, TypeError, ValueError):
        return None
    if len(prices) < 2:
        return None
    return {
        "market_id": str(m.get("id", "")),
        "question": str(m.get("question", "")),
        "prices": prices,
        "yes_price": prices[0],
        "end_date": m.get("endDate"),
        "closed": bool(m.get("closed")),
        "uma_status": m.get("umaResolutionStatus"),
        "token_ids": token_ids,
    }


def polygram_login() -> str | None:
    """Log in with POLYGRAM_EMAIL/PASSWORD, persist and return the JWT (or None)."""
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        return None
    try:
        resp = requests.post(
            f"{POLYGRAM_BASE}/auth/login",
            json={"email": POLYGRAM_EMAIL, "password": POLYGRAM_PASSWORD},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
    except Exception as e:
        log.warning(f"PolyGram login failed: {e}")
        return None
    if not token:
        log.warning("PolyGram login returned no token")
        return None
    _write_json_atomic(POLYGRAM_TOKEN_FILE, {"token": token})
    return token


def _polygram_get(path: str, params: dict | None = None):
    """GET a PolyGram path with the persisted JWT; refresh once on 401.

    Returns parsed JSON or None on any failure (uncredentialed, network error,
    non-2xx after a refresh attempt) — same None-on-failure posture as the pricers.
    """
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        return None
    token = (_load_json_or(POLYGRAM_TOKEN_FILE, {}) or {}).get("token") or polygram_login()
    if not token:
        return None
    url = f"{POLYGRAM_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30
            )
        except Exception as e:
            log.warning(f"PolyGram GET {path} failed: {e}")
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
            log.warning(f"PolyGram GET {path} failed: {e}")
            return None
    return None


def polygram_search(query: str) -> list | None:
    """Search PolyGram events/markets by free text. Returns the raw events list or None."""
    return _polygram_get("/search", params={"q": query})


def polygram_market(market_id: str) -> dict | None:
    """Fetch one market's full detail (mark + settlement status). Returns raw dict or None."""
    return _polygram_get(f"/markets/{market_id}")
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS (all client + parser tests).

- [ ] **Step 6: ruff + commit**

```bash
git add trading.py tests/test_prediction.py
git commit -m "feat: add PolyGram read client (login + 401 refresh) and market parser"
```

---

## Task 3: Claude matcher (candidate gather + match call + parse/gate)

`_gather_pg_candidates` (search → dedup → cap), `run_prediction_matcher` (one synchronous Claude call, `run_dig` shape, no web search), `_parse_matches` (resilient JSON-array parse + validation against the candidate ids).

**Files:**
- Modify: `trading.py` (after the read client)
- Modify: `tests/test_prediction.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_prediction.py`, append:

```python
# ── _parse_matches ────────────────────────────────────────────────────────────
def test_parse_matches_validates_and_filters_unknown_ids():
    text = (
        'Here are the matches:\n'
        '[{"market_id": "111", "side": "yes", "play_type": "momentum", "similarity": 0.8, "target": 0.6},'
        ' {"market_id": "999", "side": "NO", "play_type": "resolution", "similarity": 0.9, "target": null},'
        ' {"market_id": "111", "side": "BAD", "play_type": "momentum", "similarity": 0.7}]'
    )
    out = trading._parse_matches(text, {"111", "999"})
    # First (side normalised to YES) and second kept; third dropped (bad side).
    assert len(out) == 2
    assert out[0] == {"market_id": "111", "side": "YES", "play_type": "momentum", "similarity": 0.8, "target": 0.6}
    assert out[1]["market_id"] == "999" and out[1]["target"] is None


def test_parse_matches_empty_on_garbage():
    assert trading._parse_matches("no json here", {"111"}) == []
    assert trading._parse_matches("", {"111"}) == []


def test_parse_matches_drops_id_not_in_candidates():
    out = trading._parse_matches('[{"market_id":"42","side":"YES","play_type":"momentum","similarity":0.9}]', {"111"})
    assert out == []


# ── _gather_pg_candidates (dedup + cap) ───────────────────────────────────────
def test_gather_candidates_dedups_and_caps(monkeypatch):
    def fake_search(q):
        return [{"markets": [
            _raw_market(),  # id 2410562, open
            {**_raw_market(), "id": "999", "closed": True},  # closed -> dropped
        ]}]

    monkeypatch.setattr(trading, "polygram_search", fake_search)
    cands = trading._gather_pg_candidates([{"topic": "a"}, {"topic": "b", "thesis_ref": "t"}])
    ids = [c["market_id"] for c in cands]
    assert ids == ["2410562"]  # deduped across topics; closed market excluded


# ── run_prediction_matcher ────────────────────────────────────────────────────
def test_run_matcher_calls_claude_and_parses(monkeypatch):
    captured = {}

    class _ClaudeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": '[{"market_id":"2410562","side":"YES","play_type":"momentum","similarity":0.75,"target":null}]'}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _ClaudeResp()

    monkeypatch.setattr(trading.requests, "post", fake_post)
    cands = [{"market_id": "2410562", "question": "Will X?", "yes_price": 0.3, "end_date": None}]
    out = trading.run_prediction_matcher([{"topic": "x"}], cands)
    assert out == [{"market_id": "2410562", "side": "YES", "play_type": "momentum", "similarity": 0.75, "target": None}]
    assert "tools" not in captured["payload"]  # no web search
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: FAIL — `_parse_matches`/`_gather_pg_candidates`/`run_prediction_matcher` undefined.

- [ ] **Step 3: Add the matcher**

In `trading.py`, after `polygram_market`, add:

```python
def _gather_pg_candidates(signals: list) -> list:
    """Search PolyGram for markets related to the day's signals; dedup + cap.

    Searches each distinct signal `topic` and `thesis_ref`, keeps OPEN binary
    markets, dedups by market_id, and caps the total at PG_CANDIDATE_CAP to bound
    the matcher prompt. Returns the parsed-candidate dicts (market_id/question/
    yes_price/end_date) the matcher is shown.
    """
    queries = []
    for s in signals:
        for q in (s.get("topic"), s.get("thesis_ref")):
            if q and q not in queries:
                queries.append(q)
    seen: dict[str, dict] = {}
    for q in queries:
        events = polygram_search(q)
        for ev in events or []:
            for m in ev.get("markets", []):
                parsed = _parse_pg_market(m)
                if parsed is None or parsed["closed"]:
                    continue
                seen.setdefault(
                    parsed["market_id"],
                    {
                        "market_id": parsed["market_id"],
                        "question": parsed["question"],
                        "yes_price": parsed["yes_price"],
                        "end_date": parsed["end_date"],
                    },
                )
                if len(seen) >= PG_CANDIDATE_CAP:
                    return list(seen.values())
    return list(seen.values())


def _parse_matches(text: str, candidate_ids: set) -> list:
    """Parse the matcher's JSON-array reply, validating each match.

    Resilient like the signals parser: locate the array within any surrounding
    prose/fences, json.loads it, and keep only well-formed matches whose
    market_id is a real candidate. Returns [] on any failure.
    """
    try:
        arr = json.loads(text[text.index("[") : text.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("market_id", ""))
        side = str(item.get("side", "")).upper()
        play = str(item.get("play_type", "")).lower()
        if mid not in candidate_ids or side not in ("YES", "NO") or play not in ("resolution", "momentum"):
            continue
        try:
            sim = float(item.get("similarity"))
        except (TypeError, ValueError):
            continue
        target = item.get("target")
        try:
            target = float(target) if target is not None else None
        except (TypeError, ValueError):
            target = None
        out.append(
            {"market_id": mid, "side": side, "play_type": play, "similarity": sim, "target": target}
        )
    return out


def run_prediction_matcher(signals: list, candidates: list) -> list:
    """One synchronous Claude call mapping signals → prediction-market matches.

    Same Messages-API shape as run_dig but with NO tools/web search. Returns the
    validated match list (possibly empty); never raises into the cron path.
    """
    if not candidates:
        return []
    payload = {
        "model": MODEL,
        "max_tokens": 2048,
        "system": (
            "You map daily investing signals to live prediction markets. Given today's "
            "signals and candidate markets, return ONLY a JSON array (no prose, no code "
            'fences). Each element: {"market_id": str, "side": "YES"|"NO", "play_type": '
            '"resolution"|"momentum", "similarity": number 0..1, "target": number|null}. '
            "side is the outcome the signal implies. play_type is 'resolution' when the "
            "signal speaks to the eventual settled outcome, 'momentum' when it is a "
            "near-term catalyst likely to move the odds regardless of settlement. target "
            "(momentum only, else null) is an optional held-side price in 0..1 to take "
            "profit at. similarity is your confidence the signal is genuinely about this "
            "market. Omit weak matches; return [] if none."
        ),
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Today's signals:\n{json.dumps(signals)}\n\n"
                    f"Candidate markets:\n{json.dumps(candidates)}\n\n"
                    "Return the JSON array of matches."
                ),
            }
        ],
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
    except Exception as e:
        log.warning(f"Prediction matcher call failed: {e}")
        return []
    return _parse_matches(text, {c["market_id"] for c in candidates})
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS (all matcher tests).

- [ ] **Step 5: ruff + commit**

```bash
git add trading.py tests/test_prediction.py
git commit -m "feat: add Claude prediction matcher (candidate gather + match parse/gate)"
```

---

## Task 4: Prediction pricer dispatch (`price_position` branch)

Make `price_position` mark a prediction position by reading the live held-side price from market detail. `fetch_price(asset_class, instrument)` stays equity/crypto-only (prediction needs the position's `side_index`, which the 2-arg signature can't carry).

**Files:**
- Modify: `trading.py` (`price_position`, lines 149-151)
- Modify: `tests/test_prediction.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_prediction.py`, append:

```python
# ── price_position prediction dispatch ────────────────────────────────────────
def test_price_position_prediction_reads_held_side(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: _raw_market(yes="0.30", no="0.70"))
    yes_pos = {"asset_class": "prediction", "instrument": "2410562", "side_index": 0}
    no_pos = {"asset_class": "prediction", "instrument": "2410562", "side_index": 1}
    assert trading.price_position(yes_pos) == 0.30
    assert trading.price_position(no_pos) == 0.70


def test_price_position_prediction_none_when_unfetchable(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert trading.price_position({"asset_class": "prediction", "instrument": "x", "side_index": 0}) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -k price_position -q`
Expected: FAIL — `price_position` ignores `prediction`, calls `fetch_price("prediction", "2410562")` → routes to Stooq → wrong/None.

- [ ] **Step 3: Add the prediction branch**

In `trading.py`, replace `price_position` (lines 149-151):

```python
def price_position(p: dict) -> float | None:
    """Mark a position to market by dispatching on its asset_class.

    Equity/crypto go through fetch_price (instrument-level). Prediction marks the
    held side from market detail (outcomePrices[side_index]) — None if unfetchable.
    """
    if p.get("asset_class") == "prediction":
        m = polygram_market(p["instrument"])
        if m is None:
            return None
        parsed = _parse_pg_market(m)
        if parsed is None:
            return None
        return parsed["prices"][p["side_index"]]
    return fetch_price(p.get("asset_class", "equity"), p["instrument"])
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q`
Expected: PASS. Also `python -m pytest tests -q` (Expected: still green — equity/crypto `price_position` unchanged).

- [ ] **Step 5: ruff + commit**

```bash
git add trading.py tests/test_prediction.py
git commit -m "feat: dispatch prediction marks through price_position (held-side detail)"
```

---

## Task 5: Open prediction positions (`_open_prediction_positions` + `mode_paper` restructure)

Add the prediction open pass and restructure `mode_paper` so the matcher runs whenever there are signals (even with no actionable equity/crypto calls), gated on PolyGram creds so the existing hermetic tests never hit the network.

**Files:**
- Modify: `trading.py` (`mode_paper`, lines 350-458; new `_open_prediction_positions`)
- Modify: `tests/test_prediction.py`

- [ ] **Step 1: Write the failing test (prediction open)**

In `tests/test_prediction.py`, append:

```python
# ── mode_paper opens a prediction position ────────────────────────────────────
def test_mode_paper_opens_prediction_position(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", "e@x.com")
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", "pw")
    # A single NEUTRAL signal (no actionable equity/crypto) still drives the matcher.
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": null, "asset_class": "equity", "direction": "neutral", '
        '"confidence": "low", "topic": "fed-cuts", "thesis_ref": null, '
        '"rationale": "macro", "provenance": "web_search"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(trading, "_gather_pg_candidates", lambda signals: [
        {"market_id": "2410562", "question": "Will the Fed cut in June?", "yes_price": 0.3, "end_date": None}
    ])
    monkeypatch.setattr(trading, "run_prediction_matcher", lambda signals, cands: [
        {"market_id": "2410562", "side": "YES", "play_type": "momentum", "similarity": 0.8, "target": 0.6}
    ])
    monkeypatch.setattr(trading, "polygram_market", lambda mid: _raw_market(yes="0.30", no="0.70"))

    trading.mode_paper()

    book = trading.load_book()
    assert len(book["positions"]) == 1
    p = book["positions"][0]
    assert p["asset_class"] == "prediction"
    assert p["venue"] == "polygram"
    assert p["instrument"] == "2410562"
    assert p["play_type"] == "momentum"
    assert p["outcome"] == "Yes" and p["side_index"] == 0
    assert p["target"] == 0.6
    assert p["direction"] == "bullish"  # always long the held side
    assert p["entry_price"] == 0.30
    assert p["status"] == "open"


def test_mode_paper_skips_prediction_when_uncredentialed(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    monkeypatch.setattr(trading, "POLYGRAM_EMAIL", None)
    monkeypatch.setattr(trading, "POLYGRAM_PASSWORD", None)

    def _boom(*a, **k):
        raise AssertionError("matcher must not run without creds")

    monkeypatch.setattr(trading, "_gather_pg_candidates", _boom)
    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": null, "direction": "neutral", "confidence": "low", '
        '"topic": "x", "thesis_ref": null, "rationale": "", "provenance": ""}]}',
        encoding="utf-8",
    )
    trading.mode_paper()  # must not raise, must not call the matcher
    assert trading.load_book() == {"positions": []}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -k mode_paper -q`
Expected: FAIL — `mode_paper` early-returns on `not actionable` (the neutral-only snapshot), so no prediction position is opened; `len == 1` fails.

- [ ] **Step 3: Add `_open_prediction_positions`**

In `trading.py`, immediately BEFORE `mode_paper` (before line 350), add:

```python
def _open_prediction_positions(book: dict, signals: list, today: str, open_keys: set) -> int:
    """Match the day's signals to live PolyGram markets and open paper positions.

    Creds-gated (no-op when PolyGram is unconfigured, so unit tests never hit the
    network). Opens one long-the-held-side position per match above the similarity
    floor, deduped by market, priced at the live held-side mark. Returns the count.
    """
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        log.info("PolyGram not configured — skipping prediction matching")
        return 0
    candidates = _gather_pg_candidates(signals)
    if not candidates:
        log.info("No PolyGram candidates today")
        return 0
    matches = run_prediction_matcher(signals, candidates)
    opened = 0
    for mt in matches:
        if mt["similarity"] < PG_SIMILARITY_FLOOR:
            continue
        mid, side = mt["market_id"], mt["side"]
        key = ("prediction", mid, "bullish")
        if key in open_keys:
            continue  # dedup: a position for this market is already open
        m = polygram_market(mid)
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None or parsed["closed"]:
            log.warning(f"Prediction skip: market {mid} unfetchable/closed")
            continue
        side_index = 0 if side == "YES" else 1
        price = parsed["prices"][side_index]
        if price is None or price <= 0:
            log.warning(f"Prediction skip: non-positive price for {mid} ({side})")
            continue
        play_type = mt["play_type"]
        book["positions"].append(
            {
                "id": f"{today}:prediction:{mid}:{side}",
                "opened": today,
                "asset_class": "prediction",
                "venue": "polygram",
                "execution": "paper",
                "ticker": mid,
                "instrument": mid,
                "play_type": play_type,
                "outcome": "Yes" if side == "YES" else "No",
                "side_index": side_index,
                "token_id": parsed["token_ids"][side_index] if len(parsed["token_ids"]) > side_index else None,
                "target": mt["target"] if play_type == "momentum" else None,
                "direction": "bullish",  # always long the held side (long-sense return)
                "confidence": None,
                "topic": parsed["question"],
                "thesis_ref": None,
                "rationale": f"matched (similarity={mt['similarity']})",
                "entry_price": price,
                "entry_date": today,
                "status": "open",
                "close_reason": None,
                "closed_date": None,
                "checkpoints": {},
                "last_mark": None,
                "realized_return": None,
            }
        )
        open_keys.add(key)
        opened += 1
    return opened
```

- [ ] **Step 4: Restructure `mode_paper`**

In `trading.py`, replace `mode_paper` (lines 350-458). The changes vs. the current body: (a) early-return only when there are NO signals at all (so the matcher still runs on neutral/macro days); (b) the equity/crypto open loop is guarded by `if actionable:`; (c) a prediction open pass is added before `save_book`.

```python
def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Equity/crypto: each medium/high-confidence directional signal with a resolvable
    instrument opens one notional position (deduped per asset_class+ticker+direction),
    priced via Stooq/Kraken. Prediction: the Claude matcher maps ALL of today's signals
    to live PolyGram markets (creds-gated) and opens long-the-held-side positions.
    Unmappable/unpriced/macro signals are skipped and logged. MtM + close run weekly.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return

    signals = json.loads(snap_path.read_text()).get("signals", [])
    if not signals:
        log.info("No signals today — nothing to paper-trade")
        return

    actionable = [
        s
        for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("confidence") in ("medium", "high")
        and s.get("ticker")
    ]

    book = load_book()

    def _open_keys() -> set:
        return {
            (p.get("asset_class", "equity"), p["ticker"], p["direction"])
            for p in book["positions"]
            if p["status"] == "open"
        }

    open_keys = _open_keys()
    opened = 0

    if actionable:
        cache = refresh_instruments_cache()
        overrides = load_ticker_overrides()
        crypto_overrides = load_crypto_ticker_overrides()

        for s in actionable:
            ac = s.get("asset_class", "equity")
            ticker, direction = s["ticker"], s["direction"]
            opposite = "bearish" if direction == "bullish" else "bullish"

            # Reversal: a fresh opposite-direction call closes the standing position first.
            if (ac, ticker, opposite) in open_keys:
                for p in book["positions"]:
                    if (
                        p["status"] == "open"
                        and p.get("asset_class", "equity") == ac
                        and p["ticker"] == ticker
                        and p["direction"] == opposite
                        and _close_position_at_market(p, today, "reversal")
                    ):
                        log.info(f"Paper reversal: closed {ac} {ticker} {opposite}")
                open_keys = _open_keys()
                if (ac, ticker, opposite) in open_keys:
                    # Reversal close couldn't be priced — don't open the opposite yet.
                    log.warning(
                        f"Paper skip: unpriced reversal for {ticker}; not opening {direction}"
                    )
                    continue

            if (ac, ticker, direction) in open_keys:
                continue  # dedup: a position for this call is already open
            if ac == "crypto":
                symbol = resolve_kraken_pair(ticker, crypto_overrides)
            else:
                symbol = resolve_stooq_symbol(ticker, cache, overrides)
            if not symbol:
                log.warning(f"Paper skip: no instrument for {ticker} ({ac})")
                continue
            price = fetch_price(ac, symbol)
            if price is None:
                log.warning(f"Paper skip: no price for {ticker} ({symbol})")
                continue
            book["positions"].append(
                {
                    "id": f"{today}:{ac}:{ticker}:{direction}",
                    "opened": today,
                    "asset_class": ac,
                    "venue": _VENUE_BY_ASSET.get(ac, ""),
                    "execution": "paper",
                    "ticker": ticker,
                    "instrument": symbol,
                    "play_type": None,
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
                    "realized_return": None,
                }
            )
            open_keys.add((ac, ticker, direction))
            opened += 1
    else:
        log.info("No actionable equity/crypto signals today")

    opened += _open_prediction_positions(book, signals, today, open_keys)

    save_book(book)
    log.info(f"Opened {opened} paper position(s)")
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q` (Expected: PASS) then `python -m pytest tests -q` (Expected: still green — the equity/crypto open path is byte-for-byte the same logic, now under `if actionable:`).

- [ ] **Step 6: ruff + commit**

```bash
git add trading.py tests/test_prediction.py
git commit -m "feat: open prediction paper positions via the matcher in mode_paper"
```

---

## Task 6: Prediction MtM + close triggers

Add a `prediction` branch to `mark_to_market`: mark the held side from one market-detail fetch, record checkpoints, and fork the close trigger by `play_type` — momentum (target-cross or 4w backstop) vs. resolution (settlement or max-hold). Extract a shared `_record_checkpoints` helper (used by both the equity path and prediction) to stay DRY.

**Files:**
- Modify: `trading.py` (`mark_to_market`, lines 461-491; new `_record_checkpoints`, `_settle_prediction`, `_mtm_prediction`)
- Modify: `tests/test_prediction.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_prediction.py`, append:

```python
# ── prediction MtM close triggers ─────────────────────────────────────────────
from datetime import datetime, timedelta, timezone


def _pred_position(play_type, side_index=0, entry=0.30, target=None, entry_days_ago=0):
    entry_date = (datetime.now(timezone.utc) - timedelta(days=entry_days_ago)).strftime("%Y-%m-%d")
    return {
        "asset_class": "prediction",
        "ticker": "2410562",
        "instrument": "2410562",
        "play_type": play_type,
        "outcome": "Yes" if side_index == 0 else "No",
        "side_index": side_index,
        "target": target,
        "direction": "bullish",
        "entry_price": entry,
        "entry_date": entry_date,
        "status": "open",
        "close_reason": None,
        "closed_date": None,
        "checkpoints": {},
        "last_mark": None,
        "realized_return": None,
    }


def _mtm(monkeypatch, position, raw_market):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: raw_market)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return trading.mark_to_market({"positions": [position]}, today)["positions"][0]


def test_momentum_closes_on_target_cross(monkeypatch):
    p = _pred_position("momentum", entry=0.30, target=0.60, entry_days_ago=3)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.65", no="0.35"))  # YES now 0.65 >= 0.60
    assert out["status"] == "closed"
    assert out["close_reason"] == "target"
    assert out["realized_return"] == pytest.approx(0.65 / 0.30 - 1.0)


def test_momentum_force_closes_at_4w(monkeypatch):
    p = _pred_position("momentum", entry=0.30, target=0.99, entry_days_ago=30)  # target not hit
    out = _mtm(monkeypatch, p, _raw_market(yes="0.40", no="0.60"))
    assert out["status"] == "closed"
    assert out["close_reason"] == "horizon"
    assert "4w" in out["checkpoints"]


def test_momentum_stays_open_before_horizon(monkeypatch):
    p = _pred_position("momentum", entry=0.30, target=0.99, entry_days_ago=10)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.40", no="0.60"))
    assert out["status"] == "open"


def test_resolution_closes_on_settlement(monkeypatch):
    p = _pred_position("resolution", side_index=0, entry=0.30, entry_days_ago=40)
    out = _mtm(monkeypatch, p, _raw_market(yes="1", no="0", closed=True, uma="resolved"))
    assert out["status"] == "closed"
    assert out["close_reason"] == "settlement"
    assert out["realized_return"] == pytest.approx(1.0 / 0.30 - 1.0)


def test_resolution_ignores_4w_horizon(monkeypatch):
    # 40 days open, NOT resolved -> resolution must stay open past 4w (unlike equity).
    p = _pred_position("resolution", entry=0.30, entry_days_ago=40)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.50", no="0.50", closed=False))
    assert out["status"] == "open"
    assert "4w" in out["checkpoints"]  # checkpoint still recorded


def test_resolution_max_hold_backstop(monkeypatch):
    p = _pred_position("resolution", entry=0.30, entry_days_ago=trading.PG_MAX_HOLD_DAYS + 1)
    out = _mtm(monkeypatch, p, _raw_market(yes="0.50", no="0.50", closed=False))
    assert out["status"] == "closed"
    assert out["close_reason"] == "max_hold"


def test_prediction_mtm_kept_open_when_unfetchable(monkeypatch):
    p = _pred_position("momentum", entry_days_ago=40)
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = trading.mark_to_market({"positions": [p]}, today)["positions"][0]
    assert out["status"] == "open"  # no price -> retried next run
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -k "momentum or resolution or prediction_mtm" -q`
Expected: FAIL — `mark_to_market` runs the equity path on prediction positions: it calls `price_position` (works) but closes at 4w via the generic horizon rule and never reads settlement/target, so the trigger assertions fail.

- [ ] **Step 3: Extract `_record_checkpoints` and refactor the equity path**

In `trading.py`, immediately BEFORE `mark_to_market` (before line 461), add the helper:

```python
def _record_checkpoints(p: dict, today_str: str, price: float, ret: float, days_open: int):
    """Record any crossed-but-unrecorded horizon checkpoints (idempotent, one pass)."""
    for label, threshold in PAPER_HORIZONS.items():
        if label not in p["checkpoints"] and days_open >= threshold:
            p["checkpoints"][label] = {"date": today_str, "price": price, "return": ret}
```

Then replace `mark_to_market` (lines 461-491) with the version below — the equity/crypto path is unchanged behaviour (now calling the helper), with a `prediction` branch dispatched up front:

```python
def mark_to_market(book: dict, today_str: str) -> dict:
    """Mark every open position to market, record crossed horizon checkpoints, close on trigger.

    Mutates and returns the book. Equity/crypto: record 1w/2w/4w checkpoints, close at 4w.
    Prediction: dispatched to _mtm_prediction (held-side mark + play_type close trigger).
    A position whose price can't be fetched is left open and retried next run.
    """
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        if p.get("asset_class") == "prediction":
            _mtm_prediction(p, today, today_str)
            continue
        price = price_position(p)
        if price is None:
            log.warning(f"MtM kept open (no price): {p['ticker']} ({p['instrument']})")
            continue
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        _record_checkpoints(p, today_str, price, ret, days_open)
        if PAPER_CLOSE_HORIZON in p["checkpoints"]:
            p["status"] = "closed"
            p["close_reason"] = "horizon"
            p["closed_date"] = today_str
            p["realized_return"] = p["checkpoints"][PAPER_CLOSE_HORIZON]["return"]
    return book
```

- [ ] **Step 4: Add `_settle_prediction` and `_mtm_prediction`**

In `trading.py`, immediately AFTER `mark_to_market`, add:

```python
def _settle_prediction(p: dict, day: str, price: float, ret: float, reason: str):
    """Close a prediction position at the given mark with the given reason."""
    p["status"] = "closed"
    p["close_reason"] = reason
    p["closed_date"] = day
    p["realized_return"] = ret


def _mtm_prediction(p: dict, today, today_str: str):
    """Mark + (maybe) close one open prediction position from a single market-detail fetch.

    Held-side mark = outcomePrices[side_index]; return is long-sense (you hold the token).
    Close trigger forks by play_type:
      momentum  → close at target-cross (held price >= target) else 4w horizon backstop.
      resolution→ hold to settlement (closed & uma 'resolved'); PG_MAX_HOLD_DAYS backstop.
    Left open (retried next run) if the market can't be fetched/parsed.
    """
    m = polygram_market(p["instrument"])
    parsed = _parse_pg_market(m) if m is not None else None
    if parsed is None:
        log.warning(f"MtM kept open (no price): prediction {p['instrument']}")
        return
    price = parsed["prices"][p["side_index"]]
    ret = _signal_return("bullish", p["entry_price"], price)  # always long the held side
    p["last_mark"] = {"date": today_str, "price": price, "return": ret}
    days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
    _record_checkpoints(p, today_str, price, ret, days_open)

    if p["play_type"] == "resolution":
        if parsed["closed"] and parsed["uma_status"] == "resolved":
            _settle_prediction(p, today_str, price, ret, "settlement")
        elif days_open >= PG_MAX_HOLD_DAYS:
            _settle_prediction(p, today_str, price, ret, "max_hold")
    else:  # momentum
        target = p.get("target")
        if target is not None and price >= target:
            _settle_prediction(p, today_str, price, ret, "target")
        elif PAPER_CLOSE_HORIZON in p["checkpoints"]:
            _settle_prediction(p, today_str, price, ret, "horizon")
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -q` (Expected: PASS — all close-trigger tests) then `python -m pytest tests -q` (Expected: still green — the equity/crypto MtM path is behaviourally identical via `_record_checkpoints`).

- [ ] **Step 6: ruff + commit**

```bash
git add trading.py tests/test_prediction.py
git commit -m "feat: prediction MtM with resolution/momentum close triggers"
```

---

## Task 7: `mode_collect` ordering fix (failure isolation)

Reorder the collect flow so the trading stage runs **after** `clear_batch_state` and inside a `try/except`, guaranteeing a matcher/PolyGram/Claude failure can never re-collect and duplicate the brief.

**Files:**
- Modify: `brief.py` (`mode_collect`, lines 1304-1332)
- Modify: `tests/test_prediction.py` (or a collect-focused test file — keep it next to the others)

- [ ] **Step 1: Confirm the signals snapshot survives `clear_batch_state`**

Read `clear_batch_state` in `brief.py`. Confirm it clears only the batch-tracking state file (the `batch_id`), NOT `SIGNALS_DIR/signals-<date>.json` (which `save_signals` wrote and `mode_paper` reads). If `clear_batch_state` also removed the snapshot, moving `mode_paper` after it would break the open path — in that case, STOP and report (the design assumes the snapshot persists). Expected: it only clears batch state; the snapshot persists.

- [ ] **Step 2: Write the failing failure-isolation test**

In `tests/test_prediction.py`, append:

```python
# ── mode_collect failure isolation ────────────────────────────────────────────
def test_collect_trading_failure_does_not_duplicate_brief(monkeypatch):
    import brief

    calls = {"deliver": 0, "cleared": 0}
    monkeypatch.setattr(brief, "load_state", lambda: {"batch_id": "b1"})
    monkeypatch.setattr(brief, "poll_batch", lambda bid: "RAW")
    monkeypatch.setattr(brief, "split_brief_and_signals", lambda raw: ("BRIEF", [], "ok"))
    monkeypatch.setattr(brief, "normalize_signals", lambda raw: ([], []))
    monkeypatch.setattr(brief, "deliver", lambda *a, **k: calls.__setitem__("deliver", calls["deliver"] + 1))
    monkeypatch.setattr(brief, "save_signals", lambda *a, **k: None)
    monkeypatch.setattr(brief, "clear_batch_state", lambda: calls.__setitem__("cleared", calls["cleared"] + 1))
    monkeypatch.setattr(brief, "telegram_alert", lambda *a, **k: None)

    def _boom():
        raise RuntimeError("PolyGram down")

    monkeypatch.setattr(brief, "mode_paper", _boom)

    brief.mode_collect()  # must NOT raise

    assert calls["deliver"] == 1  # brief delivered exactly once
    assert calls["cleared"] == 1  # batch cleared despite the trading failure -> no re-collect
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_prediction.py -k collect_trading_failure -q`
Expected: FAIL — current `mode_collect` calls `mode_paper()` (raises) **before** `clear_batch_state()`, so the exception propagates: `clear_batch_state` is never called (`cleared == 0`) and `mode_collect` raises.

- [ ] **Step 4: Reorder `mode_collect`**

In `brief.py`, in `mode_collect` (lines 1304-1332), replace the post-`deliver` tail:

```python
        save_signals(signals, today, status=status, dropped=dropped)
        mode_paper()
        clear_batch_state()
```

with (clear first, then the guarded trading stage):

```python
        save_signals(signals, today, status=status, dropped=dropped)
        clear_batch_state()
        # Trading stage runs AFTER clear_batch_state and is isolated: a matcher /
        # PolyGram / Claude failure must never re-collect and duplicate the brief.
        try:
            mode_paper()
        except Exception as e:
            log.error(f"Trading stage failed (brief already delivered): {e}")
            telegram_alert(f"trading stage failed after brief: {e}")
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_prediction.py -k collect_trading_failure -q` (Expected: PASS) then `python -m pytest tests -q` (Expected: still green — any existing collect test must still pass; if one asserted the old `mode_paper`-before-`clear` order, update it to the new order).

- [ ] **Step 6: ruff + commit**

```bash
git add brief.py tests/test_prediction.py
git commit -m "fix: isolate trading stage after clear_batch_state so it can't duplicate the brief"
```

---

## Task 8: Final verification sweep + docs

**Files:** `docs/` data-dir tree (if present); otherwise verification only.

- [ ] **Step 1: Full pre-push gate**

Run all three, in order:
- `python -m ruff check brief.py common.py trading.py tests` → no issues
- `python -m ruff format --check brief.py common.py trading.py tests` → "N files already formatted"
- `python -m pytest tests -q` → PASS, count = Task 0 baseline + the prediction tests added (1 smoke + 5 client/parser + 5 matcher + 2 pricer + 2 open + 7 MtM + 1 collect-isolation).

If `ruff format --check` reports a file would change, run `python -m ruff format ...`, `git add`, and commit `style: apply ruff format`.

- [ ] **Step 2: Confirm clean import + all modes dispatch**

Run: `python -c "import brief; print([m for m in ('mode_submit','mode_collect','mode_weekly','mode_commands','mode_run','mode_paper') if hasattr(brief, m)])"`
Expected: lists all six modes.

- [ ] **Step 3: Confirm no prediction-field reads leaked into the equity path**

Use Grep for `side_index`, `uma_status`, `play_type == "resolution"` across `*.py`.
Expected: `side_index`/`uma_status` appear ONLY inside the prediction functions (`_open_prediction_positions`, `price_position` prediction branch, `_mtm_prediction`, `_parse_pg_market`). No equity/crypto code path reads them.

- [ ] **Step 4: Update the data-dir docs tree (if one exists)**

Grep `docs/` for `book.json` (the Phase 2 commit updated a data-dir tree). If a tree lists the `paper/` state files, add `polygram_token.json` next to `book.json`/`crypto_ticker_map.json` with a one-line note ("PolyGram JWT, refreshed on 401"). If no such tree exists, skip.

```bash
git add docs
git commit -m "docs: note polygram_token.json in the data-dir tree"
```

(Skip the commit if nothing changed.)

- [ ] **Step 5: Final commit (only if Step 1 reformatted anything uncommitted)**

```bash
git add -A
git commit -m "style: ruff format after phase 3"
```

(Skip if nothing changed.)

---

## Self-Review

- **Spec coverage:**
  - *Read client (auth login + JWT persist + 401 refresh; `/search`, `/markets/:id`)* → Task 2 (`polygram_login`/`_polygram_get`/`polygram_search`/`polygram_market`). `/price`+orderbook explicitly deferred to Phase 4 (not built). ✅
  - *§1 pricer delta (market-detail `outcomePrices[side_index]`, not `/api/price`)* → Task 4 (`price_position` prediction branch) + Task 6 (`_mtm_prediction`). ✅
  - *Matcher (all signals → capped candidates → similarity floor; one synchronous Claude call, no web search; resilient JSON parse)* → Task 3 (`_gather_pg_candidates` cap, `run_prediction_matcher` no `tools`, `_parse_matches`) + Task 5 (floor gate). ✅
  - *Prediction position shape (polymorphic + `outcome`/`side_index`/`token_id`/`target`)* → Task 5. Equity/crypto leave them unset; only the prediction branch reads them (verified Task 8 Step 3). ✅
  - *Close triggers — momentum optional target + 4w backstop; resolution hold-to-settle (ignores 4w) + 26w max-hold* → Task 6 (`_mtm_prediction`), all six trigger cases tested. ✅
  - *Wired into `collect` silently + ordering fix (try/except after `clear_batch_state`)* → Task 5 (open wired into `mode_paper`) + Task 7 (reorder). No Telegram message (Phase 4). ✅
  - *Creds plumbing (`POLYGRAM_EMAIL`/`PASSWORD`, `.env.example`, `docker-compose`)* → Task 1. ✅
  - *Out of scope held (no unified trade message, no `/price`/orderbook haircut, no validation/go-live gate, no volume monitor, no new Telegram commands, no live executor)* → none appear in any task. ✅
- **Placeholder scan:** No TBD/TODO. Every code step shows complete code; every run step shows the command + expected result. Task 0/Step 1's "data-dir tree (if present)" and Task 1/Step 6's "if it uses env_file/anchor" are conditional instructions with a concrete default action, not placeholders.
- **Type/name consistency:** `_parse_pg_market` returns `{market_id, question, prices, yes_price, end_date, closed, uma_status, token_ids}` — consumed consistently by `_gather_pg_candidates` (yes_price/closed), `price_position` (prices[side_index]), `_open_prediction_positions` (prices/token_ids/closed), `_mtm_prediction` (prices/closed/uma_status). Match dict `{market_id, side, play_type, similarity, target}` stable across `_parse_matches`/`run_prediction_matcher`/`_open_prediction_positions`. `_record_checkpoints(p, today_str, price, ret, days_open)` and `_settle_prediction(p, day, price, ret, reason)` signatures used consistently. Position keys (`side_index`, `outcome`, `token_id`, `target`, `instrument`=market_id, `direction`="bullish") consistent across writer (Task 5) and readers (Tasks 4/6). `PG_CANDIDATE_CAP`/`PG_SIMILARITY_FLOOR`/`PG_MAX_HOLD_DAYS`/`POLYGRAM_BASE`/`POLYGRAM_TOKEN_FILE` defined once (Task 2).
- **Risk:** The only behaviour change to the existing equity/crypto path is (a) wrapping the open loop in `if actionable:` (identical logic, now skipped when empty — but it was empty-no-op before via the early return) and (b) routing MtM checkpoint recording through `_record_checkpoints` (byte-identical loop). Both are covered by the existing green suite. Prediction is gated behind `asset_class == "prediction"` (only the matcher produces it) and PolyGram creds (absent in tests), so the equity/crypto flow is untouched until a prediction position exists.

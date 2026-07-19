# Bigdata.com Enrichment — Enable on Live REST — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the dark `BigdataProvider` to the live-verified REST API and enable descriptive enrichment (per-symbol sentiment + events, toggleable thematic search) in the daily brief.

**Architecture:** The `enrichment/` package already has a vendor-neutral provider seam (`Provider` protocol → Null/Fixture/Bigdata) wired into `brief.py` at submit (build + persist snapshot + inject prompt block) and collect (reload snapshot → annotate signals). This plan (a) reshapes `SentimentScore` to the vendor's native fields + a smoothed trend, (b) rewrites `BigdataProvider`'s HTTP + parse layers to the confirmed endpoints/auth, (c) makes the persisted snapshot ToS-compliant (derived sentiment only), and (d) adds a themes on/off flag. Descriptive-only invariant unchanged: enrichment never writes a sizing field.

**Tech Stack:** Python 3.14, `requests` (already a dep — no SDK, no new dependency), pytest, ruff. No pandas.

**Spec:** `docs/superpowers/specs/2026-07-19-bigdata-enrichment-enable-design.md`

## Global Constraints

- **Auth header is `X-API-KEY`** (NOT `Authorization: Bearer`). Base URL `https://api.bigdata.com`.
- **Descriptive-only invariant:** enrichment NEVER writes a position-sizing field; sentiment is a media-tone overlay. `trading.py` must never read `bigdata_sentiment`.
- **ToS: no caching of Bigdata Content.** The persisted `enrichment-{today}.json` must carry only derived sentiment + entity id — no headlines, search text, thematic docs, or event titles.
- **Degrade-never-crash:** every provider method returns an error-tagged empty bundle rather than raising; brief/signals never break on an enrichment failure.
- **No new dependency / no Dockerfile / no CI change.** `requests` only.
- **Commits via the Bash tool**, not PowerShell (PowerShell prepends a UTF-8 BOM to commit subjects on this machine). Use `python`/`pytest` via the PowerShell tool (Bash errors "stdin is not a tty").
- **Pre-push gate:** `ruff check .` + `ruff format --check .` + `pytest` must all pass; stage every file ruff reformats.
- **Module constants** (top of `providers_bigdata.py`): `SENTIMENT_LOOKBACK_DAYS = 60`, `EVENTS_FORWARD_DAYS = 90`, `SEARCH_MAX_CHUNKS = 2`.

---

## File Structure

- `enrichment/config.py` — MODIFY: add `ENRICHMENT_THEMES_ENABLED`.
- `enrichment/models.py` — MODIFY: reshape `SentimentScore`; add `EnrichmentBundles.to_persisted_dict()`.
- `enrichment/build.py` — MODIFY: gate theme fan-out on the new flag.
- `enrichment/render.py` — MODIFY: `_fmt_sentiment` + `annotate_signals` to new fields.
- `enrichment/providers_bigdata.py` — REWRITE: `X-API-KEY`; real paths/bodies; parse to new model; exact-ticker resolve; usage accounting.
- `brief.py` — MODIFY: persist `to_persisted_dict()`; log run usage.
- `tests/fixtures/bigdata_raw/` — REPLACE: `find_AAPL.json`, `sentiment_D8442A.json`, `events_D8442A.json`, `search_AAPL.json` (real shapes).
- `tests/fixtures/enrichment/symbol_CVX.json`, `theme_gold.json` — MODIFY: new `SentimentScore` shape.
- `tests/test_enrichment_*.py` — MODIFY: models, render, providers, build, wiring.

---

## Task 1: Themes on/off toggle

**Files:**
- Modify: `enrichment/config.py`
- Modify: `enrichment/build.py:23-35`
- Test: `tests/test_enrichment_build.py`

**Interfaces:**
- Produces: `config.ENRICHMENT_THEMES_ENABLED: bool` (default True); `build_enrichment` skips all `thematic_bundle` calls when it is False.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enrichment_build.py`:

```python
def test_build_skips_themes_when_flag_off(monkeypatch):
    from enrichment import build as build_mod
    from enrichment import config
    from enrichment.models import ThematicBundle, SymbolBundle
    from enrichment.universe import Universe

    monkeypatch.setattr(config, "ENRICHMENT_ENABLED", True)
    monkeypatch.setattr(config, "ENRICHMENT_THEMES_ENABLED", False)

    class RecordingProvider:
        name = "rec"
        def __init__(self): self.themes_called = []
        def symbol_bundle(self, t): return SymbolBundle(ticker=t, rp_entity_id=None, sentiment=None)
        def thematic_bundle(self, th):
            self.themes_called.append(th); return ThematicBundle(theme=th)

    prov = RecordingProvider()
    uni = Universe(tickers=["CVX"], themes=["gold", "defence"])
    bundles = build_mod.build_enrichment(uni, as_of="2026-07-19T00:00:00+00:00", provider=prov)
    assert prov.themes_called == []
    assert bundles.themes == []
    assert len(bundles.symbols) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell tool): `python -m pytest tests/test_enrichment_build.py::test_build_skips_themes_when_flag_off -v`
Expected: FAIL — `AttributeError: ... ENRICHMENT_THEMES_ENABLED` (attr not set yet).

- [ ] **Step 3: Add the config flag**

In `enrichment/config.py`, after the `ENRICHMENT_PROVIDER` block:

```python
# Thematic search (the ~10x-cost search path) — toggle off if low-value.
ENRICHMENT_THEMES_ENABLED = os.environ.get("ENRICHMENT_THEMES_ENABLED", "1") == "1"
```

- [ ] **Step 4: Gate the theme fan-out**

In `enrichment/build.py`, replace the `themes = ...` / `theme_bundles = ...` lines so themes are only fanned out when enabled:

```python
    tickers = universe.tickers[: config.ENRICHMENT_MAX_SYMBOLS]
    themes = (
        universe.themes[: config.ENRICHMENT_MAX_THEMES]
        if config.ENRICHMENT_THEMES_ENABLED
        else []
    )
    dropped_sym = len(universe.tickers) - len(tickers)
    dropped_thm = (len(universe.themes) - len(themes)) if config.ENRICHMENT_THEMES_ENABLED else 0
```

(Leave the rest of `build_enrichment` unchanged; `theme_bundles` becomes `[]` because `themes` is empty.)

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_enrichment_build.py -v`
Expected: PASS (all build tests).

- [ ] **Step 6: Commit**

```bash
git add enrichment/config.py enrichment/build.py tests/test_enrichment_build.py
git commit -m "feat(enrichment): add ENRICHMENT_THEMES_ENABLED toggle for thematic search"
```

---

## Task 2: Reshape SentimentScore + ToS-compliant persisted snapshot

**Files:**
- Modify: `enrichment/models.py`
- Test: `tests/test_enrichment_models.py`

**Interfaces:**
- Produces:
  - `SentimentScore(as_of, daily_sentiment, sentiment_pressure, abnormal_media_attention, trend_mean, trend_delta, n_points=0)` — all floats `| None` except `as_of: str | None` and `n_points: int`.
  - `EnrichmentBundles.to_persisted_dict() -> dict` = `{as_of, provider, symbols:[{ticker, rp_entity_id, sentiment}]}` where `sentiment` is the score's dict or `None`; no events/evidence/themes.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Replace the body of `tests/test_enrichment_models.py` with the new shape (keep the file's other passing tests that don't build `SentimentScore`; the two round-trip tests below replace the old ones):

```python
from enrichment.models import (
    EnrichmentBundles, EvidenceDoc, Event, SentimentScore, SymbolBundle,
    ThematicBundle, bundles_from_dict,
)

def _score():
    return SentimentScore(
        as_of="2024-03-28", daily_sentiment=0.06, sentiment_pressure=-0.75,
        abnormal_media_attention=-0.74, trend_mean=0.01, trend_delta=0.05, n_points=87,
    )

def test_empty_bundles_is_empty():
    b = EnrichmentBundles(as_of="2026-07-19T20:00:00+00:00")
    assert b.is_empty() is True
    assert b.provider == "null"

def test_bundles_with_symbol_is_not_empty():
    sym = SymbolBundle(ticker="AVAV", rp_entity_id="F1EB39", sentiment=None)
    b = EnrichmentBundles(as_of="2026-07-19T20:00:00+00:00", symbols=[sym])
    assert b.is_empty() is False

def test_full_dict_round_trips_new_score():
    b = EnrichmentBundles(
        as_of="2026-07-19T20:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle(
            ticker="CVX", rp_entity_id="D54E62", sentiment=_score(),
            events=[Event("earnings-call", "Q2 2024", "2024-08-01")],
            evidence=[EvidenceDoc("Tengiz", "Reuters", "2024-06-18", "u", -0.73)],
        )],
        themes=[ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2024-06-19")])],
    )
    assert bundles_from_dict(b.to_dict()) == b

def test_persisted_dict_drops_content():
    b = EnrichmentBundles(
        as_of="2026-07-19T20:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle(
            ticker="CVX", rp_entity_id="D54E62", sentiment=_score(),
            events=[Event("earnings-call", "Q2 2024", "2024-08-01")],
            evidence=[EvidenceDoc("Tengiz", "Reuters", "2024-06-18", "u", -0.73)],
        )],
        themes=[ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2024-06-19")])],
    )
    p = b.to_persisted_dict()
    assert p["themes"] == [] if "themes" in p else True   # no themes key or empty
    sym = p["symbols"][0]
    assert sym["ticker"] == "CVX" and sym["rp_entity_id"] == "D54E62"
    assert sym["sentiment"]["daily_sentiment"] == 0.06
    assert "events" not in sym and "evidence" not in sym
    # reload is still a valid annotate input (sentiment survives)
    reloaded = bundles_from_dict(p)
    assert reloaded.symbols[0].sentiment.daily_sentiment == 0.06

def test_persisted_dict_handles_none_sentiment():
    b = EnrichmentBundles(
        as_of="2026-07-19T20:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle(ticker="NADA", rp_entity_id=None, sentiment=None)],
    )
    p = b.to_persisted_dict()
    assert p["symbols"][0]["sentiment"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_enrichment_models.py -v`
Expected: FAIL — `SentimentScore.__init__` rejects `as_of=`/`daily_sentiment=` (old fields), and `to_persisted_dict` missing.

- [ ] **Step 3: Reshape `SentimentScore` and add `to_persisted_dict`**

In `enrichment/models.py`, replace the `SentimentScore` dataclass:

```python
@dataclass(frozen=True)
class SentimentScore:
    as_of: str | None                        # latest series point's date, "YYYY-MM-DD"
    daily_sentiment: float | None            # latest, -1..1 (media tone)
    sentiment_pressure: float | None         # latest native abnormality signal
    abnormal_media_attention: float | None   # latest native attention signal
    trend_mean: float | None = None          # mean daily_sentiment over the window
    trend_delta: float | None = None         # latest daily_sentiment - trend_mean
    n_points: int = 0                        # series length backing the stats
```

`symbol_from_dict` already does `SentimentScore(**s)` — no change needed (new keys flow through).

Add the persisted projection to `EnrichmentBundles` (after `to_dict`):

```python
    def to_persisted_dict(self) -> dict:
        """Derived-only projection for on-disk persistence. Drops all vendor
        Content (headlines, search text, thematic docs, event titles) to satisfy
        the no-cache ToS; keeps only what collect-time annotate_signals needs."""
        return {
            "as_of": self.as_of,
            "provider": self.provider,
            "symbols": [
                {
                    "ticker": s.ticker,
                    "rp_entity_id": s.rp_entity_id,
                    "sentiment": asdict(s.sentiment) if s.sentiment else None,
                }
                for s in self.symbols
            ],
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add enrichment/models.py tests/test_enrichment_models.py
git commit -m "feat(enrichment): reshape SentimentScore to native fields + add derived-only to_persisted_dict"
```

---

## Task 3: Render + annotate to new sentiment fields

**Files:**
- Modify: `enrichment/render.py:15-22` (`_fmt_sentiment`), `enrichment/render.py:77-88` (`annotate_signals` dict)
- Test: `tests/test_enrichment_render.py`

**Interfaces:**
- Consumes: `SentimentScore` (Task 2 shape).
- Produces: `annotate_signals` attaches `bigdata_sentiment = {daily_sentiment, sentiment_pressure, abnormal_media_attention, trend_delta, rp_entity_id}`.

- [ ] **Step 1: Write the failing tests**

Replace `SentimentScore(...)` constructions in `tests/test_enrichment_render.py` with the new shape and assert new output. Add/replace:

```python
from enrichment.models import EnrichmentBundles, SymbolBundle, SentimentScore, Event
from enrichment.render import render_prompt_block, annotate_signals

def _score():
    return SentimentScore("2024-03-28", 0.06, -0.75, -0.74, 0.01, 0.05, 87)

def test_prompt_block_shows_native_sentiment_fields():
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle("CVX", "D54E62", _score(),
                              events=[Event("earnings-call", "Q2 2024", "2024-08-01")])],
    )
    out = render_prompt_block(b)
    assert "CVX" in out and "0.06" in out and "-0.75" in out
    assert "Q2 2024" in out
    assert "NEVER a trade trigger" in out

def test_annotate_attaches_new_bigdata_sentiment_shape():
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle("CVX", "D54E62", _score())],
    )
    out = annotate_signals([{"ticker": "CVX", "direction": "long"}], b)
    bd = out[0]["bigdata_sentiment"]
    assert bd["daily_sentiment"] == 0.06
    assert bd["sentiment_pressure"] == -0.75
    assert bd["abnormal_media_attention"] == -0.74
    assert bd["trend_delta"] == 0.05
    assert bd["rp_entity_id"] == "D54E62"
    assert "current" not in bd and "regime" not in bd

def test_annotate_leaves_unmatched_signals_untouched():
    b = EnrichmentBundles(as_of="x", provider="bigdata",
                          symbols=[SymbolBundle("CVX", "D54E62", _score())])
    out = annotate_signals([{"ticker": "NVDA"}], b)
    assert "bigdata_sentiment" not in out[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_enrichment_render.py -v`
Expected: FAIL — old `_fmt_sentiment` reads `s.current`/`s.regime` (AttributeError) and `bigdata_sentiment` has old keys.

- [ ] **Step 3: Rewrite `_fmt_sentiment`**

In `enrichment/render.py`, replace `_fmt_sentiment`:

```python
def _fmt_sentiment(s) -> str:
    if s is None:
        return "n/a"
    trend = ""
    if s.trend_delta is not None and s.trend_mean is not None:
        trend = f" trend={s.trend_delta:+.3f} (mean {s.trend_mean:.3f} over {s.n_points}d)"
    return (
        f"sentiment={s.daily_sentiment} pressure={s.sentiment_pressure} "
        f"attention={s.abnormal_media_attention}{trend} as_of={s.as_of}"
    )
```

- [ ] **Step 4: Rewrite the `annotate_signals` payload**

In `enrichment/render.py`, replace the `"bigdata_sentiment": {...}` dict inside `annotate_signals`:

```python
                "bigdata_sentiment": {
                    "daily_sentiment": s.daily_sentiment,
                    "sentiment_pressure": s.sentiment_pressure,
                    "abnormal_media_attention": s.abnormal_media_attention,
                    "trend_delta": s.trend_delta,
                    "rp_entity_id": bundle.rp_entity_id,
                },
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_render.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add enrichment/render.py tests/test_enrichment_render.py
git commit -m "feat(enrichment): render + annotate use native sentiment fields"
```

---

## Task 4: Rewrite BigdataProvider HTTP layer (auth + real endpoints)

**Files:**
- Modify: `enrichment/providers_bigdata.py`
- Test: `tests/test_enrichment_providers.py`

**Interfaces:**
- Produces (HTTP seam, monkeypatched by parse tests):
  - `_find_entity(ticker) -> dict` → `POST /v1/knowledge-graph/companies` `{"query": ticker}`
  - `_get_sentiment(eid, start, end) -> dict` → `POST /v1/entity-sentiment/` `{"identifier":{"type":"rp_entity_id","value":eid},"timestamp":{"start":start,"end":end}}`
  - `_get_events(eid, start, end) -> dict` → `POST /v1/events-calendar/query` `{"rp_entity_id":[eid],"start_date":start,"end_date":end,"categories":["earnings-call","conference-call"],"limit":100}`
  - `_search(query) -> dict` → `POST /v1/search` `{"search_mode":"fast","query":{"text":query,"filters":{"entity":{"any_of":[...]}} or {}, "max_chunks": SEARCH_MAX_CHUNKS}}`
  - Module: `SENTIMENT_LOOKBACK_DAYS`, `EVENTS_FORWARD_DAYS`, `SEARCH_MAX_CHUNKS`; `_sentiment_window()`, `_events_window()` → `(start, end)` ISO dates.
  - Session sets header `X-API-KEY`.

Note: `_search` here takes only a free-text query (thematic search); entity filtering is added in Task 6. Keep the signature `_search(self, query: str)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_enrichment_providers.py`:

```python
from enrichment.providers_bigdata import (
    BigdataProvider, _sentiment_window, _events_window,
)

def test_bigdata_sets_x_api_key_header_not_bearer():
    p = BigdataProvider("secret", "https://api.bigdata.com")
    assert p._session.headers.get("X-API-KEY") == "secret"
    assert "Authorization" not in p._session.headers

def test_get_sentiment_builds_correct_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": []}
    p._get_sentiment("D8442A", "2024-01-01", "2024-03-01")
    assert calls == [("/v1/entity-sentiment/",
                      {"identifier": {"type": "rp_entity_id", "value": "D8442A"},
                       "timestamp": {"start": "2024-01-01", "end": "2024-03-01"}})]

def test_get_events_builds_flat_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": {}}
    p._get_events("D8442A", "2026-07-19", "2026-10-17")
    assert calls[0][0] == "/v1/events-calendar/query"
    assert calls[0][1] == {
        "rp_entity_id": ["D8442A"], "start_date": "2026-07-19",
        "end_date": "2026-10-17",
        "categories": ["earnings-call", "conference-call"], "limit": 100,
    }

def test_find_entity_builds_kg_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": []}
    p._find_entity("AAPL")
    assert calls == [("/v1/knowledge-graph/companies", {"query": "AAPL"})]

def test_search_builds_search_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": []}
    p._search("gold miners")
    path, payload = calls[0]
    assert path == "/v1/search"
    assert payload["search_mode"] == "fast"
    assert payload["query"]["text"] == "gold miners"
    assert payload["query"]["max_chunks"] == 2

def test_window_helpers_return_iso_dates():
    s0, s1 = _sentiment_window()
    e0, e1 = _events_window()
    for d in (s0, s1, e0, e1):
        assert len(d) == 10 and d[4] == "-" and d[7] == "-"
    assert s0 < s1  # lookback start before end
    assert e0 <= e1  # today before forward end
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_enrichment_providers.py -k "header or request or window" -v`
Expected: FAIL — `_sentiment_window`/`_events_window` missing; header is `Authorization`; old request shapes.

- [ ] **Step 3: Rewrite the HTTP layer**

Replace the top of `enrichment/providers_bigdata.py` (imports, helpers, constructor, HTTP helpers) with:

```python
# enrichment/providers_bigdata.py
"""Bigdata.com (RavenPack) REST client — production provider.

Live-verified against api.bigdata.com on 2026-07-19. Auth is X-API-KEY.
Every public method degrades to an error-tagged bundle instead of raising, so a
Bigdata outage can never break the brief."""

from datetime import datetime, timedelta, timezone

import requests

from common import log

from . import config
from .models import (
    EvidenceDoc, Event, SentimentScore, SymbolBundle, ThematicBundle,
)

SENTIMENT_LOOKBACK_DAYS = 60
EVENTS_FORWARD_DAYS = 90
SEARCH_MAX_CHUNKS = 2


def _iso_date(ts: str | None) -> str:
    """Trim an ISO timestamp to its date; '' for None."""
    return ts[:10] if ts else ""


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _sentiment_window() -> tuple[str, str]:
    end = _today()
    start = end - timedelta(days=SENTIMENT_LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _events_window() -> tuple[str, str]:
    start = _today()
    end = start + timedelta(days=EVENTS_FORWARD_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


class BigdataProvider:
    """Production REST client for Bigdata.com (RavenPack) enrichment.

    v1 does not populate SymbolBundle.evidence (defaults to []); thematic
    evidence comes via thematic_bundle."""

    name = "bigdata"

    def __init__(self, api_key: str, base_url: str):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._entity_cache: dict[str, str | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"X-API-KEY": api_key})

    # --- HTTP helpers (one network call each; named so tests can monkeypatch) ---
    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base}{path}", json=payload,
            timeout=config.ENRICHMENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _find_entity(self, ticker: str) -> dict:
        return self._post("/v1/knowledge-graph/companies", {"query": ticker})

    def _get_sentiment(self, eid: str, start: str, end: str) -> dict:
        return self._post(
            "/v1/entity-sentiment/",
            {"identifier": {"type": "rp_entity_id", "value": eid},
             "timestamp": {"start": start, "end": end}},
        )

    def _get_events(self, eid: str, start: str, end: str) -> dict:
        return self._post(
            "/v1/events-calendar/query",
            {"rp_entity_id": [eid], "start_date": start, "end_date": end,
             "categories": ["earnings-call", "conference-call"], "limit": 100},
        )

    def _search(self, query: str) -> dict:
        return self._post(
            "/v1/search",
            {"search_mode": "fast",
             "query": {"text": query, "filters": {}, "max_chunks": SEARCH_MAX_CHUNKS}},
        )
```

Leave `_resolve`, `symbol_bundle`, `thematic_bundle` below for now (they still reference the old parse — they get rewritten in Tasks 5 & 6). This step may leave `symbol_bundle`/`thematic_bundle` referencing changed helper signatures; that is fixed in the next tasks. To keep the suite green in the meantime, also apply the minimal parse updates in Step 4.

- [ ] **Step 4: Keep the suite green — stub the parse methods to the new helpers**

Temporarily replace `_resolve`, `symbol_bundle`, `thematic_bundle` bodies so they compile against the new helper signatures and the new model (full parse lands in Tasks 5–6). Use:

```python
    def _resolve(self, ticker: str) -> str | None:
        if ticker in self._entity_cache:
            return self._entity_cache[ticker]
        rows = self._find_entity(ticker).get("results") or []
        eid = rows[0].get("id") if rows else None
        self._entity_cache[ticker] = eid
        return eid

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(ticker, None, None, error="no entity match")
            return SymbolBundle(ticker, eid, None)  # full parse in Task 5
        except Exception as e:
            log.warning("Bigdata symbol_bundle(%s) degraded: %s", ticker, e)
            return SymbolBundle(ticker, None, None, error=str(e))

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            self._search(theme)
            return ThematicBundle(theme=theme)  # full parse in Task 6
        except Exception as e:
            log.warning("Bigdata thematic_bundle(%s) degraded: %s", theme, e)
            return ThematicBundle(theme=theme, error=str(e))
```

Delete the now-unused old `_get_sentiment`/`_get_events`/`_search` old bodies and the old imports already replaced above. Remove the old parse tests that assert `zscore_1qt`/`source == "FT"` from `tests/test_enrichment_providers.py` (they are superseded by Tasks 5–6):
- delete `test_bigdata_parse_symbol_bundle`, `test_bigdata_parse_thematic_bundle` (re-added with real fixtures in Tasks 5–6).
- keep `test_bigdata_symbol_degrades_on_error` but update it: monkeypatch `_find_entity` to raise, assert `error` contains "500".

```python
def test_bigdata_symbol_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    def boom(_): raise RuntimeError("HTTP 500")
    monkeypatch.setattr(p, "_find_entity", boom)
    sb = p.symbol_bundle("AVAV")
    assert sb.sentiment is None and sb.error and "500" in sb.error
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_providers.py -v`
Expected: PASS (HTTP-shape + degrade tests; parse tests removed until 5–6).

- [ ] **Step 6: Commit**

```bash
git add enrichment/providers_bigdata.py tests/test_enrichment_providers.py
git commit -m "refactor(enrichment): rewrite Bigdata HTTP layer to verified endpoints + X-API-KEY"
```

---

## Task 5: Parse symbol_bundle (resolve + sentiment + events) to real shapes

**Files:**
- Modify: `enrichment/providers_bigdata.py` (`_resolve`, `_pick_entity`, `symbol_bundle`)
- Create: `tests/fixtures/bigdata_raw/find_AAPL.json`, `sentiment_D8442A.json`, `events_D8442A.json`
- Test: `tests/test_enrichment_providers.py`

**Interfaces:**
- Consumes: HTTP helpers (Task 4).
- Produces: `symbol_bundle(ticker)` returns a `SymbolBundle` with a `SentimentScore` (latest point + trend) and `Event`s; `_pick_entity(rows, ticker) -> str | None` (exact-ticker, prefer PUBLIC).

- [ ] **Step 1: Create the real raw fixtures**

Create `tests/fixtures/bigdata_raw/find_AAPL.json`:

```json
{
  "results": [
    {"id": "D8442A", "name": "Apple Inc.", "type": "PUBLIC", "ticker": "AAPL", "country": "US", "sector": "Technology"},
    {"id": "T6QNVK", "name": "Abhishek Alloys Pvt Ltd.", "type": "PRIVATE", "ticker": null, "country": "IN", "sector": null}
  ]
}
```

Create `tests/fixtures/bigdata_raw/sentiment_D8442A.json`:

```json
{
  "results": [
    {"name": "Apple Inc.", "rp_entity_id": "D8442A", "values": [
      {"date": "2024-01-02", "daily_sentiment": -0.100904, "sentiment_pressure": -0.159639, "abnormal_media_attention": -0.268143},
      {"date": "2024-01-03", "daily_sentiment": 0.04873, "sentiment_pressure": -0.204247, "abnormal_media_attention": -0.299575},
      {"date": "2024-03-28", "daily_sentiment": 0.060095, "sentiment_pressure": -0.756931, "abnormal_media_attention": -0.744677}
    ]}
  ],
  "errors": []
}
```

Create `tests/fixtures/bigdata_raw/events_D8442A.json`:

```json
{
  "results": {
    "D8442A": [
      {"category": "earnings-call", "event_datetime": "2024-02-01T22:00:00.000Z", "title": "Q1 2024", "fiscal_year": 2024, "fiscal_period": "Q1", "rp_collection_id": "abc123", "created_at": "2023-11-01T00:00:00.000Z", "updated_at": "2024-01-20T00:00:00.000Z"}
    ]
  },
  "pagination": {"cursor": "1", "has_cursor": false}
}
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_enrichment_providers.py`:

```python
from enrichment.providers_bigdata import _pick_entity

def test_pick_entity_prefers_exact_ticker_public():
    rows = [
        {"id": "WRONG1", "ticker": "AAPLX", "type": "PUBLIC"},
        {"id": "T6QNVK", "ticker": None, "type": "PRIVATE"},
        {"id": "D8442A", "ticker": "AAPL", "type": "PUBLIC"},
    ]
    assert _pick_entity(rows, "AAPL") == "D8442A"

def test_pick_entity_none_when_no_ticker_match():
    assert _pick_entity([{"id": "X", "ticker": "MSFT", "type": "PUBLIC"}], "AAPL") is None

def test_bigdata_parse_symbol_bundle(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    monkeypatch.setattr(p, "_find_entity", lambda t: _raw("find_AAPL.json"))
    monkeypatch.setattr(p, "_get_sentiment", lambda eid, s, e: _raw("sentiment_D8442A.json"))
    monkeypatch.setattr(p, "_get_events", lambda eid, s, e: _raw("events_D8442A.json"))
    sb = p.symbol_bundle("AAPL")
    assert sb.rp_entity_id == "D8442A"
    assert sb.error is None
    assert sb.sentiment.daily_sentiment == 0.060095      # latest point
    assert sb.sentiment.as_of == "2024-03-28"
    assert sb.sentiment.n_points == 3
    assert abs(sb.sentiment.trend_mean - (-0.100904 + 0.04873 + 0.060095) / 3) < 1e-9
    assert abs(sb.sentiment.trend_delta - (0.060095 - sb.sentiment.trend_mean)) < 1e-9
    assert sb.events[0].category == "earnings-call"
    assert sb.events[0].date == "2024-02-01"
    assert sb.events[0].title == "Q1 2024"
    assert sb.events[0].url is None

def test_bigdata_symbol_empty_series_yields_none_sentiment(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    monkeypatch.setattr(p, "_find_entity", lambda t: _raw("find_AAPL.json"))
    monkeypatch.setattr(p, "_get_sentiment", lambda eid, s, e: {"results": [], "errors": []})
    monkeypatch.setattr(p, "_get_events", lambda eid, s, e: {"results": {}})
    sb = p.symbol_bundle("AAPL")
    assert sb.rp_entity_id == "D8442A"
    assert sb.sentiment is None
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_enrichment_providers.py -k "pick_entity or parse_symbol or empty_series" -v`
Expected: FAIL — `_pick_entity` missing; `symbol_bundle` returns `sentiment=None` (stub from Task 4).

- [ ] **Step 4: Implement resolve + symbol parse**

In `enrichment/providers_bigdata.py`, add `_pick_entity` (module-level) and rewrite `_resolve` + `symbol_bundle`:

```python
def _pick_entity(rows: list[dict], ticker: str) -> str | None:
    """Choose the entity id for an exact ticker match, preferring PUBLIC."""
    tkr = ticker.upper()
    matches = [r for r in rows if (r.get("ticker") or "").upper() == tkr]
    if not matches:
        return None
    public = [r for r in matches if r.get("type") == "PUBLIC"]
    chosen = public[0] if public else matches[0]
    return chosen.get("id")
```

```python
    def _resolve(self, ticker: str) -> str | None:
        if ticker in self._entity_cache:
            return self._entity_cache[ticker]
        rows = self._find_entity(ticker).get("results") or []
        eid = _pick_entity(rows, ticker)
        self._entity_cache[ticker] = eid
        return eid

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(ticker, None, None, error="no entity match")
            sent_start, sent_end = _sentiment_window()
            values = (
                (self._get_sentiment(eid, sent_start, sent_end).get("results") or [{}])[0]
                .get("values")
                or []
            )
            sentiment = _score_from_values(values)
            ev_start, ev_end = _events_window()
            ev_rows = self._get_events(eid, ev_start, ev_end).get("results", {}).get(eid, [])
            events = [
                Event(
                    category=e.get("category", ""),
                    title=e.get("title", ""),
                    date=_iso_date(e.get("event_datetime")),
                    url=None,
                )
                for e in ev_rows
            ]
            return SymbolBundle(ticker, eid, sentiment, events=events)
        except Exception as e:  # degrade, never crash the brief
            log.warning("Bigdata symbol_bundle(%s) degraded: %s", ticker, e)
            return SymbolBundle(ticker, None, None, error=str(e))
```

Add the series→score helper (module-level):

```python
def _score_from_values(values: list[dict]) -> SentimentScore | None:
    if not values:
        return None
    pts = sorted(values, key=lambda v: v.get("date", ""))
    latest = pts[-1]
    daily = [v["daily_sentiment"] for v in pts if v.get("daily_sentiment") is not None]
    mean = sum(daily) / len(daily) if daily else None
    latest_ds = latest.get("daily_sentiment")
    delta = (latest_ds - mean) if (mean is not None and latest_ds is not None) else None
    return SentimentScore(
        as_of=latest.get("date"),
        daily_sentiment=latest_ds,
        sentiment_pressure=latest.get("sentiment_pressure"),
        abnormal_media_attention=latest.get("abnormal_media_attention"),
        trend_mean=mean,
        trend_delta=delta,
        n_points=len(pts),
    )
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_providers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add enrichment/providers_bigdata.py tests/test_enrichment_providers.py tests/fixtures/bigdata_raw/find_AAPL.json tests/fixtures/bigdata_raw/sentiment_D8442A.json tests/fixtures/bigdata_raw/events_D8442A.json
git commit -m "feat(enrichment): parse symbol_bundle (resolve+sentiment+events) from live shapes"
```

---

## Task 6: Parse thematic_bundle (search) to real shape

**Files:**
- Modify: `enrichment/providers_bigdata.py` (`thematic_bundle`)
- Create: `tests/fixtures/bigdata_raw/search_AAPL.json`
- Test: `tests/test_enrichment_providers.py`

**Interfaces:**
- Consumes: `_search` (Task 4).
- Produces: `thematic_bundle(theme)` returns `ThematicBundle` with `EvidenceDoc`s (`source.name`, `timestamp[:10]`, first-chunk sentiment).

- [ ] **Step 1: Create the real search fixture**

Create `tests/fixtures/bigdata_raw/search_AAPL.json`:

```json
{
  "results": [
    {
      "id": "EBB46EE7EE58FD178B55E4F90D308040",
      "headline": "What's Going on With Apple Stock Friday?",
      "timestamp": "2026-07-17T11:58:44",
      "source": {"id": "5A5702", "name": "Benzinga", "rank": "RANK_1", "tier": "content-premium-news"},
      "url": "https://www.benzinga.com/node/60519766",
      "chunks": [{"cnum": 3, "text": "A key support level is around $287.50", "relevance": 0.806, "sentiment": -0.23}]
    }
  ],
  "external_results": {}
}
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_enrichment_providers.py`:

```python
def test_bigdata_parse_thematic_bundle(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    monkeypatch.setattr(p, "_search", lambda q: _raw("search_AAPL.json"))
    tb = p.thematic_bundle("apple")
    assert tb.error is None
    d = tb.docs[0]
    assert d.source == "Benzinga"
    assert d.date == "2026-07-17"
    assert d.headline.startswith("What's Going on")
    assert d.sentiment == -0.23

def test_bigdata_thematic_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    def boom(_): raise RuntimeError("HTTP 500")
    monkeypatch.setattr(p, "_search", boom)
    tb = p.thematic_bundle("apple")
    assert tb.docs == [] and tb.error and "500" in tb.error
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_enrichment_providers.py -k thematic -v`
Expected: FAIL — stub `thematic_bundle` returns no docs.

- [ ] **Step 4: Implement the thematic parse**

In `enrichment/providers_bigdata.py`, rewrite `thematic_bundle`:

```python
    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            results = self._search(theme).get("results") or []
            docs = [
                EvidenceDoc(
                    headline=r.get("headline", ""),
                    source=(r.get("source") or {}).get("name", ""),
                    date=_iso_date(r.get("timestamp")),
                    url=r.get("url"),
                    sentiment=(r.get("chunks") or [{}])[0].get("sentiment"),
                )
                for r in results
            ]
            return ThematicBundle(theme=theme, docs=docs)
        except Exception as e:
            log.warning("Bigdata thematic_bundle(%s) degraded: %s", theme, e)
            return ThematicBundle(theme=theme, error=str(e))
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_providers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add enrichment/providers_bigdata.py tests/test_enrichment_providers.py tests/fixtures/bigdata_raw/search_AAPL.json
git commit -m "feat(enrichment): parse thematic_bundle from live /v1/search shape"
```

---

## Task 7: Update model + fixture files for FixtureProvider

**Files:**
- Modify: `tests/fixtures/enrichment/symbol_CVX.json` (new `SentimentScore` shape)
- Test: `tests/test_enrichment_providers.py` (existing `test_fixture_provider_loads_symbol`)

**Interfaces:**
- Consumes: `symbol_from_dict` (Task 2).

- [ ] **Step 1: Update the model fixture**

Overwrite `tests/fixtures/enrichment/symbol_CVX.json` so its `sentiment` uses the new keys (keep events/evidence to exercise the full loader):

```json
{
  "ticker": "CVX",
  "rp_entity_id": "D54E62",
  "sentiment": {
    "as_of": "2026-06-19",
    "daily_sentiment": -0.068,
    "sentiment_pressure": -0.42,
    "abnormal_media_attention": -0.31,
    "trend_mean": -0.05,
    "trend_delta": -0.018,
    "n_points": 40
  },
  "events": [{"category": "earnings-call", "title": "Q2 2026", "date": "2026-07-25", "url": null}],
  "evidence": [{"headline": "Tengiz incident", "source": "Reuters", "date": "2026-06-18", "url": "u", "sentiment": -0.73}]
}
```

- [ ] **Step 2: Update the fixture-loader assertion**

In `tests/test_enrichment_providers.py`, replace `test_fixture_provider_loads_symbol`:

```python
def test_fixture_provider_loads_symbol():
    p = FixtureProvider(FIX)
    sb = p.symbol_bundle("CVX")
    assert sb.rp_entity_id == "D54E62"
    assert sb.sentiment.daily_sentiment == -0.068
    assert sb.sentiment.trend_delta == -0.018
    assert sb.events[0].category == "earnings-call"
    assert sb.evidence[0].sentiment == -0.73
```

- [ ] **Step 3: Run to verify pass**

Run: `python -m pytest tests/test_enrichment_providers.py::test_fixture_provider_loads_symbol -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/enrichment/symbol_CVX.json tests/test_enrichment_providers.py
git commit -m "test(enrichment): update model fixtures to reshaped SentimentScore"
```

---

## Task 8: ToS-compliant snapshot + usage logging in brief.py

**Files:**
- Modify: `brief.py:2654-2657` (persist call), `brief.py:2659-2665` (log)
- Test: `tests/test_enrichment_wiring.py`

**Interfaces:**
- Consumes: `EnrichmentBundles.to_persisted_dict()` (Task 2).

- [ ] **Step 1: Write/adjust the failing test**

In `tests/test_enrichment_wiring.py`, add a test that the persisted snapshot omits Content. If the file exercises `mode_submit` with a fixture provider, assert on the written JSON; otherwise unit-test the projection at the call boundary:

```python
def test_submit_persists_derived_only_snapshot(tmp_path):
    from enrichment.models import (
        EnrichmentBundles, SymbolBundle, SentimentScore, Event, EvidenceDoc, ThematicBundle,
    )
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00", provider="bigdata",
        symbols=[SymbolBundle("CVX", "D54E62",
                              SentimentScore("2026-07-18", -0.07, -0.4, -0.3, -0.05, -0.02, 40),
                              events=[Event("earnings-call", "Q2 2026", "2026-07-25")],
                              evidence=[EvidenceDoc("secret headline", "FT", "2026-07-01", "u", -0.5)])],
        themes=[ThematicBundle("gold", docs=[EvidenceDoc("themed headline", "FT", "2026-07-02")])],
    )
    persisted = b.to_persisted_dict()
    import json
    blob = json.dumps(persisted)
    assert "secret headline" not in blob
    assert "themed headline" not in blob
    assert "Q2 2026" not in blob            # event title is Content, dropped
    assert persisted["symbols"][0]["sentiment"]["daily_sentiment"] == -0.07
```

- [ ] **Step 2: Run to verify pass or fail**

Run: `python -m pytest tests/test_enrichment_wiring.py::test_submit_persists_derived_only_snapshot -v`
Expected: PASS at the projection level (Task 2 already implements `to_persisted_dict`). This test locks the invariant before wiring `brief.py`.

- [ ] **Step 3: Switch the snapshot write to the derived-only projection**

In `brief.py`, in `mode_submit`, change the persist call:

```python
        if not bundles.is_empty():
            _write_json_atomic(
                DATA_DIR / "enrichment" / f"enrichment-{today}.json",
                bundles.to_persisted_dict(),
            )
            enrichment_block = render_prompt_block(bundles)
```

- [ ] **Step 4: Add run-usage logging (cost visibility)**

`build_enrichment` already logs a summary line. Extend the provider to expose accumulated usage and log it. In `providers_bigdata.py`, add a `self._usage = 0` in `__init__` and, in `_post`, after `resp.raise_for_status()`:

```python
        data = resp.json()
        usage = data.get("usage") or (data.get("metadata") or {}).get("usage") or {}
        self._usage += sum(v for v in usage.values() if isinstance(v, (int, float)))
        return data
```

Add a read accessor used by build.py (optional, but keeps logging in one place):

```python
    @property
    def usage_units(self) -> float:
        return self._usage
```

In `enrichment/build.py`, after computing `errs`, add a usage line when the provider exposes it:

```python
    units = getattr(provider, "usage_units", None)
    if units is not None:
        log.info("Enrichment usage: provider=%s units=%s", provider.name, units)
```

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/test_enrichment_wiring.py tests/test_enrichment_build.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add brief.py enrichment/providers_bigdata.py enrichment/build.py tests/test_enrichment_wiring.py
git commit -m "feat(enrichment): persist derived-only snapshot (ToS) + log run usage units"
```

---

## Task 9: Full gate + capture real fixtures directory

**Files:**
- Create: `enrichment/fixtures/live_2026-07-19/` (optional real captures for manual flag-on validation) — SKIP if not needed; the `tests/fixtures/bigdata_raw/` files already cover automated tests.

- [ ] **Step 1: Run ruff**

Run: `python -m ruff check . ; python -m ruff format --check .`
Expected: no errors. If `ruff format` reports files, run `python -m ruff format .` and re-stage.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (grep the tail for `failed`/`error`; do not infer success from exit code of a piped command).

- [ ] **Step 3: Commit any formatting**

```bash
git add -A
git commit -m "chore(enrichment): ruff format after Bigdata rewrite" || echo "nothing to format"
```

---

## Rollout (operator, after merge — not a code task)

1. Code is dark by default (`ENRICHMENT_ENABLED` off). No Dockerfile/CI change (enrichment/ already COPYd; no new dep).
2. On the deploy host set: `ENRICHMENT_ENABLED=1`, `ENRICHMENT_PROVIDER=bigdata` (`BIGDATA_API_KEY` already present), `ENRICHMENT_THEMES_ENABLED=1`.
3. First live `submit` run: confirm the log shows `Enrichment built: ... errors=0` and an `Enrichment usage: ... units=N` line; eyeball the rendered prompt block and the derived-only `enrichment-{today}.json` (must contain no headlines/titles); cross-check `units` against the balance at `app.bigdata.com/usage`.
4. If any symbol shows `error=`, capture one raw `_post` response (temporary DEBUG log) and reconcile the JSON path.
5. If thematic search proves low-value, set `ENRICHMENT_THEMES_ENABLED=0`.

---

## Self-Review notes (author)

- **Spec coverage:** themes toggle (T1), SentimentScore reshape + persisted-dict (T2), render/annotate (T3), X-API-KEY + endpoints (T4), resolve exact-match + sentiment latest+trend + events flat shape (T5), search per-chunk (T6), fixture update (T7), ToS snapshot + usage log (T8), full gate (T9). All spec sections mapped.
- **Type consistency:** `SentimentScore(as_of, daily_sentiment, sentiment_pressure, abnormal_media_attention, trend_mean, trend_delta, n_points)` used identically across models/render/provider/fixtures. `_get_sentiment`/`_get_events` carry `(eid, start, end)` everywhere they are called or monkeypatched. `bigdata_sentiment` dict keys match between render and its test.
- **Descriptive-only invariant:** no task adds a sizing field; `bigdata_sentiment` stays descriptive.

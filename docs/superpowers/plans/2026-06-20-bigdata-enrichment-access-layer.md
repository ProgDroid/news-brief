# Bigdata.com Enrichment — Shared Access Layer + Feature A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a flag-gated `enrichment/` package that feeds Bigdata.com sentiment/events/evidence into the daily brief as read-only context, behind a vendor-neutral provider seam, with zero behaviour change until the feature flag is turned on.

**Architecture:** A new first-party package `enrichment/` exposes a `Provider` protocol (vendor seam) with three implementations — `NullProvider` (default; empty bundles), `FixtureProvider` (model-level JSON, used in tests and as the in-session MCP-captured interim), and `BigdataProvider` (production REST client, flag-gated OFF). A `build_universe` step derives the per-run query set from existing artifacts (`book.json`, `watchlist.json`, latest signals snapshot, feedback pins). `build_enrichment` assembles `EnrichmentBundles` (per-symbol + thematic), which `render_prompt_block` serialises into a delimited, read-only section of the daily prompt. The two bundle types are the future digest↔trading seam. When the flag is OFF (default), `build_enrichment` returns empty bundles and the pipeline runs exactly as today.

**Tech Stack:** Python 3.12, `requests` (existing dep — no new dependencies), `dataclasses`, pytest, ruff. Anthropic Sonnet 4.6 Batch API (unchanged).

## Global Constraints

- **Python 3.12** (matches `Dockerfile` `python:3.12-slim` and CI `python-version: '3.12'`).
- **No new runtime dependencies** — the REST client uses `requests` (already in `requirements.txt`). Do not add the `bigdata-client` SDK.
- **Feature flag default OFF.** `ENRICHMENT_ENABLED` defaults to `"0"`. With the flag off (or on any provider error/timeout) the brief pipeline must be byte-for-byte unchanged.
- **Read-only context, never auto-sizing.** Enrichment output is injected as prompt context and may carry a *descriptive* `bigdata_sentiment` annotation on saved signals. It must never write or influence any position-sizing field. The sizing question is gated by the separate Feature B backtest.
- **Branding:** Refer to the service as exactly `Bigdata.com` (uppercase B, lowercase d, include `.com`) in any user-facing prompt text or report; link `https://bigdata.com`.
- **Interpretive caveat (must appear in the rendered prompt block):** Bigdata.com sentiment = *media tone about the company*, NOT a price/direction forecast — treat as an orthogonal tone overlay, never a trigger.
- **New first-party module → allowlist update required.** A new top-level package needs `COPY enrichment/ ./enrichment/` in the `Dockerfile` AND `enrichment/**` added to the CI workflow `paths:` and the `ruff` lint/format commands, or the container hits `ModuleNotFoundError` at runtime even though CI (full checkout) passes. (Memory: `dockerfile-copy-allowlist`.)
- **Commits via the Bash tool, not PowerShell** (PowerShell prepends a UTF-8 BOM to the commit subject). End commit messages with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- **Full pre-push gate:** `ruff check brief.py common.py trading.py enrichment tests` + `ruff format --check ...` + `pytest -q`. Stage every reformatted file or CI fails. (Memory: `brief-local-run`.)
- **Run Python via the PowerShell tool** in this environment (the Bash tool errors `stdin is not a tty`); run `git` via the Bash tool. (Memory: `python-via-powershell`.)
- **Commit straight to `main`** — solo repo, don't branch. (Memory: `newsbrief-commit-to-main`.)

## File Structure

**New package `enrichment/`:**

- `enrichment/__init__.py` — public API re-exports: `build_enrichment`, `build_universe`, `render_prompt_block`, `annotate_signals`, the model types, `is_enabled`.
- `enrichment/config.py` — env-driven flags/limits for the subsystem (kept local to keep the optional subsystem self-contained; `common.py` stays focused on always-on config).
- `enrichment/models.py` — frozen dataclasses: `SentimentScore`, `Event`, `EvidenceDoc`, `SymbolBundle`, `ThematicBundle`, `EnrichmentBundles`.
- `enrichment/providers.py` — `Provider` protocol, `NullProvider`, `FixtureProvider`, `BigdataProvider`, `get_provider()`.
- `enrichment/universe.py` — `Universe` dataclass, `build_universe(...)`, ticker normalisation, ETF→theme routing, `latest_signal_tickers(...)`.
- `enrichment/build.py` — `build_enrichment(...)` orchestration (flag gate + bounded fan-out).
- `enrichment/render.py` — `render_prompt_block(...)` and `annotate_signals(...)`.

**New tests:**

- `tests/test_enrichment_models.py`
- `tests/test_enrichment_providers.py`
- `tests/test_enrichment_universe.py`
- `tests/test_enrichment_build.py`
- `tests/test_enrichment_render.py`
- `tests/fixtures/enrichment/` — model-level JSON fixtures (one per symbol/theme) for `FixtureProvider`.
- `tests/fixtures/bigdata_raw/` — documented-shape raw REST JSON for `BigdataProvider` parser tests.

**Modified:**

- `brief.py` — imports; `build_daily_prompt` gains an `enrichment_block` param; `mode_submit` assembles + persists + injects; `mode_collect` annotates signals.
- `Dockerfile:13` — add `COPY enrichment/ ./enrichment/`.
- `.github/workflows/docker-publish.yml` — add `enrichment/**` to `paths:` and `enrichment` to the lint commands.

---

### Task 1: Package skeleton — config + models

**Files:**
- Create: `enrichment/__init__.py`
- Create: `enrichment/config.py`
- Create: `enrichment/models.py`
- Test: `tests/test_enrichment_models.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `enrichment.config`: `ENRICHMENT_ENABLED: bool`, `ENRICHMENT_PROVIDER: str`, `BIGDATA_API_KEY: str`, `BIGDATA_BASE_URL: str`, `ENRICHMENT_MAX_SYMBOLS: int`, `ENRICHMENT_MAX_THEMES: int`, `ENRICHMENT_HTTP_TIMEOUT: float`, `FIXTURE_DIR: str`.
  - `enrichment.models`: frozen dataclasses `SentimentScore`, `Event`, `EvidenceDoc`, `SymbolBundle`, `ThematicBundle`, `EnrichmentBundles`. `EnrichmentBundles` has `.is_empty() -> bool` and `.to_dict() -> dict`.

- [ ] **Step 1: Create the package `__init__.py` (stub re-exports filled in later tasks)**

```python
# enrichment/__init__.py
"""Bigdata.com enrichment subsystem.

Flag-gated (OFF by default). Feeds entity-resolved sentiment, events and
evidence into the daily brief as READ-ONLY context behind a vendor-neutral
provider seam. The bundle types here are the future digest<->trading seam.
"""

from .config import ENRICHMENT_ENABLED as _ENABLED
from .models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)


def is_enabled() -> bool:
    return _ENABLED


__all__ = [
    "is_enabled",
    "EnrichmentBundles",
    "EvidenceDoc",
    "Event",
    "SentimentScore",
    "SymbolBundle",
    "ThematicBundle",
]
```

- [ ] **Step 2: Create `enrichment/config.py`**

```python
# enrichment/config.py
"""Env-driven configuration for the enrichment subsystem.

Kept local to the package (not in common.py) because the whole subsystem is
optional and flag-gated; common.py stays focused on always-on infrastructure.
"""

import os

# Master switch. OFF by default — enrichment ships dark until REST creds land.
ENRICHMENT_ENABLED = os.environ.get("ENRICHMENT_ENABLED", "0") == "1"

# Provider selection: "null" | "fixture" | "bigdata". Empty -> auto (bigdata if a
# key is present, else null). Only consulted when ENRICHMENT_ENABLED is true.
ENRICHMENT_PROVIDER = os.environ.get("ENRICHMENT_PROVIDER", "").strip().lower()

# Bigdata.com REST credentials/endpoint (business-email REST key; see design spec).
BIGDATA_API_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()
BIGDATA_BASE_URL = os.environ.get("BIGDATA_BASE_URL", "https://api.bigdata.com").strip()

# Bounded fan-out — hard ceilings so a large watchlist can't blow the query budget.
ENRICHMENT_MAX_SYMBOLS = int(os.environ.get("ENRICHMENT_MAX_SYMBOLS", "20"))
ENRICHMENT_MAX_THEMES = int(os.environ.get("ENRICHMENT_MAX_THEMES", "8"))
ENRICHMENT_HTTP_TIMEOUT = float(os.environ.get("ENRICHMENT_HTTP_TIMEOUT", "20"))

# Directory of model-level JSON fixtures for FixtureProvider (tests / MCP interim).
FIXTURE_DIR = os.environ.get("ENRICHMENT_FIXTURE_DIR", "").strip()
```

- [ ] **Step 3: Write the failing test for the models**

```python
# tests/test_enrichment_models.py
from enrichment.models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)


def test_empty_bundles_is_empty():
    b = EnrichmentBundles(as_of="2026-06-20T20:00:00+00:00")
    assert b.is_empty() is True
    assert b.provider == "null"


def test_bundles_with_symbol_is_not_empty():
    sym = SymbolBundle(ticker="AVAV", rp_entity_id="F1EB39", sentiment=None)
    b = EnrichmentBundles(as_of="2026-06-20T20:00:00+00:00", symbols=[sym])
    assert b.is_empty() is False


def test_to_dict_round_trips_nested_dataclasses():
    b = EnrichmentBundles(
        as_of="2026-06-20T20:00:00+00:00",
        provider="fixture",
        symbols=[
            SymbolBundle(
                ticker="CVX",
                rp_entity_id="D54E62",
                sentiment=SentimentScore(
                    current=-0.068,
                    baseline=0.0,
                    zscore_1mo=-0.9,
                    zscore_1qt=-1.4,
                    regime="Neutral",
                ),
                events=[Event(category="earnings-call", title="Q2 call", date="2026-07-25")],
                evidence=[
                    EvidenceDoc(
                        headline="Tengiz incident",
                        source="Reuters",
                        date="2026-06-18",
                        url="https://example.com/a",
                        sentiment=-0.73,
                    )
                ],
            )
        ],
        themes=[ThematicBundle(theme="gold", docs=[])],
    )
    d = b.to_dict()
    assert d["provider"] == "fixture"
    assert d["symbols"][0]["sentiment"]["regime"] == "Neutral"
    assert d["symbols"][0]["events"][0]["category"] == "earnings-call"
    assert d["symbols"][0]["evidence"][0]["sentiment"] == -0.73
    assert d["themes"][0]["theme"] == "gold"


def test_bundles_from_dict_round_trips():
    from enrichment.models import bundles_from_dict

    b = EnrichmentBundles(
        as_of="2026-06-20T20:00:00+00:00",
        provider="fixture",
        symbols=[
            SymbolBundle(
                ticker="CVX",
                rp_entity_id="D54E62",
                sentiment=SentimentScore(-0.07, 0.0, -0.9, -1.4, "Neutral", "reduced"),
                events=[Event("earnings-call", "Q2 call", "2026-07-25")],
                evidence=[EvidenceDoc("h", "Reuters", "2026-06-18", "u", -0.73)],
            )
        ],
        themes=[ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2026-06-19")])],
    )
    assert bundles_from_dict(b.to_dict()) == b
```

- [ ] **Step 4: Run the test to verify it fails**

Run (PowerShell tool): `python -m pytest tests/test_enrichment_models.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.models'`.

- [ ] **Step 5: Create `enrichment/models.py`**

```python
# enrichment/models.py
"""Enrichment data model. Frozen dataclasses — these bundle types are the
digest<->trading contract (independently consumable by a future standalone
digest or trading system)."""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class SentimentScore:
    current: float | None
    baseline: float | None
    zscore_1mo: float | None
    zscore_1qt: float | None
    regime: str  # "Positive" | "Neutral" | "Negative" | "Unknown"
    confidence: str | None = None  # provider self-flag, e.g. "reduced" when source-concentrated


@dataclass(frozen=True)
class Event:
    category: str  # "earnings-call" | "conference-call" | ...
    title: str
    date: str  # ISO date "YYYY-MM-DD"
    url: str | None = None


@dataclass(frozen=True)
class EvidenceDoc:
    headline: str
    source: str
    date: str  # ISO
    url: str | None = None
    sentiment: float | None = None


@dataclass(frozen=True)
class SymbolBundle:
    ticker: str  # base symbol used to query (e.g. "AVAV")
    rp_entity_id: str | None
    sentiment: SentimentScore | None
    events: list[Event] = field(default_factory=list)
    evidence: list[EvidenceDoc] = field(default_factory=list)
    error: str | None = None  # set when this symbol degraded; bundle still returned


@dataclass(frozen=True)
class ThematicBundle:
    theme: str
    docs: list[EvidenceDoc] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class EnrichmentBundles:
    as_of: str  # ISO timestamp the enrichment was built
    symbols: list[SymbolBundle] = field(default_factory=list)
    themes: list[ThematicBundle] = field(default_factory=list)
    provider: str = "null"

    def is_empty(self) -> bool:
        return not self.symbols and not self.themes

    def to_dict(self) -> dict:
        return asdict(self)


# --- deserialization (public; used by FixtureProvider and the collect reload) ---
def symbol_from_dict(d: dict) -> SymbolBundle:
    s = d.get("sentiment")
    return SymbolBundle(
        ticker=d["ticker"],
        rp_entity_id=d.get("rp_entity_id"),
        sentiment=SentimentScore(**s) if s else None,
        events=[Event(**e) for e in d.get("events", [])],
        evidence=[EvidenceDoc(**ev) for ev in d.get("evidence", [])],
        error=d.get("error"),
    )


def theme_from_dict(d: dict) -> ThematicBundle:
    return ThematicBundle(
        theme=d["theme"],
        docs=[EvidenceDoc(**ev) for ev in d.get("docs", [])],
        error=d.get("error"),
    )


def bundles_from_dict(d: dict) -> EnrichmentBundles:
    return EnrichmentBundles(
        as_of=d.get("as_of", ""),
        provider=d.get("provider", "null"),
        symbols=[symbol_from_dict(s) for s in d.get("symbols", [])],
        themes=[theme_from_dict(t) for t in d.get("themes", [])],
    )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_models.py -q`
Expected: PASS (3 passed).

- [ ] **Step 7: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_models.py
ruff check enrichment tests/test_enrichment_models.py
git add enrichment/__init__.py enrichment/config.py enrichment/models.py tests/test_enrichment_models.py
git commit -m "feat(enrichment): package skeleton — config flags + bundle models"
```

---

### Task 2: Provider seam — Null, Fixture, selector

**Files:**
- Create: `enrichment/providers.py`
- Create: `tests/fixtures/enrichment/symbol_CVX.json`
- Create: `tests/fixtures/enrichment/theme_gold.json`
- Test: `tests/test_enrichment_providers.py`

**Interfaces:**
- Consumes: `enrichment.models` (all bundle types), `enrichment.config`.
- Produces:
  - `Provider` protocol: attribute `name: str`; methods `symbol_bundle(ticker: str) -> SymbolBundle`, `thematic_bundle(theme: str) -> ThematicBundle`.
  - `NullProvider()` — `name = "null"`; returns empty bundles.
  - `FixtureProvider(fixture_dir: str)` — `name = "fixture"`; reads `symbol_<TICKER>.json` / `theme_<theme>.json`; missing file → empty bundle.
  - `get_provider() -> Provider` — selects per config (only meaningful when enabled).

- [ ] **Step 1: Create the fixture files**

`tests/fixtures/enrichment/symbol_CVX.json`:
```json
{
  "ticker": "CVX",
  "rp_entity_id": "D54E62",
  "sentiment": {
    "current": -0.068, "baseline": 0.0,
    "zscore_1mo": -0.9, "zscore_1qt": -1.4,
    "regime": "Neutral", "confidence": "reduced"
  },
  "events": [
    {"category": "earnings-call", "title": "Q2 earnings call", "date": "2026-07-25", "url": null}
  ],
  "evidence": [
    {"headline": "Tengiz incident", "source": "Reuters", "date": "2026-06-18", "url": "https://example.com/a", "sentiment": -0.73}
  ],
  "error": null
}
```

`tests/fixtures/enrichment/theme_gold.json`:
```json
{
  "theme": "gold",
  "docs": [
    {"headline": "Royal Gold investor day", "source": "FT", "date": "2026-06-17", "url": "https://example.com/g", "sentiment": 0.2}
  ],
  "error": null
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_enrichment_providers.py
from pathlib import Path

import pytest

from enrichment import config
from enrichment.models import SymbolBundle, ThematicBundle
from enrichment.providers import (
    FixtureProvider,
    NullProvider,
    get_provider,
)

FIX = str(Path(__file__).parent / "fixtures" / "enrichment")


def test_null_provider_returns_empty_bundles():
    p = NullProvider()
    assert p.name == "null"
    sb = p.symbol_bundle("CVX")
    assert sb == SymbolBundle(ticker="CVX", rp_entity_id=None, sentiment=None)
    tb = p.thematic_bundle("gold")
    assert tb == ThematicBundle(theme="gold")


def test_fixture_provider_loads_symbol():
    p = FixtureProvider(FIX)
    sb = p.symbol_bundle("CVX")
    assert sb.rp_entity_id == "D54E62"
    assert sb.sentiment.regime == "Neutral"
    assert sb.events[0].category == "earnings-call"
    assert sb.evidence[0].sentiment == -0.73


def test_fixture_provider_loads_theme():
    p = FixtureProvider(FIX)
    tb = p.thematic_bundle("gold")
    assert tb.docs[0].source == "FT"


def test_fixture_provider_missing_file_returns_empty():
    p = FixtureProvider(FIX)
    assert p.symbol_bundle("NOPE").sentiment is None
    assert p.thematic_bundle("nonexistent").docs == []


def test_get_provider_defaults_to_null_without_key(monkeypatch):
    monkeypatch.setattr(config, "ENRICHMENT_PROVIDER", "")
    monkeypatch.setattr(config, "BIGDATA_API_KEY", "")
    assert get_provider().name == "null"


def test_get_provider_explicit_fixture(monkeypatch):
    monkeypatch.setattr(config, "ENRICHMENT_PROVIDER", "fixture")
    monkeypatch.setattr(config, "FIXTURE_DIR", FIX)
    p = get_provider()
    assert p.name == "fixture"
    assert p.symbol_bundle("CVX").rp_entity_id == "D54E62"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_enrichment_providers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.providers'`.

- [ ] **Step 4: Create `enrichment/providers.py` (Null + Fixture + selector; Bigdata added in Task 3)**

```python
# enrichment/providers.py
"""Provider seam: vendor-neutral interface + implementations.

NullProvider   — default; empty bundles (flag OFF / no creds).
FixtureProvider — model-level JSON fixtures (tests + MCP-captured interim).
BigdataProvider — production REST client (added in Task 3; flag-gated).

A provider returns fully-assembled, normalised bundle objects — never raw
vendor JSON — so swapping vendors is a one-file change.
"""

import json
from pathlib import Path
from typing import Protocol

from . import config
from .models import (
    SymbolBundle,
    ThematicBundle,
    symbol_from_dict,
    theme_from_dict,
)


class Provider(Protocol):
    name: str

    def symbol_bundle(self, ticker: str) -> SymbolBundle: ...

    def thematic_bundle(self, theme: str) -> ThematicBundle: ...


class NullProvider:
    name = "null"

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        return SymbolBundle(ticker=ticker, rp_entity_id=None, sentiment=None)

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        return ThematicBundle(theme=theme)


class FixtureProvider:
    """Reads model-level JSON from <dir>/symbol_<TICKER>.json and theme_<theme>.json."""

    name = "fixture"

    def __init__(self, fixture_dir: str):
        self._dir = Path(fixture_dir)

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        path = self._dir / f"symbol_{ticker}.json"
        if not path.exists():
            return SymbolBundle(ticker=ticker, rp_entity_id=None, sentiment=None)
        return symbol_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        path = self._dir / f"theme_{theme}.json"
        if not path.exists():
            return ThematicBundle(theme=theme)
        return theme_from_dict(json.loads(path.read_text(encoding="utf-8")))


def get_provider() -> Provider:
    """Select a provider from config. Only meaningful when ENRICHMENT_ENABLED."""
    name = config.ENRICHMENT_PROVIDER or (
        "bigdata" if config.BIGDATA_API_KEY else "null"
    )
    if name == "fixture":
        return FixtureProvider(config.FIXTURE_DIR)
    if name == "bigdata" and config.BIGDATA_API_KEY:
        from .providers_bigdata import BigdataProvider  # added in Task 3

        return BigdataProvider(config.BIGDATA_API_KEY, config.BIGDATA_BASE_URL)
    return NullProvider()
```

Note: `BigdataProvider` is imported lazily from `providers_bigdata` (created in Task 3) so this task is self-contained and the import can't fail before that file exists.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_providers.py -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_providers.py
ruff check enrichment tests/test_enrichment_providers.py
git add enrichment/providers.py tests/test_enrichment_providers.py tests/fixtures/enrichment/
git commit -m "feat(enrichment): provider seam — Null + Fixture providers + selector"
```

---

### Task 3: BigdataProvider — production REST client

> **Before implementing:** confirm the live REST field names/paths. Use Context7 (`resolve-library-id` → `query-docs` for "Bigdata.com" / "RavenPack API") and, if absent there, WebFetch `https://docs.bigdata.com`. We have no REST credentials yet, so this client is unit-tested against a documented-shape fixture and ships flag-gated OFF; the live shape is verified when business-email creds land. The model normalisation (Task 1) is grounded in the MCP trial output, so only the JSON paths below may need adjusting to match docs.

**Files:**
- Create: `enrichment/providers_bigdata.py`
- Create: `tests/fixtures/bigdata_raw/find_securities_AVAV.json`
- Create: `tests/fixtures/bigdata_raw/sentiment_F1EB39.json`
- Create: `tests/fixtures/bigdata_raw/events_F1EB39.json`
- Create: `tests/fixtures/bigdata_raw/search_defence.json`
- Modify: `tests/test_enrichment_providers.py` (append Bigdata parser tests)

**Interfaces:**
- Consumes: `enrichment.models`, `enrichment.config`, `requests`.
- Produces: `BigdataProvider(api_key: str, base_url: str)` — `name = "bigdata"`; implements the `Provider` protocol. Internally caches `rp_entity_id` by ticker. **Every public method catches all exceptions and returns a bundle with `error=<str>`** — it never raises to the caller (degrade-not-crash, mirroring `_dump_raw_batch_result`'s swallow-and-continue).

- [ ] **Step 1: Create documented-shape raw fixtures** (adjust field names to match the confirmed docs; the structure below reflects the MCP trial shapes)

`tests/fixtures/bigdata_raw/find_securities_AVAV.json`:
```json
{"results": [{"rp_entity_id": "F1EB39", "name": "AeroVironment Inc", "ticker": "AVAV", "entity_type": "company"}]}
```

`tests/fixtures/bigdata_raw/sentiment_F1EB39.json`:
```json
{"rp_entity_id": "F1EB39", "current": -0.41, "baseline": -0.05, "zscore_1mo": -1.8, "zscore_1qt": -2.0, "regime": "Negative", "confidence": "reduced"}
```

`tests/fixtures/bigdata_raw/events_F1EB39.json`:
```json
{"events": [
  {"category": "conference-call", "headline": "Investor Day", "date": "2026-07-08", "url": "https://example.com/iday"},
  {"category": "earnings-call", "headline": "Q1 FY27 earnings", "date": "2026-09-03", "url": null}
]}
```

`tests/fixtures/bigdata_raw/search_defence.json`:
```json
{"documents": [
  {"headline": "European defence spending surge", "source_name": "FT", "timestamp": "2026-06-19T08:00:00Z", "url": "https://example.com/def", "sentiment": 0.31}
]}
```

- [ ] **Step 2: Write the failing parser tests (append to `tests/test_enrichment_providers.py`)**

```python
# --- appended to tests/test_enrichment_providers.py ---
import json as _json

from enrichment.providers_bigdata import BigdataProvider

RAW = Path(__file__).parent / "fixtures" / "bigdata_raw"


def _raw(name):
    return _json.loads((RAW / name).read_text(encoding="utf-8"))


def test_bigdata_parse_symbol_bundle(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    monkeypatch.setattr(p, "_find_entity", lambda t: _raw("find_securities_AVAV.json"))
    monkeypatch.setattr(p, "_get_sentiment", lambda eid: _raw("sentiment_F1EB39.json"))
    monkeypatch.setattr(p, "_get_events", lambda eid: _raw("events_F1EB39.json"))

    sb = p.symbol_bundle("AVAV")
    assert sb.rp_entity_id == "F1EB39"
    assert sb.sentiment.regime == "Negative"
    assert sb.sentiment.zscore_1qt == -2.0
    assert {e.category for e in sb.events} == {"conference-call", "earnings-call"}
    assert sb.error is None


def test_bigdata_symbol_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")

    def boom(_):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(p, "_find_entity", boom)
    sb = p.symbol_bundle("AVAV")
    assert sb.sentiment is None
    assert sb.error is not None and "500" in sb.error


def test_bigdata_parse_thematic_bundle(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")
    monkeypatch.setattr(p, "_search", lambda q: _raw("search_defence.json"))
    tb = p.thematic_bundle("defence")
    assert tb.docs[0].source == "FT"
    assert tb.docs[0].date == "2026-06-19"
    assert tb.error is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_enrichment_providers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.providers_bigdata'`.

- [ ] **Step 4: Create `enrichment/providers_bigdata.py`**

```python
# enrichment/providers_bigdata.py
"""Bigdata.com (RavenPack) REST client — production provider.

Flag-gated OFF in production until business-email REST creds land; unit-tested
against documented-shape fixtures. Every public method degrades to an
error-tagged bundle instead of raising, so a Bigdata outage can never break the
brief. JSON paths below must match docs.bigdata.com (confirm via Context7)."""

import requests

from . import config
from .models import (
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)


def _iso_date(ts: str | None) -> str:
    """Trim an ISO timestamp to its date; pass through a bare date; '' for None."""
    if not ts:
        return ""
    return ts[:10]


class BigdataProvider:
    name = "bigdata"

    def __init__(self, api_key: str, base_url: str):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._entity_cache: dict[str, str | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

    # --- HTTP helpers (one network call each; named so tests can monkeypatch) ---
    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base}{path}",
            json=payload,
            timeout=config.ENRICHMENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _find_entity(self, ticker: str) -> dict:
        return self._post("/securities/search", {"query": ticker})

    def _get_sentiment(self, rp_entity_id: str) -> dict:
        return self._post("/sentiment/tearsheet", {"rp_entity_id": rp_entity_id})

    def _get_events(self, rp_entity_id: str) -> dict:
        return self._post(
            "/events/calendar",
            {"rp_entity_id": rp_entity_id, "categories": ["earnings-call", "conference-call"]},
        )

    def _search(self, query: str) -> dict:
        return self._post("/search", {"query": query, "mode": "smart"})

    # --- resolution (cached per design: never re-resolve a known entity) ---
    def _resolve(self, ticker: str) -> str | None:
        if ticker in self._entity_cache:
            return self._entity_cache[ticker]
        data = self._find_entity(ticker)
        results = data.get("results") or []
        eid = results[0].get("rp_entity_id") if results else None
        self._entity_cache[ticker] = eid
        return eid

    # --- public Provider interface ---
    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(
                    ticker=ticker, rp_entity_id=None, sentiment=None,
                    error="no entity match",
                )
            s = self._get_sentiment(eid)
            sentiment = SentimentScore(
                current=s.get("current"),
                baseline=s.get("baseline"),
                zscore_1mo=s.get("zscore_1mo"),
                zscore_1qt=s.get("zscore_1qt"),
                regime=s.get("regime", "Unknown"),
                confidence=s.get("confidence"),
            )
            events = [
                Event(
                    category=e.get("category", ""),
                    title=e.get("headline", ""),
                    date=_iso_date(e.get("date")),
                    url=e.get("url"),
                )
                for e in self._get_events(eid).get("events", [])
            ]
            return SymbolBundle(
                ticker=ticker, rp_entity_id=eid, sentiment=sentiment, events=events
            )
        except Exception as e:  # degrade, never crash the brief
            return SymbolBundle(
                ticker=ticker, rp_entity_id=None, sentiment=None, error=str(e)
            )

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            docs = [
                EvidenceDoc(
                    headline=d.get("headline", ""),
                    source=d.get("source_name", ""),
                    date=_iso_date(d.get("timestamp")),
                    url=d.get("url"),
                    sentiment=d.get("sentiment"),
                )
                for d in self._search(theme).get("documents", [])
            ]
            return ThematicBundle(theme=theme, docs=docs)
        except Exception as e:
            return ThematicBundle(theme=theme, error=str(e))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_enrichment_providers.py -q`
Expected: PASS (9 passed).

- [ ] **Step 6: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_providers.py
ruff check enrichment tests/test_enrichment_providers.py
git add enrichment/providers_bigdata.py tests/test_enrichment_providers.py tests/fixtures/bigdata_raw/
git commit -m "feat(enrichment): Bigdata.com REST provider (flag-gated, degrade-not-crash)"
```

---

### Task 4: Universe builder

**Files:**
- Create: `enrichment/universe.py`
- Test: `tests/test_enrichment_universe.py`

**Interfaces:**
- Consumes: nothing from other enrichment modules (pure functions over plain dicts/lists).
- Produces:
  - `Universe` frozen dataclass: `tickers: list[str]` (base symbols, deduped, order-stable), `themes: list[str]`.
  - `normalize_ticker(raw: str) -> str` — uppercases; strips a `_..._EQ`/`_EQ` venue suffix; strips a single trailing lowercase LSE/Xetra marker (`l`/`d`) after an uppercase base. (Memory: `stooq-ticker-resolution`.)
  - `latest_signal_tickers(signals_dir: Path) -> list[str]` — tickers from the newest `signals-*.json` snapshot.
  - `build_universe(book: dict, watchlist: dict, signal_tickers: list[str], pins: list[str]) -> Universe`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrichment_universe.py
import json
from pathlib import Path

from enrichment.universe import (
    Universe,
    build_universe,
    latest_signal_tickers,
    normalize_ticker,
)


def test_normalize_ticker_strips_venue_suffix():
    assert normalize_ticker("SHELl_EQ") == "SHEL"
    assert normalize_ticker("AVAV_US_EQ") == "AVAV"


def test_normalize_ticker_strips_lse_marker():
    assert normalize_ticker("SHELl") == "SHEL"
    assert normalize_ticker("DXJGl") == "DXJG"


def test_normalize_ticker_leaves_plain_symbol():
    assert normalize_ticker("AVAV") == "AVAV"
    assert normalize_ticker("CVX") == "CVX"


def test_build_universe_dedups_and_routes_etf_to_theme():
    book = {"positions": [
        {"status": "open", "ticker": "CVX", "asset_class": "equity"},
        {"status": "closed", "ticker": "MU", "asset_class": "equity"},
    ]}
    watchlist = {"items": [
        {"raw": "AVAV", "asset_class": "equity", "instrument": "AVAV_US_EQ"},
        {"raw": "SGLN", "asset_class": "equity", "instrument": "SGLNl_EQ"},  # gold ETF
        {"raw": "CVX", "asset_class": "equity", "instrument": "CVX_US_EQ"},  # dup of book
    ]}
    signal_tickers = ["MU", "RGLD"]
    pins = ["ukraine", "iran"]

    u = build_universe(book, watchlist, signal_tickers, pins)
    # open equity positions + watchlist stocks + signal tickers, ETFs excluded, deduped
    assert u.tickers == ["CVX", "AVAV", "MU", "RGLD"]
    # pins + ETF-derived theme (SGLN -> gold), order: pins first then ETF themes
    assert "ukraine" in u.themes and "iran" in u.themes and "gold" in u.themes


def test_latest_signal_tickers_reads_newest_snapshot(tmp_path):
    older = {"signals": [{"ticker": "OLD"}]}
    newer = {"signals": [{"ticker": "CVX"}, {"ticker": None}, {"ticker": "MU"}]}
    (tmp_path / "signals-2026-06-18.json").write_text(json.dumps(older))
    (tmp_path / "signals-2026-06-19.json").write_text(json.dumps(newer))
    assert latest_signal_tickers(Path(tmp_path)) == ["CVX", "MU"]


def test_latest_signal_tickers_empty_when_no_snapshots(tmp_path):
    assert latest_signal_tickers(Path(tmp_path)) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_enrichment_universe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.universe'`.

- [ ] **Step 3: Create `enrichment/universe.py`**

```python
# enrichment/universe.py
"""Derive the per-run enrichment query set from existing artifacts.

Reads book.json positions, watchlist.json items, the latest signals snapshot and
feedback pins — no new coupling. Single stocks -> per-symbol bundles; known ETFs
-> a thematic search (ETFs resolve as funds, so company sentiment is meaningless);
pins -> themes.

v1 SCOPE NOTE: the design spec also listed "the brief's Top Stories" as a theme
source. Those don't exist at submit time (they ARE the batch output), so
Top-Stories-driven thematic enrichment is deferred to a possible collect-time
fast-follow. v1 themes = pins + ETF-watchlist themes."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Known ETF base symbols from the live watchlist -> theme string. ETFs resolve as
# funds (no company sentiment), so they feed a thematic search instead of a
# per-symbol bundle. (Memory: bigdata-evaluation-and-trading-split.)
_ETF_THEME_MAP = {
    "DXJG": "Japan equities and yen",
    "DXJ": "Japan equities and yen",
    "EXV1": "European banks",
    "KSTR": "China STAR / China tech",
    "ARMG": "defence",
    "SGLN": "gold",
}

_VENUE_SUFFIX_RE = re.compile(r"_(?:[A-Z]{2}_)?EQ$")  # _US_EQ, _EQ
_LSE_MARKER_RE = re.compile(r"^([A-Z0-9]+?)[ld]$")  # trailing l/d after uppercase base


@dataclass(frozen=True)
class Universe:
    tickers: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)


def normalize_ticker(raw: str) -> str:
    """Reduce a raw/instrument token to the base symbol Bigdata.com resolves on."""
    t = (raw or "").strip().upper()
    t = _VENUE_SUFFIX_RE.sub("", t)
    # Re-apply marker strip on the case-preserving original tail: a single trailing
    # lowercase l/d is an LSE/Xetra marker; uppercase L/D is part of the symbol.
    tail = (raw or "").strip()
    tail = re.sub(r"_(?:[A-Za-z]{2}_)?[Ee][Qq]$", "", tail)
    if tail and tail[-1] in ("l", "d") and tail[:-1].isupper():
        t = tail[:-1].upper()
    return t


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def latest_signal_tickers(signals_dir: Path) -> list[str]:
    snaps = sorted(Path(signals_dir).glob("signals-*.json"))
    if not snaps:
        return []
    try:
        data = json.loads(snaps[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _dedup(
        normalize_ticker(s["ticker"])
        for s in data.get("signals", [])
        if s.get("ticker")
    )


def _pos_ticker(p: dict) -> str:
    return p.get("ticker") or p.get("instrument", "")


def build_universe(
    book: dict,
    watchlist: dict,
    signal_tickers: list[str],
    pins: list[str],
) -> Universe:
    raw_symbols: list[str] = []
    etf_themes: list[str] = []

    for p in book.get("positions", []):
        if p.get("status") == "open" and p.get("asset_class") == "equity":
            raw_symbols.append(_pos_ticker(p))

    for it in watchlist.get("items", []):
        if it.get("asset_class") != "equity":
            continue
        raw_symbols.append(it.get("raw") or it.get("instrument", ""))

    raw_symbols.extend(signal_tickers)

    tickers: list[str] = []
    for raw in raw_symbols:
        base = normalize_ticker(raw)
        if not base:
            continue
        if base in _ETF_THEME_MAP:
            etf_themes.append(_ETF_THEME_MAP[base])
        else:
            tickers.append(base)

    themes = _dedup([*pins, *etf_themes])
    return Universe(tickers=_dedup(tickers), themes=themes)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_universe.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_universe.py
ruff check enrichment tests/test_enrichment_universe.py
git add enrichment/universe.py tests/test_enrichment_universe.py
git commit -m "feat(enrichment): universe builder (dedup, ETF->theme routing, marker strip)"
```

---

### Task 5: build_enrichment orchestration (flag gate + bounded fan-out)

**Files:**
- Create: `enrichment/build.py`
- Modify: `enrichment/__init__.py` (export `build_enrichment`, `build_universe`, `Universe`)
- Test: `tests/test_enrichment_build.py`

**Interfaces:**
- Consumes: `enrichment.config`, `enrichment.models`, `enrichment.providers` (`get_provider`, `Provider`), `enrichment.universe.Universe`.
- Produces: `build_enrichment(universe: Universe, *, as_of: str, provider: Provider | None = None) -> EnrichmentBundles`.
  - Flag OFF → returns `EnrichmentBundles(as_of=as_of, provider="null")` (empty; no provider calls).
  - Flag ON → uses `provider` or `get_provider()`; fans out over `universe.tickers[:ENRICHMENT_MAX_SYMBOLS]` and `universe.themes[:ENRICHMENT_MAX_THEMES]`; logs counts + dropped-over-cap.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrichment_build.py
from enrichment import config
from enrichment.build import build_enrichment
from enrichment.models import SymbolBundle, ThematicBundle
from enrichment.universe import Universe

AS_OF = "2026-06-20T20:00:00+00:00"


class _StubProvider:
    name = "stub"

    def symbol_bundle(self, ticker):
        return SymbolBundle(ticker=ticker, rp_entity_id="X", sentiment=None)

    def thematic_bundle(self, theme):
        return ThematicBundle(theme=theme)


def test_build_returns_empty_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "ENRICHMENT_ENABLED", False)
    u = Universe(tickers=["CVX"], themes=["gold"])
    out = build_enrichment(u, as_of=AS_OF, provider=_StubProvider())
    assert out.is_empty()
    assert out.provider == "null"


def test_build_fans_out_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "ENRICHMENT_ENABLED", True)
    u = Universe(tickers=["CVX", "MU"], themes=["gold"])
    out = build_enrichment(u, as_of=AS_OF, provider=_StubProvider())
    assert [s.ticker for s in out.symbols] == ["CVX", "MU"]
    assert [t.theme for t in out.themes] == ["gold"]
    assert out.provider == "stub"


def test_build_respects_caps(monkeypatch):
    monkeypatch.setattr(config, "ENRICHMENT_ENABLED", True)
    monkeypatch.setattr(config, "ENRICHMENT_MAX_SYMBOLS", 1)
    monkeypatch.setattr(config, "ENRICHMENT_MAX_THEMES", 0)
    u = Universe(tickers=["CVX", "MU"], themes=["gold"])
    out = build_enrichment(u, as_of=AS_OF, provider=_StubProvider())
    assert [s.ticker for s in out.symbols] == ["CVX"]
    assert out.themes == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_enrichment_build.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.build'`.

- [ ] **Step 3: Create `enrichment/build.py`**

```python
# enrichment/build.py
"""Assemble EnrichmentBundles for one run: flag gate + bounded fan-out."""

from common import log

from . import config
from .models import EnrichmentBundles
from .providers import Provider, get_provider
from .universe import Universe


def build_enrichment(
    universe: Universe,
    *,
    as_of: str,
    provider: Provider | None = None,
) -> EnrichmentBundles:
    if not config.ENRICHMENT_ENABLED:
        return EnrichmentBundles(as_of=as_of, provider="null")

    provider = provider or get_provider()

    tickers = universe.tickers[: config.ENRICHMENT_MAX_SYMBOLS]
    themes = universe.themes[: config.ENRICHMENT_MAX_THEMES]
    dropped_sym = len(universe.tickers) - len(tickers)
    dropped_thm = len(universe.themes) - len(themes)
    if dropped_sym or dropped_thm:
        log.warning(
            "Enrichment fan-out capped: dropped %d symbol(s), %d theme(s) over limit",
            dropped_sym,
            dropped_thm,
        )

    symbols = [provider.symbol_bundle(t) for t in tickers]
    theme_bundles = [provider.thematic_bundle(th) for th in themes]
    errs = sum(1 for s in symbols if s.error) + sum(1 for t in theme_bundles if t.error)
    log.info(
        "Enrichment built: provider=%s symbols=%d themes=%d errors=%d",
        provider.name,
        len(symbols),
        len(theme_bundles),
        errs,
    )
    return EnrichmentBundles(
        as_of=as_of, symbols=symbols, themes=theme_bundles, provider=provider.name
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_build.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Update `enrichment/__init__.py` re-exports**

Replace the `__init__.py` body created in Task 1 with:

```python
# enrichment/__init__.py
"""Bigdata.com enrichment subsystem.

Flag-gated (OFF by default). Feeds entity-resolved sentiment, events and
evidence into the daily brief as READ-ONLY context behind a vendor-neutral
provider seam. The bundle types here are the future digest<->trading seam.
"""

from .build import build_enrichment
from .config import ENRICHMENT_ENABLED as _ENABLED
from .models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)
from .universe import Universe, build_universe, latest_signal_tickers


def is_enabled() -> bool:
    return _ENABLED


__all__ = [
    "is_enabled",
    "build_enrichment",
    "build_universe",
    "latest_signal_tickers",
    "Universe",
    "EnrichmentBundles",
    "EvidenceDoc",
    "Event",
    "SentimentScore",
    "SymbolBundle",
    "ThematicBundle",
]
```

- [ ] **Step 6: Run the full enrichment test set to verify nothing regressed**

Run: `python -m pytest tests/test_enrichment_models.py tests/test_enrichment_providers.py tests/test_enrichment_universe.py tests/test_enrichment_build.py -q`
Expected: PASS (all).

- [ ] **Step 7: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_build.py
ruff check enrichment tests/test_enrichment_build.py
git add enrichment/build.py enrichment/__init__.py tests/test_enrichment_build.py
git commit -m "feat(enrichment): build_enrichment orchestration + public API exports"
```

---

### Task 6: Render prompt block + signal annotation

**Files:**
- Create: `enrichment/render.py`
- Modify: `enrichment/__init__.py` (export `render_prompt_block`, `annotate_signals`)
- Test: `tests/test_enrichment_render.py`

**Interfaces:**
- Consumes: `enrichment.models`.
- Produces:
  - `render_prompt_block(bundles: EnrichmentBundles) -> str` — empty string when `bundles.is_empty()`; otherwise a delimited, read-only context section that includes the literal interpretive caveat and the `Bigdata.com` branding.
  - `annotate_signals(signals: list[dict], bundles: EnrichmentBundles) -> list[dict]` — returns NEW signal dicts, each with a descriptive `bigdata_sentiment` key when a symbol bundle matches by base ticker; never mutates inputs; never touches any sizing field.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrichment_render.py
from enrichment.models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)
from enrichment.render import annotate_signals, render_prompt_block

AS_OF = "2026-06-20T20:00:00+00:00"


def _bundles():
    return EnrichmentBundles(
        as_of=AS_OF,
        provider="fixture",
        symbols=[
            SymbolBundle(
                ticker="AVAV",
                rp_entity_id="F1EB39",
                sentiment=SentimentScore(-0.41, -0.05, -1.8, -2.0, "Negative", "reduced"),
                events=[Event("conference-call", "Investor Day", "2026-07-08")],
                evidence=[EvidenceDoc("Class action", "Reuters", "2026-06-15", None, -0.6)],
            )
        ],
        themes=[ThematicBundle(theme="gold", docs=[EvidenceDoc("Gold up", "FT", "2026-06-19")])],
    )


def test_render_empty_when_no_data():
    assert render_prompt_block(EnrichmentBundles(as_of=AS_OF)) == ""


def test_render_contains_caveat_branding_and_data():
    out = render_prompt_block(_bundles())
    assert "Bigdata.com" in out
    assert "media tone" in out  # the interpretive caveat
    assert "never" in out.lower() and "trigger" in out.lower()
    assert "AVAV" in out and "Negative" in out
    assert "Investor Day" in out
    assert "gold" in out


def test_annotate_signals_attaches_descriptive_field():
    signals = [
        {"ticker": "AVAV", "topic": "defence", "direction": "bearish", "confidence": "medium"},
        {"ticker": "MU", "topic": "memory", "direction": "bullish", "confidence": "high"},
        {"ticker": None, "topic": "macro", "direction": "neutral", "confidence": "low"},
    ]
    out = annotate_signals(signals, _bundles())
    assert out[0]["bigdata_sentiment"]["regime"] == "Negative"
    assert out[0]["bigdata_sentiment"]["current"] == -0.41
    assert "bigdata_sentiment" not in out[1]  # no bundle for MU
    assert "bigdata_sentiment" not in out[2]  # null ticker
    # inputs not mutated
    assert "bigdata_sentiment" not in signals[0]


def test_annotate_signals_no_op_when_empty():
    signals = [{"ticker": "AVAV", "topic": "x", "direction": "bearish", "confidence": "low"}]
    out = annotate_signals(signals, EnrichmentBundles(as_of=AS_OF))
    assert out == signals
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_enrichment_render.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrichment.render'`.

- [ ] **Step 3: Create `enrichment/render.py`**

```python
# enrichment/render.py
"""Serialise enrichment bundles into (a) a read-only prompt section and (b) a
descriptive signal annotation. Neither path may influence position sizing."""

from .models import EnrichmentBundles
from .universe import normalize_ticker

_CAVEAT = (
    "Bigdata.com sentiment is MEDIA TONE about the company, NOT a price or "
    "direction forecast. Treat it as an orthogonal tone overlay and context for "
    "your own reading — never as a trade trigger. Source: https://bigdata.com"
)


def _fmt_sentiment(s) -> str:
    if s is None:
        return "n/a"
    conf = f", confidence={s.confidence}" if s.confidence else ""
    return (
        f"current={s.current} baseline={s.baseline} "
        f"z1mo={s.zscore_1mo} z1qt={s.zscore_1qt} regime={s.regime}{conf}"
    )


def render_prompt_block(bundles: EnrichmentBundles) -> str:
    if bundles.is_empty():
        return ""
    lines: list[str] = [
        "## BIGDATA.COM ENRICHMENT (read-only context — NEVER a trade trigger)",
        _CAVEAT,
        "",
    ]
    if bundles.symbols:
        lines.append("### Per-symbol sentiment & events")
        for s in bundles.symbols:
            if s.error:
                lines.append(f"- {s.ticker}: (unavailable: {s.error})")
                continue
            lines.append(f"- {s.ticker} [{s.rp_entity_id}]: {_fmt_sentiment(s.sentiment)}")
            for e in s.events:
                lines.append(f"    • event [{e.category}] {e.date}: {e.title}")
            for d in s.evidence:
                lines.append(
                    f"    • evidence {d.date} {d.source}: {d.headline}"
                    + (f" (sent {d.sentiment})" if d.sentiment is not None else "")
                )
    if bundles.themes:
        lines.append("### Thematic coverage")
        for t in bundles.themes:
            if t.error:
                lines.append(f"- {t.theme}: (unavailable: {t.error})")
                continue
            lines.append(f"- {t.theme}:")
            for d in t.docs:
                lines.append(f"    • {d.date} {d.source}: {d.headline}")
    return "\n".join(lines)


def annotate_signals(signals: list[dict], bundles: EnrichmentBundles) -> list[dict]:
    """Attach a DESCRIPTIVE bigdata_sentiment dict to signals whose base ticker
    matches a symbol bundle. Read-only/informational — explicitly distinct from
    any sizing input. Returns new dicts; never mutates the inputs."""
    if bundles.is_empty():
        return signals
    by_ticker = {
        s.ticker: s for s in bundles.symbols if s.sentiment is not None and not s.error
    }
    out = []
    for sig in signals:
        tkr = sig.get("ticker")
        bundle = by_ticker.get(normalize_ticker(tkr)) if tkr else None
        if bundle is None:
            out.append(sig)
            continue
        s = bundle.sentiment
        out.append(
            {
                **sig,
                "bigdata_sentiment": {
                    "current": s.current,
                    "regime": s.regime,
                    "zscore_1mo": s.zscore_1mo,
                    "rp_entity_id": bundle.rp_entity_id,
                },
            }
        )
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_render.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Add the two exports to `enrichment/__init__.py`**

Add `from .render import annotate_signals, render_prompt_block` to the imports, and add `"render_prompt_block"` and `"annotate_signals"` to `__all__`.

- [ ] **Step 6: Lint, format, commit**

```bash
ruff format enrichment tests/test_enrichment_render.py
ruff check enrichment tests/test_enrichment_render.py
git add enrichment/render.py enrichment/__init__.py tests/test_enrichment_render.py
git commit -m "feat(enrichment): render read-only prompt block + descriptive signal annotation"
```

---

### Task 7: Wire enrichment into `brief.py` (submit injection + persistence + collect annotation)

**Files:**
- Modify: `brief.py` — imports (after line 83); `build_daily_prompt` signature + body (1436–1577); `mode_submit` (2004–2065); `mode_collect` (2080–2089).
- Test: `tests/test_enrichment_wiring.py`

**Interfaces:**
- Consumes: `enrichment.build_enrichment`, `enrichment.build_universe`, `enrichment.latest_signal_tickers`, `enrichment.render_prompt_block`, `enrichment.annotate_signals`; `trading.load_book`, `trading.load_watchlist`; `resolved_pins`.
- Produces: `build_daily_prompt(..., enrichment_block: str = "")` — the block is rendered into the prompt after the portfolio section, unchanged when empty.

- [ ] **Step 1: Write the failing test for the prompt param**

```python
# tests/test_enrichment_wiring.py
import brief


def _kwargs(**over):
    base = dict(
        feed_content="(feeds)",
        web_content="(web)",
        chroma_context="(chroma)",
        yesterday_brief="",
        weekly_summary="",
        fb={},
        portfolio="",
    )
    base.update(over)
    return base


def test_build_daily_prompt_includes_enrichment_block():
    block = "## BIGDATA.COM ENRICHMENT (read-only context — NEVER a trade trigger)\nfoo"
    prompt = brief.build_daily_prompt(**_kwargs(), enrichment_block=block)
    assert "BIGDATA.COM ENRICHMENT" in prompt
    assert "NEVER a trade trigger" in prompt


def test_build_daily_prompt_omits_block_when_empty():
    prompt = brief.build_daily_prompt(**_kwargs(), enrichment_block="")
    assert "BIGDATA.COM ENRICHMENT" not in prompt
    # spine intact
    assert "@@@SIGNALS@@@" in prompt
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_enrichment_wiring.py -q`
Expected: FAIL — `build_daily_prompt() got an unexpected keyword argument 'enrichment_block'`.

- [ ] **Step 3: Add the `enrichment_block` parameter to `build_daily_prompt`**

In `brief.py`, change the signature (line 1436–1445) to add the new keyword after `market_block`:

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
    enrichment_block: str = "",
) -> str:
```

Then, immediately after the `portfolio_block = ...` assignment (ends at line 1496) and before `market_section = ...`, add:

```python
    enrichment_section = f"\n{enrichment_block}\n" if enrichment_block else ""
```

Finally, in the returned f-string, insert `{enrichment_section}` on its own line right after `{portfolio_block}` (currently line 1524, the line reading `{yesterday_block}{weekly_block}{portfolio_block}`):

```python
{yesterday_block}{weekly_block}{portfolio_block}{enrichment_section}
{perf_block}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_enrichment_wiring.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the enrichment imports to `brief.py`**

After the `from validation import (...)` block (ends line 83), add:

```python
from enrichment import (
    annotate_signals,
    build_enrichment,
    build_universe,
    is_enabled as enrichment_enabled,
    latest_signal_tickers,
    render_prompt_block,
)
```

- [ ] **Step 6: Assemble + persist + inject enrichment in `mode_submit`**

In `mode_submit`, immediately before the `prompt = build_daily_prompt(` call (line 2047), add:

```python
    enrichment_block = ""
    try:
        universe = build_universe(
            load_book(),
            load_watchlist(),
            latest_signal_tickers(SIGNALS_DIR),
            resolved_pins(fb),
        )
        bundles = build_enrichment(
            universe, as_of=datetime.now(timezone.utc).isoformat()
        )
        if not bundles.is_empty():
            _write_json_atomic(
                DATA_DIR / "enrichment" / f"enrichment-{today}.json", bundles.to_dict()
            )
            enrichment_block = render_prompt_block(bundles)
        log.info(
            "Enrichment: enabled=%s symbols=%d themes=%d block=%dch",
            enrichment_enabled(),
            len(bundles.symbols),
            len(bundles.themes),
            len(enrichment_block),
        )
    except Exception as e:
        log.error(f"Enrichment skipped (brief unaffected): {e}")
```

Then pass it into the call by adding `enrichment_block,` as the final positional argument:

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
        enrichment_block,
    )
```

- [ ] **Step 7: Annotate saved signals in `mode_collect`**

In `mode_collect`, between `signals, dropped = normalize_signals(raw_signals)` (line 2081) and the `deliver(...)` call, add a load-and-annotate of today's persisted bundle:

```python
        try:
            enr_path = DATA_DIR / "enrichment" / f"enrichment-{today}.json"
            if enr_path.exists():
                raw = json.loads(enr_path.read_text(encoding="utf-8"))
                signals = annotate_signals(signals, bundles_from_dict(raw))
        except Exception as e:
            log.error(f"Signal annotation skipped (signals unaffected): {e}")
```

Add `bundles_from_dict` to the enrichment import block from Step 5 — change the import to `from enrichment.models import bundles_from_dict` as a separate line (it lives in `enrichment.models`, not the package root).

- [ ] **Step 8: Run the full suite + an import smoke check**

Run: `python -c "import brief"` (Expected: no output, no error)
Run: `python -m pytest -q`
Expected: PASS (full suite, including the new enrichment tests; existing 240 tests unchanged).

- [ ] **Step 9: Lint, format, commit**

```bash
ruff format brief.py tests/test_enrichment_wiring.py
ruff check brief.py common.py trading.py enrichment tests
git add brief.py tests/test_enrichment_wiring.py
git commit -m "feat(enrichment): wire read-only enrichment into submit prompt + collect signals"
```

---

### Task 8: Deploy allowlist — Dockerfile COPY + CI workflow paths/lint

> Memory `dockerfile-copy-allowlist`: a new first-party module that isn't COPYed into the image fails at runtime with `ModuleNotFoundError` even though CI (full checkout) passes. This task closes that gap.

**Files:**
- Modify: `Dockerfile:13`
- Modify: `.github/workflows/docker-publish.yml` (paths block lines 6–15; lint commands lines 46–47)

**Interfaces:** none (deployment config only).

- [ ] **Step 1: Add the package COPY to the Dockerfile**

Change `Dockerfile` line 13 from:

```dockerfile
COPY common.py trading.py validation.py brief.py .
```

to:

```dockerfile
COPY common.py trading.py validation.py brief.py .
COPY enrichment/ ./enrichment/
```

- [ ] **Step 2: Add `enrichment/**` to the CI trigger paths**

In `.github/workflows/docker-publish.yml`, add to the `paths:` list (after the `'trading.py'` / `'validation.py'` entries, lines 8–10):

```yaml
      - 'enrichment/**'
```

- [ ] **Step 3: Add `enrichment` to the lint commands**

Change the `Lint` step (lines 45–47) from:

```yaml
          ruff check brief.py common.py trading.py tests
          ruff format --check brief.py common.py trading.py tests
```

to:

```yaml
          ruff check brief.py common.py trading.py enrichment tests
          ruff format --check brief.py common.py trading.py enrichment tests
```

- [ ] **Step 4: Verify the local pre-push gate passes exactly as CI will run it**

Run: `ruff check brief.py common.py trading.py enrichment tests`
Run: `ruff format --check brief.py common.py trading.py enrichment tests`
Run: `python -m pytest -q`
Expected: all clean / PASS.

- [ ] **Step 5: Build the image locally to confirm the COPY resolves (optional but recommended)**

Run: `docker build -t newsbrief-local .` then `docker run --rm --entrypoint python newsbrief-local -c "import enrichment; print('ok')"`
Expected: prints `ok` (the package is present in the image).

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .github/workflows/docker-publish.yml
git commit -m "build(enrichment): add package to Docker COPY allowlist + CI paths/lint"
```

---

## Post-implementation: capture live MCP fixtures (interim production path)

Not a code task — an operator step the agent can do in-session. With the Bigdata.com MCP connector connected, capture real `FixtureProvider` data so the enabled path can be validated without REST creds:

1. For each universe symbol, call `mcp__claude_ai_Bigdata_com__find_securities`, `bigdata_sentiment_tearsheet`, `bigdata_events_calendar` (categories: earnings-call AND conference-call). Re-use the cached `rp_entity_id`s (CVX=D54E62, MU=49BBBC, RGLD=263216, ESLT=0401A0, AVAV=F1EB39).
2. Normalise each into the `symbol_<TICKER>.json` / `theme_<theme>.json` shape (Task 2) and drop into a fixtures dir.
3. Run with `ENRICHMENT_ENABLED=1 ENRICHMENT_PROVIDER=fixture ENRICHMENT_FIXTURE_DIR=<dir>` to validate the rendered prompt block end-to-end against real data.

This is how the flag-on path is exercised until the business-email REST key lands; flipping to `ENRICHMENT_PROVIDER=bigdata` + `BIGDATA_API_KEY` is then the only production change.

## Self-Review

**Spec coverage:**
- Shared access layer (REST client + key, flag default OFF, fixtures, graceful-degrade) → Tasks 1–3, 5. ✓
- Provider-adapter seam (the alternatives-research outcome) → Task 2 (`Provider` protocol, Null/Fixture/Bigdata). ✓
- Feature A enrichment module + per-symbol & thematic bundles → Tasks 4–6. ✓
- Inputs = book ∪ watchlist ∪ latest signals; themes = pins ∪ ETF themes → Task 4. **Deviation from spec:** "brief's Top Stories" as a theme source is deferred (chicken-and-egg at submit time) — documented in `universe.py` and flagged to the user. ✓ (with noted deviation)
- Ticker resolution + marker strip + entity-id cache → Task 4 (`normalize_ticker`), Task 3 (`_entity_cache`). ✓
- Read-only context, never auto-sizing; descriptive `bigdata_sentiment` annotation distinct from sizing → Task 6 (`render_prompt_block`, `annotate_signals`). ✓
- Bundles as the digest↔trading seam (plain dataclasses) → Task 1 (frozen dataclasses, `to_dict`). ✓
- Degradation (flag OFF / unreachable → empty → pipeline unchanged) → Task 5 gate, Task 3 try/except, Task 7 try/except. ✓
- Per-run usage/error logging + bounded fan-out ceiling → Task 5 (`log.info`/`log.warning`, caps). ✓
- Tests against recorded fixtures, no live calls in CI → all tasks (Fixture/raw fixtures); MCP capture is a post-step. ✓
- Dockerfile COPY + CI paths → Task 8. ✓
- Feature B backtest → **out of scope for this plan** (separate following plan), per agreement. ✓

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" — every code/test step shows full content. The only deferred items are explicitly scoped (Top Stories theme; Feature B) and the one "confirm field names via Context7/docs" note in Task 3, which is a verification action against a concrete documented-shape fixture, not a placeholder.

**Type consistency:** `Provider` protocol methods (`symbol_bundle`, `thematic_bundle`) are identical across `NullProvider`/`FixtureProvider`/`BigdataProvider`/`_StubProvider`. `SymbolBundle`/`ThematicBundle`/`SentimentScore`/`Event`/`EvidenceDoc`/`EnrichmentBundles` field names match between `models.py`, the providers' constructors, `_symbol_from_dict`/`_theme_from_dict`, `render.py`, and all fixtures. `build_enrichment(universe, *, as_of, provider=None)` and `build_universe(book, watchlist, signal_tickers, pins)` signatures match their call sites in Task 7. `normalize_ticker` is the single shared ticker-normaliser used by both `universe.py` and `render.annotate_signals`.

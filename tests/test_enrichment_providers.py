# tests/test_enrichment_providers.py
import json as _json
import logging
from pathlib import Path

from enrichment import config
from enrichment.models import SymbolBundle, ThematicBundle
from enrichment.providers import (
    FixtureProvider,
    NullProvider,
    get_provider,
)
from enrichment.providers_bigdata import (
    BigdataProvider,
    _events_window,
    _sentiment_window,
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
    assert sb.sentiment.daily_sentiment == -0.068
    assert sb.sentiment.trend_delta == -0.018
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


def test_get_provider_fixture_without_dir_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(config, "ENRICHMENT_PROVIDER", "fixture")
    monkeypatch.setattr(config, "FIXTURE_DIR", "")
    with caplog.at_level("WARNING"):
        provider = get_provider()
    assert provider.name == "null"
    assert "ENRICHMENT_FIXTURE_DIR is unset" in caplog.text


def test_get_provider_bigdata_missing_key_warns_and_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(config, "ENRICHMENT_PROVIDER", "bigdata")
    monkeypatch.setattr(config, "BIGDATA_API_KEY", "")
    with caplog.at_level(logging.WARNING):
        provider = get_provider()
    assert provider.name == "null"
    assert "BIGDATA_API_KEY is unset" in caplog.text


RAW = Path(__file__).parent / "fixtures" / "bigdata_raw"


def _raw(name):
    return _json.loads((RAW / name).read_text(encoding="utf-8"))


def test_bigdata_sets_x_api_key_header_not_bearer():
    p = BigdataProvider("secret", "https://api.bigdata.com")
    assert p._session.headers.get("X-API-KEY") == "secret"
    assert "Authorization" not in p._session.headers


def test_get_sentiment_builds_correct_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": []}
    p._get_sentiment("D8442A", "2024-01-01", "2024-03-01")
    assert calls == [
        (
            "/v1/entity-sentiment/",
            {
                "identifier": {"type": "rp_entity_id", "value": "D8442A"},
                "timestamp": {"start": "2024-01-01", "end": "2024-03-01"},
            },
        )
    ]


def test_get_events_builds_flat_request():
    p = BigdataProvider("k", "https://api.bigdata.com")
    calls = []
    p._post = lambda path, payload: calls.append((path, payload)) or {"results": {}}
    p._get_events("D8442A", "2026-07-19", "2026-10-17")
    assert calls[0][0] == "/v1/events-calendar/query"
    assert calls[0][1] == {
        "rp_entity_id": ["D8442A"],
        "start_date": "2026-07-19",
        "end_date": "2026-10-17",
        "categories": ["earnings-call", "conference-call"],
        "limit": 100,
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


def test_bigdata_symbol_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")

    def boom(_):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(p, "_find_entity", boom)
    sb = p.symbol_bundle("AVAV")
    assert sb.sentiment is None and sb.error and "500" in sb.error


def test_bigdata_thematic_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")

    def boom(_):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(p, "_search", boom)
    tb = p.thematic_bundle("defence")
    assert tb.docs == []
    assert tb.error is not None and "500" in tb.error

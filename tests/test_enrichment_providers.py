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
from enrichment.providers_bigdata import BigdataProvider

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


def test_bigdata_thematic_degrades_on_error(monkeypatch):
    p = BigdataProvider("k", "https://api.bigdata.com")

    def boom(_):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(p, "_search", boom)
    tb = p.thematic_bundle("defence")
    assert tb.docs == []
    assert tb.error is not None and "500" in tb.error

# tests/test_enrichment_providers.py
from pathlib import Path

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

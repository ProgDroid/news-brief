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


def test_build_skips_themes_when_flag_off(monkeypatch):
    from enrichment import build as build_mod
    from enrichment import config
    from enrichment.models import ThematicBundle, SymbolBundle
    from enrichment.universe import Universe

    monkeypatch.setattr(config, "ENRICHMENT_ENABLED", True)
    monkeypatch.setattr(config, "ENRICHMENT_THEMES_ENABLED", False)

    class RecordingProvider:
        name = "rec"

        def __init__(self):
            self.themes_called = []

        def symbol_bundle(self, t):
            return SymbolBundle(ticker=t, rp_entity_id=None, sentiment=None)

        def thematic_bundle(self, th):
            self.themes_called.append(th)
            return ThematicBundle(theme=th)

    prov = RecordingProvider()
    uni = Universe(tickers=["CVX"], themes=["gold", "defence"])
    bundles = build_mod.build_enrichment(
        uni, as_of="2026-07-19T00:00:00+00:00", provider=prov
    )
    assert prov.themes_called == []
    assert bundles.themes == []
    assert len(bundles.symbols) == 1

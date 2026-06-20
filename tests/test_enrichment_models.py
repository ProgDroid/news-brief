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
                events=[
                    Event(category="earnings-call", title="Q2 call", date="2026-07-25")
                ],
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
        themes=[
            ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2026-06-19")])
        ],
    )
    assert bundles_from_dict(b.to_dict()) == b

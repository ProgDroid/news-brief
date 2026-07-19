from enrichment.models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
    bundles_from_dict,
)


def _score():
    return SentimentScore(
        as_of="2024-03-28",
        daily_sentiment=0.06,
        sentiment_pressure=-0.75,
        abnormal_media_attention=-0.74,
        trend_mean=0.01,
        trend_delta=0.05,
        n_points=87,
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
        as_of="2026-07-19T20:00:00+00:00",
        provider="bigdata",
        symbols=[
            SymbolBundle(
                ticker="CVX",
                rp_entity_id="D54E62",
                sentiment=_score(),
                events=[Event("earnings-call", "Q2 2024", "2024-08-01")],
                evidence=[EvidenceDoc("Tengiz", "Reuters", "2024-06-18", "u", -0.73)],
            )
        ],
        themes=[
            ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2024-06-19")])
        ],
    )
    assert bundles_from_dict(b.to_dict()) == b


def test_persisted_dict_drops_content():
    b = EnrichmentBundles(
        as_of="2026-07-19T20:00:00+00:00",
        provider="bigdata",
        symbols=[
            SymbolBundle(
                ticker="CVX",
                rp_entity_id="D54E62",
                sentiment=_score(),
                events=[Event("earnings-call", "Q2 2024", "2024-08-01")],
                evidence=[EvidenceDoc("Tengiz", "Reuters", "2024-06-18", "u", -0.73)],
            )
        ],
        themes=[
            ThematicBundle(theme="gold", docs=[EvidenceDoc("g", "FT", "2024-06-19")])
        ],
    )
    p = b.to_persisted_dict()
    assert "themes" not in p
    sym = p["symbols"][0]
    assert sym["ticker"] == "CVX" and sym["rp_entity_id"] == "D54E62"
    assert sym["sentiment"]["daily_sentiment"] == 0.06
    assert "events" not in sym and "evidence" not in sym
    # reload is still a valid annotate input (sentiment survives)
    reloaded = bundles_from_dict(p)
    assert reloaded.symbols[0].sentiment.daily_sentiment == 0.06


def test_persisted_dict_handles_none_sentiment():
    b = EnrichmentBundles(
        as_of="2026-07-19T20:00:00+00:00",
        provider="bigdata",
        symbols=[SymbolBundle(ticker="NADA", rp_entity_id=None, sentiment=None)],
    )
    p = b.to_persisted_dict()
    assert p["symbols"][0]["sentiment"] is None

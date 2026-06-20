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
                sentiment=SentimentScore(
                    -0.41, -0.05, -1.8, -2.0, "Negative", "reduced"
                ),
                events=[Event("conference-call", "Investor Day", "2026-07-08")],
                evidence=[
                    EvidenceDoc("Class action", "Reuters", "2026-06-15", None, -0.6)
                ],
            )
        ],
        themes=[
            ThematicBundle(
                theme="gold", docs=[EvidenceDoc("Gold up", "FT", "2026-06-19")]
            )
        ],
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
        {
            "ticker": "AVAV",
            "topic": "defence",
            "direction": "bearish",
            "confidence": "medium",
        },
        {
            "ticker": "MU",
            "topic": "memory",
            "direction": "bullish",
            "confidence": "high",
        },
        {"ticker": None, "topic": "macro", "direction": "neutral", "confidence": "low"},
    ]
    out = annotate_signals(signals, _bundles())
    assert out[0]["bigdata_sentiment"]["regime"] == "Negative"
    assert out[0]["bigdata_sentiment"]["current"] == -0.41
    assert "bigdata_sentiment" not in out[1]  # no bundle for MU
    assert "bigdata_sentiment" not in out[2]  # null ticker
    # inputs not mutated
    assert "bigdata_sentiment" not in signals[0]
    # returned list and unmatched elements must be independent objects
    assert out is not signals
    assert out[1] is not signals[1]


def test_annotate_signals_no_op_when_empty():
    signals = [
        {"ticker": "AVAV", "topic": "x", "direction": "bearish", "confidence": "low"}
    ]
    out = annotate_signals(signals, EnrichmentBundles(as_of=AS_OF))
    assert out == signals
    assert out is not signals
    assert out[0] is not signals[0]

from enrichment.models import EnrichmentBundles, Event, SentimentScore, SymbolBundle
from enrichment.render import annotate_signals, render_prompt_block


def _score():
    return SentimentScore("2024-03-28", 0.06, -0.75, -0.74, 0.01, 0.05, 87)


def test_prompt_block_shows_native_sentiment_fields():
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00",
        provider="bigdata",
        symbols=[
            SymbolBundle(
                "CVX",
                "D54E62",
                _score(),
                events=[Event("earnings-call", "Q2 2024", "2024-08-01")],
            )
        ],
    )
    out = render_prompt_block(b)
    assert "CVX" in out and "0.06" in out and "-0.75" in out
    assert "Q2 2024" in out
    assert "NEVER a trade trigger" in out


def test_annotate_attaches_new_bigdata_sentiment_shape():
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00",
        provider="bigdata",
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
    b = EnrichmentBundles(
        as_of="x", provider="bigdata", symbols=[SymbolBundle("CVX", "D54E62", _score())]
    )
    out = annotate_signals([{"ticker": "NVDA"}], b)
    assert "bigdata_sentiment" not in out[0]


def test_render_empty_when_no_data():
    assert (
        render_prompt_block(EnrichmentBundles(as_of="2026-07-19T00:00:00+00:00")) == ""
    )


def test_annotate_signals_no_op_when_empty():
    signals = [{"ticker": "AVAV", "direction": "bearish"}]
    out = annotate_signals(
        signals, EnrichmentBundles(as_of="2026-07-19T00:00:00+00:00")
    )
    assert out == signals
    assert out is not signals
    assert out[0] is not signals[0]


def test_annotate_does_not_mutate_inputs():
    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00",
        provider="bigdata",
        symbols=[SymbolBundle("CVX", "D54E62", _score())],
    )
    signals = [{"ticker": "CVX", "direction": "long"}]
    out = annotate_signals(signals, b)
    assert "bigdata_sentiment" not in signals[0]
    assert out is not signals
    assert out[0] is not signals[0]

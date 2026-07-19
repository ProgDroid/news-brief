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
    assert "Keep the entire brief under 600 words" in prompt


def test_submit_persists_derived_only_snapshot(tmp_path):
    from enrichment.models import (
        EnrichmentBundles,
        SymbolBundle,
        SentimentScore,
        Event,
        EvidenceDoc,
        ThematicBundle,
    )

    b = EnrichmentBundles(
        as_of="2026-07-19T00:00:00+00:00",
        provider="bigdata",
        symbols=[
            SymbolBundle(
                "CVX",
                "D54E62",
                SentimentScore("2026-07-18", -0.07, -0.4, -0.3, -0.05, -0.02, 40),
                events=[Event("earnings-call", "Q2 2026", "2026-07-25")],
                evidence=[
                    EvidenceDoc("secret headline", "FT", "2026-07-01", "u", -0.5)
                ],
            )
        ],
        themes=[
            ThematicBundle(
                "gold", docs=[EvidenceDoc("themed headline", "FT", "2026-07-02")]
            )
        ],
    )
    persisted = b.to_persisted_dict()
    import json

    blob = json.dumps(persisted)
    assert "secret headline" not in blob
    assert "themed headline" not in blob
    assert "Q2 2026" not in blob  # event title is Content, dropped
    assert persisted["symbols"][0]["sentiment"]["daily_sentiment"] == -0.07

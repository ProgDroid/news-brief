# enrichment/__init__.py
"""Bigdata.com enrichment subsystem.

Flag-gated (OFF by default). Feeds entity-resolved sentiment, events and
evidence into the daily brief as READ-ONLY context behind a vendor-neutral
provider seam. The bundle types here are the future digest<->trading seam.
"""

from . import config
from .build import build_enrichment
from .models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)
from .render import annotate_signals, render_prompt_block
from .universe import Universe, build_universe, latest_signal_tickers


def is_enabled() -> bool:
    # Read at CALL time, through the forwarding seam. This was a from-import
    # copy, frozen at import, which no host toggle could move.
    return config.ENRICHMENT_ENABLED


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
    "render_prompt_block",
    "annotate_signals",
]

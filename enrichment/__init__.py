# enrichment/__init__.py
"""Bigdata.com enrichment subsystem.

Flag-gated (OFF by default). Feeds entity-resolved sentiment, events and
evidence into the daily brief as READ-ONLY context behind a vendor-neutral
provider seam. The bundle types here are the future digest<->trading seam.
"""

from .config import ENRICHMENT_ENABLED as _ENABLED
from .models import (
    EnrichmentBundles,
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)


def is_enabled() -> bool:
    return _ENABLED


__all__ = [
    "is_enabled",
    "EnrichmentBundles",
    "EvidenceDoc",
    "Event",
    "SentimentScore",
    "SymbolBundle",
    "ThematicBundle",
]

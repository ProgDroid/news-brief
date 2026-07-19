# enrichment/models.py
"""Enrichment data model. Frozen dataclasses — these bundle types are the
digest<->trading contract (independently consumable by a future standalone
digest or trading system)."""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class SentimentScore:
    as_of: str | None  # latest series point's date, "YYYY-MM-DD"
    daily_sentiment: float | None  # latest, -1..1 (media tone)
    sentiment_pressure: float | None  # latest native abnormality signal
    abnormal_media_attention: float | None  # latest native attention signal
    trend_mean: float | None = None  # mean daily_sentiment over the window
    trend_delta: float | None = None  # latest daily_sentiment - trend_mean
    n_points: int = 0  # series length backing the stats


@dataclass(frozen=True)
class Event:
    category: str  # "earnings-call" | "conference-call" | ...
    title: str
    date: str  # ISO date "YYYY-MM-DD"
    url: str | None = None


@dataclass(frozen=True)
class EvidenceDoc:
    headline: str
    source: str
    date: str  # ISO
    url: str | None = None
    sentiment: float | None = None


@dataclass(frozen=True)
class SymbolBundle:
    ticker: str  # base symbol used to query (e.g. "AVAV")
    rp_entity_id: str | None
    sentiment: SentimentScore | None
    events: list[Event] = field(default_factory=list)
    evidence: list[EvidenceDoc] = field(default_factory=list)
    error: str | None = None  # set when this symbol degraded; bundle still returned


@dataclass(frozen=True)
class ThematicBundle:
    theme: str
    docs: list[EvidenceDoc] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class EnrichmentBundles:
    as_of: str  # ISO timestamp the enrichment was built
    symbols: list[SymbolBundle] = field(default_factory=list)
    themes: list[ThematicBundle] = field(default_factory=list)
    provider: str = "null"

    def is_empty(self) -> bool:
        return not self.symbols and not self.themes

    def to_dict(self) -> dict:
        return asdict(self)

    def to_persisted_dict(self) -> dict:
        """Derived-only projection for on-disk persistence. Drops all vendor
        Content (headlines, search text, thematic docs, event titles) to satisfy
        the no-cache ToS; keeps only what collect-time annotate_signals needs."""
        return {
            "as_of": self.as_of,
            "provider": self.provider,
            "symbols": [
                {
                    "ticker": s.ticker,
                    "rp_entity_id": s.rp_entity_id,
                    "sentiment": asdict(s.sentiment) if s.sentiment else None,
                }
                for s in self.symbols
            ],
        }


# --- deserialization (public; used by FixtureProvider and the collect reload) ---
def symbol_from_dict(d: dict) -> SymbolBundle:
    s = d.get("sentiment")
    return SymbolBundle(
        ticker=d["ticker"],
        rp_entity_id=d.get("rp_entity_id"),
        sentiment=SentimentScore(**s) if s else None,
        events=[Event(**e) for e in d.get("events", [])],
        evidence=[EvidenceDoc(**ev) for ev in d.get("evidence", [])],
        error=d.get("error"),
    )


def theme_from_dict(d: dict) -> ThematicBundle:
    return ThematicBundle(
        theme=d["theme"],
        docs=[EvidenceDoc(**ev) for ev in d.get("docs", [])],
        error=d.get("error"),
    )


def bundles_from_dict(d: dict) -> EnrichmentBundles:
    return EnrichmentBundles(
        as_of=d.get("as_of", ""),
        provider=d.get("provider", "null"),
        symbols=[symbol_from_dict(s) for s in d.get("symbols", [])],
        themes=[theme_from_dict(t) for t in d.get("themes", [])],
    )

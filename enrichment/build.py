# enrichment/build.py
"""Assemble EnrichmentBundles for one run: flag gate + bounded fan-out."""

from common import log

from . import config
from .models import EnrichmentBundles
from .providers import Provider, get_provider
from .universe import Universe


def build_enrichment(
    universe: Universe,
    *,
    as_of: str,
    provider: Provider | None = None,
) -> EnrichmentBundles:
    if not config.ENRICHMENT_ENABLED:
        return EnrichmentBundles(as_of=as_of, provider="null")

    provider = provider or get_provider()

    tickers = universe.tickers[: config.ENRICHMENT_MAX_SYMBOLS]
    themes = (
        universe.themes[: config.ENRICHMENT_MAX_THEMES]
        if config.ENRICHMENT_THEMES_ENABLED
        else []
    )
    dropped_sym = len(universe.tickers) - len(tickers)
    dropped_thm = (
        (len(universe.themes) - len(themes)) if config.ENRICHMENT_THEMES_ENABLED else 0
    )
    if dropped_sym or dropped_thm:
        log.warning(
            "Enrichment fan-out capped: dropped %d symbol(s), %d theme(s) over limit",
            dropped_sym,
            dropped_thm,
        )

    symbols = [provider.symbol_bundle(t) for t in tickers]
    theme_bundles = [provider.thematic_bundle(th) for th in themes]
    errs = sum(1 for s in symbols if s.error) + sum(1 for t in theme_bundles if t.error)
    log.info(
        "Enrichment built: provider=%s symbols=%d themes=%d errors=%d",
        provider.name,
        len(symbols),
        len(theme_bundles),
        errs,
    )
    return EnrichmentBundles(
        as_of=as_of, symbols=symbols, themes=theme_bundles, provider=provider.name
    )

# enrichment/providers.py
"""Provider seam: vendor-neutral interface + implementations.

NullProvider   — default; empty bundles (flag OFF / no creds).
FixtureProvider — model-level JSON fixtures (tests + MCP-captured interim).
BigdataProvider — production REST client (added in Task 3; flag-gated).

A provider returns fully-assembled, normalised bundle objects — never raw
vendor JSON — so swapping vendors is a one-file change.
"""

import json
from pathlib import Path
from typing import Protocol

from . import config
from .models import (
    SymbolBundle,
    ThematicBundle,
    symbol_from_dict,
    theme_from_dict,
)


class Provider(Protocol):
    name: str

    def symbol_bundle(self, ticker: str) -> SymbolBundle: ...

    def thematic_bundle(self, theme: str) -> ThematicBundle: ...


class NullProvider:
    name = "null"

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        return SymbolBundle(ticker=ticker, rp_entity_id=None, sentiment=None)

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        return ThematicBundle(theme=theme)


class FixtureProvider:
    """Reads model-level JSON from <dir>/symbol_<TICKER>.json and theme_<theme>.json."""

    name = "fixture"

    def __init__(self, fixture_dir: str):
        self._dir = Path(fixture_dir)

    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        path = self._dir / f"symbol_{ticker}.json"
        if not path.exists():
            return SymbolBundle(ticker=ticker, rp_entity_id=None, sentiment=None)
        return symbol_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        path = self._dir / f"theme_{theme}.json"
        if not path.exists():
            return ThematicBundle(theme=theme)
        return theme_from_dict(json.loads(path.read_text(encoding="utf-8")))


def get_provider() -> Provider:
    """Select a provider from config. Only meaningful when ENRICHMENT_ENABLED."""
    name = config.ENRICHMENT_PROVIDER or (
        "bigdata" if config.BIGDATA_API_KEY else "null"
    )
    if name == "fixture":
        return FixtureProvider(config.FIXTURE_DIR)
    if name == "bigdata" and config.BIGDATA_API_KEY:
        from .providers_bigdata import BigdataProvider  # added in Task 3

        return BigdataProvider(config.BIGDATA_API_KEY, config.BIGDATA_BASE_URL)
    return NullProvider()

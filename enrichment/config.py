# enrichment/config.py
"""Configuration seam for the enrichment subsystem.

The non-secret knobs are `settings` rows, declared in `common.KNOBS` and reached
through the module `__getattr__` below. Forwarding rather than re-exporting is
what keeps them LIVE: a re-export binds once at import, and a knob that cannot
move without a container restart is not a knob — it is a build artifact.

The forwarding list is explicit rather than "anything in common.KNOBS" so that
`config.PG_A_ENABLED` stays a typo instead of becoming a lookup that happens to
succeed. Two values remain real constants read from the environment: the API key
(a credential, and a settings row would land in every pg_dump) and the fixture
directory (test-only, set by tests that have no database).
"""

import os

import common

# Bigdata.com REST credential (business-email REST key; see design spec).
BIGDATA_API_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()

# Directory of model-level JSON fixtures for FixtureProvider (tests / MCP interim).
FIXTURE_DIR = os.environ.get("ENRICHMENT_FIXTURE_DIR", "").strip()

_FORWARDED = frozenset(
    {
        "ENRICHMENT_ENABLED",
        "ENRICHMENT_PROVIDER",
        "ENRICHMENT_THEMES_ENABLED",
        "ENRICHMENT_MAX_SYMBOLS",
        "ENRICHMENT_MAX_THEMES",
        "ENRICHMENT_HTTP_TIMEOUT",
        "BIGDATA_BASE_URL",
    }
)


def __getattr__(name: str):
    """Resolve a forwarded knob on attribute access (PEP 562).

    Python consults this only for names absent from the module, so every
    `config.ENRICHMENT_X` read in the package reaches `common` and, through it,
    the settings cache — with no call site changed.
    """
    if name in _FORWARDED:
        return getattr(common, name)
    raise AttributeError(f"module 'enrichment.config' has no attribute '{name}'")

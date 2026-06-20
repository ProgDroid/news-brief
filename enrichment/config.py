# enrichment/config.py
"""Env-driven configuration for the enrichment subsystem.

Kept local to the package (not in common.py) because the whole subsystem is
optional and flag-gated; common.py stays focused on always-on infrastructure.
"""

import os

# Master switch. OFF by default — enrichment ships dark until REST creds land.
ENRICHMENT_ENABLED = os.environ.get("ENRICHMENT_ENABLED", "0") == "1"

# Provider selection: "null" | "fixture" | "bigdata". Empty -> auto (bigdata if a
# key is present, else null). Only consulted when ENRICHMENT_ENABLED is true.
ENRICHMENT_PROVIDER = os.environ.get("ENRICHMENT_PROVIDER", "").strip().lower()

# Bigdata.com REST credentials/endpoint (business-email REST key; see design spec).
BIGDATA_API_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()
BIGDATA_BASE_URL = os.environ.get("BIGDATA_BASE_URL", "https://api.bigdata.com").strip()

# Bounded fan-out — hard ceilings so a large watchlist can't blow the query budget.
ENRICHMENT_MAX_SYMBOLS = int(os.environ.get("ENRICHMENT_MAX_SYMBOLS", "20"))
ENRICHMENT_MAX_THEMES = int(os.environ.get("ENRICHMENT_MAX_THEMES", "8"))
ENRICHMENT_HTTP_TIMEOUT = float(os.environ.get("ENRICHMENT_HTTP_TIMEOUT", "20"))

# Directory of model-level JSON fixtures for FixtureProvider (tests / MCP interim).
FIXTURE_DIR = os.environ.get("ENRICHMENT_FIXTURE_DIR", "").strip()

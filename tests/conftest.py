"""Test bootstrap.

NEWSBRIEF_DATA_DIR must be set BEFORE `brief` is imported anywhere: the module
binds DATA_DIR (and every path constant derived from it) at import time, and
the default is the container volume path /app/logs.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
import requests

os.environ.setdefault("NEWSBRIEF_DATA_DIR", tempfile.mkdtemp(prefix="newsbrief-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DB-backed tests skip when no database is configured — `db.is_configured`, the
# same predicate `db.connect` reads, so either DATABASE_URL or the discrete
# POSTGRES_* variables will do (see tests/test_db.py) — and say how to get one.
# They must never skip in CI: the workflow sets DATABASE_URL, so a skip there
# means the services block is broken, not that the test is optional.


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch):
    """Block every outbound HTTP call for the whole suite.

    Three tests reached api.telegram.org for real through `telegram_alert` and
    passed offline only because the send is wrapped in `except Exception` — at
    roughly 15 seconds of connect timeout per call (fix round 2, Important 4).

    The block sits on `requests.sessions.Session.request`, the single funnel
    every requests entry point goes through, rather than on
    `common.telegram_alert`: `supervisor` and `brief` bind that name with
    `from common import telegram_alert`, so patching the attribute on `common`
    would never reach them. Tests that stub `requests.post`/`requests.get` on a
    module keep working — they replace a function that sits ABOVE this funnel,
    and monkeypatch undoes both in the right order.
    """

    def _blocked(self, method, url, *args, **kwargs):
        raise RuntimeError(
            f"outbound HTTP is blocked in tests: {method} {url}. "
            "Stub the call instead (monkeypatch the module's `requests`)."
        )

    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)

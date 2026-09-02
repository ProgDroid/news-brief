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


def pytest_configure(config):
    # The parameter name is fixed by pytest's hookspec and unfortunately collides
    # with our own `config` module. Harmless: the module is imported inside the
    # fixture below, never at this scope.
    config.addinivalue_line(
        "markers",
        "real_config: do not stub config.chat_id — the module under test is config itself",
    )


@pytest.fixture(autouse=True)
def _stubbed_chat_id(request, monkeypatch):
    """Resolve the delivery chat id without a database, suite-wide.

    From phase 2 the chat id is a row in `users` rather than an environment
    variable, and production hard-requires Postgres for it. Substituting at the
    accessor — the seam, not a fallback inside it — is what keeps the ~400 tests
    that merely need *a* chat id infra-free. The handful that care which chat id
    override this by patching `config.chat_id` themselves, and `test_config.py`
    opts out entirely with the `real_config` marker.
    """
    if request.node.get_closest_marker("real_config"):
        return
    import config

    monkeypatch.setattr(config, "chat_id", lambda: TEST_CHAT_ID)
    monkeypatch.setattr(config, "alert_chat_id", lambda: TEST_CHAT_ID)


# The chat id the suite runs as, unless a test says otherwise.
TEST_CHAT_ID = "123456"


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

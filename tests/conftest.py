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
def _stubbed_config(request, monkeypatch):
    """Resolve identity and knobs without a database, suite-wide.

    From phase 2 both the chat id and every knob are rows, and production
    hard-requires Postgres for them. Substituting at the seam — the two readers,
    not a fallback inside them — is what keeps the ~1100 tests that merely need
    *a* chat id and default knobs infra-free.

    `_read_settings` returns empty rather than a canned map on purpose: no rows
    means every knob resolves to its declared default through the real coercion
    path, which is both the production first-boot state and the value the suite
    has always asserted against. Tests that want a different knob still patch
    `common.<NAME>` exactly as they did before.

    `test_config.py` opts out entirely with the `real_config` marker.
    """
    import common
    import config

    # Knobs resolve through common.__getattr__, which Python consults ONLY for
    # names absent from the module. `monkeypatch.setattr(common, "PG_A_ENABLED",
    # True)` works, but its undo restores the resolved value as a REAL
    # attribute — which then shadows __getattr__ for the rest of the process and
    # quietly freezes that knob for every later test. Clearing the leak here, at
    # setup, rather than in teardown: monkeypatch is torn down after us, so a
    # teardown cleanup would be undone immediately by the very thing it fixes.
    for name in common.KNOBS:
        common.__dict__.pop(name, None)

    # `enrichment.config` forwards its knobs to `common` through the same PEP 562
    # mechanism, and its call sites are patched with setattr all over
    # test_enrichment_*.py — so it leaks the same way and needs the same sweep.
    from enrichment import config as enrichment_config

    for name in enrichment_config._FORWARDED:
        enrichment_config.__dict__.pop(name, None)

    # Not an early `return`: this is a generator fixture, and a path that skips
    # the yield fails at setup with "did not yield a value" — invisibly, until
    # someone runs the DB-backed module that carries the marker.
    if not request.node.get_closest_marker("real_config"):
        config.invalidate()
        monkeypatch.setattr(config, "chat_id", lambda: TEST_CHAT_ID)
        monkeypatch.setattr(config, "alert_chat_id", lambda: TEST_CHAT_ID)
        monkeypatch.setattr(config, "_read_settings", dict)
    yield
    # The stub is cached like any other read, so it must not outlive the test
    # that installed it — a `real_config` test running next would otherwise see
    # an empty settings map it never asked for.
    config.invalidate()


# The chat id the suite runs as, unless a test says otherwise.
TEST_CHAT_ID = "123456"


@pytest.fixture()
def state_store(monkeypatch):
    """An in-memory stand-in for the `runtime_state` table, returned as a dict.

    `dict.update` is precisely the per-key merge the real upsert performs, so a
    test using this fake sees the same clobber-nothing semantics production has.
    The SQL is covered in test_config.py against a real database.
    """
    import config

    store: dict = {}
    monkeypatch.setattr(config, "runtime_state", lambda: dict(store))
    monkeypatch.setattr(config, "set_runtime_state", store.update)
    monkeypatch.setattr(
        config,
        "clear_runtime_state",
        lambda keys: [store.pop(k, None) for k in keys],
    )
    return store


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

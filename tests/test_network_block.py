"""The suite must not be able to reach the network.

Before the `_no_outbound_http` fixture in conftest, `telegram_alert` really did
POST to api.telegram.org from three tests — the startup-failure test here and
the two reclaim tests in test_job_interlock.py. They passed offline only because
the send is wrapped in `except Exception`, at roughly 15 seconds of connect
timeout each (fix round 2, Important 4).

These tests are the positive control for that fixture: a block nobody exercises
is indistinguishable from no block at all.
"""

import pytest
import requests

import supervisor


def test_a_bare_requests_call_cannot_reach_the_network():
    with pytest.raises(RuntimeError, match="outbound HTTP is blocked"):
        requests.post("https://api.telegram.org/botFAKE/sendMessage", json={})


def test_a_session_call_cannot_reach_the_network():
    """requests.post and Session.get funnel through the same Session.request,
    which is why the block sits there rather than on the module functions."""
    with pytest.raises(RuntimeError, match="outbound HTTP is blocked"):
        requests.Session().get("https://api.telegram.org/botFAKE/getMe")


def test_the_block_reaches_a_from_import_bound_alert(caplog):
    """`supervisor` and `brief` do `from common import telegram_alert`, so
    patching `common.telegram_alert` would not touch those already-bound names.
    Blocking at the requests layer holds regardless of import style — and the
    alert still swallows the failure, so nothing downstream changes."""
    with caplog.at_level("ERROR"):
        supervisor.telegram_alert("this must not leave the machine")

    assert any("outbound HTTP is blocked" in r.message for r in caplog.records), (
        "telegram_alert reached the network, or the block never fired"
    )

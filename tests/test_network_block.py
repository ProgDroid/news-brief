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

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every test in this file trips the block on purpose -- that is what makes it
# a control rather than a claim -- so the teardown verdict must not fail them.
pytestmark = pytest.mark.allow_blocked_network


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


# ── The socket layer (news-brief-0q0.13) ─────────────────────────────────────
# The requests-level block funnels every requests entry point, and covers
# nothing else. feedparser and the urllib paths in brief.py and backtest/ walk
# straight past it, so the suite's "no network" property held for one library
# and was simply untrue for the others -- untrue quietly, which is the part
# that matters: a test that reaches the real network passes, slowly, and only
# on a machine that has one.

import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import urllib.request  # noqa: E402

import conftest  # noqa: E402


def test_a_urllib_call_cannot_reach_the_network():
    """feedparser fetches through urllib, so this is the hole the requests-level
    block could never see."""
    with pytest.raises(conftest.BlockedNetwork):
        urllib.request.urlopen("http://example.com/feed.xml", timeout=1)


def test_a_bare_socket_cannot_reach_the_network():
    with pytest.raises(conftest.BlockedNetwork):
        socket.create_connection(("example.com", 80), timeout=1)


def test_loopback_is_still_reachable():
    """The presence control. "Blocks the network" is satisfied for free by
    blocking everything, which would also cut off the test Postgres and turn
    every DB-backed test into a skip -- a green suite that stopped looking."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        socket.create_connection(listener.getsockname(), timeout=2).close()
    finally:
        listener.close()


def test_a_swallowed_block_fails_the_test_that_caused_it():
    """The second half of the bead, and the one that made the original three
    offenders invisible: telegram_alert wraps its send in `except Exception`, so
    a blocked call left no trace at all. Run in a subprocess because the thing
    under test is a teardown verdict -- asserting on the mechanism alone would
    prove the check works while saying nothing about whether it runs."""
    case = """
import requests
def test_swallows_a_blocked_call():
    try:
        requests.get("https://api.telegram.org/botFAKE/getMe")
    except Exception:
        pass
"""
    path = REPO_ROOT / "tests" / "test_tmp_swallow_probe.py"
    path.write_text(case, encoding="utf-8")
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    finally:
        path.unlink()
    assert done.returncode != 0, (
        "a test that swallowed a blocked network call passed:\n" + done.stdout
    )
    assert "reached for the network" in done.stdout


def test_the_probe_above_passes_when_it_makes_no_call():
    """The positive control for the subprocess harness itself. Without it, a
    broken harness -- wrong path, import error, pytest not found -- returns
    non-zero for reasons unrelated to the network and the test above passes
    while proving nothing."""
    case = "def test_touches_nothing():\n    assert True\n"
    path = REPO_ROOT / "tests" / "test_tmp_quiet_probe.py"
    path.write_text(case, encoding="utf-8")
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    finally:
        path.unlink()
    assert done.returncode == 0, done.stdout

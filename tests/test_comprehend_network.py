"""The network seam: which timeout and attempt budget each call gets.

Deliberately carries no database skipmark. Every other comprehend test file
skips wholesale when DATABASE_URL is unset, and these assertions need no
database -- putting them here is what makes them run in CI rather than skip.
"""

import pytest

import brief
import common
import comprehend


class _OK:
    def raise_for_status(self):
        pass

    def json(self):
        return {"content": []}


@pytest.fixture()
def posts(monkeypatch):
    """Capture every requests.post the call under test makes."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"timeout": timeout, "payload": json})
        return _OK()

    monkeypatch.setattr(brief.requests, "post", fake_post)
    return calls


@pytest.fixture()
def always_times_out(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(timeout)
        raise brief.requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(brief.requests, "post", fake_post)
    return calls


# --- The signals path must not move. This is the positive control: every
# assertion below about comprehend's budget is only meaningful if the caller
# that already existed still gets exactly what it got before.


def test_post_messages_still_defaults_to_the_signals_budget(posts):
    brief._post_messages({"model": "x"})
    assert posts[0]["timeout"] == brief.SIGNALS_TIMEOUT


def test_post_messages_still_defaults_to_two_signals_attempts(always_times_out):
    with pytest.raises(brief.requests.exceptions.Timeout):
        brief._post_messages({"model": "x"})
    assert len(always_times_out) == brief.SIGNALS_MAX_ATTEMPTS == 2


def test_post_messages_honours_an_explicit_timeout(posts):
    brief._post_messages({"model": "x"}, timeout=1234)
    assert posts[0]["timeout"] == 1234


def test_post_messages_honours_an_explicit_attempt_count(always_times_out):
    with pytest.raises(brief.requests.exceptions.Timeout):
        brief._post_messages({"model": "x"}, max_attempts=1)
    assert len(always_times_out) == 1


# --- Integration: its own budget, sized for its own workload.


def test_integration_does_not_inherit_the_signals_timeout(posts):
    """SIGNALS_TIMEOUT=90 was sized for a different call, and brief.py:2852
    justifies it with 'latency is free' -- true after delivery, false inside
    an hourly job generating at INTEGRATE_MAX_TOKENS=8192."""
    comprehend.call_integration({"model": "x"})
    assert posts[0]["timeout"] != brief.SIGNALS_TIMEOUT
    assert posts[0]["timeout"] == int(common.COMPREHEND_INTEGRATE_TIMEOUT)


def test_the_integration_timeout_leaves_room_for_a_full_length_generation():
    """A floor, not an exact value: 8192 output tokens cannot complete in 90s.
    The knob exists so the host can tighten it once real durations are logged;
    this pins that the shipped default is not the one that caused the bug."""
    assert int(common.COMPREHEND_INTEGRATE_TIMEOUT) > brief.SIGNALS_TIMEOUT


def test_integration_makes_one_http_attempt_because_the_item_ceiling_retries(
    always_times_out,
):
    """comprehend.py:178 already selects on integrate_attempts < 3 and the
    failure path increments it, so a failed item is retried on the next hourly
    pass. A second HTTP attempt multiplies against that ceiling -- 3 x 2 = 6
    charged 8192-token generations for one bad item -- and spends the pass's
    own DEADLINE_SECONDS budget, which the item-level retry does not."""
    with pytest.raises(brief.requests.exceptions.Timeout):
        comprehend.call_integration({"model": "x"})
    assert len(always_times_out) == 1


# --- Triage: a much smaller generation, so a much smaller budget.


def test_triage_uses_its_own_timeout(posts):
    comprehend.call_triage({"model": "x"})
    assert posts[0]["timeout"] == int(common.COMPREHEND_TRIAGE_TIMEOUT)


def test_triage_gets_a_smaller_budget_than_integration():
    """TRIAGE_MAX_TOKENS=2048 against INTEGRATE_MAX_TOKENS=8192. Sizing both
    the same would hand the cheap call the expensive call's deadline share."""
    assert int(common.COMPREHEND_TRIAGE_TIMEOUT) < int(
        common.COMPREHEND_INTEGRATE_TIMEOUT
    )


def test_both_timeouts_are_settings_knobs_not_constants():
    """This repo's rule is that configuration is a settings row, so the host
    can retune after seeing real durations without a redeploy."""
    assert "COMPREHEND_INTEGRATE_TIMEOUT" in common.KNOBS
    assert "COMPREHEND_TRIAGE_TIMEOUT" in common.KNOBS

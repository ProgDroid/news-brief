"""Spend accounting and the budget (spec 2026-09-25 section 4.3)."""

import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

import common
import comprehend
import config
import db

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def test_sonnet_cost_is_priced_from_the_table():
    usd = comprehend.cost_usd(
        "claude-sonnet-5", {"input_tokens": 1_000_000, "output_tokens": 100_000}
    )
    assert usd == pytest.approx(2.00 + 1.00)


def test_the_batch_rate_is_half():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert comprehend.cost_usd("claude-sonnet-5", usage, batch=True) == pytest.approx(
        1.00
    )


def test_an_unpriced_model_is_refused_not_priced_at_zero():
    """A silent $0 would make the budget accepted-and-inert."""
    with pytest.raises(comprehend.UnpricedModel):
        comprehend.cost_usd("claude-sonnet-9", {"input_tokens": 1})


def test_a_response_without_usage_costs_nothing_and_warns(caplog):
    """Review Focus 3."""
    with caplog.at_level(logging.WARNING):
        assert comprehend.cost_usd("claude-sonnet-5", {}) == 0.0
    assert "no usage" in caplog.text


@needs_db
def test_record_spend_writes_one_ledger_row(kb):
    usd = comprehend.record_spend(
        kb, "triage", "claude-haiku-4-5", {"input_tokens": 2000, "output_tokens": 100}
    )
    kb.commit()
    row = kb.execute(
        "SELECT stage, model, input_tokens, output_tokens, usd FROM comprehend_spend"
    ).fetchone()
    assert row[:4] == ("triage", "claude-haiku-4-5", 2000, 100)
    assert float(row[4]) == pytest.approx(usd) == pytest.approx(0.0025)


@needs_db
def test_an_unpriced_model_aborts_the_pass_before_any_call(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-sonnet-9")
    monkeypatch.setattr(
        comprehend, "call_triage", lambda r: pytest.fail("called an unpriced model")
    )

    tally = comprehend.run(kb)

    assert tally.aborted == "unpriced_model:claude-sonnet-9"


@needs_db
def test_a_successful_integration_call_ledgers_its_spend(kb, monkeypatch):
    """The other charge point in run() (M3): triage's is covered by
    test_an_unpriced_model_aborts_the_pass_before_any_call and the triage
    ledger row is exercised via record_spend directly, but nothing drove a
    real integration batch through run() to prove the second call site
    actually charges -- specifically, that it charges the INTEGRATION model
    and prices at ITS rate. With both TRIAGE_MODEL and INTEGRATE_MODEL unset
    the two calls resolve to the same common.MODEL, so a mixed-up call site
    (`_triage_model()` where `_integrate_model()` belongs) would still pass
    every assertion here -- review Important finding, fix round 1. Pinning
    TRIAGE_MODEL to Haiku makes the two differ. The item is rules-matched
    (tracked_story), so no triage call should happen at all; call_triage is
    stubbed to fail the test if it is, so that claim is enforced, not assumed.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-haiku-4-5")
    monkeypatch.setattr(
        comprehend,
        "call_triage",
        lambda r: pytest.fail("item was rules-matched; call_triage must not run"),
    )
    kb.execute("INSERT INTO stories (name, scope) VALUES ('Ukraine talks', 'episodic')")
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, published_at) "
        "VALUES (%s, 'u', 'Ukraine talks resume', 'body', 'H1', now()) RETURNING id",
        (outlet_id,),
    ).fetchone()[0]
    kb.commit()

    def fake_integrate(req):
        return {
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": item_id,
                                "entities": [
                                    {
                                        "name": "Ukraine",
                                        "type": "country",
                                        "aliases": [],
                                    }
                                ],
                                "events": [
                                    {
                                        "summary": "Talks resumed",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    integrate_model = comprehend._integrate_model()
    triage_model = comprehend._triage_model()
    assert integrate_model != triage_model, (
        "the two models must differ or the row's model column can't discriminate "
        "which call site actually charged"
    )
    usage = {"input_tokens": 1000, "output_tokens": 100}
    expected_usd = comprehend.cost_usd(integrate_model, usage)
    haiku_usd = comprehend.cost_usd(triage_model, usage)
    assert expected_usd != pytest.approx(haiku_usd), (
        "the two rates must differ or a Haiku-priced row would look correct too"
    )

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.material == 1
    rows = kb.execute(
        "SELECT model, usd FROM comprehend_spend WHERE stage = 'integration'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == integrate_model
    assert float(rows[0][1]) == pytest.approx(expected_usd) == pytest.approx(0.003)
    assert tally.spent_usd == pytest.approx(0.003)


@needs_db
def test_a_model_row_edit_mid_pass_does_not_change_this_pass(kb, monkeypatch):
    """I1 (triage side, reviewer finding). comprehend.run() resolves
    `_triage_model()`/`_integrate_model()` once for the unpriced-model check,
    but every OTHER use re-resolved at call time -- the request builder, both
    record_spend calls, record_triage's model argument, and the provenance
    writes inside write_batch/write_extraction. The settings cache TTL is 60s
    and a pass can run up to 40 minutes, so a settings-row edit mid-pass sent
    LATER calls in the same pass to a different model.

    The reviewer's probe: flipping the model to an unpriced one mid-pass
    produced 0 ledger rows, a $0 debit, and 3 items marked `failed`, because
    UnpricedModel was raised by record_spend AFTER the (already-paid-for) call.
    Batches of size 1 over 3 items so each of the three triage calls is a
    separate iteration of the model-half loop -- the loop the row edit must
    not be able to steer once it has started.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_TRIAGE_BATCH", 1)
    _seed(kb, 3)
    started_as = comprehend._triage_model()

    calls = []

    def fake_triage(req):
        calls.append(req)
        if len(calls) == 1:
            # An unpriced model: if the pass re-resolved after this point, the
            # NEXT record_spend call would raise UnpricedModel instead of
            # ledgering -- which is exactly the failure mode this test exists
            # to catch, not something it should tolerate by swallowing it.
            monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-opus-9")
        ids = [
            int(line.split("id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if line.startswith("- id=")
        ]
        return {
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": i, "material": False} for i in ids]},
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)

    tally = comprehend.run(kb)
    kb.commit()

    assert len(calls) == 3
    assert {req["model"] for req in calls} == {started_as}, (
        "every request must use the model resolved at pass start"
    )

    rows = kb.execute(
        "SELECT model FROM comprehend_spend WHERE stage = 'triage'"
    ).fetchall()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {started_as}
    assert tally.failed_triage == 0


@needs_db
def test_a_model_row_edit_inside_the_call_does_not_change_the_integration_ledger(
    kb, monkeypatch
):
    """I1 (integration side). Complements the triage test above: this flips
    INTEGRATE_MODEL from INSIDE the fake call_integration -- the earliest point
    a settings-row edit could land relative to run()'s single resolution -- and
    checks the ledger row still carries the model the request actually used,
    not whatever the row reads by the time record_spend runs. Also covers the
    deferred gap "the triage spend is ledgered with the right model through
    run()" on the integration side, since nothing before this pinned the
    integration ledger row's model end-to-end through run() with a value that
    could plausibly have drifted mid-call.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    kb.execute("INSERT INTO stories (name, scope) VALUES ('Ukraine talks', 'episodic')")
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, published_at) "
        "VALUES (%s, 'u', 'Ukraine talks resume', 'body', 'H1', now()) RETURNING id",
        (outlet_id,),
    ).fetchone()[0]
    kb.commit()

    started_as = comprehend._integrate_model()

    def fake_integrate(req):
        # Flip the settings row from inside the call, as if an operator's edit
        # landed between the request being built and the response coming back.
        monkeypatch.setattr(comprehend.common, "INTEGRATE_MODEL", "claude-opus-9")
        assert req["model"] == started_as
        return {
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": item_id,
                                "entities": [
                                    {
                                        "name": "Ukraine",
                                        "type": "country",
                                        "aliases": [],
                                    }
                                ],
                                "events": [
                                    {
                                        "summary": "Talks resumed",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.material == 1
    rows = kb.execute(
        "SELECT model FROM comprehend_spend WHERE stage = 'integration'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == started_as, (
        "the ledger row's model must equal the model the request actually used, "
        "not the row read after the mid-call edit"
    )
    # The provenance columns write the same frozen value (I1's other call sites).
    assert (
        kb.execute("SELECT extractor_model FROM entities").fetchone()[0] == started_as
    )
    assert kb.execute("SELECT extractor_model FROM events").fetchone()[0] == started_as
    assert (
        kb.execute("SELECT extractor_model FROM assertions").fetchone()[0] == started_as
    )


T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_a_first_pass_starts_with_one_days_allowance():
    s = comprehend.accrue(None, T0, 1.5, 3)
    assert s["balance_usd"] == pytest.approx(1.5)


def test_the_balance_accrues_continuously():
    s = comprehend.accrue(
        {"balance_usd": 0.0, "at": T0.isoformat()}, T0 + timedelta(hours=12), 1.5, 3
    )
    assert s["balance_usd"] == pytest.approx(0.75)


def test_the_balance_is_capped_at_max_days():
    s = comprehend.accrue(
        {"balance_usd": 4.0, "at": T0.isoformat()}, T0 + timedelta(days=10), 1.5, 3
    )
    assert s["balance_usd"] == pytest.approx(4.5)


def test_a_future_timestamp_never_accrues_negative():
    """Review Focus 2: a clock moved back must not drain the bucket."""
    s = comprehend.accrue(
        {"balance_usd": 1.0, "at": (T0 + timedelta(hours=5)).isoformat()}, T0, 1.5, 3
    )
    assert s["balance_usd"] == pytest.approx(1.0)


def test_a_corrupt_budget_row_is_reinitialised_not_fatal():
    """Review Focus 1: a hand-edited row must not crash every pass."""
    for junk in (
        {"balance_usd": "abc", "at": T0.isoformat()},
        {"balance_usd": 1.0, "at": "not a date"},
        {"balance_usd": 1.0, "at": "2026-09-30T00:00:00"},  # naive
        {"at": T0.isoformat()},
        "not a dict",
    ):
        s = comprehend.accrue(junk, T0, 1.5, 3)
        assert s["balance_usd"] == pytest.approx(1.5)


def _fake_triage_costing(calls):
    def fake(req):
        calls.append(req)
        ids = [
            int(line.split("id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if line.startswith("- id=")
        ]
        return {
            "stop_reason": "tool_use",
            # Sonnet: 1000 in + 100 out = $0.003
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": i, "material": False} for i in ids]},
                }
            ],
        }

    return fake


def _seed(kb, n):
    outlet = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Wire', 'wire') RETURNING id"
    ).fetchone()[0]
    for k in range(n):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
            "VALUES (%s, 'u', %s, %s, now())",
            (outlet, f"Trade talk {k}", f"H{k}"),
        )
    kb.commit()


@needs_db
def test_an_empty_bucket_makes_no_call(kb, monkeypatch, state_store):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    NOW_T = datetime.now(timezone.utc)
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.0,
        "at": NOW_T.isoformat(),
    }
    _seed(kb, 3)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    tally = comprehend.run(kb, now=NOW_T)

    assert calls == []
    assert tally.budget_exhausted is True
    assert state_store[comprehend.BUDGET_STATE_KEY]["exhausted_on"] == (
        NOW_T.date().isoformat()
    )


@needs_db
def test_a_pass_stops_when_the_balance_runs_out(kb, monkeypatch, state_store):
    """Worst-case overdraft is ONE call: $0.002 left, each call costs $0.003."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_TRIAGE_BATCH", 1)
    NOW_T = datetime.now(timezone.utc)
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.002,
        "at": NOW_T.isoformat(),
    }
    _seed(kb, 3)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    tally = comprehend.run(kb, now=NOW_T)

    assert len(calls) == 1
    assert tally.budget_exhausted is True
    assert state_store[comprehend.BUDGET_STATE_KEY]["balance_usd"] == pytest.approx(
        -0.001
    )


@needs_db
def test_a_disabled_pass_does_not_bank_budget(kb, monkeypatch, state_store):
    """Re-enabling after a pause accrues from the flip, not from the pause."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", False)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    state_store[comprehend.BUDGET_STATE_KEY] = {"balance_usd": 0.0, "at": long_ago}

    comprehend.run(kb)

    assert state_store[comprehend.BUDGET_STATE_KEY]["at"] != long_ago
    assert state_store[comprehend.BUDGET_STATE_KEY]["balance_usd"] == 0.0


@needs_db
def test_an_empty_bucket_skips_integration(kb, monkeypatch, state_store):
    """Controller Ruling R11: the brief pins the TRIAGE can_spend guard but not
    the INTEGRATION guard, which is the one that stops integration spending
    after triage has already emptied the bucket. A triaged-material item with
    nothing left in the bucket must never reach call_integration."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, published_at) "
        "VALUES (%s, 'u', 'Trade talks resume', 'body', 'H1', now()) RETURNING id",
        (outlet_id,),
    ).fetchone()[0]
    comprehend.record_triage(
        kb, item_id, "material", "tracked_story", None, comprehend.TRIAGE_PROMPT_VERSION
    )
    kb.commit()

    NOW_T = datetime.now(timezone.utc)
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.0,
        "at": NOW_T.isoformat(),
    }

    monkeypatch.setattr(
        comprehend,
        "call_integration",
        lambda req: pytest.fail("integrated on an empty bucket"),
    )

    tally = comprehend.run(kb, now=NOW_T)

    assert tally.budget_exhausted is True


def _seed_one(kb):
    """One item that clears the free rules and reaches the model, same title
    convention as `_seed` (both dodge `is_quote_page` and `triage_by_rules`)."""
    outlet = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Wire', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
        "VALUES (%s, 'u', 'Trade talk', 'H1', now())",
        (outlet,),
    )
    kb.commit()


@needs_db
def test_a_debit_is_persisted_not_just_held_in_memory(kb, monkeypatch):
    """R15 (reviewer finding): tests/conftest.py's `state_store` fixture
    fakes config.runtime_state/set_runtime_state with a dict that
    `Budget.debit` mutates and `store.update` then merges back into --
    aliasing that hides a DELETED `config.set_runtime_state(...)` call inside
    `debit`, since the in-memory object was already correct regardless. The
    real `runtime_state` table round-trips through JSON, so this test does
    NOT use state_store and reads the persisted row back through
    config.runtime_state() -- an aliasing bug cannot fake that.

    Must NOT exhaust the bucket: mark_exhausted persists the whole state too,
    so an exhausting pass would mask a debit-persist deletion just as well as
    state_store's aliasing does.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    NOW_T = datetime.now(timezone.utc)
    config.set_runtime_state(
        {comprehend.BUDGET_STATE_KEY: {"balance_usd": 1.0, "at": NOW_T.isoformat()}}
    )
    _seed_one(kb)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    comprehend.run(kb, now=NOW_T)

    state = config.runtime_state()[comprehend.BUDGET_STATE_KEY]
    assert len(calls) == 1
    assert state["balance_usd"] == pytest.approx(0.997)
    assert "exhausted_on" not in state


@needs_db
def test_exhaustion_is_persisted(kb, monkeypatch):
    """R15, the mark_exhausted half of the same finding."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    NOW_T = datetime.now(timezone.utc)
    config.set_runtime_state(
        {comprehend.BUDGET_STATE_KEY: {"balance_usd": 0.0, "at": NOW_T.isoformat()}}
    )
    _seed_one(kb)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    comprehend.run(kb, now=NOW_T)

    state = config.runtime_state()[comprehend.BUDGET_STATE_KEY]
    assert state["exhausted_on"] == NOW_T.date().isoformat()


def test_non_finite_budget_values_cannot_disable_the_guard(monkeypatch):
    """R16 (6): an operator-set inf/nan allowance, or a corrupted non-finite
    stored balance, must fall back to a finite default rather than making the
    bucket non-finite (an effectively unbounded budget)."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_DAILY_BUDGET_USD", float("inf"))
    allowance, _max_days = comprehend._allowance()
    assert math.isfinite(allowance)
    assert allowance == pytest.approx(
        common.KNOBS["COMPREHEND_DAILY_BUDGET_USD"].default
    )

    s = comprehend.accrue(
        {"balance_usd": float("nan"), "at": T0.isoformat()}, T0, 1.5, 3
    )
    assert s["balance_usd"] == pytest.approx(1.5)


def test_an_abort_names_its_reason_and_model():
    v = comprehend.abort_verdict(
        {
            comprehend.ABORT_STATE_KEY: {
                "reason": "unpriced_model:claude-sonnet-9",
                "at": T0.isoformat(),
            }
        }
    )
    key, message = v
    assert key == "abort:unpriced_model:claude-sonnet-9"
    assert "claude-sonnet-9" in message and "PRICES_PER_MTOK" in message


def test_a_billing_abort_says_to_top_up():
    _, message = comprehend.abort_verdict(
        {comprehend.ABORT_STATE_KEY: {"reason": "billing", "at": T0.isoformat()}}
    )
    assert "balance" in message.lower()


def test_no_abort_state_is_no_verdict():
    assert comprehend.abort_verdict({}) is None


def _exhausted_on(day):
    return {
        comprehend.BUDGET_STATE_KEY: {
            "balance_usd": 0.0,
            "at": T0.isoformat(),
            "exhausted_on": day,
        }
    }


@needs_db
def test_one_budget_key_per_utc_day(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_DAILY_BUDGET_USD", 1.5)
    today = T0.date().isoformat()
    first = comprehend.budget_verdict(kb, _exhausted_on(today), T0)
    later = comprehend.budget_verdict(kb, _exhausted_on(today), T0 + timedelta(hours=9))
    assert first[0] == later[0] == f"budget:{today}"
    assert "COMPREHEND_DAILY_BUDGET_USD" in first[1]
    assert "aged out" in first[1] and "stale" in first[1]


@needs_db
def test_yesterdays_exhaustion_is_no_verdict_today(kb):
    """The per-day key can never go permanently silent AND never fires more
    than daily: yesterday's episode is over at midnight UTC."""
    yesterday = (T0 - timedelta(days=1)).date().isoformat()
    assert comprehend.budget_verdict(kb, _exhausted_on(yesterday), T0) is None


@needs_db
def test_no_exhaustion_is_no_budget_verdict(kb):
    state = {comprehend.BUDGET_STATE_KEY: {"balance_usd": 1.0, "at": T0.isoformat()}}
    assert comprehend.budget_verdict(kb, state, T0) is None


@needs_db
def test_a_clean_pass_clears_a_previous_abort(kb, monkeypatch, state_store):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    state_store[comprehend.ABORT_STATE_KEY] = {"reason": "billing", "at": "x"}

    tally = comprehend.run(kb)  # nothing to do, nothing fails

    assert tally.aborted == ""
    assert comprehend.ABORT_STATE_KEY not in state_store


@needs_db
def test_the_budget_alert_fires_once_per_episode(kb, monkeypatch, state_store):
    import brief

    sent = []
    monkeypatch.setattr(brief, "telegram_alert", sent.append)
    today = datetime.now(timezone.utc).date().isoformat()
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.0,
        "at": T0.isoformat(),
        "exhausted_on": today,
    }

    for _ in range(3):
        brief.comprehend_budget_alert(kb)

    assert len(sent) == 1


@needs_db
def test_an_abort_persists_its_reason(kb, monkeypatch, state_store):
    """R14 (1): the reason must reach state_store through the real run() path,
    not just through _abort called directly -- an unpriced TRIAGE_MODEL is the
    cheapest way to force run() to abort before any network call."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-sonnet-9")

    comprehend.run(kb)

    assert (
        state_store[comprehend.ABORT_STATE_KEY]["reason"]
        == "unpriced_model:claude-sonnet-9"
    )


def test_the_abort_alert_fires_once_per_episode(monkeypatch, state_store):
    """R14 (2): the brief.comprehend_abort_alert() counterpart to
    test_the_budget_alert_fires_once_per_episode above."""
    import brief

    sent = []
    monkeypatch.setattr(brief, "telegram_alert", sent.append)
    state_store[comprehend.ABORT_STATE_KEY] = {
        "reason": "unpriced_model:claude-sonnet-9",
        "at": T0.isoformat(),
    }

    for _ in range(3):
        brief.comprehend_abort_alert()

    assert len(sent) == 1
    assert "claude-sonnet-9" in sent[0]


@needs_db
def test_stale_today_is_counted_on_the_utc_day_not_the_session_day(kb, monkeypatch):
    """R18 (fix round 1): `created_at >= %s::date` casts the bound in the
    SESSION TimeZone, which db.connect() never pins to UTC. Pacific/Kiritimati
    is UTC+14, so local midnight on `today` is 10:00 UTC the day BEFORE --
    wide enough to pull a row from yesterday-UTC into "today" under the old
    cast. `today` is derived from `now`, which every caller in this module
    already treats as UTC, so the boundary must be built the same way: a
    tz-aware UTC midnight, no cast.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_DAILY_BUDGET_USD", 1.5)
    kb.execute("SET TIME ZONE 'Pacific/Kiritimati'")

    outlet = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Wire', 'wire') RETURNING id"
    ).fetchone()[0]
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
        "VALUES (%s, 'u', 'Old trade talk', 'H1', now()) RETURNING id",
        (outlet,),
    ).fetchone()[0]
    # Yesterday-UTC at noon: unambiguously NOT today, whichever way the
    # session zone might shift the boundary.
    yesterday_utc_noon = T0 - timedelta(days=1)
    kb.execute(
        "INSERT INTO item_triage "
        "  (item_id, verdict, reason, triage_prompt_version, created_at) "
        "VALUES (%s, 'stale', 'stale', %s, %s)",
        (item_id, comprehend.TRIAGE_PROMPT_VERSION, yesterday_utc_noon),
    )
    kb.commit()

    today = T0.date().isoformat()  # the day AFTER yesterday_utc_noon's date
    state = {
        comprehend.BUDGET_STATE_KEY: {
            "balance_usd": 0.0,
            "at": T0.isoformat(),
            "exhausted_on": today,
        }
    }

    _, message = comprehend.budget_verdict(kb, state, T0)

    assert "0 went stale today" in message

"""Spend accounting and the budget (spec 2026-09-25 section 4.3)."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

import comprehend
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

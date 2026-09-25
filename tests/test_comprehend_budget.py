"""Spend accounting and the budget (spec 2026-09-25 section 4.3)."""

import logging

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

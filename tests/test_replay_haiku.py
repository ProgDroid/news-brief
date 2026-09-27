"""scripts/replay_haiku.py: M2, the Haiku-vs-Sonnet integration replay (y1x).

The replay spends real money once, on the host, and its verdict binds the
phase-2 route choice. So the parts that decide the verdict -- batch shape, the
as-of cut-off, entity births, the decision encoding, the agreement population,
the rule's boundaries, the spend guard, and the failure handling -- are pinned
here before it ever runs.
"""

import datetime as dt
import random
import re

import pytest
import requests

import comprehend
import db
from scripts import replay_haiku as rh
from scripts.probe_clustering import Pair

T0 = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


def _m(i, minutes, title=None):
    return {
        "id": i,
        "title": title or f"t{i}",
        "body": "",
        "outlet_id": 1,
        "created_at": T0 + dt.timedelta(minutes=minutes),
    }


# --- Batch shape.


def test_a_batch_is_the_anchor_and_the_four_captured_just_before_it():
    mats = [_m(i, 10 * i) for i in range(1, 9)]  # every 10 minutes
    got = rh.batch_around(mats, 6)  # anchor id 7
    assert [it["id"] for it in got] == [7, 6, 5, 4, 3]  # id DESC, nothing later


def test_a_batch_does_not_reach_past_the_real_time_window():
    mats = [_m(1, 0), _m(2, 10), _m(3, 200), _m(4, 210)]
    assert [it["id"] for it in rh.batch_around(mats, 3)] == [4, 3]


def test_pair_batches_anchor_on_the_LATER_item_and_remember_the_earlier():
    """The pair's later item by capture time, whatever the id order: the
    pre-registration weights toward the item that could MATCH the other."""
    mats = [_m(10, 60), _m(20, 0)] + [_m(i, 70 + i) for i in range(30, 40)]
    pair = Pair(10, 20, mats[0]["created_at"], mats[1]["created_at"])
    batches = rh.select_batches(mats, [pair], random.Random(1))
    pairs = [b for b in batches if b.stratum == "pair"]
    assert [(b.anchor_id, b.earlier_id) for b in pairs] == [(10, 20)]
    assert all(b.anchor_id != 10 for b in batches if b.stratum == "random")


def test_the_cutoff_is_the_EARLIEST_member_not_the_anchor():
    """Review B1: members are integrated together in real time, so nothing any
    of them produced may be offered to the batch."""
    mats = [_m(i, 10 * i) for i in range(1, 6)]
    b = rh.Batch("random", 5, rh.batch_around(mats, 4))
    assert b.cutoff == T0 + dt.timedelta(minutes=10)


def test_batch_selection_is_reproducible_for_a_seed_and_a_pair_order():
    """The dry run and the paid run must select the SAME batches, even when
    the pairs arrive in a different order (no ORDER BY upstream; review m5)."""
    mats = [_m(i, 5 * i) for i in range(1, 200)]
    pairs = [
        Pair(i, i + 1, mats[i - 1]["created_at"], mats[i]["created_at"])
        for i in range(1, 150, 3)
    ]
    # Item 50 is the later item of two pairs, so its partner is order-sensitive.
    pairs.append(Pair(40, 50, mats[39]["created_at"], mats[49]["created_at"]))
    one = rh.select_batches(mats, pairs, random.Random(rh.SEED))
    two = rh.select_batches(mats, list(reversed(pairs)), random.Random(rh.SEED))
    assert [(b.stratum, b.anchor_id, b.earlier_id) for b in one] == [
        (b.stratum, b.anchor_id, b.earlier_id) for b in two
    ]
    assert sum(b.stratum == "random" for b in one) == rh.RANDOM_BATCHES


# --- The decision encoding and the agreement population.


def _x(item, *events):
    return {"item_id": item, "events": list(events)}


def test_an_in_request_link_is_the_same_cluster_whichever_item_declares():
    """Review m3: A has item 5 declare and item 4 reference; H the reverse.
    Same partition, so every item must agree."""
    a = rh.decisions(
        [_x(5, {"summary": "x", "new_label": "NEW1"}), _x(4, {"new_ref": "NEW1"})]
    )
    h = rh.decisions(
        [_x(4, {"summary": "x", "new_label": "NEW3"}), _x(5, {"new_ref": "NEW3"})]
    )
    assert (
        a
        == h
        == {4: frozenset({("CLUSTER", frozenset({4, 5}))})}
        | {5: frozenset({("CLUSTER", frozenset({4, 5}))})}
    )


def test_a_label_declared_on_an_existing_event_resolves_to_that_event():
    via_label = rh.decisions(
        [
            _x(5, {"candidate_id": 99, "new_label": "NEW1"}),
            _x(4, {"new_ref": "NEW1"}),
        ]
    )
    direct = rh.decisions([_x(5, {"candidate_id": 99}), _x(4, {"candidate_id": 99})])
    assert (
        via_label
        == direct
        == {
            5: frozenset({("EVT", 99)}),
            4: frozenset({("EVT", 99)}),
        }
    )


def test_new_events_nobody_links_to_are_the_empty_decision():
    got = rh.decisions(
        [_x(3, {"summary": "y"}), _x(2, {"summary": "z", "new_label": "NEW1"})]
    )
    assert got == {3: frozenset(), 2: frozenset()}


def _res(decs):
    return rh.RunResult(kept=set(decs), decisions=decs)


def test_agreement_counts_only_items_all_three_runs_kept():
    """One denominator for A-A' and A-H (the operator's choice). An item one
    run dropped is a DROP, measured separately, never a disagreement."""
    e = frozenset()
    m = frozenset({("EVT", 1)})
    results = {
        "A": _res({1: m, 2: e, 3: m}),
        "A'": _res({1: m, 2: e, 3: m}),
        "H": _res({1: e, 2: e}),  # dropped item 3
    }
    rows = rh.agreement_rows(results, [1, 2, 3])
    assert rows == [(True, False, True), (True, True, False)]


# --- The rule, at its boundaries.


@pytest.mark.parametrize(
    ("aa", "ah", "da", "dh", "want"),
    [
        (0.85, 0.80, 0.02, 0.07, "HAIKU QUALIFIES"),  # both exactly at 5 pts
        (0.85, 0.79, 0.02, 0.02, "agreement"),
        (0.85, 0.85, 0.02, 0.0701, "validation drop"),
        (0.85, 0.70, 0.02, 0.20, "agreement and validation drop"),
    ],
)
def test_the_rule_is_five_points_on_both_clauses(aa, ah, da, dh, want):
    got = rh.decide(aa, ah, da, dh, n_batches=rh.MIN_BATCHES)
    if want == "HAIKU QUALIFIES":
        assert got == want
    else:
        assert got == "HAIKU DOES NOT QUALIFY: " + want


def test_too_few_complete_batches_render_no_verdict():
    got = rh.decide(0.9, 0.9, 0.0, 0.0, n_batches=rh.MIN_BATCHES - 1)
    assert got.startswith("NOT MEASURABLE")


def test_the_reserve_covers_every_attempt_of_every_run():
    req = comprehend.build_integration_request(
        [{"id": 1, "title": "t", "body": "b"}], [], [], model="claude-sonnet-5"
    )
    models = {"A": "claude-sonnet-5", "A'": "claude-sonnet-5", "H": rh.HAIKU}
    one_each = sum(rh.worst_case_usd(models[r], req) for r in rh.RUNS)
    assert rh.batch_reserve(models, req) == pytest.approx(rh.ATTEMPTS * one_each)
    assert rh.ATTEMPTS == 2


# --- Entity hits and the as-of index.


def test_the_index_offers_only_entities_born_before_the_cutoff():
    forms = {1: ["Moldova"], 2: ["Moldova Gas"]}
    b = rh.Batch("random", 9, [_m(9, 0, "Moldova Gas deal")])
    hits = {9: {1, 2}}
    births = {1: T0 - dt.timedelta(days=1), 2: T0}  # 2 is born AT the cut-off
    got = rh.index_for(b, hits, births, forms)
    assert {sf.entity_id for sf in got.match("Moldova Gas deal")} == {1}


def test_narrowing_agrees_with_the_full_index():
    SF = comprehend.SurfaceForm
    full = comprehend.SurfaceIndex(
        [SF("Moldova", "tracked_entity", 1), SF("New York", "tracked_entity", 2)]
    )
    b = rh.Batch("random", 9, [_m(9, 0, "Moldova talks in New York")])
    by_token = rh.forms_by_token(full)
    assert rh.item_hits(by_token, b.items[0]) == {1, 2}
    assert rh.narrowing_disagreements(full, by_token, [b]) == 0


# --- Failure handling in one call.


def _http_error(status, text=""):
    resp = requests.Response()
    resp.status_code = status
    resp._content = text.encode()
    return requests.HTTPError(f"{status}", response=resp)


def _ok(rows):
    return {
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        "content": [
            {"type": "tool_use", "name": "emit_extraction", "input": {"items": rows}}
        ],
    }


GOOD_ROW = {
    "item_id": 1,
    "entities": [{"name": "Moldova", "type": "country", "aliases": []}],
    "events": [
        {
            "summary": "Border checks tightened",
            "type": "action",
            "commitment_state": "in_force",
            "standing": "reported",
        }
    ],
}


def _call(monkeypatch, *outcomes):
    seq = list(outcomes)
    calls = []

    def fake(request):
        calls.append(request["model"])
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(comprehend, "call_integration", fake)
    return calls


def test_a_refused_request_aborts_the_run(monkeypatch):
    """A 400 on Haiku's request shape would fail every later Haiku call the
    same way; recording it as a drop would score Haiku 100% dropped."""
    _call(monkeypatch, _http_error(400, "thinking not supported"))
    with pytest.raises(rh.Abort, match="thinking not supported"):
        rh.call_one({"model": "x"}, rh.HAIKU, {1}, {}, {})


def test_an_account_failure_aborts_the_run(monkeypatch):
    _call(monkeypatch, _http_error(402))
    with pytest.raises(rh.Abort, match="account"):
        rh.call_one({"model": "x"}, rh.HAIKU, {1}, {}, {})


def test_a_transient_fault_is_retried_once_then_recorded(monkeypatch):
    calls = _call(monkeypatch, _http_error(529), _ok([GOOD_ROW]))
    got = rh.call_one({"model": "x"}, rh.HAIKU, {1}, {}, {})
    assert got.kept == {1} and got.transport_error is None
    assert got.failed_attempts == 1
    assert calls == [rh.HAIKU, rh.HAIKU]

    _call(monkeypatch, _http_error(529), _http_error(529))
    got = rh.call_one({"model": "x"}, rh.HAIKU, {1}, {}, {})
    assert got.transport_error is not None and got.kept == set()
    assert got.failed_attempts == 2


def test_an_unparseable_response_is_a_whole_batch_drop_that_still_costs(monkeypatch):
    truncated = _ok([GOOD_ROW]) | {"stop_reason": "max_tokens"}
    _call(monkeypatch, truncated)
    got = rh.call_one({"model": "x"}, rh.HAIKU, {1}, {}, {})
    assert got.parse_error is not None
    assert got.kept == set()
    assert got.usd > 0


def test_an_abort_still_counts_the_siblings_that_finished(monkeypatch):
    """Review m1: the executor waits for every call, and they bill, so an
    abort in one run must not discard the others' spend."""

    def fake(request):
        if request["model"] == rh.HAIKU:
            raise _http_error(400, "no")
        return _ok([GOOD_ROW])

    monkeypatch.setattr(comprehend, "call_integration", fake)
    models = {"A": "claude-sonnet-5", "A'": "claude-sonnet-5", "H": rh.HAIKU}
    results, abort = rh.run_batch({"model": "x"}, models, {1}, {}, {})
    assert abort is not None
    assert set(results) == {"A", "A'"}
    assert all(results[r].usd > 0 for r in ("A", "A'"))


# --- Against a real KB, with the model faked.


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def _outlet(kb, name):
    return kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


_seq = iter(range(10**9))


def _item(kb, outlet, minutes, title, material=True):
    n = next(_seq)
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (outlet, f"u{n}", title, f"h{n}", T0 + dt.timedelta(minutes=minutes)),
    ).fetchone()[0]
    if material:
        kb.execute(
            "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version)"
            " VALUES (%s, 'material', 'topical', %s)",
            (iid, comprehend.TRIAGE_PROMPT_VERSION),
        )
    return iid


def _entity(kb, name, type_="country"):
    return kb.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id", (name, type_)
    ).fetchone()[0]


def _event(kb, summary, created_minutes, entity_ids, item_ids):
    ev = kb.execute(
        "INSERT INTO events (summary, type, commitment_state, occurred_at, created_at)"
        " VALUES (%s, 'action', 'in_force', %s, %s) RETURNING id",
        (summary, T0, T0 + dt.timedelta(minutes=created_minutes)),
    ).fetchone()[0]
    for e in entity_ids:
        kb.execute(
            "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)", (ev, e)
        )
    for i in item_ids:
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'reported')",
            (i, ev),
        )
    return ev


@needs_db
def test_an_entity_is_born_with_the_first_item_that_NAMES_it(kb):
    """Review M1. X was tagged onto an OLDER event by a later matching item.
    Dating X from every item asserting that event would put its birth at the
    first one, which never mentioned X and could not have created it."""
    o = _outlet(kb, "Reuters")
    i1 = _item(kb, o, -60, "Summit opens")
    i2 = _item(kb, o, -10, "Summit: Xanadu joins the talks")
    x = _entity(kb, "Xanadu")
    lonely = _entity(kb, "Nowhere")
    _event(kb, "Summit", -55, [x, lonely], [i1, i2])
    kb.commit()
    births = rh.naming_births(kb, {x, lonely}, {x: ["Xanadu"], lonely: ["Nowhere"]})
    assert births[x] == T0 - dt.timedelta(minutes=10)
    created = kb.execute(
        "SELECT created_at FROM entities WHERE id = %s", (lonely,)
    ).fetchone()[0]
    assert births[lonely] == created  # named by nothing: its own row's time


def _seed_kb(kb):
    """A pair 30 minutes apart (items[3], items[4]) whose shared event
    "Border talks" was integrated at -20, BEFORE the pair's later item was
    captured, but AFTER the batch's earliest member. "Europe" is old: born
    with an item at -300 that created "Old summit". A late item at +100 sits
    alone in its batch."""
    reuters, ap = _outlet(kb, "Reuters"), _outlet(kb, "AP")
    old = _item(kb, reuters, -300, "Europe summit", material=False)
    items = [
        _item(kb, reuters, -50, "Chile election results"),
        _item(kb, ap, -40, "Oil prices steady"),
        _item(kb, reuters, -35, "Rates held"),
        _item(kb, reuters, -30, "Moldova border talks with Europe"),
        _item(kb, ap, 0, "Moldova border talks resume"),
        _item(kb, ap, 100, "Moldova gas deal with Europe"),
    ]
    europe = _entity(kb, "Europe", "institution")
    moldova = _entity(kb, "Moldova")
    _event(kb, "Old summit", -300, [europe], [old])
    _event(kb, "Border talks", -20, [moldova, europe], items[3:5])
    kb.commit()
    return items


def _counts(kb):
    return kb.execute(
        "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM assertions), "
        "(SELECT count(*) FROM entities), (SELECT count(*) FROM comprehend_spend), "
        "(SELECT count(*) FROM item_triage WHERE integrated_at IS NOT NULL)"
    ).fetchone()


def _faker(seen):
    def fake(request):
        seen.append(request)
        content = request["messages"][0]["content"]
        offered = [int(x) for x in re.findall(r"item_id=(\d+)", content)]
        rows = [dict(GOOD_ROW, item_id=i) for i in offered]
        if request["model"] == rh.HAIKU:
            rows = rows[1:]  # Haiku drops the first-listed item
        return _ok(rows)

    return fake


@needs_db
def test_the_replay_offers_nothing_a_batch_member_produced_and_writes_nothing(
    kb, monkeypatch, capsys
):
    """Review B1, both halves, end to end. The pair batch (anchored at 0,
    earliest member at -50) must NOT be offered Moldova (born at -30 with a
    member) nor "Border talks" (created at -20 by a member's integration),
    but IS offered the old Europe entity and its old event. The lone +100
    batch is offered both, since everything it sees predates it."""
    items = _seed_kb(kb)
    before = _counts(kb)
    monkeypatch.setattr(comprehend, "_integrate_model", lambda: "claude-sonnet-5")
    monkeypatch.setattr(rh, "MIN_BATCHES", 1)
    seen = []
    monkeypatch.setattr(comprehend, "call_integration", _faker(seen))
    assert rh.main([]) == 0
    out = capsys.readouterr().out
    assert _counts(kb) == before

    by_anchor = {}
    for r in seen:
        ids = [
            int(x) for x in re.findall(r"item_id=(\d+)", r["messages"][0]["content"])
        ]
        by_anchor.setdefault(max(ids), []).append(r)
    for trio in by_anchor.values():
        assert sorted(r["model"] for r in trio) == sorted(
            ["claude-sonnet-5", "claude-sonnet-5", rh.HAIKU]
        )
        bodies = {repr({k: v for k, v in r.items() if k != "model"}) for r in trio}
        assert len(bodies) == 1

    pair = by_anchor[items[4]][0]["messages"][0]["content"]
    assert "Moldova (country)" not in pair
    assert "Border talks" not in pair
    assert "Europe (institution)" in pair
    assert "Old summit" in pair

    late = by_anchor[items[5]][0]["messages"][0]["content"]
    assert "Moldova (country)" in late
    assert "Border talks" in late

    assert "Narrowing control: 0 of" in out
    assert "VERDICT: HAIKU DOES NOT QUALIFY: validation drop" in out
    assert out.rstrip().endswith("REPLAY COMPLETE")


@needs_db
def test_the_run_stops_before_a_batch_that_could_breach_the_cap(
    kb, monkeypatch, capsys
):
    _seed_kb(kb)
    monkeypatch.setattr(comprehend, "_integrate_model", lambda: "claude-sonnet-5")
    monkeypatch.setattr(rh, "batch_reserve", lambda models, request: 1.0)
    monkeypatch.setattr(rh, "SPEND_CAP_USD", 1.01)
    seen = []
    monkeypatch.setattr(comprehend, "call_integration", _faker(seen))
    assert rh.main([]) == 0
    out = capsys.readouterr().out
    assert len(seen) == 3  # one batch fits; the second's reserve does not
    assert "STOPPED EARLY: spend cap" in out


@needs_db
def test_the_dry_run_calls_no_model(kb, monkeypatch, capsys):
    _seed_kb(kb)
    monkeypatch.setattr(comprehend, "_integrate_model", lambda: "claude-sonnet-5")

    def forbidden(request):
        raise AssertionError("the dry run called a model")

    monkeypatch.setattr(comprehend, "call_integration", forbidden)
    assert rh.main(["--dry-run"]) == 0
    assert "DRY RUN: no model was called." in capsys.readouterr().out


@needs_db
def test_a_non_sonnet_integration_model_is_refused(kb, monkeypatch, capsys):
    monkeypatch.setattr(comprehend, "_integrate_model", lambda: rh.HAIKU)
    assert rh.main(["--dry-run"]) == 2
    assert "REFUSED" in capsys.readouterr().out


@needs_db
def test_failed_attempts_count_against_the_cap_at_their_worst_case(
    kb, monkeypatch, capsys
):
    """Review m1: a timed-out attempt may still have been generated and
    billed. Its cost is unknown, so the cap must assume the worst."""
    _seed_kb(kb)
    monkeypatch.setattr(comprehend, "_integrate_model", lambda: "claude-sonnet-5")
    monkeypatch.setattr(rh, "batch_reserve", lambda models, request: 0.5)
    monkeypatch.setattr(rh, "worst_case_usd", lambda model, request: 0.2)
    monkeypatch.setattr(rh, "SPEND_CAP_USD", 1.0)
    calls = []

    def flaky(request, model, offered, ent_labels, ev_labels):
        # call_one's own retry counting is tested above; this pins that the
        # LOOP charges what it reports.
        calls.append(model)
        return rh.RunResult(
            kept=set(offered),
            decisions={i: frozenset() for i in offered},
            usd=0.001,
            failed_attempts=1,
        )

    monkeypatch.setattr(rh, "call_one", flaky)
    assert rh.main([]) == 0
    out = capsys.readouterr().out
    # Batch 1: three runs, each failing once then succeeding. That leaves
    # $0.60 unknown, so batch 2's $0.50 reserve no longer fits under $1.00.
    assert len(calls) == 3
    assert "STOPPED EARLY: spend cap" in out
    assert "$0.60 unknown" in out

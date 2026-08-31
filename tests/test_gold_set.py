"""Gold-set fixture integrity and scorer arithmetic.

Everything here runs offline. The scoring run itself needs live Haiku calls and is
`scripts/score_gold_set.py`, run by hand — CI has no ANTHROPIC_API_KEY. What CI can
still guarantee is that the fixture stays well-formed and that the scorer counts
correctly, so a green suite never certifies a gate that would have miscounted.
"""

import json

import pytest

import brief_memory
from scripts.score_gold_set import (
    CLASSIFIABLE,
    GOLD_SET_PATH,
    VALID_LABELS,
    VARIANCE_FIELDS,
    admission_probe,
    build_admission_brief,
    build_probe_brief,
    build_probe_ledger,
    choose_call,
    field_variance,
    load_gold_set,
    main,
    probe,
    score,
    score_admission,
    sdk_call,
)

DOC = load_gold_set()
ITEMS = DOC["items"]

# replay.py truncated claim/broken_by to exactly 150 chars when it wrote
# replay_events.json; the fixture was built from the untruncated checkpoint instead.
# Length alone is not the signature — one row is genuinely 150 chars — so a re-seed
# from the wrong file is caught by a 150-char field that also stops mid-sentence.
_TRUNCATION_LENGTH = 150
_SENTENCE_END = ".!?\"'"


def test_fixture_has_items_and_meta():
    assert ITEMS, "gold set is empty"
    meta = DOC["meta"]
    for key in ("labels", "baseline", "gate", "seed_extractor_model", "labelled_on"):
        assert meta.get(key), f"meta.{key} missing"


def test_ids_are_unique_and_sequential():
    ids = [i["id"] for i in ITEMS]
    assert len(set(ids)) == len(ids), "duplicate gold-set ids"
    assert ids == [f"gs-{n:02d}" for n in range(1, len(ITEMS) + 1)]


@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_item_schema(item):
    for key in (
        "replay_id",
        "first_seen",
        "resolved_on",
        "claim",
        "broken_by",
        "rationale",
    ):
        assert str(item.get(key) or "").strip(), f"{item['id']}: {key} empty"
    assert item["label"] in VALID_LABELS
    assert isinstance(item["admissible"], bool)
    # A label without a stated reason cannot be argued with, which is the whole
    # point of committing a single-annotator set.
    assert len(item["rationale"]) > 40, f"{item['id']}: rationale too thin to review"


@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_failure_mode_present_exactly_for_false_breaks(item):
    if item["label"] == "false_break":
        assert item.get("failure_mode"), (
            f"{item['id']}: false_break needs a failure_mode"
        )
    else:
        assert "failure_mode" not in item, (
            f"{item['id']}: failure_mode only belongs on false_break"
        )


@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_text_is_not_the_truncated_copy(item):
    for field in ("claim", "broken_by"):
        text = item[field]
        cut_short = len(text) == _TRUNCATION_LENGTH and text[-1] not in _SENTENCE_END
        assert not cut_short, (
            f"{item['id']}: {field} looks re-seeded from replay_events.json"
        )


def test_no_vendor_derived_values_in_a_public_repo():
    """The enrichment integration is scoped to a derived-only snapshot and this repo
    is public, so no Bigdata.com-derived value may be committed in the fixture. One
    seed row was excluded on this ground; the exclusion is recorded in meta."""
    blob = json.dumps(ITEMS).lower()
    assert "bigdata" not in blob
    assert DOC["meta"]["excluded_rows"], "the excluded row must stay documented"


def test_both_classes_are_represented():
    labels = {i["label"] for i in ITEMS}
    assert set(CLASSIFIABLE) <= labels, (
        "a set with only one class cannot measure precision"
    )


def test_baseline_is_the_seed_detectors_own_score():
    """Every row is here because the seed detector called it broken, so the baseline
    is the gold positive rate with recall 1.0 — computed, never asserted."""
    results = [
        {"id": i["id"], "label": i["label"], "predicted": "broken", "error": None}
        for i in ITEMS
    ]
    s = score(results)
    assert s["precision"] == pytest.approx(s["baseline_precision"])
    assert s["recall"] == 1.0 == s["baseline_recall"]
    assert s["lost"] == 0
    assert s["converted"] == 0


def _r(label, predicted, error=None):
    return {"id": "x", "label": label, "predicted": predicted, "error": error}


def test_score_counts_each_cell():
    s = score(
        [
            _r("true_break", "broken"),
            _r("true_break", "standing"),
            _r("false_break", "broken"),
            _r("false_break", "challenged"),
            _r("false_break", "standing"),
        ]
    )
    assert (s["tp"], s["fn"], s["fp"], s["tn"]) == (1, 1, 1, 2)
    assert s["precision"] == pytest.approx(0.5)
    assert s["recall"] == pytest.approx(0.5)
    assert s["converted"] == 2
    assert s["lost"] == 1


def test_challenged_is_not_a_break():
    """Only "broken" is a positive prediction; "challenged" is a correct rejection
    of a false break, not a hedged hit."""
    s = score([_r("false_break", "challenged"), _r("true_break", "challenged")])
    assert s["fp"] == 0
    assert s["tn"] == 1
    assert s["fn"] == 1


def test_unclear_rows_are_excluded_from_the_denominator():
    s = score(
        [_r("true_break", "broken"), _r("unclear", "broken"), _r("unclear", "standing")]
    )
    assert s["n_unclear"] == 2
    assert s["n_scored"] == 1
    assert s["precision"] == 1.0


def test_errors_are_excluded_rather_than_scored_as_rejections():
    """An item that errored is UNKNOWN. Counting it as "not broken" would credit the
    prompt with a correct rejection it never made."""
    s = score([_r("false_break", None, "HTTPError: 500"), _r("true_break", "broken")])
    assert s["n_errors"] == 1
    assert s["tn"] == 0
    assert s["n_scored"] == 1
    assert s["precision"] == 1.0


def test_precision_is_none_when_nothing_was_predicted_broken():
    s = score([_r("false_break", "standing"), _r("true_break", "standing")])
    assert s["precision"] is None
    assert s["recall"] == 0.0


def test_field_variance_flags_a_uniform_field():
    rows = [{"severity": "high", "status": "broken"} for _ in range(5)]
    v = field_variance(rows)
    assert v["severity"]["degenerate"] is True
    assert v["severity"]["counts"] == {"high": 5}
    assert v["origin"]["absent"] is True
    assert v["origin"]["degenerate"] is False


def test_field_variance_accepts_a_varied_field():
    rows = [{"severity": s} for s in ("high", "normal", "low", "normal")]
    v = field_variance(rows)
    assert v["severity"]["distinct"] == 3
    assert v["severity"]["degenerate"] is False


def test_single_row_is_not_degenerate():
    """One observation cannot show a field is uniform."""
    assert field_variance([{"severity": "high"}])["severity"]["degenerate"] is False


def test_probe_prompt_carries_both_texts_and_the_live_template():
    item = ITEMS[0]
    prompt = build_probe_brief(item)
    assert item["broken_by"] in prompt
    ledger = build_probe_ledger(item)
    assert ledger["claims"][0]["claim"] == item["claim"]
    assert ledger["claims"][0]["status"] == "standing"


def test_probe_reads_the_status_the_model_returned():
    def fake_call(system, user):
        assert "TODAY'S BRIEF" in user, (
            "probe must go through the live reconcile template"
        )
        return json.dumps(
            [
                {
                    "id": "c-0001",
                    "claim": "x",
                    "status": "broken",
                    "broken_by": "y",
                    "severity": "high",
                }
            ]
        )

    r = probe(ITEMS[0], call=fake_call)
    assert r["predicted"] == "broken"
    assert r["error"] is None
    assert r["row"]["severity"] == "high"


def test_probe_defaults_a_missing_status_to_standing():
    r = probe(ITEMS[0], call=lambda s, u: json.dumps([{"id": "c-0001", "claim": "x"}]))
    assert r["predicted"] == "standing"


def test_probe_reports_a_dropped_claim_as_an_error_not_a_rejection():
    r = probe(ITEMS[0], call=lambda s, u: json.dumps([{"claim": "something else"}]))
    assert r["predicted"] is None
    assert "not returned" in r["error"]


def test_probe_captures_a_failed_call():
    def boom(system, user):
        raise RuntimeError("connection reset")

    r = probe(ITEMS[0], call=boom)
    assert r["predicted"] is None
    assert "connection reset" in r["error"]


def test_auto_transport_prefers_the_production_rest_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    assert choose_call("auto") is brief_memory._messages_call
    assert choose_call("rest") is brief_memory._messages_call


def test_auto_transport_falls_back_to_the_sdk_without_a_key(monkeypatch):
    """This box keeps no key on disk and authenticates through an `ant auth login`
    profile, which only the SDK client resolves."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert choose_call("auto") is sdk_call
    assert choose_call("sdk") is sdk_call


def test_unknown_transport_is_rejected():
    with pytest.raises(ValueError, match="unknown transport"):
        choose_call("carrier-pigeon")


def test_load_gold_set_rejects_an_empty_file(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"meta": {}, "items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no items"):
        load_gold_set(p)


def test_gold_set_path_points_at_the_committed_fixture():
    assert GOLD_SET_PATH.exists()
    assert GOLD_SET_PATH.name == "gold_set_breaks.json"


# ── Admission mode (news-brief-93u) ───────────────────────────────────────────
# The break scorer asks "is this contradiction a break?". Admission asks the
# prior question: should this row have entered the ledger at all? Six fixture
# rows are labelled admissible=false, and the first run showed the single
# surviving false positive and two of the four lost breaks all sit on them.
def _stub(payload):
    def call(system, user):
        return json.dumps(payload)

    return call


def test_admission_brief_carries_the_claim_text():
    b = build_admission_brief(ITEMS[0])
    assert ITEMS[0]["claim"] in b


def test_an_observation_label_is_scored_as_rejected():
    r = admission_probe(ITEMS[0], call=_stub([{"claim": "x", "kind": "observation"}]))
    assert r["predicted"] == "rejected"
    assert r["reason"] == "observation"


def test_a_claim_label_is_scored_as_admitted():
    r = admission_probe(ITEMS[0], call=_stub([{"claim": "x", "kind": "claim"}]))
    assert r["predicted"] == "admitted"


def test_an_unlabelled_row_is_scored_as_admitted():
    """merge_ledger fails open on a missing `kind`, and the scorer has to report
    what the guard actually does, not what it was meant to do."""
    r = admission_probe(ITEMS[0], call=_stub([{"claim": "x"}]))
    assert r["predicted"] == "admitted"


def test_emitting_nothing_is_rejected_but_recorded_separately():
    """The row does not enter the ledger either way, so it scores as a rejection
    — but 'the model never proposed it' is a different mechanism from 'the guard
    caught it', and a guard credited for extraction misses is unfalsifiable."""
    r = admission_probe(ITEMS[0], call=_stub([]))
    assert r["predicted"] == "rejected"
    assert r["reason"] == "not_emitted"


def test_admission_probe_captures_a_failed_call():
    def boom(system, user):
        raise RuntimeError("connection reset")

    r = admission_probe(ITEMS[0], call=boom)
    assert r["predicted"] is None
    assert "connection reset" in r["error"]


def _adm(admissible, predicted):
    return {"id": "x", "admissible": admissible, "predicted": predicted, "error": None}


def test_score_admission_counts_the_guard_not_the_break():
    results = [
        _adm(False, "rejected"),  # tp - junk kept out
        _adm(False, "admitted"),  # fn - junk let through
        _adm(True, "rejected"),  # fp - a good claim lost
        _adm(True, "admitted"),  # tn
        _adm(True, "admitted"),
    ]
    s = score_admission(results)
    assert (s["tp"], s["fp"], s["fn"], s["tn"]) == (1, 1, 1, 2)
    assert s["precision"] == 0.5
    assert s["recall"] == 0.5


def test_score_admission_excludes_errored_rows():
    s = score_admission([_adm(False, "rejected"), _adm(True, None)])
    assert s["n_scored"] == 1


def test_score_admission_reports_the_baseline_as_admitting_everything():
    """Before the guard nothing was ever rejected, so its recall is 0 by
    construction and its junk share is just the fixture's inadmissible rate."""
    s = score_admission([_adm(False, "rejected"), _adm(True, "admitted")])
    assert s["baseline_recall"] == 0.0
    assert s["junk_share_before"] == 0.5


def test_junk_share_after_is_measured_over_what_still_gets_in():
    """The number that matters operationally: of the rows that still enter the
    ledger, what share should never have been there."""
    s = score_admission(
        [_adm(False, "rejected"), _adm(False, "admitted"), _adm(True, "admitted")]
    )
    assert s["junk_share_before"] == pytest.approx(2 / 3)
    assert s["junk_share_after"] == 0.5


def test_kind_is_a_variance_field():
    """A guard driven by a field that comes back uniform is not a guard."""
    assert "kind" in VARIANCE_FIELDS
    v = field_variance([{"kind": "claim"}, {"kind": "claim"}], fields=("kind",))
    assert v["kind"]["degenerate"] is True


def test_admission_dry_run_needs_no_api_key(capsys):
    assert main(["--mode", "admission", "--dry-run", "--limit", "1"]) == 0
    assert ITEMS[0]["claim"] in capsys.readouterr().out


def test_a_split_reply_records_both_outcomes():
    """The model may answer a price-anchored claim with TWO rows: the level as an
    observation and the thesis as a claim. That is the rubric working as written
    ('a fact may cite a level; it must not BE one'), and collapsing it to a bare
    'admitted' hides the guard firing. Splits are reported, not scored."""
    r = admission_probe(
        ITEMS[0],
        call=_stub(
            [
                {"claim": "Brent traded near $90", "kind": "observation"},
                {"claim": "the two narratives cannot both hold", "kind": "claim"},
            ]
        ),
    )
    assert r["predicted"] == "split"
    assert (r["n_rows"], r["n_admitted"]) == (2, 1)


def test_probe_records_every_kind_the_model_used():
    r = admission_probe(
        ITEMS[0],
        call=_stub([{"claim": "a", "kind": "observation"}, {"claim": "b"}]),
    )
    assert r["kinds"] == ["observation", None]


def test_score_admission_excludes_splits_but_counts_them():
    results = [_adm(False, "rejected"), _adm(False, "split"), _adm(True, "admitted")]
    s = score_admission(results)
    assert s["n_split"] == 1
    assert s["n_scored"] == 2
    assert s["recall"] == 1.0


# ── jx9.9: the break probe must also report the LEDGER's verdict ──────────────
# `predicted` stays the model's raw label so the 2026-08-29 and v4 runs remain
# comparable. The guard lives in merge_ledger and never ran on this path, so it
# was unmeasurable here; `ledger_status` and `guard_fired` are what measure it.
def test_probe_reports_the_ledger_verdict_alongside_the_model_label():
    r = probe(
        ITEMS[8],  # gs-09: "Colombia holds 6 pts, Portugal 4 pts"
        call=_stub([{"id": "c-0001", "claim": "both teams hold 4 pts"}]),
    )
    assert r["predicted"] == "standing"  # what the model said
    assert r["ledger_status"] == "challenged"  # what the ledger recorded
    assert r["guard_fired"] is True


def test_a_plain_reaffirmation_does_not_fire_the_guard():
    item = ITEMS[8]
    r = probe(item, call=_stub([{"id": "c-0001", "claim": item["claim"]}]))
    assert r["guard_fired"] is False
    assert r["ledger_status"] == "standing"


def test_an_explicit_break_is_not_counted_as_a_guard_fire():
    """The guard only catches contradictions the model declined to mark."""
    r = probe(
        ITEMS[8],
        call=_stub(
            [{"id": "c-0001", "claim": "both teams hold 4 pts", "status": "broken"}]
        ),
    )
    assert r["guard_fired"] is False
    assert r["ledger_status"] == "broken"


def test_score_counts_guard_fires():
    results = [
        {
            "id": "a",
            "label": "true_break",
            "predicted": "standing",
            "error": None,
            "guard_fired": True,
        },
        {
            "id": "b",
            "label": "false_break",
            "predicted": "standing",
            "error": None,
            "guard_fired": False,
        },
    ]
    assert score(results)["n_guard_fired"] == 1

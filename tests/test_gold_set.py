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
    build_probe_brief,
    build_probe_ledger,
    choose_call,
    field_variance,
    load_gold_set,
    probe,
    score,
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

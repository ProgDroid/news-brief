"""The judge request and the code-side validation of its answer (news-brief-8lp)."""

from watch import (
    MAX_LINKS,
    EventRow,
    Item,
    Verdict,
    build_judge_input,
    build_judge_request,
    validate_verdicts,
)

E1 = EventRow("e1", "Example ceasefire signed", "A ceasefire is formally signed.")
E2 = EventRow("e2", "Example rate cut", "The central bank cuts its policy rate.")
ITEMS = [Item(f"Headline {n}", f"https://example.com/{n}", "Wire") for n in range(6)]
JI = build_judge_input([E1, E2], ITEMS)


def _resp(verdicts, stop_reason="tool_use"):
    return {
        "stop_reason": stop_reason,
        "content": [
            {
                "type": "tool_use",
                "name": "emit_verdicts",
                "input": {"verdicts": verdicts},
            }
        ],
    }


def _v(row_id, verdict="not", evidence=(), reason="r"):
    return {
        "row_id": row_id,
        "verdict": verdict,
        "evidence": list(evidence),
        "reason": reason,
    }


def test_request_forces_the_tool_and_disables_thinking():
    req = build_judge_request([E1], JI, model="m")
    assert req["tool_choice"] == {"type": "tool", "name": "emit_verdicts"}
    assert req["thinking"] == {"type": "disabled"}
    assert req["model"] == "m"
    assert req["max_tokens"] == 512 + 300 * 1
    assert JI.text in req["messages"][0]["content"]


def test_labels_map_to_urls_in_code():
    out = validate_verdicts(
        _resp([_v("e1", "triggered", ["I2"]), _v("e2")]), [E1, E2], JI
    )
    assert out[0] == Verdict("e1", "triggered", JI.labels["I2"], "r")
    assert out[1] == Verdict("e2", "not", [], "r")


def test_unknown_label_makes_that_row_invalid():
    out = validate_verdicts(
        _resp([_v("e1", "triggered", ["I99"]), _v("e2", "unclear", ["I1"])]),
        [E1, E2],
        JI,
    )
    assert out[0].verdict == "invalid" and "I99" in out[0].reason
    assert out[1] == Verdict("e2", "unclear", JI.labels["I1"], "r")


def test_unknown_row_id_is_ignored_and_missing_row_is_invalid():
    out = validate_verdicts(
        _resp([_v("zz", "triggered", ["I1"]), _v("e1")]), [E1, E2], JI
    )
    assert [v.row_id for v in out] == ["e1", "e2"]
    assert out[0].verdict == "not"
    assert out[1].verdict == "invalid" and "no verdict" in out[1].reason


def test_duplicate_row_id_first_wins():
    out = validate_verdicts(
        _resp([_v("e1", "not"), _v("e1", "triggered", ["I1"]), _v("e2")]), [E1, E2], JI
    )
    assert out[0].verdict == "not"
    assert len(out) == 2


def test_triggered_without_evidence_is_downgraded_to_unclear():
    out = validate_verdicts(_resp([_v("e1", "triggered", []), _v("e2")]), [E1, E2], JI)
    assert out[0].verdict == "unclear" and out[0].evidence == []


def test_unknown_verdict_value_is_invalid():
    out = validate_verdicts(_resp([_v("e1", "maybe", ["I1"]), _v("e2")]), [E1, E2], JI)
    assert out[0].verdict == "invalid"


def test_no_tool_block_marks_every_row_invalid_naming_stop_reason():
    out = validate_verdicts({"stop_reason": "max_tokens", "content": []}, [E1, E2], JI)
    assert [v.verdict for v in out] == ["invalid", "invalid"]
    assert all("max_tokens" in v.reason for v in out)


def test_evidence_is_capped_at_three_links():
    syndicated = [Item("Same", f"https://example.com/s{n}", "W") for n in range(5)]
    ji = build_judge_input([E1], syndicated + ITEMS)
    out = validate_verdicts(_resp([_v("e1", "triggered", ["I1", "I2"])]), [E1], ji)
    assert len(out[0].evidence) == MAX_LINKS == 3

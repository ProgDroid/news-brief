"""The labelled judge input for watch event rows (news-brief-8lp). Pure."""

import re

from watch import EventRow, Item, build_judge_input

EV = EventRow("e1", "Example ceasefire signed", "A ceasefire is formally signed.")


def test_identical_titles_collapse_to_one_label_keeping_every_url():
    ji = build_judge_input(
        [EV],
        [
            Item("Truce agreed", "https://a/1", "A"),
            Item("truce agreed ", "https://b/2", "B"),
            Item("Other", "https://a/3", "A"),
        ],
    )
    assert ji.n_titles == 2
    assert sorted(i.url for i in ji.labels["I1"]) == ["https://a/1", "https://b/2"]
    assert [i.url for i in ji.labels["I2"]] == ["https://a/3"]
    assert "I1" in ji.text and "I2" in ji.text
    assert "https://" not in ji.text  # URLs never reach the model


def test_blank_titles_are_dropped():
    ji = build_judge_input(
        [EV], [Item("  ", "https://a/1", "A"), Item("Real", "https://a/2", "A")]
    )
    assert ji.n_titles == 1
    assert list(ji.labels) == ["I1"]
    assert ji.labels["I1"][0].title == "Real"


def test_rows_appear_by_id_name_and_trigger():
    ji = build_judge_input([EV], [Item("t", "u", "A")])
    assert "e1" in ji.text and EV.name in ji.text and EV.trigger in ji.text


def test_labels_are_opaque_not_db_ids():
    ji = build_judge_input([EV], [Item(f"t{n}", f"u{n}", "A") for n in range(30)])
    assert all(re.fullmatch(r"I\d+", k) for k in ji.labels)
    assert len(ji.labels) == 30


def test_outlet_is_shown_with_each_title():
    ji = build_judge_input([EV], [Item("Truce agreed", "u", "Reuters")])
    assert re.search(
        r"I1\b.*Reuters.*Truce agreed|I1\b.*Truce agreed.*Reuters", ji.text
    )

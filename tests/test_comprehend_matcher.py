"""Surface-form matching rules.

Every rule here exists because of a recorded failure. PolyGram's substring
search made `MU` match "Musk" (news-brief polygram-candidate-search-fix), and a
geopolitics corpus answers "who is DISCUSSED" when you ask "who is PRESENT".
These are not stylistic choices.
"""

import comprehend


def test_a_long_form_matches_case_insensitively():
    assert comprehend.form_matches("Ukraine", "ukraine signals a ceasefire")


def test_a_long_form_matches_on_a_word_boundary():
    assert comprehend.form_matches("Iran", "Iran and Israel met today")


def test_a_long_form_does_NOT_match_inside_a_word():
    """Substring matching is the recorded PolyGram bug. 'Iran' must not match
    'Iranian-adjacent' via a bare `in` check -- it matches here only because a
    hyphen is a word boundary, so use a word that truly embeds it."""
    assert not comprehend.form_matches("Iran", "the tiranian delegation")


def test_a_short_form_matches_case_SENSITIVELY():
    """Acronyms are real entities. Casefolding them is what makes them toxic:
    lowercased, `US` matches the pronoun in every second sentence."""
    assert comprehend.form_matches("US", "The US said today")
    assert not comprehend.form_matches("US", "he told us today")


def test_a_short_form_still_needs_a_word_boundary():
    assert not comprehend.form_matches("US", "USB drives were seized")


def test_html_entities_are_decoded_and_whitespace_collapsed():
    """Measured on production 2026-09-05: bodies carry literal &nbsp;, e.g.
    `...facing 10 years in jail&nbsp;&nbsp;Reuters`."""
    assert comprehend.clean("jail&nbsp;&nbsp;Reuters") == "jail Reuters"


def test_a_MULTI_WORD_form_needs_the_decode_to_match():
    """This is where the decode earns its place, and the single-word case is
    NOT the example: `&nbsp;` ends in a semicolon, which is already a word
    boundary, so `Reuters` matches with or without it. A form containing a
    SPACE is what breaks -- the entity sits where the space should be.
    """
    raw = "the New&nbsp;York talks resumed"
    assert not comprehend.form_matches("New York", raw), (
        "undecoded, the space in the form cannot match the entity in the text"
    )
    assert comprehend.form_matches("New York", comprehend.clean(raw)), (
        "presence sibling: decoded, the same form matches the same text"
    )


def test_a_stop_listed_form_never_matches():
    assert "will" in comprehend.STOP_FORMS
    assert not comprehend.form_matches("will", "the deal will collapse")


def test_a_regex_metacharacter_in_a_name_is_matched_literally():
    """Entity names contain dots and parentheses. An unescaped form would
    either crash or match far too much."""
    assert comprehend.form_matches("U.S.", "U.S. officials confirmed")
    assert not comprehend.form_matches("U.S.", "UXSX officials confirmed")


def test_the_index_matches_an_entity_by_alias():
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Volodymyr Zelenskyy", ["Zelensky", "Zelenskyy"])
    hits = index.match("Zelensky met the delegation")
    assert [h.entity_id for h in hits] == [7]
    assert hits[0].reason == "tracked_entity"


def test_the_index_returns_nothing_for_an_unrelated_text():
    """Absence assertion. Its presence sibling is the test above -- without
    one, an index that always returns [] passes this."""
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Volodymyr Zelenskyy", ["Zelensky"])
    assert index.match("chip export controls tightened") == []


def test_the_index_deduplicates_one_entity_matched_twice():
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Zelensky", ["Zelenskyy"])
    hits = index.match("Zelensky and Zelenskyy are the same person")
    assert len({h.entity_id for h in hits}) == 1

"""Surface-form matching rules.

Every rule here exists because of a recorded failure. PolyGram's substring
search made `MU` match "Musk" (news-brief polygram-candidate-search-fix), and a
geopolitics corpus answers "who is DISCUSSED" when you ask "who is PRESENT".
These are not stylistic choices.
"""

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


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
    """Asserts the HIT LIST collapses, not the entity-id set. The set is {7}
    whether or not `match` dedups -- there is only one entity -- so the
    obvious assertion passes with the dedup branch deleted. What dedup
    actually changes is len(hits): 2 forms match, 1 hit comes back."""
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Zelensky", ["Zelenskyy"])
    hits = index.match("Zelensky and Zelenskyy are the same person")
    assert len(hits) == 1, "both forms matched; dedup should collapse them to one hit"
    assert hits[0].form == "Zelensky", "insertion order: the name precedes its aliases"
    assert hits[0].entity_id == 7


def test_dedup_collapses_within_an_entity_and_NOT_across_entities():
    """Presence sibling for the test above. `len(hits) == 1` there is also
    satisfied by a match() that can only ever return one hit, which would
    silently drop every second entity in a story. Two entities, both
    matched, must give two hits."""
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Zelensky", ["Zelenskyy"])
    index.add_entity(9, "Macron", [])
    hits = index.match("Zelensky, Zelenskyy and Macron met today")
    assert len(hits) == 2, (
        "two distinct entities matched; dedup is per-entity, not global"
    )
    assert {h.entity_id for h in hits} == {7, 9}


def test_build_reads_all_three_sources_and_honours_their_filters(kb):
    """The three SELECTs in build() are the only DB path in the matcher, and
    two carry filters. A retired claim and a closed story must NOT become
    trackable surface forms -- but an absence assertion alone would pass if
    build() returned nothing at all, so every exclusion here has an included
    sibling from the same table."""
    kb.execute(
        "INSERT INTO entities (name, type, aliases) "
        "VALUES ('Zelensky', 'person', ARRAY['Zelenskyy'])"
    )
    # `challenged` is selectable but the table's CHECK requires resolved_on for
    # any status other than 'standing' -- a bare status flip fails to insert.
    kb.execute(
        "INSERT INTO claims (claim, topic, first_seen, status) "
        "VALUES ('a', 'grain corridor', '2026-09-01', 'standing')"
    )
    kb.execute(
        "INSERT INTO claims (claim, topic, first_seen, status, resolved_on) "
        "VALUES ('b', 'ceasefire talks', '2026-09-01', 'challenged', '2026-09-02')"
    )
    kb.execute(
        "INSERT INTO claims (claim, topic, first_seen, status, resolved_on) "
        "VALUES ('c', 'withdrawn topic', '2026-09-01', 'withdrawn', '2026-09-02')"
    )
    # Retired but still 'standing': the case the status filter alone cannot catch.
    kb.execute(
        "INSERT INTO claims (claim, topic, first_seen, status, retired_on) "
        "VALUES ('d', 'retired topic', '2026-09-01', 'standing', '2026-09-02')"
    )
    kb.execute(
        "INSERT INTO stories (name, scope, state) "
        "VALUES ('Black Sea shipping', 'episodic', 'active')"
    )
    kb.execute(
        "INSERT INTO stories (name, scope, state) "
        "VALUES ('Old story', 'episodic', 'closed')"
    )

    index = comprehend.SurfaceIndex.build(kb)
    by_form = {f.form: f.reason for f in index._forms}

    # Included -- the presence siblings that make the exclusions below mean something.
    assert by_form.get("Zelensky") == "tracked_entity"
    assert by_form.get("Zelenskyy") == "tracked_entity", "aliases are surface forms too"
    assert by_form.get("grain corridor") == "tracked_claim"
    assert by_form.get("ceasefire talks") == "tracked_claim", (
        "'challenged' is trackable"
    )
    assert by_form.get("Black Sea shipping") == "tracked_story"

    # Excluded by the two filters.
    assert "withdrawn topic" not in by_form, (
        "claims filter admits only standing/challenged"
    )
    assert "retired topic" not in by_form, (
        "a retired claim is not live even while its status is still 'standing' "
        "(migrations/0007: the predicate is an obligation in a query, not an index)"
    )
    assert "Old story" not in by_form, "stories filter excludes state = 'closed'"


def test_clean_tolerates_none():
    """The signature admits None -- items.body is nullable -- and every caller
    passes a database column straight in."""
    assert comprehend.clean(None) == ""

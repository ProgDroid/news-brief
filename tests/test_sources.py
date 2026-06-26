"""Outcome tests for the curated RSS_FEEDS registry: the perspective matrix is
complete and every feed dict is well-formed."""

import brief


def test_perspective_matrix_filled():
    sourced = {f.get("perspective") for f in brief.RSS_FEEDS if f.get("perspective")}
    for vantage in ("RUSSIAN", "IRANIAN", "ISRAELI", "INDIAN"):
        assert vantage in sourced, f"{vantage} has no source in RSS_FEEDS"


def test_rss_feeds_well_formed():
    for f in brief.RSS_FEEDS:
        assert f["name"], f"feed missing name: {f!r}"
        assert f["url"], f"feed missing url: {f['name']}"
        assert f["category"], f"feed missing category: {f['name']}"
        assert f.get("kind", "wire") in brief.VALID_KINDS, f"bad kind: {f['name']}"
        p = f.get("perspective")
        assert p is None or p in brief.VALID_PERSPECTIVES, (
            f"bad perspective: {f['name']}"
        )
        assert isinstance(f.get("state_funded", False), bool), (
            f"bad state_funded: {f['name']}"
        )

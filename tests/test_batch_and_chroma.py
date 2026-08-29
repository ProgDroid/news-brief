# tests/test_batch_and_chroma.py
"""Characterization + behavior tests for the batch-submit and Chroma-query
helpers. These lock the outgoing JSON-RPC / batch payload shapes so the
duplication-merge refactor cannot silently change what hits the wire."""

import brief


def _capturing_post(captured, response_json):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return response_json

    def post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        captured["timeout"] = kwargs.get("timeout")
        return _R()

    return post


# --- submit_batch / submit_batch_no_search -------------------------------


def test_submit_batch_includes_web_search_tool(monkeypatch):
    cap = {}
    monkeypatch.setattr(brief.requests, "post", _capturing_post(cap, {"id": "batch_1"}))
    bid = brief.submit_batch("SYS", "USER", "cid-1")
    assert bid == "batch_1"
    req = cap["json"]["requests"][0]
    assert req["custom_id"] == "cid-1"
    params = req["params"]
    assert params["tools"] == [{"type": "web_search_20250305", "name": "web_search"}]
    assert params["system"] == "SYS"
    assert params["messages"] == [{"role": "user", "content": "USER"}]
    assert cap["url"].endswith("/v1/messages/batches")
    assert cap["timeout"] == 30


def test_submit_batch_no_search_omits_tools(monkeypatch):
    cap = {}
    monkeypatch.setattr(brief.requests, "post", _capturing_post(cap, {"id": "batch_2"}))
    bid = brief.submit_batch_no_search("SYS", "USER", "cid-2")
    assert bid == "batch_2"
    params = cap["json"]["requests"][0]["params"]
    assert "tools" not in params
    assert params["system"] == "SYS"
    assert params["messages"] == [{"role": "user", "content": "USER"}]


# --- query_chroma / query_chroma_latest ----------------------------------


def test_query_chroma_calls_search_podcasts_and_extracts_text(monkeypatch):
    cap = {}
    content = [
        {"type": "text", "text": "alpha"},
        {"type": "image"},
        {"type": "text", "text": "beta"},
    ]
    monkeypatch.setattr(
        brief.requests, "post", _capturing_post(cap, {"result": {"content": content}})
    )
    out = brief.query_chroma("q", n_results=3)
    assert out == ["alpha", "beta"]
    params = cap["json"]["params"]
    assert params["name"] == "search_podcasts"
    assert params["arguments"] == {"query": "q", "n_results": 3}


def test_query_chroma_latest_uses_latest_on_topic(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        brief.requests, "post", _capturing_post(cap, {"result": {"content": []}})
    )
    brief.query_chroma_latest("topicX", n_results=4)
    params = cap["json"]["params"]
    assert params["name"] == "latest_on_topic"
    assert params["arguments"] == {"topic": "topicX", "n_results": 4}


def test_query_chroma_latest_with_date_falls_back_to_search(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        brief.requests, "post", _capturing_post(cap, {"result": {"content": []}})
    )
    brief.query_chroma_latest("topicX", n_results=4, after_date="2026-06-01")
    params = cap["json"]["params"]
    assert params["name"] == "search_podcasts"
    assert params["arguments"] == {
        "query": "topicX",
        "n_results": 4,
        "after_date": "2026-06-01",
    }


def test_query_chroma_returns_empty_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(brief.requests, "post", boom)
    assert brief.query_chroma("q") == []
    assert brief.query_chroma_latest("t") == []


# ── news-brief-ba9: citation-boundary newlines corrupting brief prose ─────────
def test_fetch_batch_results_joins_citation_split_blocks_without_newlines(monkeypatch):
    """The API splits prose into separate text blocks at citation boundaries, so
    joining them with a newline inserted a line break mid-sentence — often right
    before a comma. Measured across 90 archived briefs: 85 affected, mean ~4.7
    breaks each, worst 12."""
    payload = {
        "result": {
            "type": "succeeded",
            "message": {
                "content": [
                    {"type": "text", "text": "Brent rose 2% on Hormuz transit risk"},
                    {"type": "text", "text": ", according to shipping data."},
                ]
            },
        }
    }

    class _StreamResp:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield brief.json.dumps(payload).encode()

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _StreamResp())
    monkeypatch.setattr(brief, "_dump_raw_batch_result", lambda *a, **k: None)

    out = brief.fetch_batch_results("http://example.invalid/results")
    assert out == "Brent rose 2% on Hormuz transit risk, according to shipping data."
    assert "\n" not in out


def test_fetch_batch_results_still_skips_non_text_blocks(monkeypatch):
    payload = {
        "result": {
            "type": "succeeded",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "dropped"},
                    {"type": "text", "text": "kept"},
                ]
            },
        }
    }

    class _StreamResp:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield brief.json.dumps(payload).encode()

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _StreamResp())
    monkeypatch.setattr(brief, "_dump_raw_batch_result", lambda *a, **k: None)

    assert brief.fetch_batch_results("http://example.invalid/results") == "kept"

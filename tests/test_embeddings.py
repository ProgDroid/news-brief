"""embeddings.py: text prep, vector literals, and the two providers.

No network: every provider takes an injectable `post`. Invented text only.
"""

import math

import pytest
import requests

import common
import embeddings
from embeddings import (
    CallRecord,
    EmbedError,
    ModalEmbedder,
    OpenAIEmbedder,
    active_embedder,
    embed_text,
    from_pg_vector,
    space_id,
    text_hash,
    to_pg_vector,
)

GOOGLE = "https://news.google.com/rss/articles/x"
PLAIN = "https://example.com/a"


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Poster:
    """Records every call; replies is a list (consumed in order) or a callable."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        i = len(self.calls) - 1
        r = self.replies(i, kw) if callable(self.replies) else self.replies[i]
        if isinstance(r, BaseException):
            raise r
        return r


def modal_reply(
    n, *, model="m", dim=4, runtime=2.0, boot=0.0, cold=False, tokens=10, pv=None
):
    return Resp(
        200,
        {
            "vectors": [[3.0, 4.0, 0.0, 0.0]] * n,
            "model": model,
            "dim": dim,
            "prompt_version": embeddings.EMBED_PROMPT_VERSION if pv is None else pv,
            "tokens": tokens,
            "runtime_s": runtime,
            "boot_s": boot,
            "cold_start": cold,
        },
    )


def modal(post, **kw):
    args = dict(key="k", secret="s", gpu_rate=0.0001, idle_s=10.0, timeout=5)
    args.update(kw)
    return ModalEmbedder("https://modal.invalid/embed", "m", 4, post=post, **args)


# ---- embed_text ----


def test_embed_text_strips_the_google_suffix_and_a_repeated_body():
    t = "Widget plant shut - Gizmo News Agency"
    nbsp = "\u00a0"
    assert (
        embed_text(t, f"Widget plant shut{nbsp}{nbsp}Gizmo News Agency", GOOGLE)
        == "Widget plant shut"
    )
    assert (
        embed_text(t, "Widget plant shut - Gizmo News Agency", GOOGLE)
        == "Widget plant shut"
    )
    assert embed_text(t, "Widget plant shut", GOOGLE) == "Widget plant shut"
    assert embed_text(t, None, PLAIN) == t
    assert (
        embed_text(t, "Gizmo output fell.", GOOGLE)
        == "Widget plant shut\nGizmo output fell."
    )


def test_embed_text_keeps_a_real_lede_that_starts_with_the_headline():
    out = embed_text(
        "Widget plant shut - Gizmo", "Widget plant shut as orders fall", GOOGLE
    )
    assert out == "Widget plant shut\nWidget plant shut as orders fall"
    # Same publisher words, but a real sentence after them: still a lede.
    out = embed_text(
        "Widget plant shut - Gizmo", "Widget plant shut Gizmo says orders fell", GOOGLE
    )
    assert "orders fell" in out


def test_embed_text_never_empties_a_real_title():
    # A title that is only a publisher segment: stripping would empty it.
    assert embed_text(" - Gizmo", None, GOOGLE) == "- Gizmo"
    assert (
        embed_text("- Gizmo", "Widget output fell.", GOOGLE)
        == "- Gizmo\nWidget output fell."
    )
    assert embed_text("  \u00a0 ", "body", GOOGLE) == ""


def test_embed_text_caps_on_a_word_boundary():
    out = embed_text("Widget", "gizmo " * 900, PLAIN)
    assert 0 < len(out) <= embeddings.EMBED_TEXT_MAX_CHARS
    assert out.endswith("gizmo")
    assert (
        len(embed_text("Widget", "x" * 5000, PLAIN)) <= embeddings.EMBED_TEXT_MAX_CHARS
    )


def test_text_hash_is_stable_and_differs_by_text():
    assert text_hash("a") == text_hash("a")
    assert text_hash("a") != text_hash("b")
    assert len(text_hash("a")) == 64


def test_pg_vector_round_trip():
    assert from_pg_vector(to_pg_vector([0.5, -0.25])) == [0.5, -0.25]
    assert to_pg_vector([0.5, 1 / 3]) == "[0.500000,0.333333]"


def test_space_id_changes_with_dim():
    assert space_id("modal", "m", 1024) == "modal:m@1024"
    assert space_id("modal", "m", 1024) != space_id("modal", "m", 512)


# ---- Modal ----


def test_modal_chunks_and_preserves_order():
    def reply(i, kw):
        texts = kw["json"]["texts"]
        r = modal_reply(len(texts))
        # Tag each vector with its text's number so order is observable.
        r._body["vectors"] = [[0.0, float(int(t[1:])) + 1.0, 0.0, 0.0] for t in texts]
        return r

    post = Poster(reply)
    texts = [f"t{i}" for i in range(300)]
    vecs, calls = modal(post).embed(texts, kind="document")
    assert [len(kw["json"]["texts"]) for _, kw in post.calls] == [128, 128, 44]
    assert [c.inputs for c in calls] == [128, 128, 44]
    # After normalisation every tagged vector is (0,1,0,0): order lives in the
    # request, so check it there, and check the reply was mapped positionally by
    # making vector 0 distinguishable below.
    sent = [t for _, kw in post.calls for t in kw["json"]["texts"]]
    assert sent == texts
    assert len(vecs) == 300
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0) for v in vecs)


def test_modal_vectors_come_back_in_input_order():
    def reply(i, kw):
        r = modal_reply(len(kw["json"]["texts"]))
        # Tag by absolute position: chunk i holds texts i*128 .. ; encode in dim 0/1.
        base = i * 128
        r._body["vectors"] = [
            [float(base + j + 1), 1.0, 0.0, 0.0]
            for j in range(len(kw["json"]["texts"]))
        ]
        return r

    vecs, _ = modal(Poster(reply)).embed(["x"] * 300, kind="document")
    ratios = [v[0] / v[1] for v in vecs]
    assert ratios == pytest.approx([float(k + 1) for k in range(300)])


def test_modal_sends_the_documented_request():
    post = Poster([modal_reply(1)])
    modal(post).embed(["hello"], kind="query")
    url, kw = post.calls[0]
    assert url == "https://modal.invalid/embed"
    assert kw["json"] == {
        "texts": ["hello"],
        "dim": 4,
        "kind": "query",
        "model": "m",
        "prompt_version": embeddings.EMBED_PROMPT_VERSION,
    }
    assert kw["headers"]["Modal-Key"] == "k"
    assert kw["headers"]["Modal-Secret"] == "s"
    assert kw["timeout"] == 5


def test_modal_vectors_are_l2_normalised():
    vecs, _ = modal(Poster([modal_reply(1)])).embed(["x"], kind="document")
    assert vecs[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])


def test_modal_cold_call_is_charged_boot_and_idle():
    cold = modal(Poster([modal_reply(1, runtime=2, boot=20, cold=True)]))
    _, calls = cold.embed(["x"], kind="document")
    assert calls[0].usd == pytest.approx(0.0032)
    assert calls[0].cold_start is True and calls[0].boot_s == 20
    warm = modal(Poster([modal_reply(1, runtime=2, boot=0, cold=False)]))
    _, calls = warm.embed(["x"], kind="document")
    assert calls[0].usd == pytest.approx(0.0002)


def test_modal_record_carries_tokens_and_the_openai_equivalent():
    _, calls = modal(Poster([modal_reply(1, tokens=1_000_000)])).embed(
        ["x"], kind="document"
    )
    c = calls[0]
    assert isinstance(c, CallRecord)
    assert c.provider == "modal" and c.space == "modal:m@4"
    assert c.tokens == 1_000_000 and c.openai_equivalent_usd == pytest.approx(0.13)
    assert c.batch is False and c.outcome == "ok" and c.problem is None


def test_modal_reply_with_a_different_model_is_bad_response():
    with pytest.raises(EmbedError) as ei:
        modal(Poster([modal_reply(1, model="other")])).embed(["x"], kind="document")
    assert ei.value.kind == "bad_response"
    assert ei.value.calls[-1].outcome == "failed"


def test_modal_reply_with_a_different_dim_is_bad_response():
    with pytest.raises(EmbedError) as ei:
        modal(Poster([modal_reply(1, dim=8)])).embed(["x"], kind="document")
    assert ei.value.kind == "bad_response"


def test_modal_reply_with_another_prompt_version_is_bad_response():
    reply = modal_reply(1, pv=embeddings.EMBED_PROMPT_VERSION + 1)
    with pytest.raises(EmbedError) as ei:
        modal(Poster([reply])).embed(["x"], kind="document")
    assert ei.value.kind == "bad_response"


@pytest.mark.parametrize("missing", ["runtime_s", "boot_s", "cold_start", "tokens"])
def test_modal_reply_missing_a_billing_field_is_bad_response(missing):
    reply = modal_reply(1)
    del reply._body[missing]
    with pytest.raises(EmbedError) as ei:
        modal(Poster([reply])).embed(["x"], kind="document")
    assert ei.value.kind == "bad_response"
    assert ei.value.calls[-1].outcome == "failed"


def test_modal_wrong_vector_count_is_bad_response():
    with pytest.raises(EmbedError) as ei:
        modal(Poster([modal_reply(2)])).embed(["x"], kind="document")
    assert ei.value.kind == "bad_response"


@pytest.mark.parametrize(
    "reply,kind",
    [
        (requests.Timeout("t"), "transient"),
        (requests.ConnectionError("c"), "transient"),
        (Resp(429), "transient"),
        (Resp(503), "transient"),
        (Resp(401), "config"),
        (Resp(403), "config"),
        (Resp(404), "config"),
        (Resp(400), "input"),
        (Resp(413), "input"),
        (Resp(422), "input"),
        (Resp(200, {"vectors": "x"}), "bad_response"),
        (Resp(200, ValueError("not json")), "bad_response"),
    ],
)
def test_error_kinds(reply, kind):
    with pytest.raises(EmbedError) as ei:
        modal(Poster([reply])).embed(["x"], kind="document")
    assert ei.value.kind == kind
    assert ei.value.calls and ei.value.calls[-1].outcome == "failed"
    assert ei.value.calls[-1].problem and len(ei.value.calls[-1].problem) <= 300


def test_error_after_a_good_chunk_keeps_both_records():
    post = Poster([modal_reply(128), Resp(503)])
    with pytest.raises(EmbedError) as ei:
        modal(post).embed(["x"] * 200, kind="document")
    assert [c.outcome for c in ei.value.calls] == ["ok", "failed"]


def test_long_problem_text_is_cut_to_300():
    with pytest.raises(EmbedError) as ei:
        modal(Poster([Resp(500, text="y" * 2000)])).embed(["x"], kind="document")
    assert len(ei.value.calls[-1].problem) == 300


def test_modal_without_url_is_config():
    e = ModalEmbedder("", "m", 4, key="k", secret="s", gpu_rate=1, idle_s=1, timeout=1)
    with pytest.raises(EmbedError) as ei:
        e.embed(["x"], kind="document")
    assert ei.value.kind == "config"


# ---- OpenAI ----


def oa(post, model="text-embedding-3-large"):
    return OpenAIEmbedder("sk-test", model, 4, timeout=7, post=post)


def test_openai_reorders_by_index_and_prices_from_usage():
    body = {
        "data": [
            {"index": 1, "embedding": [0.0, 2.0, 0.0, 0.0]},
            {"index": 0, "embedding": [3.0, 0.0, 4.0, 0.0]},
        ],
        "usage": {"prompt_tokens": 1_000_000, "total_tokens": 1_000_000},
    }
    vecs, calls = oa(Poster([Resp(200, body)])).embed(["a", "b"], kind="document")
    assert vecs[0] == pytest.approx([0.6, 0.0, 0.8, 0.0])
    assert vecs[1] == pytest.approx([0.0, 1.0, 0.0, 0.0])
    assert calls[0].usd == pytest.approx(0.13)
    assert calls[0].openai_equivalent_usd == calls[0].usd
    assert calls[0].tokens == 1_000_000 and calls[0].provider == "openai"
    assert calls[0].runtime_s is None and calls[0].cold_start is None


def test_openai_small_model_is_priced_at_its_own_rate():
    body = {
        "data": [{"index": 0, "embedding": [1.0, 0, 0, 0]}],
        "usage": {"prompt_tokens": 1_000_000},
    }
    _, calls = oa(Poster([Resp(200, body)]), model="text-embedding-3-small").embed(
        ["a"], kind="query"
    )
    assert calls[0].usd == pytest.approx(0.02)


def test_openai_sends_dimensions_and_bearer_key():
    body = {
        "data": [{"index": 0, "embedding": [1.0, 0, 0, 0]}],
        "usage": {"prompt_tokens": 3},
    }
    post = Poster([Resp(200, body)])
    oa(post).embed(["a"], kind="query")
    url, kw = post.calls[0]
    assert url == "https://api.openai.com/v1/embeddings"
    assert kw["json"] == {
        "model": "text-embedding-3-large",
        "input": ["a"],
        "dimensions": 4,
        "encoding_format": "float",
    }
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    assert kw["timeout"] == 7


def _oa_echo(i, kw):
    n = len(kw["json"]["input"])
    return Resp(
        200,
        {
            "data": [{"index": j, "embedding": [1.0, 0, 0, 0]} for j in range(n)],
            "usage": {"prompt_tokens": n},
        },
    )


def test_openai_chunks_by_count_and_by_token_estimate():
    by_count = Poster(_oa_echo)
    vecs, _ = oa(by_count).embed(["x"] * 1500, kind="document")
    assert [len(kw["json"]["input"]) for _, kw in by_count.calls] == [512, 512, 476]
    assert len(vecs) == 1500
    # 4000 chars = 1000 estimated tokens each, so 250 fit under 250_000.
    by_tokens = Poster(_oa_echo)
    oa(by_tokens).embed(["y" * 4000] * 600, kind="document")
    assert [len(kw["json"]["input"]) for _, kw in by_tokens.calls] == [250, 250, 100]


def test_openai_wrong_count_is_bad_response():
    body = {
        "data": [{"index": 0, "embedding": [1.0, 0, 0, 0]}],
        "usage": {"prompt_tokens": 1},
    }
    with pytest.raises(EmbedError) as ei:
        oa(Poster([Resp(200, body)])).embed(["a", "b"], kind="document")
    assert ei.value.kind == "bad_response"


def test_openai_error_kind_and_missing_key():
    with pytest.raises(EmbedError) as ei:
        oa(Poster([Resp(401)])).embed(["a"], kind="document")
    assert ei.value.kind == "config" and ei.value.calls[-1].outcome == "failed"
    with pytest.raises(EmbedError) as ei:
        OpenAIEmbedder("", "text-embedding-3-large", 4, timeout=1).embed(
            ["a"], kind="document"
        )
    assert ei.value.kind == "config"


# ---- active_embedder ----


def test_active_embedder_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "modal")
    monkeypatch.setattr(common, "EMBEDDING_MODAL_URL", "")
    assert active_embedder() == (None, "EMBEDDING_MODAL_URL is empty")
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert active_embedder() == (None, "OPENAI_API_KEY is not set")
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "carrier-pigeon")
    emb, problem = active_embedder()
    assert emb is None and "carrier-pigeon" in problem


def test_active_embedder_builds_each_provider(monkeypatch):
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "modal")
    monkeypatch.setattr(common, "EMBEDDING_MODAL_URL", "https://modal.invalid/e")
    monkeypatch.setenv("MODAL_PROXY_KEY", "k")
    monkeypatch.setenv("MODAL_PROXY_SECRET", "s")
    emb, problem = active_embedder()
    assert problem is None and isinstance(emb, ModalEmbedder)
    assert emb.space == space_id(
        "modal", common.EMBEDDING_MODAL_MODEL, common.EMBEDDING_DIM
    )
    monkeypatch.delenv("MODAL_PROXY_SECRET")
    assert active_embedder() == (
        None,
        "MODAL_PROXY_KEY / MODAL_PROXY_SECRET are not set",
    )
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    emb, problem = active_embedder()
    assert problem is None and isinstance(emb, OpenAIEmbedder)
    assert emb.space == space_id(
        "openai", common.EMBEDDING_OPENAI_MODEL, common.EMBEDDING_DIM
    )

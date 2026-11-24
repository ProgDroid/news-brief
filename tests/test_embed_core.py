import pytest

import embeddings
from modal_apps import embed_core as core

QWEN = "Qwen/Qwen3-Embedding-4B"
QWEN8 = "Qwen/Qwen3-Embedding-8B"
OCTEN = "Octen/Octen-Embedding-8B"
INSTR = (
    "Instruct: Given a topic description, retrieve news articles relevant to it\nQuery:"
)


def good(**over):
    p = {
        "texts": ["Widget maker TESTCO ships Gizmo"],
        "dim": 256,
        "kind": "document",
        "model": QWEN,
        "prompt_version": core.PROMPT_VERSION,
    }
    p.update(over)
    return p


def parse(payload, model=QWEN, native=2560):
    return core.parse_request(payload, served_model=model, native_dim=native)


@pytest.mark.parametrize(
    "payload, field",
    [
        ({k: v for k, v in good().items() if k != "texts"}, "texts"),
        (good(texts="one string"), "texts"),
        (good(texts=["x"] * (core.MAX_INPUTS + 1)), "texts"),
        (good(texts=[1, 2]), "texts"),
        (good(dim=31), "dim"),
        (good(dim=2561), "dim"),
        (good(dim="256"), "dim"),
        (good(dim=True), "dim"),
        (good(kind="doc"), "kind"),
        (good(model="other/model"), "model"),
        (good(prompt_version=core.PROMPT_VERSION + 1), "prompt_version"),
        (good(prompt_version=None), "prompt_version"),
    ],
)
def test_parse_request_rejects_each_bad_field(payload, field):
    err = parse(payload)
    assert isinstance(err, str)
    assert field in err


def test_parse_request_rejects_a_non_object():
    assert isinstance(parse(["texts"]), str)


def test_parse_request_accepts_a_valid_request():
    req = parse(good(texts=["a", "b"], kind="query"))
    assert not isinstance(req, str)
    assert (req.texts, req.dim, req.kind, req.model) == (["a", "b"], 256, "query", QWEN)
    # the bounds themselves are valid
    assert not isinstance(parse(good(dim=32)), str)
    assert not isinstance(parse(good(dim=2560)), str)
    assert not isinstance(parse(good(texts=["x"] * core.MAX_INPUTS)), str)


@pytest.mark.parametrize("model", [QWEN, QWEN8])
def test_qwen_queries_get_the_instruction_and_documents_do_not(model):
    assert core.prepare(["Widget news"], "query", model) == [INSTR + "Widget news"]
    assert core.prepare(["Widget news"], "document", model) == ["Widget news"]


def test_octen_gets_no_prefix():
    for kind in ("query", "document"):
        assert core.prepare(["Gizmo"], kind, OCTEN) == ["Gizmo"]


def test_prepare_refuses_an_unknown_model():
    with pytest.raises(KeyError):
        core.prepare(["x"], "query", "nobody/unknown")


def test_truncate_and_normalise():
    out = core.truncate_and_normalise([3.0, 4.0, 12.0], 2)
    assert out == pytest.approx([0.6, 0.8])
    # an already-longer vector is renormalised after the cut, not before
    assert sum(
        x * x for x in core.truncate_and_normalise([1.0, 2.0, 3.0, 4.0], 3)
    ) == pytest.approx(1.0)


def test_truncate_and_normalise_refuses_a_zero_vector():
    with pytest.raises(ValueError):
        core.truncate_and_normalise([0.0, 0.0, 5.0], 2)


def test_response_shape():
    r = core.response(
        model=QWEN,
        dim=2,
        vectors=[[0.6, 0.8]],
        tokens=7,
        runtime_s=0.5,
        boot_s=3.0,
        cold_start=False,
    )
    assert set(r) == {
        "model",
        "dim",
        "vectors",
        "tokens",
        "runtime_s",
        "boot_s",
        "cold_start",
        "prompt_version",
    }
    assert r["prompt_version"] == core.PROMPT_VERSION
    assert r["cold_start"] is False and r["boot_s"] == 3.0


def test_prompt_versions_agree():
    assert core.PROMPT_VERSION == embeddings.EMBED_PROMPT_VERSION


def test_max_inputs_agrees_with_the_client():
    assert core.MAX_INPUTS == embeddings.MODAL_MAX_INPUTS


def test_every_prompt_model_has_a_dtype_table_for_each_gpu():
    assert set(core.DTYPE_BY_GPU) == {"T4", "L4", "A10"}
    assert core.DTYPE_BY_GPU["T4"] == "float16"


def test_client_request_round_trips_through_the_service_core():
    seen = {}

    class Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def post(url, timeout, headers, json):
        seen["payload"] = json
        req = core.parse_request(json, served_model=QWEN, native_dim=2560)
        assert not isinstance(req, str), req
        prepared = core.prepare(req.texts, req.kind, req.model)
        assert prepared == [INSTR + "Widget news"]
        vecs = [[1.0] + [0.0] * (req.dim - 1) for _ in prepared]
        reply = core.response(
            model=req.model,
            dim=req.dim,
            vectors=vecs,
            tokens=5,
            runtime_s=0.1,
            boot_s=2.0,
            cold_start=True,
        )
        return Resp(reply)

    emb = embeddings.ModalEmbedder(
        "https://example.invalid/embed",
        QWEN,
        64,
        key="k",
        secret="s",
        gpu_rate=0.0001,
        idle_s=10,
        timeout=5,
        post=post,
    )
    vecs, calls = emb.embed(["Widget news"], kind="query")
    assert vecs == [[1.0] + [0.0] * 63]
    assert seen["payload"]["dim"] == 64
    assert len(calls) == 1 and calls[0].outcome == "ok" and calls[0].cold_start is True

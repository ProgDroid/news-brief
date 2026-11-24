"""Request and response logic of the Modal embedding service, kept pure.

Standard library only, so it is tested without `modal`, `torch` or a GPU.
`embed_service.py` is the thin shell around it. `embeddings.ModalEmbedder` is
the client; the two must agree on the request keys, the reply keys and
`PROMPT_VERSION` (a test pins the number).
"""

import math
from dataclasses import dataclass

MAX_INPUTS = 128
MAX_SEQ_TOKENS = 512
MIN_DIM = 32

# Bump together with embeddings.EMBED_PROMPT_VERSION whenever PROMPTS changes.
PROMPT_VERSION = 1

_QWEN_QUERY = (
    "Instruct: Given a topic description, retrieve news articles relevant to it\n"
    "Query:{text}"
)

# Keyed by model id. "query" is a template for queries; documents go as they are.
PROMPTS: dict[str, dict] = {
    "Qwen/Qwen3-Embedding-4B": {"query": _QWEN_QUERY, "document": "{text}"},
    "Qwen/Qwen3-Embedding-8B": {"query": _QWEN_QUERY, "document": "{text}"},
    "Octen/Octen-Embedding-8B": {"query": "{text}", "document": "{text}"},
}

# A T4 has no fast bf16, and fp32 weights for a 4B model fill its 16 GB.
DTYPE_BY_GPU = {"T4": "float16", "L4": "bfloat16", "A10": "bfloat16"}


@dataclass(frozen=True)
class Request:
    texts: list[str]
    dim: int
    kind: str
    model: str


@dataclass(frozen=True)
class Refusal:
    """Why a request was refused, and the HTTP status to answer with.

    400: the body is not a request (texts missing, not a list of strings, or
    more than MAX_INPUTS). 409: a well-formed request this deployment does not
    serve: the model, prompt version, dim or kind disagree with it. The client
    reads 400 as "this content was refused" and may skip the item; a 409 is a
    configuration fault and skips nothing. Every contract mismatch must
    therefore be 409, or a misdeployed service would retire the whole queue.
    """

    status: int
    message: str


def _bad(message: str) -> Refusal:
    return Refusal(400, message)


def _mismatch(message: str) -> Refusal:
    return Refusal(409, message)


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def parse_request(
    payload, *, served_model: str, native_dim: int
) -> "Request | Refusal":
    """A Request, or a Refusal naming the first bad field."""
    if not isinstance(payload, dict):
        return _bad("request body must be a JSON object")
    texts = payload.get("texts")
    if not isinstance(texts, list):
        return _bad("texts must be a list")
    if not all(isinstance(t, str) for t in texts):
        return _bad("texts must all be strings")
    if len(texts) > MAX_INPUTS:
        return _bad(f"texts has {len(texts)} items, at most {MAX_INPUTS}")
    dim = payload.get("dim")
    if not _is_int(dim) or not MIN_DIM <= dim <= native_dim:
        return _mismatch(f"dim must be an integer in [{MIN_DIM}, {native_dim}]")
    kind = payload.get("kind")
    if kind not in ("document", "query"):
        return _mismatch("kind must be 'document' or 'query'")
    model = payload.get("model")
    if model != served_model:
        return _mismatch(f"model is {model!r}, this service serves {served_model!r}")
    if payload.get("prompt_version") != PROMPT_VERSION:
        return _mismatch(
            f"prompt_version is {payload.get('prompt_version')!r}, "
            f"this service is {PROMPT_VERSION}"
        )
    return Request(texts, dim, kind, model)


def prepare(texts: list[str], kind: str, model: str) -> list[str]:
    """Apply the model's prompt for `kind`. An unknown model is a KeyError."""
    template = PROMPTS[model][kind]
    return [template.format(text=t) for t in texts]


def truncate_and_normalise(vec: list[float], dim: int) -> list[float]:
    """Matryoshka cut to `dim`, then L2-normalise."""
    cut = [float(x) for x in vec[:dim]]
    norm = math.sqrt(sum(x * x for x in cut))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("vector has no usable norm after the cut")
    return [x / norm for x in cut]


def response(*, model, dim, vectors, tokens, runtime_s, boot_s, cold_start) -> dict:
    """Every key the client requires, always: `boot_s` is the container's boot
    time even on a warm call (the client charges it only when `cold_start`)."""
    return {
        "model": model,
        "dim": dim,
        "vectors": vectors,
        "tokens": tokens,
        "runtime_s": runtime_s,
        "boot_s": boot_s,
        "cold_start": cold_start,
        "prompt_version": PROMPT_VERSION,
    }

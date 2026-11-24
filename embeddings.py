"""One interface over the two embedding providers (Modal, OpenAI).

Text prep, hashing, vector literals, the cost record every call produces, and
`active_embedder()`. No SQL here and nothing is written: callers persist the
`CallRecord`s. Providers take an injectable `post` so tests never touch the
network.

`modal_apps` is not in the image, so nothing here imports it. Query text is
prepared on the service; this side only says `kind`.
"""

import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence
from urllib.parse import urlsplit

import requests

import common

EMBED_TEXT_MAX_CHARS = 1000  # about 256 tokens
MODAL_MAX_INPUTS = 128
OPENAI_MAX_INPUTS = 512
OPENAI_MAX_TOKENS_PER_REQUEST = 250_000  # estimated at chars / 4
OPENAI_PRICES_PER_MTOK = {
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
}
OPENAI_EQUIVALENT_MODEL = "text-embedding-3-large"
OPENAI_BATCH_FACTOR = 0.5
OPENAI_URL = "https://api.openai.com/v1/embeddings"

# Goes in the Modal request and must come back in the reply; also part of every
# query-cache key and calibration row. A change to the service's prompts bumps it
# on both sides, which retires cached query vectors and forces a recalibration
# instead of silently mixing prompts.
EMBED_PROMPT_VERSION = 1

PROBLEM_MAX_CHARS = 300  # embedding_calls.problem is limited to this
_GOOGLE_HOST = "news.google.com"
_PUBLISHER_SEGMENT_MAX_CHARS = 40
_PUBLISHER_SEGMENT_MAX_WORDS = 5


# ---- text prep ----


def _squash(s: str | None) -> str:
    # str.split() with no argument splits on every Unicode space, U+00A0 included.
    return " ".join((s or "").split())


def _is_google(url: str) -> bool:
    try:
        return (urlsplit(url or "").hostname or "").lower() == _GOOGLE_HOST
    except ValueError:
        return False


def _repeats_title(title: str, body: str) -> bool:
    """The body is the title, optionally followed by one short publisher segment."""
    if body == title:
        return True
    if not body.startswith(title):
        return False
    rest = body[len(title) :].strip(" -–—|:")
    return (
        bool(rest)
        and len(rest) <= _PUBLISHER_SEGMENT_MAX_CHARS
        and len(rest.split()) <= _PUBLISHER_SEGMENT_MAX_WORDS
    )


def _cut_on_word(text: str, limit: int) -> str:
    """At most `limit` chars, not ending mid-word (one unbroken run is cut hard)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace():
        cut = max(head.rfind(" "), head.rfind("\n"))
        if cut > 0:
            head = head[:cut]
    return head.rstrip()


def embed_text(title: str, body: str | None, url: str) -> str:
    """The text that is embedded for one item. "" only when the title is blank
    (the caller turns that into a skip row)."""
    title = _squash(title)
    body = _squash(body)
    if not title:
        return ""
    if _is_google(url):
        # Google appends " - <its own publisher name>", which is not our outlet's
        # name and gives every item from one wire a shared tail.
        stripped = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
        stripped = stripped or title
        if body and _repeats_title(stripped, body):
            body = ""
        title = stripped
    text = f"{title}\n{body}" if body else title
    return _cut_on_word(text, EMBED_TEXT_MAX_CHARS)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def space_id(provider: str, model: str, dim: int) -> str:
    return f"{provider}:{model}@{dim}"


def to_pg_vector(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def from_pg_vector(text: str) -> list[float]:
    inner = text.strip().strip("[]").strip()
    return [float(x) for x in inner.split(",")] if inner else []


# ---- records and errors ----


@dataclass(frozen=True)
class CallRecord:
    provider: str
    space: str
    inputs: int
    tokens: int
    runtime_s: float | None
    boot_s: float | None
    cold_start: bool | None
    batch: bool
    usd: float
    openai_equivalent_usd: float
    outcome: str  # "ok" | "failed"
    problem: str | None


class EmbedError(Exception):
    """kind: transient (timeout, connection, 429, 5xx), config (401/403/404, other
    4xx, missing URL or key), input (400/413/422: the content was refused, not the
    service) or bad_response (the reply was the wrong shape or the wrong space).
    `calls` holds every call made before the failure, the failed one included."""

    def __init__(
        self, kind: str, message: str, calls: "list[CallRecord] | None" = None
    ):
        super().__init__(message)
        self.kind = kind
        self.calls: list[CallRecord] = list(calls or [])


class Embedder(Protocol):
    provider: str
    space: str

    def embed(
        self, texts: list[str], *, kind: Literal["document", "query"]
    ) -> tuple[list[list[float]], list[CallRecord]]: ...


def _problem(text: str) -> str:
    return text[:PROBLEM_MAX_CHARS]


def _status_kind(code: int) -> str:
    if code == 429 or code >= 500:
        return "transient"
    if code in (400, 413, 422):
        return "input"
    if 400 <= code < 500:
        return "config"
    return "bad_response"  # 1xx/3xx: not something a POST should get back


def _normalise(vec: Sequence[float], dim: int) -> list[float]:
    if len(vec) != dim:
        raise ValueError(f"vector has {len(vec)} components, expected {dim}")
    vals = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in vals))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("vector has no usable norm")
    return [x / norm for x in vals]


def _failed(provider: str, space: str, inputs: int, problem: str) -> CallRecord:
    return CallRecord(
        provider,
        space,
        inputs,
        0,
        None,
        None,
        None,
        False,
        0.0,
        0.0,
        "failed",
        _problem(problem),
    )


def _chunks(texts: list[str], limit: int) -> "list[list[str]]":
    return [texts[i : i + limit] for i in range(0, len(texts), limit)]


@dataclass
class _Sender:
    """Shared POST-and-classify step, so both providers fail the same way."""

    provider: str
    space: str
    timeout: float
    post: object = field(default=requests.post, repr=False)

    def send(self, url: str, n: int, calls: list[CallRecord], **kw):
        try:
            resp = self.post(url, timeout=self.timeout, **kw)
        except (requests.Timeout, requests.ConnectionError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            calls.append(_failed(self.provider, self.space, n, msg))
            raise EmbedError("transient", msg, calls) from exc
        code = resp.status_code
        if code != 200:
            msg = f"HTTP {code}: {getattr(resp, 'text', '') or ''}"
            calls.append(_failed(self.provider, self.space, n, msg))
            raise EmbedError(_status_kind(code), msg, calls)
        try:
            return resp.json()
        except ValueError as exc:
            msg = f"reply is not JSON: {exc}"
            calls.append(_failed(self.provider, self.space, n, msg))
            raise EmbedError("bad_response", msg, calls) from exc

    def bad(self, n: int, calls: list[CallRecord], msg: str) -> EmbedError:
        calls.append(_failed(self.provider, self.space, n, msg))
        return EmbedError("bad_response", msg, calls)

    def config(self, n: int, calls: list[CallRecord], msg: str) -> EmbedError:
        calls.append(_failed(self.provider, self.space, n, msg))
        return EmbedError("config", msg, calls)


# ---- Modal ----


class ModalEmbedder:
    provider = "modal"

    def __init__(
        self,
        url: str,
        model: str,
        dim: int,
        *,
        key: str,
        secret: str,
        gpu_rate: float,
        idle_s: float,
        timeout: float,
        post=requests.post,
    ):
        self.url, self.model, self.dim = url, model, dim
        self.key, self.secret = key, secret
        self.gpu_rate, self.idle_s, self.timeout = gpu_rate, idle_s, timeout
        self.space = space_id(self.provider, model, dim)
        self._net = _Sender(self.provider, self.space, timeout, post)

    def embed(self, texts, *, kind):
        vectors: list[list[float]] = []
        calls: list[CallRecord] = []
        if not self.url:
            raise self._net.config(len(texts), calls, "EMBEDDING_MODAL_URL is empty")
        if not (self.key and self.secret):
            raise self._net.config(
                len(texts), calls, "MODAL_PROXY_KEY / MODAL_PROXY_SECRET are not set"
            )
        for chunk in _chunks(texts, MODAL_MAX_INPUTS):
            n = len(chunk)
            body = self._net.send(
                self.url,
                n,
                calls,
                headers={"Modal-Key": self.key, "Modal-Secret": self.secret},
                json={
                    "texts": chunk,
                    "dim": self.dim,
                    "kind": kind,
                    "model": self.model,
                    "prompt_version": EMBED_PROMPT_VERSION,
                },
            )
            try:
                vecs, record = self._parse(body, n)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise self._net.bad(
                    n, calls, f"unusable reply: {type(exc).__name__}: {exc}"
                ) from exc
            calls.append(record)
            vectors.extend(vecs)
        return vectors, calls

    def _parse(self, body, n: int):
        if not isinstance(body, dict):
            raise ValueError("reply is not an object")
        if body["model"] != self.model:
            raise ValueError(
                f"reply is for model {body['model']!r}, asked for {self.model!r}"
            )
        if body["dim"] != self.dim:
            raise ValueError(f"reply has dim {body['dim']!r}, asked for {self.dim}")
        if body["prompt_version"] != EMBED_PROMPT_VERSION:
            raise ValueError(
                f"reply has prompt_version {body['prompt_version']!r}, expected {EMBED_PROMPT_VERSION}"
            )
        raw = body["vectors"]
        if not isinstance(raw, list) or len(raw) != n:
            raise ValueError("vector count does not match the request")
        vecs = [_normalise(v, self.dim) for v in raw]
        runtime = float(body["runtime_s"])
        boot = float(body.get("boot_s") or 0.0)
        cold = bool(body.get("cold_start", False))
        tokens = int(body.get("tokens") or 0)
        # A cold start implies one billed idle tail when its container ends.
        usd = (runtime + (boot + self.idle_s if cold else 0.0)) * self.gpu_rate
        equiv = tokens * OPENAI_PRICES_PER_MTOK[OPENAI_EQUIVALENT_MODEL] / 1e6
        record = CallRecord(
            self.provider,
            self.space,
            n,
            tokens,
            runtime,
            boot,
            cold,
            False,
            usd,
            equiv,
            "ok",
            None,
        )
        return vecs, record


# ---- OpenAI ----


class OpenAIEmbedder:
    provider = "openai"

    def __init__(
        self, api_key: str, model: str, dim: int, *, timeout: float, post=requests.post
    ):
        self.api_key, self.model, self.dim, self.timeout = api_key, model, dim, timeout
        self.space = space_id(self.provider, model, dim)
        self._net = _Sender(self.provider, self.space, timeout, post)

    def _batches(self, texts: list[str]) -> "list[list[str]]":
        out: list[list[str]] = []
        cur: list[str] = []
        est = 0.0
        for t in texts:
            t_est = len(t) / 4
            if cur and (
                len(cur) >= OPENAI_MAX_INPUTS
                or est + t_est > OPENAI_MAX_TOKENS_PER_REQUEST
            ):
                out.append(cur)
                cur, est = [], 0.0
            cur.append(t)
            est += t_est
        if cur:
            out.append(cur)
        return out

    def embed(
        self, texts, *, kind
    ):  # `kind` is ignored: OpenAI has no query instruction
        vectors: list[list[float]] = []
        calls: list[CallRecord] = []
        if not self.api_key:
            raise self._net.config(len(texts), calls, "OPENAI_API_KEY is not set")
        if self.model not in OPENAI_PRICES_PER_MTOK:
            raise self._net.config(
                len(texts), calls, f"no price known for model {self.model!r}"
            )
        for chunk in self._batches(texts):
            n = len(chunk)
            body = self._net.send(
                OPENAI_URL,
                n,
                calls,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "input": chunk,
                    "dimensions": self.dim,
                    "encoding_format": "float",
                },
            )
            try:
                vecs, record = self._parse(body, n)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise self._net.bad(
                    n, calls, f"unusable reply: {type(exc).__name__}: {exc}"
                ) from exc
            calls.append(record)
            vectors.extend(vecs)
        return vectors, calls

    def _parse(self, body, n: int):
        if not isinstance(body, dict):
            raise ValueError("reply is not an object")
        data = sorted(body["data"], key=lambda d: d["index"])
        if [d["index"] for d in data] != list(range(n)):
            raise ValueError("reply indexes do not match the request")
        vecs = [_normalise(d["embedding"], self.dim) for d in data]
        tokens = int(body["usage"]["prompt_tokens"])
        usd = tokens * OPENAI_PRICES_PER_MTOK[self.model] / 1e6
        record = CallRecord(
            self.provider,
            self.space,
            n,
            tokens,
            None,
            None,
            None,
            False,
            usd,
            usd,
            "ok",
            None,
        )
        return vecs, record


# ---- configuration ----


def active_embedder() -> "tuple[Embedder | None, str | None]":
    """Read the knobs once: (embedder, None), or (None, what is missing)."""
    provider = common.EMBEDDING_PROVIDER
    dim = common.EMBEDDING_DIM
    timeout = common.EMBEDDING_TIMEOUT
    if provider == "modal":
        url = common.EMBEDDING_MODAL_URL
        if not url:
            return None, "EMBEDDING_MODAL_URL is empty"
        key = os.environ.get("MODAL_PROXY_KEY", "").strip()
        secret = os.environ.get("MODAL_PROXY_SECRET", "").strip()
        if not (key and secret):
            return None, "MODAL_PROXY_KEY / MODAL_PROXY_SECRET are not set"
        return (
            ModalEmbedder(
                url,
                common.EMBEDDING_MODAL_MODEL,
                dim,
                key=key,
                secret=secret,
                gpu_rate=common.EMBEDDING_GPU_RATE,
                idle_s=common.EMBEDDING_MODAL_IDLE_SECONDS,
                timeout=timeout,
            ),
            None,
        )
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None, "OPENAI_API_KEY is not set"
        return OpenAIEmbedder(
            api_key, common.EMBEDDING_OPENAI_MODEL, dim, timeout=timeout
        ), None
    return None, f"EMBEDDING_PROVIDER is {provider!r}, expected 'modal' or 'openai'"

"""The Modal embedding service (deployed by hand; not imported by tests or shipped).

Deploy, one app per candidate model (see README.md):
    EMBED_MODEL=Qwen/Qwen3-Embedding-4B EMBED_GPU=T4 modal deploy modal_apps/embed_service.py

Nothing here could be run in CI, which has no `modal`; `ruff check` and an AST
parse are its only static checks. The request and reply logic is in
`embed_core.py`, which is tested.
"""

import os
import time

import modal

from modal_apps import embed_core as core

EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
EMBED_GPU = os.environ.get("EMBED_GPU", "T4")
MODELS_DIR = "/models"
SLUG = EMBED_MODEL.split("/")[-1].lower().replace(".", "-")

# Pins are the latest release at least 14 days old on 2026-11-24 (dates in README.md).
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "sentence-transformers==6.2.0",
        "torch==2.15.0",
        "transformers==5.22.2",
        "fastapi[standard]==0.162.2",
    )
    .env({"HF_HOME": MODELS_DIR, "EMBED_MODEL": EMBED_MODEL, "EMBED_GPU": EMBED_GPU})
    .add_local_python_source("modal_apps")
)

app = modal.App(f"newsbrief-embed-{SLUG}")
volume = modal.Volume.from_name("newsbrief-embed-models", create_if_missing=True)

with image.imports():
    from fastapi.responses import JSONResponse
    from sentence_transformers import SentenceTransformer


@app.cls(
    image=image,
    gpu=EMBED_GPU,
    enable_memory_snapshot=True,  # CPU snapshot only: GPU snapshots are alpha
    scaledown_window=10,  # keep in step with EMBEDDING_MODAL_IDLE_SECONDS
    max_containers=1,
    volumes={MODELS_DIR: volume},
)
class Embedder:
    @modal.enter(snap=True)
    def load(self):
        # Runs before the snapshot, on CPU (the GPU is not reachable here).
        self.model = SentenceTransformer(
            EMBED_MODEL,
            cache_folder=MODELS_DIR,
            device="cpu",  # no torch.cuda probing inside the CPU-only snapshot
            model_kwargs={"dtype": core.DTYPE_BY_GPU[EMBED_GPU]},
        )
        self.model.max_seq_length = core.MAX_SEQ_TOKENS
        self.native_dim = self.model.get_sentence_embedding_dimension()
        self.warm = False

    @modal.enter(snap=False)
    def to_gpu(self):
        # Runs on every container start, after the snapshot is restored.
        t0 = time.monotonic()
        self.model.to("cuda")
        self.boot_s = time.monotonic() - t0
        self.warm = False

    @modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
    def encode(self, payload: dict) -> dict:
        t0 = time.monotonic()
        req = core.parse_request(
            payload, served_model=EMBED_MODEL, native_dim=self.native_dim
        )
        if isinstance(req, str):
            return JSONResponse(status_code=400, content={"error": req})
        cold = not self.warm
        self.warm = True
        texts = core.prepare(req.texts, req.kind, req.model)
        if texts:
            raw = self.model.encode(texts, normalize_embeddings=True)
            tokens = int(self.model.tokenize(texts)["attention_mask"].sum())
        else:
            raw, tokens = [], 0
        vectors = [core.truncate_and_normalise(v.tolist(), req.dim) for v in raw]
        return core.response(
            model=req.model,
            dim=req.dim,
            vectors=vectors,
            tokens=tokens,
            runtime_s=time.monotonic() - t0,
            boot_s=self.boot_s,
            cold_start=cold,
        )

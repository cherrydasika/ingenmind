"""A careful second pass over search results (R22): a cross-encoder reads the
question together with each candidate and scores how well it answers it;
retrieval.hybrid_search then keeps the best.

Runs in-process (fastembed's ONNX cross-encoders, as the local embedding
model does), downloaded once into FASTEMBED_CACHE_PATH.

    RERANK              on for searches that don't say (the classic pipeline,
                        the search test's default): true/false, default false.
                        A flow's Retriever has its own switch.
    RERANK_MODEL        the cross-encoder (default Xenova/ms-marco-MiniLM-L-6-v2,
                        about 80 MB; BAAI/bge-reranker-base is about 1 GB)
    RERANK_CANDIDATES   how many fused results it re-sorts (default 20)
"""

import os
import threading

from common import config

ENABLED = os.environ.get("RERANK", "false").strip().lower() in ("1", "true", "yes")
MODEL = os.environ.get("RERANK_MODEL", "").strip() or "Xenova/ms-marco-MiniLM-L-6-v2"
CANDIDATES = max(1, int(os.environ.get("RERANK_CANDIDATES", "20")))
MAX_CHARS = 2000          # of each candidate (the model reads ~512 tokens anyway)

_lock = threading.Lock()
_models: dict[str, object] = {}


def default_candidates() -> int:
    """How many candidates a search re-sorts when it doesn't say: 0 when off."""
    return CANDIDATES if ENABLED else 0


def _model(name: str):
    with _lock:
        if name not in _models:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            _models[name] = TextCrossEncoder(model_name=name, cache_dir=config.FASTEMBED_CACHE)
        return _models[name]


def scores(question: str, texts: list[str], model: str | None = None) -> list[float]:
    """One relevance score per text (higher is better; the scale is the model's)."""
    if not texts:
        return []
    return [float(s) for s in _model(model or MODEL).rerank(question, [t[:MAX_CHARS] for t in texts])]

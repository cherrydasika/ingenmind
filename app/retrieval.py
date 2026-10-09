"""Dense and full-text PostgreSQL retrieval, fused by reciprocal rank."""

import time

from common import config, embedding, storage
from index_info import get_index_info
from tracing import observation

TOP_K = 5
PREFETCH_LIMIT = TOP_K * 2
RRF_K = 2


def _hit_to_chunk(hit: dict) -> dict:
    return {
        "id": hit["id"], "text": hit["payload"]["text"],
        "source_url": hit["payload"]["source_url"],
        "chunk_index": hit["payload"]["chunk_index"],
        "score": hit["score"], "vector": hit["vector"],
        # For the evidence evaluator's freshness check.
        "ingested_at": hit["payload"].get("ingested_at"), "ttl_days": hit["payload"].get("ttl_days"),
        # What the page is about (#15): who published it and how current it is, for citations.
        "meta": {k: v for k, v in (hit["payload"].get("meta") or {}).items()
                 if k in ("organisation", "effective_date", "authority", "topic", "content_type")},
    }


def _fuse(dense_hits: list[dict], text_hits: list[dict], top_k: int,
          rrf_k: int = RRF_K) -> tuple[list[dict], list[dict]]:
    scores = {}
    hits = {}
    for ranking in (dense_hits, text_hits):
        for position, hit in enumerate(ranking):
            scores[hit["id"]] = scores.get(hit["id"], 0) + 1 / (rrf_k + position)
            hits[hit["id"]] = hit
    fused = []
    # Equal fused scores: the more authoritative source first (#15), then a stable order.
    authority = {hit_id: (((hit.get("payload") or {}).get("meta") or {}).get("authority") or 0)
                 for hit_id, hit in hits.items()}
    for hit_id in sorted(scores, key=lambda key: (-round(scores[key], 9), -authority[key], key))[:top_k]:
        fused.append({**hits[hit_id], "score": scores[hit_id]})
    breakdown = []
    dense_ids = [hit["id"] for hit in dense_hits]
    text_ids = [hit["id"] for hit in text_hits]
    for hit in fused:
        dense_position = dense_ids.index(hit["id"]) if hit["id"] in dense_ids else None
        text_position = text_ids.index(hit["id"]) if hit["id"] in text_ids else None
        breakdown.append({
            "dense_rank": dense_position + 1 if dense_position is not None else None,
            "dense_part": 1 / (rrf_k + dense_position) if dense_position is not None else 0,
            "sparse_rank": text_position + 1 if text_position is not None else None,
            "sparse_part": 1 / (rrf_k + text_position) if text_position is not None else 0,
        })
    return fused, breakdown


def ignore_stage(stage: str, state: str, **info) -> None:
    pass


def hybrid_search(question: str, top_k: int = TOP_K, on_stage=ignore_stage, prefetch: int | None = None,
                  rrf_k: int = RRF_K, dense: bool = True, full_text: bool = True, filters: dict | None = None) -> dict:
    """on_stage(stage, state, **info) is told when the embedding and retrieval
    stages start and finish, for the Retrieval page's live workflow chart.
    prefetch: candidates per search before fusion (default 2 × top_k, at
    least top_k); rrf_k: the reciprocal-rank-fusion constant; dense /
    full_text: run the vector or the keyword search (at least one). The query
    is embedded either way: the embedding plots show it. filters: only chunks
    whose labels match (storage.meta_filter, #15); none: every chunk, as before."""
    if not (dense or full_text):
        raise ValueError("hybrid_search needs the dense or the full-text search")
    prefetch = max(prefetch or top_k * 2, top_k)
    with observation(as_type="span", name="hybrid_search",
                     input={"question": question, "top_k": top_k, "prefetch": prefetch, "rrf_k": rrf_k,
                            **({"filters": filters} if filters else {})}) as span:
        client = storage.get_client()
        on_stage("embedding", "start")
        start = time.perf_counter()
        dense_vector = embedding.embed_texts([question], query=True)[0]
        embed_seconds = time.perf_counter() - start
        on_stage("embedding", "done", seconds=embed_seconds, model=config.EMBEDDING_MODEL, dim=len(dense_vector))

        on_stage("retrieval", "start")
        start = time.perf_counter()
        dense_hits = storage.search_dense(client, dense_vector, prefetch, filters) if dense else []
        dense_seconds = time.perf_counter() - start

        start = time.perf_counter()
        text_hits = storage.search_text(client, question, prefetch, filters) if full_text else []
        text_seconds = time.perf_counter() - start

        start = time.perf_counter()
        fused_hits, breakdown = _fuse(dense_hits, text_hits, top_k, rrf_k)
        fused_seconds = time.perf_counter() - start
        rankings = {
            "dense": [_hit_to_chunk(hit) for hit in dense_hits[:top_k]],
            "sparse": [_hit_to_chunk(hit) for hit in text_hits[:top_k]],
            "fused": [_hit_to_chunk(hit) for hit in fused_hits],
        }
        on_stage("retrieval", "done", seconds=dense_seconds + text_seconds + fused_seconds,
                 chunks=len(fused_hits), sources=len({hit["payload"]["source_url"] for hit in fused_hits}))
        result = {
            "model": config.EMBEDDING_MODEL,
            "vector_name": config.DENSE_VECTOR_NAME,
            "rankings": rankings,
            "dense_vector": dense_vector,
            "sparse_term_count": len(question.split()),
            "filters": filters or None,
            "explain": {
                "index": get_index_info(),
                "fused": {"k": rrf_k, "prefetch": prefetch, "rows": breakdown},
            },
            "timings": {
                "embed": embed_seconds, "dense_search": dense_seconds,
                "sparse_search": text_seconds, "fused_search": fused_seconds,
            },
            "error": None,
        }
        span.update(output={stage: [
            {"rank": rank + 1, "score": chunk["score"], "source_url": chunk["source_url"]}
            for rank, chunk in enumerate(chunks)
        ] for stage, chunks in rankings.items()})
        return {**result, "alt": {"model": None, "error": "Alternative embedding not configured"}}
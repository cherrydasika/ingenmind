"""Ad-hoc documents: pasted text goes through the same chunk → embed
(dense + sparse) → upsert path as the Airflow URL ingestion, keyed by a
synthetic source id instead of a URL.

Ingestion is incremental: only this document's chunks are embedded and
written. Existing chunks are never re-embedded — dense vectors and BM25
term-frequency weights are computed per chunk, and BM25's IDF is applied by
PostgreSQL at query time, so adding a document updates it automatically."""

import hashlib
import re
import time
from datetime import datetime, timezone

from common import chunking, config, dedup, embedding, storage

SCHEME = "adhoc://"


def source_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{SCHEME}{slug or 'untitled'}"


def preview(title: str, text: str) -> dict:
    """What a submit would do, without writing anything."""
    chunks = chunking.chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP) if text.strip() else []
    sid = source_id(title) if title.strip() else None
    existing = None
    if sid:
        try:
            existing = storage.get_existing_metadata(storage.get_client(), sid)
        except Exception:
            existing = None  # collection may not exist yet
    return {"source_id": sid, "chunks": len(chunks), "chars": len(text), "exists": existing is not None}


def ingest(title: str, text: str, ttl_days: float) -> dict:
    sid = source_id(title)
    t0 = time.perf_counter()
    client = storage.get_client()
    storage.ensure_collection(client)

    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = storage.get_existing_metadata(client, sid)
    if existing and existing.get("content_hash") == doc_hash:
        # Same title, same content: nothing to re-embed, just renew the lifetime.
        storage.refresh_expiry(client, sid, ttl_days)
        return {"status": "unchanged_ttl_refreshed", "source_id": sid, "chunks": 0,
                "seconds": time.perf_counter() - t0}

    # Same paragraph dedup as URL ingestion: drop paragraphs another source
    # already stores (or repeated within this document).
    text, owned_hashes, dedup_stats = dedup.dedupe_page(client, sid, text)
    chunks = chunking.chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    if not chunks:
        return {"status": "empty", "source_id": sid, "chunks": 0, "dedup": dedup_stats,
                "seconds": time.perf_counter() - t0}

    t_embed = time.perf_counter()
    vectors = embedding.embed_texts(chunks)
    t_embed = time.perf_counter() - t_embed

    storage.upsert_chunks(
        client, sid, chunks, vectors, doc_hash, ttl_days,
        extra_payload={"title": title.strip(), "source_type": "adhoc",
                       **{f"dedup_{k}": v for k, v in dedup_stats.items()}},
        paragraph_hashes=owned_hashes,
        replace_existing=bool(existing),
    )
    return {"status": "replaced" if existing else "added", "source_id": sid, "chunks": len(chunks),
            "dedup": dedup_stats, "embed_seconds": t_embed, "seconds": time.perf_counter() - t0}


def list_adhoc() -> list[dict]:
    """Ad-hoc documents currently stored, one row per document."""
    try:
        return storage.list_adhoc(storage.get_client())
    except Exception:
        return []


def expires_on(ttl_days: float) -> datetime:
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + ttl_days * 86400, tz=timezone.utc)

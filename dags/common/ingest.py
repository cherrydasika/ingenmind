"""Per-URL ingestion logic used by the manually triggered worker.

Failure policy:
- Per-URL fetch/extract errors are recorded without aborting the job.
- PostgreSQL and embedding errors stop the job; a new manual run skips fresh URLs."""

from __future__ import annotations

from datetime import datetime, timezone

from . import chunking, config, dedup, embedding, scraping, storage

def ingest_url(client: storage.PgStore, entry: dict, force: bool = False) -> dict:
    url = entry["url"]
    ttl_days = entry.get("ttl_days", config.DEFAULT_TTL_DAYS)
    existing = storage.get_existing_metadata(client, url)

    if existing is not None and not force:
        time_left_days = (existing["expires_at"] - datetime.now(timezone.utc).timestamp()) / 86400
        if time_left_days > config.REFRESH_MARGIN_DAYS:
            # Already ingested and not close to expiring: skip entirely, no
            # fetch. This is what keeps re-runs incremental as the list grows.
            return {"url": url, "status": "skipped_fresh"}

    pdf = entry.get("kind") == "pdf" or scraping.is_pdf(url)
    try:
        # fetch_* already retried transient errors
        page = scraping.fetch_bytes(url) if pdf else scraping.fetch_html(url)
    except Exception as exc:
        return {"url": url, "status": "failed", "stage": "fetch", "error": f"{type(exc).__name__}: {exc}"}
    try:
        text = scraping.extract_pdf_text(page[0], url) if pdf else scraping.extract_text(page, url)
    except Exception as exc:
        return {"url": url, "status": "failed", "stage": "extract", "error": f"{type(exc).__name__}: {exc}"}

    page_hash = scraping.content_hash(text)
    existing_hash = existing["content_hash"] if existing else None
    if existing_hash == page_hash and not force:
        # Content hasn't changed since last crawl: skip re-embedding, but
        # renew the TTL clock so a still-valid page doesn't get pruned.
        storage.refresh_expiry(client, url, ttl_days)
        return {"url": url, "status": "unchanged_ttl_refreshed"}

    # Drop paragraphs repeated within this page or already stored by another
    # page, before chunking. page_hash stays on the raw text so "unchanged
    # page" detection above still works.
    text, owned_hashes, dedup_stats = dedup.dedupe_page(client, url, text)
    chunks = chunking.chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    if not chunks:
        if existing_hash is not None:
            storage.delete_url_points(client, url)
        return {"url": url, "status": "empty", **dedup_stats}

    vectors = embedding.embed_texts(chunks)
    storage.upsert_chunks(
        client, url, chunks, vectors, page_hash, ttl_days,
        # entry["metadata"]: provenance, e.g. from the research agent.
        extra_payload={**{f"dedup_{k}": v for k, v in dedup_stats.items()}, **(entry.get("metadata") or {})},
        paragraph_hashes=owned_hashes,
        replace_existing=existing_hash is not None,
    )
    return {"url": url, "status": "updated", "chunks": len(chunks), **dedup_stats}

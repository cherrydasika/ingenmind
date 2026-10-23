"""Per-URL ingestion logic used by the manually triggered worker.

Failure policy:
- Per-URL fetch/extract errors are recorded without aborting the job.
- PostgreSQL and embedding errors stop the job; a new manual run skips fresh URLs."""

from __future__ import annotations

from datetime import datetime, timezone

from . import chunking, config, dedup, embedding, scraping, storage

# Payload fields ingestion writes itself (or per chunk): not carried over.
_OWN_FIELDS = {"source_url", "chunk_index", "text", "content_hash", "ingested_at", "ttl_days", "expires_at",
               "paragraph_hashes", "headings", "chunking", "page_date"}


def _page_fields(payload: dict | None) -> dict:
    return {k: v for k, v in (payload or {}).items() if k not in _OWN_FIELDS and not k.startswith("dedup_")}


def ingest_url(client: storage.PgStore, entry: dict, force: bool = False) -> dict:
    url = entry["url"]
    ttl_days = entry.get("ttl_days", config.DEFAULT_TTL_DAYS)
    existing = storage.get_existing_metadata(client, url)

    force = force or bool(entry.get("force"))
    # recheck (R49's re-crawl): fetch even when fresh, but an unchanged text is still only renewed.
    if existing is not None and not force and not entry.get("recheck"):
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
        text = scraping.extract_pdf_text(page[0], url) if pdf else scraping.extract_text(page, url, headings=chunking.wants_headings())
    except Exception as exc:
        return {"url": url, "status": "failed", "stage": "extract", "error": f"{type(exc).__name__}: {exc}"}

    # The page's own date (published or modified), when an HTML page states one:
    # the knowledge metadata's effective_date (#15).
    page_date = None
    if not pdf:
        try:
            page_date = scraping.extract_metadata(page, url).get("date")
        except Exception:
            page_date = None

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
    pieces = chunking.split(text)
    chunks = [p["text"] for p in pieces]
    if not chunks:
        if existing_hash is not None:
            storage.delete_url_points(client, url)
        return {"url": url, "status": "empty", **dedup_stats}

    # Chunks whose text is unchanged keep their vectors: only new or changed ones are embedded (R49).
    known = {c["text"]: c["vector"] for c in storage.page_chunks(client, url)} if existing_hash is not None else {}
    fresh = [c for c in dict.fromkeys(chunks) if c not in known]
    made = dict(zip(fresh, embedding.embed_texts(fresh))) if fresh else {}
    vectors = [known.get(c) or made[c] for c in chunks]
    storage.upsert_chunks(
        client, url, chunks, vectors, page_hash, ttl_days,
        # entry["metadata"]: provenance, e.g. from the research agent.
        # entry["keep_payload"]: what the page's chunks carried before (labels,
        # provenance), kept when it is cut again (python -m rechunk, R21).
        extra_payload={**_page_fields(existing if entry.get("keep_payload") else None),
                       "chunking": config.CHUNKING,
                       **{f"dedup_{k}": v for k, v in dedup_stats.items()},
                       **({"page_date": page_date} if page_date else {}), **(entry.get("metadata") or {})},
        paragraph_hashes=owned_hashes,
        replace_existing=existing_hash is not None,
        chunk_payloads=[{"headings": p["headings"]} if p["headings"] else {} for p in pieces],
    )
    return {"url": url, "status": "updated", "chunks": len(chunks), "embedded": len(fresh), **dedup_stats}

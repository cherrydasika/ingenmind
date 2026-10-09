"""Shared helpers for reading ingested-document data out of PostgreSQL, used by
the Ingestion and Sources pages of both UIs (summary stats, full table).

Pages ask for it on every load and refresh,
so this must stay cheap as the collection grows: it reads one point per
source (chunk_index == 0 — every chunk of a source carries the same
metadata), gets chunk counts from a facet on source_url instead of scanning
all points, and caches the result briefly."""

from datetime import datetime, timezone

from common import config, storage
from ttl_cache import ttl_cache

OVERVIEW_CACHE_SECONDS = 60


@ttl_cache(OVERVIEW_CACHE_SECONDS)
def fetch_qdrant_overview() -> tuple[int | None, list[dict]]:
    client = storage.get_client()
    try:
        count = storage.count_points(client)
        first_chunks = storage.source_summary(client)
    except Exception:
        return None, []

    rows = []
    for p in first_chunks:
        payload = p["payload"]
        url = payload.get("source_url")
        rows.append({
            "url": url,
            "ingested_at": payload.get("ingested_at", ""),
            "ttl_days": payload.get("ttl_days"),
            "expires_at": payload.get("expires_at"),
            # None = ingested before paragraph dedup existed.
            "dedup_paragraphs": (
                payload["dedup_removed_cross_page"] + payload.get("dedup_removed_within_page", 0)
                if "dedup_removed_cross_page" in payload else None
            ),
            "dedup_chars": payload.get("dedup_removed_chars"),
            "chunks": p["chunks"],
        })
    rows.sort(key=lambda r: r["ingested_at"], reverse=True)
    return count, rows


@ttl_cache(OVERVIEW_CACHE_SECONDS)
def fetch_research_overview() -> list[dict]:
    """Pages the research agent ingested (research_chunks), newest first,
    with why they were added; [] before the first one."""
    try:
        first_chunks = storage.source_summary(storage.get_client("research"))
    except Exception:
        return []
    rows = []
    for p in first_chunks:
        payload = p["payload"]
        rows.append({
            "url": payload.get("source_url"),
            "ingested_at": payload.get("ingested_at", ""),
            "expires_at": payload.get("expires_at"),
            "chunks": p["chunks"],
            "task": payload.get("research_task"),
            "publisher": payload.get("research_publisher"),
            "scores": payload.get("research_scores"),
        })
    return rows


def format_timestamp(iso_str: str) -> str:
    if not iso_str:
        return "—"
    return iso_str[:19].replace("T", " ")


def days_left(expires_at) -> str:
    if expires_at is None:
        return "—"
    delta_days = (expires_at - datetime.now(timezone.utc).timestamp()) / 86400
    if delta_days < 0:
        return "⚠️ expired"
    return f"{delta_days:.1f}d"

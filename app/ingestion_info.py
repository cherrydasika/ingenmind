"""What the Ingestion page shows about each manual job and run state — shared by
both UIs so the descriptions (with live config values) are written once."""

from common import config

STATE_ICON = {
    "success": "✅",
    "failed": "❌",
    "running": "🔄",
    "queued": "⏳",
    "up_for_retry": "🔁",
    "upstream_failed": "⛔",
}

JOB_INFO = {
    "ingest_urls": {
        "icon": "🔄",
        "label": "ingest_urls",
        "description": (
            "Manual trigger. Processes the first 10 configured URLs (imported from `data/urls.json`), in order, "
            "checkpointing progress after each URL. Fresh URLs are skipped; failed fetches "
            "are recorded. After a systemic failure, trigger a new run to retry."
        ),
    },
    "prune_expired_documents": {
        "icon": "🧹",
        "label": "prune_expired_documents",
        "description": (
            "Manual trigger. Deletes stored chunks whose TTL has expired, so "
            "time-sensitive content ages out automatically if it isn't re-crawled in time."
        ),
    },
}

STATUS_LABELS = {
    "updated": "✅ Updated", "skipped_fresh": "⏭️ Skipped (fresh)",
    "unchanged_ttl_refreshed": "🔁 Unchanged (TTL renewed)", "empty": "∅ Empty", "failed": "❌ Failed",
}

"""The stack's services, shortcuts and project links, with a reachability
check — for the web app's Home page and sidebar."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from common import storage

_LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000").rstrip("/")
_WEB_PUBLIC_URL = os.environ.get("WEB_PUBLIC_URL", "http://localhost:18000")

# "url" is what you click (from your browser: localhost). "health" is checked
# from inside the web app's container, so it uses the internal service names.
SERVICES = [
    {
        "icon": "🧭",
        "name": "Web app",
        "url": _WEB_PUBLIC_URL,
        "health": "http://webapp:8000/api/config",
        "description": (
            "The HTML/JS UI: Retrieval, Ad-hoc documents, manual Ingestion, Sources and Pipeline docs."
        ),
    },
    {
        "icon": "🗄️",
        "name": "PostgreSQL / pgvector",
        "url": None,
        "health": "postgresql",
        "description": "Chunk storage, vector search and full-text retrieval.",
    },
    {
        "icon": "📊",
        "name": "Langfuse",
        "url": "http://localhost:3000",
        "health": f"{_LANGFUSE_HOST}/api/public/health",
        "description": (
            "LLM observability — Traces and Sessions with cost, latency and tokens for every "
            "question. **Optional overlay** (`docker-compose.langfuse.yml`). Login: "
            "`admin@ragsystems.local` / password in `.env` (`LANGFUSE_INIT_USER_PASSWORD`)."
        ),
    },
    {
        "icon": "🪣",
        "name": "MinIO Console",
        "url": "http://localhost:9091",
        "health": "http://minio:9000/minio/health/live",
        "description": (
            "Object storage for Langfuse's media/event uploads. **Optional overlay**, same as "
            "Langfuse. Login: `minio` / password in `.env` (`MINIO_ROOT_PASSWORD`)."
        ),
    },
]

SHORTCUTS = []

PROJECT = [
    ("🐙", "GitHub repository", "https://github.com/cherrydasika/ingenmind",
     "Source code, commit history."),
    ("🔑", "Amazon Bedrock", "https://console.aws.amazon.com/bedrock/",
     "Managed model access and usage, when Bedrock is the LLM or embedding provider."),
]


def _is_up(url: str) -> bool:
    try:
        if url == "postgresql":
            storage.count_points(storage.get_client())
            return True
        return requests.get(url, timeout=2).status_code < 500
    except Exception:
        return False


STATUS_TTL_SECONDS = 30
_status_cache: dict = {"at": 0.0, "value": None}


def statuses() -> dict[str, bool]:
    """Reachability of each service, checked in parallel and cached briefly
    (the UI asks on every page load)."""
    if _status_cache["value"] is not None and time.monotonic() - _status_cache["at"] < STATUS_TTL_SECONDS:
        return _status_cache["value"]
    checks = {s["name"]: s["health"] for s in SERVICES if s["health"]}
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = dict(zip(checks, pool.map(_is_up, checks.values())))
    _status_cache.update(at=time.monotonic(), value=results)
    return results

"""The stack's services, shortcuts and project links, with a reachability
check — for the web app's Home page and sidebar."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import llm
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
            "The web app: Ask, the Agent Builder, Evaluations, the Knowledge base and Monitoring."
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



def listed() -> list[dict]:
    """SERVICES, with Ollama while the chat model is a local one (LLM_PROVIDER=ollama and
    no other model chosen in the app): up when it answers and has the agents' model, so
    a stopped Ollama or a model not pulled shows before a question fails."""
    if llm.PROVIDER != "ollama":
        return SERVICES
    return SERVICES[:1] + [{
        "icon": "🦙",
        "name": f"Ollama · {llm.MODEL}",
        "url": None,
        "health": "ollama",
        "description": f"Runs the agents' model ({llm.MODEL}) on this machine.",
        "fix": "Start Ollama (ollama serve, or brew services start ollama) and check the model is there "
               "(ollama list). scripts/start.sh does both. Or choose a hosted model under Overview → Chat model.",
    }] + SERVICES[1:]

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
        if url == "ollama":
            base = llm.OLLAMA_BASE_URL.rstrip("/").removesuffix("/v1")
            names = {m["name"] for m in requests.get(f"{base}/api/tags", timeout=2).json().get("models", [])}
            return llm.MODEL in names or f"{llm.MODEL}:latest" in names
        return requests.get(url, timeout=2).status_code < 500
    except Exception:
        return False


STATUS_TTL_SECONDS = 30
_status_cache: dict = {"at": 0.0, "value": None}


def statuses() -> dict[str, bool]:
    """Reachability of each service, checked in parallel and cached briefly
    (the UI asks on every page load)."""
    checks = {s["name"]: s["health"] for s in listed() if s["health"]}
    cached = _status_cache["value"]
    if cached is not None and cached.keys() == checks.keys() and time.monotonic() - _status_cache["at"] < STATUS_TTL_SECONDS:
        return cached   # (another model chosen since changes the list: checked again)
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = dict(zip(checks, pool.map(_is_up, checks.values())))
    _status_cache.update(at=time.monotonic(), value=results)
    return results

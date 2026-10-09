"""Build RAG (epic #27, #22): an approved ingestion plan read into the
knowledge base by the ingestion worker, and why each page is there.

    build_rag(version)   approve the reviewed plan (plan.approve), then start
    start(version)       queue one `ingest_plan` job; INGESTION_APPROVED → INGESTING
    run(client, version) the worker's part: first remove the chunks of pages the
                         previous plan had and this one dropped; then every
                         pending page, in order — robots.txt checked again
                         (cached per host), ingested through common.ingest with
                         its provenance, its result written to the plan as it
                         finishes (the checkpoint: a restart carries on with the
                         pending pages); then INGESTING → INDEXING, the indexing
                         check, INDEXING → EVALUATING
    retry()              failed pages back to pending, and a new job; also carries on a
                         build whose job stopped (at INGESTING or INDEXING)

Provenance on every chunk (payload, via entry["metadata"]): origin "setup",
plan_version, plan_position, source_id, source, content_id, section, areas,
approved_by, approved_at. A page already stored and still fresh is not
fetched again, but its chunks take the new plan's provenance.

If no page could be read, setup stays at INGESTING with the failures shown
(retry, or go back to the content).
"""

import threading
from typing import Callable
from urllib import robotparser
from urllib.parse import urlparse

import psycopg

import identity
import knowledge_system
from common import ingest, scraping, storage
from tracing import observation

from . import content, plan, site_map, sources, state

RESULT = {"updated": "ingested", "skipped_fresh": "unchanged", "unchanged_ttl_refreshed": "unchanged",
          "empty": "skipped", "failed": "failed"}
FAILURES_SHOWN = 20


def build_rag(version: int, user_id: str | None) -> dict:
    """Approve the reviewed plan and start reading it."""
    approved = plan.approve(version, user_id)
    start(version, user_id)
    return approved


def start(version: int, user_id: str | None = None) -> dict:
    """Queue the plan's job (from INGESTION_APPROVED, or again if queuing failed)."""
    if knowledge_system.status()["state"] != state.INGESTION_APPROVED:
        raise state.TransitionNotAllowed("the build starts once a plan is approved")
    approved = plan.latest("approved")
    if not approved or approved["version"] != version:
        raise ValueError("that is not the approved plan")
    job = _enqueue(version)
    state.transition(state.INGESTING, user_id, expected=state.INGESTION_APPROVED, plan_version=version,
                     job_id=job["id"])
    return job


def _enqueue(version: int) -> dict:
    try:
        return storage.enqueue_job(storage.get_client(), "ingest_plan", [{"plan_version": version}])
    except psycopg.errors.UniqueViolation as error:
        raise RuntimeError("another ingestion job is queued or running: try again when it ends") from error


def retry(user_id: str | None = None) -> dict:
    """The approved plan's failed pages read again, or a stopped build carried on."""
    current = knowledge_system.status()["state"]
    if current not in (state.INGESTING, state.INDEXING, state.EVALUATING):
        raise state.TransitionNotAllowed("failed pages can be retried after a build")
    approved = plan.latest("approved")
    if not approved:
        raise ValueError("there is no approved plan")
    with plan._connect() as connection:
        reset = connection.execute("UPDATE kb_ingestion_plan_pages SET status = 'pending', error = NULL "
                                   "WHERE version = %s AND status = 'failed'", (approved["version"],)).rowcount
    if not reset and current == state.EVALUATING:
        raise ValueError("no page failed")
    job = _enqueue(approved["version"])
    knowledge_system.record_event("build_retried", user_id, {"plan_version": approved["version"], "pages": reset})
    return job


# ---------- the worker's part ----------

class Robots:
    """robots.txt per host, read once per run; a site that refuses us blocks all its pages."""

    def __init__(self, fetch: Callable | None = None):
        self.fetch = fetch or scraping.fetch_bytes
        self.parsers: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self.parsers:
            try:
                parser, _, _ = site_map.read_robots(host, site_map.Fetcher(self.fetch, max_requests=1))
                self.parsers[host] = parser
            except site_map.Blocked:
                self.parsers[host] = None
        parser = self.parsers[host]
        return parser is not None and parser.can_fetch(scraping.USER_AGENT, url)


def _provenance(page: dict, approved: dict, hosts: dict[int, str]) -> dict:
    return {"origin": "setup", "plan_version": page["version"], "plan_position": page["position"],
            "source_id": page["source_id"], "source": hosts.get(page["source_id"]),
            "content_id": page["content_id"], "section": page["section"], "areas": page["areas"],
            "approved_by": approved["approved_by"], "approved_at": approved["approved_at"]}


def _record(version: int, url: str, status: str, chunks: int = 0, error: str | None = None) -> None:
    with plan._connect() as connection:
        connection.execute("""
            UPDATE kb_ingestion_plan_pages SET status = %s, chunks = %s, error = %s, ingested_at = now()
            WHERE version = %s AND url = %s""", (status, chunks, error, version, url))


def _remove_dropped(client: storage.PgStore, approved: dict) -> int:
    """Chunks of pages the previous approved plan had and this one does not."""
    with plan._connect() as connection:
        before = connection.execute(
            "SELECT max(version) AS v FROM kb_ingestion_plans WHERE status = 'superseded' AND version < %s",
            (approved["version"],)).fetchone()["v"]
    if before is None:
        return 0
    keep = {p["url"] for p in plan.pages(approved["version"])}
    dropped = [p["url"] for p in plan.pages(before) if p["url"] not in keep]
    for url in dropped:
        storage.delete_url_points(client, url)
    return len(dropped)


def _check_index(client: storage.PgStore, version: int) -> dict:
    """After ingestion: statistics refreshed, and every page read has chunks."""
    with client.connection() as connection:
        connection.execute(f"ANALYZE {client.table}")
        stored = {r["source_url"]: r["n"] for r in connection.execute(
            f"SELECT source_url, count(*) AS n FROM {client.table} WHERE payload->>'plan_version' = %s "
            "GROUP BY source_url", (str(version),)).fetchall()}
    read = [p["url"] for p in plan.pages(version) if p["status"] in ("ingested", "unchanged")]
    missing = [u for u in read if not stored.get(u)]
    return {"pages_with_chunks": len(read) - len(missing), "chunks": sum(stored.values()), "missing": missing[:20]}


def run(client: storage.PgStore, version: int, stop: threading.Event | None = None,
        ingest_url: Callable = ingest.ingest_url, robots: Robots | None = None) -> bool:
    """Read the plan's pending pages; True when none is left (the job is done)."""
    approved = plan.latest("approved")
    if not approved or approved["version"] != version:
        return True                    # superseded meanwhile: a newer plan has its own job
    robots = robots or Robots()
    hosts = {s["source_id"]: s["host"] for s in sources.list_sources()}
    with observation(as_type="span", name="ingestion_plan", input={"plan_version": version}) as span:
        removed = _remove_dropped(client, approved) if not plan.pages(version, "ingested") else 0
        for page in plan.pages(version, "pending"):
            if stop is not None and stop.is_set():
                return False
            if not robots.allowed(page["url"]):
                _record(version, page["url"], "blocked", error="robots.txt does not allow it")
                continue
            entry = {"url": page["url"], "ttl_days": page["ttl_days"], "kind": page["kind"],
                     "metadata": _provenance(page, approved, hosts)}
            result = ingest_url(client, entry)
            status = RESULT.get(result["status"], "failed")
            if status == "unchanged":      # not fetched again, or the same text: the chunks take this plan
                storage.merge_payload(client, page["url"], entry["metadata"])
            _record(version, page["url"], status, result.get("chunks", 0), result.get("error"))
        done = plan.view()
        span.update(output={"progress": done["progress"], "removed_pages": removed})
    _finish(client, version, done["progress"])
    return True


def _finish(client: storage.PgStore, version: int, progress: dict) -> None:
    current = knowledge_system.status()["state"]
    if current == state.EVALUATING:     # a retry after the build: check again, the state stays
        plan.set_build(version, {"progress": progress, **_check_index(client, version)})
        return
    if current not in (state.INGESTING, state.INDEXING):
        return
    if not (progress.get("ingested") or progress.get("unchanged")):
        knowledge_system.record_event("build_failed", None, {"plan_version": version, "progress": progress})
        return                          # nothing could be read: stay, with the failures shown
    if current == state.INGESTING:      # (at INDEXING already when a stopped build carries on)
        state.transition(state.INDEXING, expected=state.INGESTING, plan_version=version, progress=progress)
    checked = _check_index(client, version)
    plan.set_build(version, {"progress": progress, **checked})
    state.transition(state.EVALUATING, expected=state.INDEXING, plan_version=version, **checked)


# ---------- why is this here? ----------

def provenance(url: str) -> dict:
    """A stored page, back to the plan entry, section, source and approval it came from."""
    stored = storage.get_existing_metadata(storage.get_client(), url)
    if stored is None:
        raise LookupError("that page is not in the knowledge base")
    if stored.get("origin") != "setup":
        return {"url": url, "origin": stored.get("source_type") or "configured list",
                "ingested_at": stored.get("ingested_at")}
    version = int(stored["plan_version"])
    with plan._connect() as connection:
        entry = connection.execute("SELECT * FROM kb_ingestion_plan_pages WHERE version = %s AND url = %s",
                                   (version, url)).fetchone()
        approved = connection.execute("SELECT version, status, approved_by, approved_at FROM kb_ingestion_plans "
                                      "WHERE version = %s", (version,)).fetchone()
    try:
        section = content.get_section(int(stored["content_id"]))
    except LookupError:
        section = None
    source = next((s for s in sources.list_sources() if s["source_id"] == int(stored["source_id"])), None)
    approver = None
    if approved and approved["approved_by"]:
        try:
            user = identity.get_user(approved["approved_by"])
            approver = (user.get("first_name") or user.get("email")) if user else None
        except Exception:
            approver = None
    return {
        "url": url, "origin": "setup", "ingested_at": stored.get("ingested_at"),
        "plan": {**plan._iso(approved), "approver": approver} if approved else {"version": version},
        "entry": plan._iso(entry) if entry else None,
        "section": {k: section[k] for k in ("content_id", "name", "path_prefix", "reason", "areas", "relevance")}
                   if section else {"name": stored.get("section")},
        "source": {k: source[k] for k in ("source_id", "name", "host", "authority", "reason")}
                  if source else {"host": stored.get("source")},
    }


def view() -> dict:
    """The Build step: the approved plan, its progress, failures, and the check after indexing."""
    out = plan.view()
    if out["plan"]:
        failed = plan.pages(out["plan"]["version"], "failed") + plan.pages(out["plan"]["version"], "blocked")
        out["failures"] = [{k: p[k] for k in ("url", "section", "status", "error")} for p in failed[:FAILURES_SHOWN]]
        try:
            out["job"] = next(iter(storage.list_jobs(storage.get_client(), "ingest_plan", 1)), None)
        except psycopg.Error:           # no knowledge store yet: the page still shows the plan
            out["job"] = None
    return out

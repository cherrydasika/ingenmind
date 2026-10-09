"""Running the Sources step of setup (#12: approval 1): discovery in the
background, the user's choices, adding their own site, continuing.

    app_source_discoveries  one row per discovery run: status (researching |
                            ready | failed), its searches, an error

Discovery starts when the blueprint is confirmed (blueprint_run.confirm) and
moves setup from DISCOVERING_SOURCES to AWAITING_SOURCE_SELECTION when it is
done; it can be run again from there, keeping the user's choices. Only the
user selects: recommended sites are suggestions. Nothing is scraped, except
one request to check a site the user adds (and the research guardrail checks
it fits the scope). Continuing needs a selected source and moves setup to
ANALYSING_SOURCES, where #13 maps each chosen site's content.
"""

import re
import threading
from datetime import datetime
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import guardrails
import knowledge_system
import research
from common import config

from . import blueprint, content, conversation, sources, state

STALE_MINUTES = 15
CONTINUED = ("Thanks. Next I'll look through the sites you chose and show you their sections and pages, so you "
             "can choose exactly what goes into the knowledge base.")


def _start_thread(work) -> None:
    """Background work (tests replace this to run inline)."""
    threading.Thread(target=work, name="source-discovery", daemon=True).start()


# ---------- discovery runs ----------

_schema_lock = threading.Lock()
_schema_ready = False


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_source_discoveries (
                    run_id serial PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    status text NOT NULL,
                    record jsonb,
                    error text
                )""")
        _schema_ready = True


def latest_run() -> dict | None:
    ensure_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE app_source_discoveries SET status = 'failed', error = 'interrupted: please start again' "
            "WHERE status = 'researching' AND created_at < now() - make_interval(mins => %s)", (STALE_MINUTES,))
        row = connection.execute("SELECT * FROM app_source_discoveries ORDER BY run_id DESC LIMIT 1").fetchone()
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()} if row else None


def _confirmed_blueprint() -> dict:
    version = knowledge_system.status()["blueprint_version"]
    row = blueprint.get(version) if version else None
    if not row or not row["confirmed_at"]:
        raise ValueError("there is no confirmed blueprint")
    return row["blueprint"]


def start(discover=None) -> int:
    """Discover sources in the background; from DISCOVERING_SOURCES, or again from the selection."""
    current = knowledge_system.status()["state"]
    if current not in (state.DISCOVERING_SOURCES, state.AWAITING_SOURCE_SELECTION):
        raise state.TransitionNotAllowed("the sources step is not open now")
    bp = _confirmed_blueprint()
    latest_run()   # expires a stale run first
    ensure_schema()
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616704)")
        if connection.execute("SELECT 1 FROM app_source_discoveries WHERE status = 'researching'").fetchone():
            raise RuntimeError("sources are already being looked for")
        run_id = connection.execute("INSERT INTO app_source_discoveries (status) VALUES ('researching') "
                                    "RETURNING run_id").fetchone()["run_id"]

    def work():
        try:
            sites, record = (discover or sources.discover)(bp)
            sources.save_discovered(sites)
            status, error = "ready", None
        except Exception as exc:
            record, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:1000]
        with _connect() as connection:
            connection.execute("UPDATE app_source_discoveries SET status = %s, record = %s, error = %s WHERE run_id = %s",
                               (status, Jsonb(record) if record else None, error, run_id))
        if status == "ready" and knowledge_system.status()["state"] == state.DISCOVERING_SOURCES:
            state.transition(state.AWAITING_SOURCE_SELECTION, expected=state.DISCOVERING_SOURCES, run_id=run_id)
    _start_thread(work)
    return run_id


# ---------- the user's choices ----------

def _selecting() -> None:
    if knowledge_system.status()["state"] != state.AWAITING_SOURCE_SELECTION:
        raise state.TransitionNotAllowed("sources can be chosen once they have been found")


def choose(source_id: int, status: str) -> dict:
    _selecting()
    return sources.set_status(source_id, status)


GENERIC_TITLE = re.compile(r"^(home|homepage|home page|welcome|index|official site|official website)$", re.IGNORECASE)


def site_name(title: str | None, host: str) -> str:
    """A page title as a site name: "Homepage | Transport for Wales" → "Transport for Wales"."""
    parts = [p.strip() for p in re.split(r"\s[|–—-]\s", title or "") if p.strip()]
    named = [p for p in parts if not GENERIC_TITLE.match(p)]
    return (max(named, key=len) if named else host)[:200]


def add_site(url: str, check=None, fetch=None) -> dict:
    """The user's own site: one request to check it answers, then the scope check."""
    _selecting()
    url = (url or "").strip()
    parts = urlparse(url if "://" in url else f"https://{url}")
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ValueError("give a web address, such as https://www.example.org")
    url = parts.geturl()
    page = (fetch or research.fetch_source)(url)
    if page.get("error"):
        if " 401 " in page["error"] or " 403 " in page["error"]:
            raise ValueError("that site refuses automated access, so its pages could not be read into the "
                             "knowledge base")
        raise ValueError(f"that site did not answer: {page['error']}")
    bp = _confirmed_blueprint()
    scope = guardrails.Scope(scope=(bp.get("flow") or {}).get("scope") or bp.get("purpose") or "")
    # The question is about the site, not about the home page's news of the day
    # (live disruption notices made an operator's site look out of scope).
    question = (f"Could this website be a source of knowledge for this: {bp.get('purpose') or ''}? Judge the "
                "site's subject and owner from its home page, not the announcements of the day on it.")
    verdict = (check or guardrails.check)("research", question, (
        f"{page.get('title') or ''}\n{page.get('text') or ''}")[:6000], scope=scope)
    if not verdict.allowed:
        raise ValueError(f"that site does not fit this knowledge system's scope ({verdict.reason})")
    host = sources.host_of(url)
    site = {"host": host, "name": site_name(page.get("title"), host), "base_url": f"{parts.scheme}://{parts.hostname}",
            "kind": "website", "origin": "user",
            "authority": "high" if research.OFFICIAL_DOMAINS.search(host) else "medium", "relevance": 0.5,
            "areas": [], "reason": "Added by you", "evidence": [{"url": url, "title": page.get("title")}],
            "recommended": False}
    return sources.add_user_site(site)


def continue_(user_id: str | None = None) -> None:
    """The chosen sources are final for now: on to analysing their content (#13)."""
    _selecting()
    if not any(s["status"] == "selected" for s in sources.list_sources()):
        raise ValueError("choose at least one source")
    chosen = [s["host"] for s in sources.list_sources() if s["status"] == "selected"]
    state.transition(state.ANALYSING_SOURCES, user_id, expected=state.AWAITING_SOURCE_SELECTION, sources=chosen)
    conversation.add_turn("agent", CONTINUED, state.ANALYSING_SOURCES)
    try:
        content.start()            # the sites are analysed at once; progress shows on the page
    except Exception:
        pass                       # the page offers to analyse them again


def view() -> dict:
    """What the Sources step shows."""
    try:
        bp = _confirmed_blueprint()
    except ValueError:
        return {"run": None, "sources": [], "coverage": [], "live_areas": []}
    listed = sources.list_sources()
    return {"run": latest_run(), "sources": listed, "coverage": sources.coverage(bp, listed),
            "live_areas": [a["name"] for a in bp["knowledge_areas"] if a["knowledge_class"] not in sources.SOURCED_CLASSES]}

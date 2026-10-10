"""The one knowledge system an installation has: whether it is set up, the
URLs it ingests, and an audit log of what happened to it.

    app_knowledge_system         one row: state, origin, blueprint version,
                                 who set it up and when
    app_knowledge_system_events  audit log: event, user, time, details
    kb_urls                      the URLs to ingest, in order (was data/urls.json)

A fresh install starts NEW and is set up by the Initialization Agent
(epic #19). An install that already has knowledge when this record is first
created (every install from before setup existed) starts READY with origin
"existing", so it carries on unchanged. The demo seed marks itself READY
with origin "demo".

An existing data/urls.json is imported into kb_urls once; from then on the
database is the list. urls.example.json is a sample and never imported.
"""

import json
import threading
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

NEW, READY = "NEW", "READY"
ORIGINS = ("existing", "demo", "setup")
EXAMPLE_URLS = "urls.example.json"
_schema_lock = threading.Lock()
_schema_ready = False


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    """For tests that switch databases."""
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def ensure_schema() -> None:
    """Tables, the URL import and the record itself, once per process."""
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            # One transaction, serialised: the web app and the worker may both run the first run.
            connection.execute("SELECT pg_advisory_xact_lock(72616702)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_knowledge_system (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    state text NOT NULL,
                    origin text,
                    blueprint_version integer,
                    set_up_at timestamptz,
                    set_up_by text,
                    urls_imported_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_knowledge_system_events (
                    event_id bigserial PRIMARY KEY,
                    at timestamptz NOT NULL DEFAULT now(),
                    event text NOT NULL,
                    user_id text,
                    details jsonb NOT NULL DEFAULT '{}'
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb_urls (
                    url text PRIMARY KEY,
                    position bigserial,
                    ttl_days double precision,
                    origin text NOT NULL DEFAULT 'manual',
                    added_at timestamptz NOT NULL DEFAULT now(),
                    added_by text
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS kb_urls_position ON kb_urls (position)")
            row = connection.execute("SELECT 1 FROM app_knowledge_system").fetchone()
            if row is None:
                imported = _import_urls(connection)
                state = READY if imported or _has_knowledge(connection) else NEW
                connection.execute(
                    "INSERT INTO app_knowledge_system (state, origin, set_up_at, urls_imported_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (state, "existing" if state == READY else None,
                     datetime.now(timezone.utc) if state == READY else None,
                     datetime.now(timezone.utc) if imported else None))
                _event(connection, "created", None, {"state": state, "urls_imported": imported})
            elif connection.execute("SELECT urls_imported_at FROM app_knowledge_system").fetchone()["urls_imported_at"] is None:
                # A urls.json that appeared after the record was created (a new install given a list).
                if imported := _import_urls(connection):
                    connection.execute("UPDATE app_knowledge_system SET urls_imported_at = now(), updated_at = now()")
                    _event(connection, "urls_imported", None, {"urls": imported})
        _schema_ready = True


def _import_urls(connection) -> int:
    """Import data/urls.json into kb_urls (never the example list); the number imported."""
    path = config.URLS_CONFIG_PATH
    if path.name == EXAMPLE_URLS or not path.exists():
        return 0
    entries = [e for e in json.loads(path.read_text()) if isinstance(e, dict) and isinstance(e.get("url"), str)]
    with connection.cursor() as cursor:   # in file order, so position keeps it
        cursor.executemany("INSERT INTO kb_urls (url, ttl_days, origin) VALUES (%s, %s, 'import') "
                           "ON CONFLICT (url) DO NOTHING", [(e["url"], e.get("ttl_days")) for e in entries])
    return len(entries)


def _has_knowledge(connection) -> bool:
    for table in ("rag_chunks", "research_chunks"):
        if connection.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] and \
                connection.execute(f"SELECT EXISTS (SELECT 1 FROM {table}) AS any").fetchone()["any"]:
            return True
    return False


def _event(connection, event: str, user_id: str | None, details: dict) -> None:
    connection.execute("INSERT INTO app_knowledge_system_events (event, user_id, details) VALUES (%s, %s, %s)",
                       (event, user_id, Jsonb(details)))


def status() -> dict:
    """{"state", "origin", "blueprint_version", "set_up_at", "set_up_by", "urls_imported_at", "urls"}"""
    ensure_schema()
    try:
        row, urls = _read_status()
    except psycopg.errors.UndefinedTable:
        # The database was replaced since this process set it up (a new
        # install's database, or tests): set it up again, once.
        reset_schema_cache()
        ensure_schema()
        row, urls = _read_status()
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k != "singleton"}
    return {**out, "urls": urls}


def _read_status():
    with _connect() as connection:
        return (connection.execute("SELECT * FROM app_knowledge_system").fetchone(),
                connection.execute("SELECT count(*) AS n FROM kb_urls").fetchone()["n"])


def is_ready() -> bool:
    return status()["state"] == READY


def mark_ready(origin: str, user_id: str | None = None) -> None:
    """The knowledge system has knowledge to answer from (the demo seed; setup's go-live, #17)."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(ORIGINS)}")
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_knowledge_system SET state = %s, origin = %s, set_up_at = now(), "
                           "set_up_by = %s, updated_at = now()", (READY, origin, user_id))
        _event(connection, "ready", user_id, {"origin": origin})


def record_event(event: str, user_id: str | None, details: dict | None = None) -> None:
    """An entry in the audit log on its own (state changes record theirs)."""
    ensure_schema()
    with _connect() as connection:
        _event(connection, event, user_id, details or {})


def set_state(current: str, to: str, user_id: str | None = None, details: dict | None = None) -> bool:
    """Compare-and-set: move from `current` to `to`; False if the state was no
    longer `current`. Reaching READY this way means guided setup built it.
    Which moves are allowed is initialization/state.py's to decide."""
    ensure_schema()
    ready = to == READY
    with _connect() as connection:
        moved = connection.execute(
            "UPDATE app_knowledge_system SET state = %s, updated_at = now(), "
            "origin = CASE WHEN %s THEN 'setup' ELSE origin END, "
            "set_up_at = CASE WHEN %s THEN now() ELSE set_up_at END, "
            "set_up_by = CASE WHEN %s THEN %s ELSE set_up_by END "
            "WHERE state = %s", (to, ready, ready, ready, user_id, current)).rowcount == 1
        if moved:
            _event(connection, "state", user_id, {"from": current, "to": to, **(details or {})})
    return moved


def set_blueprint_version(version: int | None) -> None:
    """The Domain Blueprint version setup confirmed (initialization/blueprint.py)."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_knowledge_system SET blueprint_version = %s, updated_at = now()", (version,))


def urls(limit: int | None = None) -> list[dict]:
    """The URLs to ingest, in order: [{"url", "ttl_days"}] (ttl_days left out when unset)."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT url, ttl_days FROM kb_urls ORDER BY position LIMIT %s", (limit,)).fetchall()
    return [{"url": r["url"], **({"ttl_days": r["ttl_days"]} if r["ttl_days"] is not None else {})} for r in rows]


def events(limit: int = 20) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT event, user_id, at, details FROM app_knowledge_system_events "
                                  "ORDER BY event_id DESC LIMIT %s", (limit,)).fetchall()
    return [{**r, "at": r["at"].isoformat()} for r in rows]


# ---------- reset ----------
#
# Empties the knowledge and what was learnt from it, so setup can start
# again; keeps people (users, their sessions) and flow history. One
# transaction: all of it or none of it.

RESET_CONFIRMATION = "RESET"
EMPTIED = (
    ("rag_chunks", "Knowledge base pages (curated)"),
    ("research_chunks", "Knowledge base pages (added by research)"),
    ("ingestion_jobs", "Ingestion jobs"),
    ("kb_urls", "URLs to ingest"),
    ("research_source_profiles", "Data source profiles"),
    ("research_source_reports", "Data source reports"),
    ("agent_eval_results", "Evaluation results"),
    ("agent_eval_runs", "Evaluation runs"),
    ("agent_eval_sets", "Saved evaluation sets"),
    ("app_agent_memory", "Agent memory (learnings)"),
    ("agent_sessions", "Agent conversations (local harness)"),
    ("app_setup_turns", "Setup conversation"),
    ("app_setup", "Setup requirements"),
    ("app_domain_blueprints", "Domain blueprints"),
    ("kb_sources", "Sources found and chosen in setup"),
    ("app_source_discoveries", "Source discovery runs"),
    ("kb_site_analyses", "Analyses of the chosen sites"),
    ("kb_content", "Sections and pages chosen in setup"),
    ("kb_ingestion_plan_pages", "Pages of the ingestion plans"),
    ("kb_ingestion_plans", "Ingestion plans"),
    ("app_setup_evaluations", "Setup's evaluation preparations"),
    ("kb_labelling_runs", "Runs labelling the pages"),
)
KEPT = ("Users and permissions", "People's sessions and their history", "Flow versions and the live flow",
        "The embedding model setting", "Built-in evaluation sets", "Langfuse traces")


class Busy(RuntimeError):
    """An ingestion job or an evaluation run is queued or running."""


def _exists(connection, table: str) -> bool:
    return bool(connection.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"])


def counts() -> dict[str, int]:
    """Rows in each store a reset empties (0 for a store not created yet)."""
    ensure_schema()
    with _connect() as connection:
        return {table: (connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                        if _exists(connection, table) else 0) for table, _ in EMPTIED}


def reset(confirmation: str, user_id: str | None) -> dict[str, int]:
    """Empty every store in EMPTIED and set the state to NEW; returns the rows
    deleted per store. ValueError without the typed confirmation; Busy while
    an ingestion job or an evaluation run is queued or running."""
    if confirmation != RESET_CONFIRMATION:
        raise ValueError(f"type {RESET_CONFIRMATION} to confirm")
    ensure_schema()
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616702)")
        for table, what in (("ingestion_jobs", "an ingestion job"), ("agent_eval_runs", "an evaluation run")):
            if _exists(connection, table) and connection.execute(
                    f"SELECT EXISTS (SELECT 1 FROM {table} WHERE status IN ('queued', 'running')) AS busy"
            ).fetchone()["busy"]:
                raise Busy(f"{what} is queued or running: wait for it or cancel it first")
        deleted = {}
        for table, _ in EMPTIED:   # results before runs before sets: their order in EMPTIED
            deleted[table] = connection.execute(f"DELETE FROM {table}").rowcount if _exists(connection, table) else 0
        # urls_imported_at stays: data/urls.json is not imported again after a reset.
        connection.execute("UPDATE app_knowledge_system SET state = %s, origin = NULL, blueprint_version = NULL, "
                           "set_up_at = NULL, set_up_by = NULL, updated_at = now()", (NEW,))
        _event(connection, "reset", user_id, {"deleted": deleted})
    return deleted

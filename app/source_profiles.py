"""Data sources the research agent has profiled, and its reports.

    research_source_profiles  one row per (provider, name): the latest
                              verified SourceProfile, the gaps it was found
                              for (searchable) and when it was verified
    research_source_reports   one row per data-source gap: the profiles the
                              judge saw and its recommendation

A later gap looks up known profiles first (find()): fresh ones are reused
without profiling again; stale ones are verified again. Reports are shown to
admins (Ingestion tab); nothing in them is registered, ingested or coded.
"""

import re
import threading
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

MAX_TOPIC_CHARS = 4000     # gap text kept per profile, for lookups
MAX_QUERY_WORDS = 24
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
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS research_source_profiles (
                    provider text NOT NULL,
                    name text NOT NULL,
                    profile jsonb NOT NULL,
                    topic text NOT NULL DEFAULT '',
                    search tsvector GENERATED ALWAYS AS (to_tsvector('english',
                        name || ' ' || provider || ' ' || coalesce(profile->>'coverage', '') || ' ' || topic)) STORED,
                    verified_at timestamptz NOT NULL DEFAULT now(),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (provider, name)
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS research_source_profiles_search "
                               "ON research_source_profiles USING gin (search)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS research_source_reports (
                    report_id text PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    task text NOT NULL,
                    missing jsonb NOT NULL DEFAULT '[]',
                    brief text NOT NULL DEFAULT '',
                    profiles jsonb NOT NULL DEFAULT '[]',
                    recommendation jsonb NOT NULL DEFAULT '{}',
                    trace_id text
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS research_source_reports_created "
                               "ON research_source_reports (created_at DESC)")
        _schema_ready = True


def _key(profile: dict) -> tuple[str, str]:
    return (profile.get("provider") or "").strip().lower(), (profile.get("name") or "").strip().lower()


def upsert(profiles: list[dict], topic: str) -> None:
    """Store freshly verified profiles; a known source keeps the gaps it was found for."""
    if not profiles:
        return
    ensure_schema()
    with _connect() as connection:
        for profile in profiles:
            provider, name = _key(profile)
            if not provider or not name:
                continue
            connection.execute("""
                INSERT INTO research_source_profiles (provider, name, profile, topic, verified_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (provider, name) DO UPDATE SET
                    profile = EXCLUDED.profile, verified_at = now(),
                    topic = right(research_source_profiles.topic || ' ' || EXCLUDED.topic, %s)""",
                (provider, name, Jsonb(profile), topic[:MAX_TOPIC_CHARS], MAX_TOPIC_CHARS))


def _query(text: str) -> str:
    """A tsquery matching any of the text's words (OR), longest first."""
    words = sorted({w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text)}, key=len, reverse=True)
    return " | ".join(words[:MAX_QUERY_WORDS])


def find(text: str, limit: int, max_age_days: int, now: datetime | None = None) -> list[dict]:
    """Known profiles that match a gap, best first:
    [{"profile", "verified_at", "fresh"}] (fresh: verified within max_age_days)."""
    query = _query(text)
    if not query:
        return []
    ensure_schema()
    now = now or datetime.now(timezone.utc)
    with _connect() as connection:
        rows = connection.execute("""
            SELECT profile, verified_at FROM research_source_profiles, to_tsquery('english', %s) AS q
            WHERE search @@ q ORDER BY ts_rank(search, q) DESC, verified_at DESC LIMIT %s""",
            (query, limit)).fetchall()
    return [{"profile": r["profile"], "verified_at": r["verified_at"].isoformat(),
             "fresh": (now - r["verified_at"]).total_seconds() < max_age_days * 86400} for r in rows]


def save_report(task: str, missing: list[str], brief: str, profiles: list[dict], recommendation: dict,
                trace_id: str | None = None) -> str:
    ensure_schema()
    report_id = f"r_{uuid.uuid4().hex}"
    with _connect() as connection:
        connection.execute("""
            INSERT INTO research_source_reports (report_id, task, missing, brief, profiles, recommendation, trace_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (report_id, task, Jsonb(missing), brief, Jsonb(profiles), Jsonb(recommendation), trace_id))
    return report_id


def list_reports(limit: int = 50) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM research_source_reports ORDER BY created_at DESC LIMIT %s",
                                  (limit,)).fetchall()
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


def delete_reports(report_ids: list[str]) -> int:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("DELETE FROM research_source_reports WHERE report_id = ANY(%s)",
                                  (list(report_ids),)).rowcount

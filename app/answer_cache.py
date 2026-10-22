"""Answers people asked for before, reused (R19).

A question on the Ask page first looks for a cached answer to a question
that means the same, asked of the same live flow version, against the same
knowledge base (knowledge()): a hit answers in milliseconds and spends no
model tokens. "Means the same" is strict, because a wrong hit is a wrong
answer: the two questions' key words (signature(): everything but question
words, filler and words like price or cost) must be the same, in the same
order, and their embeddings must be at least SIMILARITY alike. So "What is
the price of an Anytime return from A to B?" reuses "How much is an
Anytime return from A to B?", but an Off-Peak return, a single, or B to A
don't. What is cached is what users would have been shown anyway:
an answer that passed the answer check, with no personal data, no live
tool (weather, timetables: they change), no research (it changes the
knowledge base) and not a follow-up (it depends on the conversation).

Going live with another version, starting over and going live with a new
domain clear the cache; a change to the knowledge base (ingestion, expiry,
removal) makes its entries miss, and old ones are pruned. Admins can see
the hit rate and clear it on Monitoring → Overview.

Settings: ANSWER_CACHE (true), ANSWER_CACHE_SIMILARITY (0.97),
ANSWER_CACHE_HOURS (24).

    answer_cache        flow_id, version, knowledge, question, embedding,
                        result (the public result), created_at, hits
    answer_cache_stats  day, hits, misses
"""
import logging
import os
import re
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import pii
from common import config

log = logging.getLogger(__name__)
SCHEMA_LOCK = 72616713

_schema_ready = False
_schema_lock = threading.Lock()


# Words that don't change what is asked: question words, filler, and how a price is asked for.
IGNORED = set("""a an the of for from to in on at by with and or about into via per
how what which when where who whom whose why is are was were be been being am do does did done can could would
should will shall may might must i me my mine we us our ours you your yours it its this that these those there
here please tell know let need want like get give show find much many cost costs price prices priced fare fares
charge charges pay how's what's whats it's is't""".split())


def signature(question: str) -> str:
    """The question's key words in order: two questions reuse an answer only if these match."""
    words = re.findall(r"[0-9]+(?:[.,][0-9]+)*|[a-z]+", question.lower().replace("-", " "))
    return " ".join(w for w in words if w not in IGNORED)


def enabled() -> bool:
    return os.environ.get("ANSWER_CACHE", "true").strip().lower() not in ("0", "false", "no")


def similarity() -> float:
    return float(os.environ.get("ANSWER_CACHE_SIMILARITY", "0.97") or 0.97)


def hours() -> float:
    return float(os.environ.get("ANSWER_CACHE_HOURS", "24") or 24)


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
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS answer_cache (
                    id bigserial PRIMARY KEY,
                    flow_id text NOT NULL,
                    version text NOT NULL,
                    knowledge text NOT NULL,
                    question text NOT NULL,
                    signature text NOT NULL DEFAULT '',
                    embedding vector NOT NULL,
                    result jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    hits integer NOT NULL DEFAULT 0)""")
            connection.execute("ALTER TABLE answer_cache ADD COLUMN IF NOT EXISTS signature text NOT NULL DEFAULT ''")
            connection.execute("CREATE INDEX IF NOT EXISTS answer_cache_key ON answer_cache "
                               "(flow_id, version, knowledge, signature)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS answer_cache_stats (
                    day date PRIMARY KEY DEFAULT current_date,
                    hits integer NOT NULL DEFAULT 0,
                    misses integer NOT NULL DEFAULT 0)""")
        _schema_ready = True


def knowledge(connection) -> str:
    """A fingerprint of the knowledge base: it changes when pages are added, expire or are removed."""
    parts = []
    for table in ("rag_chunks", "research_chunks"):
        try:
            with connection.transaction():
                row = connection.execute(f"SELECT count(*) AS n, max(expires_at) AS last FROM {table}").fetchone()
            parts.append(f"{row['n']}:{row['last'].isoformat() if row['last'] else ''}")
        except psycopg.errors.UndefinedTable:
            parts.append("-")
    return "|".join(parts)


def _embed(question: str) -> list[float]:
    from common.embedding import embed_texts
    return embed_texts([question], query=True)[0]


def _count(connection, column: str) -> None:
    connection.execute(f"""
        INSERT INTO answer_cache_stats (day, {column}) VALUES (current_date, 1)
        ON CONFLICT (day) DO UPDATE SET {column} = answer_cache_stats.{column} + 1""")


def eligible_question(question: str, follow_up: bool) -> bool:
    return enabled() and not follow_up and not pii.kinds(question)


def lookup(question: str, flow_id: str, version) -> dict | None:
    """A cached public result for this question, or None. Never raises."""
    try:
        ensure_schema()
        vector = _embed(question)
        with _connect() as connection:
            row = connection.execute("""
                SELECT id, result, 1 - (embedding <=> %s::vector) AS similarity FROM answer_cache
                WHERE flow_id = %s AND version = %s AND knowledge = %s AND signature = %s AND vector_dims(embedding) = %s
                  AND created_at > now() - make_interval(secs => %s)
                ORDER BY embedding <=> %s::vector LIMIT 1""",
                (str(vector), flow_id, str(version), knowledge(connection), signature(question), len(vector),
                 hours() * 3600, str(vector))).fetchone()
            if row and row["similarity"] >= similarity():
                connection.execute("UPDATE answer_cache SET hits = hits + 1 WHERE id = %s", (row["id"],))
                _count(connection, "hits")
                return row["result"]
            _count(connection, "misses")
    except Exception as error:
        log.warning("answer cache lookup failed: %s", error)
    return None


def cacheable(result: dict) -> bool:
    """Only what users were shown as a checked answer, using nothing that changes."""
    check = result.get("answer_check") or {}
    blocked = any((g or {}).get("decision") not in (None, "ALLOW") for g in (result.get("guardrails") or {}).values())
    return (check.get("passed") is True and not blocked and not result.get("calls") and not result.get("research")
            and not (result.get("budget") or {}).get("hit") and not result.get("privacy"))


def store(question: str, flow_id: str, version, result: dict) -> None:
    """Keep a result if it may be reused. Never raises."""
    if not enabled() or not cacheable(result):
        return
    try:
        ensure_schema()
        vector = _embed(question)
        public = {k: v for k, v in result.items() if k != "trace_id"}
        with _connect() as connection:
            connection.execute("""
                INSERT INTO answer_cache (flow_id, version, knowledge, question, signature, embedding, result)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)""",
                (flow_id, str(version), knowledge(connection), question[:1000], signature(question), str(vector),
                 Jsonb(public)))
            connection.execute("DELETE FROM answer_cache WHERE created_at < now() - make_interval(secs => %s)",
                               (hours() * 3600,))
    except Exception as error:
        log.warning("answer cache store failed: %s", error)


def clear() -> int:
    """Forget every cached answer; returns how many there were."""
    try:
        ensure_schema()
        with _connect() as connection:
            return connection.execute("DELETE FROM answer_cache").rowcount
    except Exception as error:
        log.warning("answer cache clear failed: %s", error)
        return 0


def stats(days: int = 7) -> dict:
    ensure_schema()
    with _connect() as connection:
        entries = connection.execute("SELECT count(*) AS n FROM answer_cache").fetchone()["n"]
        totals = connection.execute("""
            SELECT coalesce(sum(hits), 0) AS hits, coalesce(sum(misses), 0) AS misses FROM answer_cache_stats
            WHERE day > current_date - %s""", (days,)).fetchone()
    asked = totals["hits"] + totals["misses"]
    return {"enabled": enabled(), "entries": entries, "hits": totals["hits"], "misses": totals["misses"],
            "hit_rate": round(totals["hits"] / asked, 3) if asked else None, "days": days,
            "similarity": similarity(), "hours": hours()}

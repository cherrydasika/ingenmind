"""Each user's sessions and their history. The server decides the session:
a question joins the user's current session when it was active in the last
IDLE_MINUTES, else a new one starts; signing in starts fresh and signing out
ends it. Every question asked on the Retrieval page is a turn: what was
asked, the answer the user saw, the sources it cites, the answer check, the
flow version, timings, and the Langfuse trace for the full detail. History
older than RETENTION_DAYS is deleted.

    app_sessions       session_id, user_id, started_at, last_activity,
                       ended_at, status, title (the first question)
    app_session_turns  one row per question: question, answer, result (the
                       public result: sources, answer check, guardrails,
                       activities, flow, timings), trace_id, asked_at
"""

import threading
import time
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import identity
from common import config

IDLE_MINUTES = 30
RETENTION_DAYS = 30
PRUNE_EVERY_SECONDS = 3600
TITLE_CHARS = 80
# What of a public result a turn keeps: enough to show the answer again and
# redraw the workflow graph; numbers, labels and URLs, no chunk text.
RESULT_KEYS = ("answer", "sources", "answer_check", "guardrails", "activities", "rounds", "total", "flow", "status")

_schema_ready = False
_schema_lock = threading.Lock()
_last_prune = 0.0


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    """For tests that switch databases."""
    global _schema_ready, _last_prune
    with _schema_lock:
        _schema_ready = False
        _last_prune = 0.0


def ensure_schema() -> None:
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        identity.ensure_schema()   # sessions are listed with their user's name
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_sessions (
                    session_id text PRIMARY KEY,
                    user_id text NOT NULL,
                    started_at timestamptz NOT NULL DEFAULT now(),
                    last_activity timestamptz NOT NULL DEFAULT now(),
                    ended_at timestamptz,
                    status text NOT NULL DEFAULT 'active',
                    title text NOT NULL DEFAULT ''
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS app_sessions_user ON app_sessions (user_id, last_activity DESC)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_session_turns (
                    turn_id bigserial PRIMARY KEY,
                    session_id text NOT NULL REFERENCES app_sessions (session_id) ON DELETE CASCADE,
                    user_id text NOT NULL,
                    asked_at timestamptz NOT NULL DEFAULT now(),
                    question text NOT NULL,
                    answer text NOT NULL DEFAULT '',
                    result jsonb NOT NULL DEFAULT '{}',
                    trace_id text
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS app_session_turns_session ON app_session_turns (session_id, turn_id)")
        _schema_ready = True


def current(user_id: str, session_id: str | None) -> str:
    """The session a new question belongs to: session_id when it is this
    user's, active, and was used in the last IDLE_MINUTES; else a new one
    (any older active session of the user is ended)."""
    ensure_schema()
    with _connect() as connection:
        if session_id:
            row = connection.execute(
                "SELECT session_id FROM app_sessions WHERE session_id = %s AND user_id = %s AND status = 'active' "
                "AND last_activity > now() - make_interval(mins => %s)", (session_id, user_id, IDLE_MINUTES)).fetchone()
            if row:
                return row["session_id"]
        connection.execute("UPDATE app_sessions SET status = 'ended', ended_at = last_activity "
                           "WHERE user_id = %s AND status = 'active'", (user_id,))
        new_id = f"s_{uuid.uuid4().hex}"
        connection.execute("INSERT INTO app_sessions (session_id, user_id) VALUES (%s, %s)", (new_id, user_id))
        return new_id


def end(user_id: str, session_id: str | None) -> None:
    """Signing out ends the session."""
    if not session_id:
        return
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_sessions SET status = 'ended', ended_at = now() "
                           "WHERE session_id = %s AND user_id = %s AND status = 'active'", (session_id, user_id))


def record_turn(session_id: str, user_id: str, question: str, result: dict, trace_id: str | None = None) -> None:
    """One question and what the user was shown."""
    ensure_schema()
    kept = {key: result.get(key) for key in RESULT_KEYS if key in result}
    with _connect() as connection:
        connection.execute(
            "INSERT INTO app_session_turns (session_id, user_id, question, answer, result, trace_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, user_id, question, result.get("answer") or "", Jsonb(kept), trace_id))
        connection.execute(
            "UPDATE app_sessions SET last_activity = now(), "
            "title = CASE WHEN title = '' THEN %s ELSE title END WHERE session_id = %s",
            (question.strip()[:TITLE_CHARS], session_id))
    prune()


def prune(force: bool = False) -> int:
    """Delete sessions (and their turns) last used more than RETENTION_DAYS
    ago; at most once an hour unless forced. Returns how many went."""
    global _last_prune
    if not force and time.monotonic() - _last_prune < PRUNE_EVERY_SECONDS and _last_prune:
        return 0
    _last_prune = time.monotonic()
    ensure_schema()
    with _connect() as connection:
        gone = connection.execute("DELETE FROM app_sessions WHERE last_activity < now() - make_interval(days => %s)",
                                  (RETENTION_DAYS,)).rowcount
    return gone


def list_sessions(user_id: str | None = None, limit: int = 200) -> list[dict]:
    """Sessions, newest first, with their number of turns; user_id None: everyone's."""
    ensure_schema()
    where, params = ("WHERE s.user_id = %s", [user_id]) if user_id else ("", [])
    with _connect() as connection:
        return connection.execute(f"""
            SELECT s.session_id, s.user_id, u.first_name, s.started_at, s.last_activity, s.ended_at,
                   s.status, s.title, count(t.turn_id) AS turns
            FROM app_sessions s
            LEFT JOIN app_users u ON u.user_id = s.user_id
            LEFT JOIN app_session_turns t ON t.session_id = s.session_id
            {where}
            GROUP BY s.session_id, u.first_name
            HAVING count(t.turn_id) > 0
            ORDER BY s.last_activity DESC
            LIMIT %s""", (*params, limit)).fetchall()


def get_session(session_id: str) -> dict | None:
    """A session and its turns (oldest first), or None."""
    ensure_schema()
    with _connect() as connection:
        session = connection.execute(
            "SELECT s.*, u.first_name FROM app_sessions s LEFT JOIN app_users u ON u.user_id = s.user_id "
            "WHERE s.session_id = %s", (session_id,)).fetchone()
        if not session:
            return None
        turns = connection.execute(
            "SELECT turn_id, asked_at, question, answer, result, trace_id FROM app_session_turns "
            "WHERE session_id = %s ORDER BY turn_id", (session_id,)).fetchall()
    return {**session, "turns": turns}

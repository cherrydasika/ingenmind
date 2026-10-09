"""The setup conversation and the requirements it gathers.

    app_setup_turns  every message, in order: who said it (user or agent),
                     the text, the setup state at the time, its trace
    app_setup        one row: the requirements gathered so far, whether the
                     agent judged them complete, and when the user confirmed

The transcript is what the supervisor sends the model each turn, so setup
resumes after any pause. A reset (knowledge_system.py) empties both.
"""

import threading
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

ROLES = ("user", "agent")
MAX_TEXT_CHARS = 8000
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
                CREATE TABLE IF NOT EXISTS app_setup_turns (
                    turn_id bigserial PRIMARY KEY,
                    at timestamptz NOT NULL DEFAULT now(),
                    role text NOT NULL,
                    text text NOT NULL,
                    state text NOT NULL,
                    trace_id text
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_setup (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    requirements jsonb NOT NULL DEFAULT '{}',
                    complete boolean NOT NULL DEFAULT false,
                    confirmed_at timestamptz,
                    confirmed_by text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k != "singleton"}


def add_turn(role: str, text: str, state: str, trace_id: str | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    text = (text or "").strip()[:MAX_TEXT_CHARS]
    if not text:
        raise ValueError("a turn needs text")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("INSERT INTO app_setup_turns (role, text, state, trace_id) VALUES (%s, %s, %s, %s) "
                                 "RETURNING *", (role, text, state, trace_id)).fetchone()
    return _iso(row)


def turns(last: int | None = None) -> list[dict]:
    """The conversation, oldest first; `last`: only the most recent ones."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM (SELECT * FROM app_setup_turns ORDER BY turn_id DESC LIMIT %s) t "
                                  "ORDER BY turn_id", (last,)).fetchall()
    return [_iso(r) for r in rows]


def requirements() -> dict:
    """{"requirements", "complete", "confirmed_at", "confirmed_by", "updated_at"}"""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM app_setup").fetchone()
    return _iso(row) if row else {"requirements": {}, "complete": False, "confirmed_at": None,
                                  "confirmed_by": None, "updated_at": None}


def save_requirements(values: dict, complete: bool) -> None:
    """Replace the requirements gathered so far; changing them withdraws a confirmation."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("""
            INSERT INTO app_setup (requirements, complete) VALUES (%s, %s)
            ON CONFLICT (singleton) DO UPDATE SET requirements = EXCLUDED.requirements,
                complete = EXCLUDED.complete, confirmed_at = NULL, confirmed_by = NULL, updated_at = now()""",
            (Jsonb(values), complete))


def confirm(user_id: str | None) -> None:
    ensure_schema()
    with _connect() as connection:
        if connection.execute("UPDATE app_setup SET confirmed_at = now(), confirmed_by = %s, updated_at = now() "
                              "WHERE complete", (user_id,)).rowcount != 1:
            raise ValueError("the requirements are not complete yet")

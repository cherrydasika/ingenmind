"""Per-user rate limits and daily token quotas (R06).

Every question costs model tokens, so each person gets a number of questions
per minute and of tokens per day; a deployment can also cap everyone's
tokens per day together. Over a limit, a question gets 429 with how long to
wait. Counters live in PostgreSQL, so restarts don't reset them; days are
UTC days.

    app_usage          user_id, day, questions, tokens
    app_usage_minute   user_id, minute, questions (pruned after an hour)
    app_user_limits    user_id, per_minute, tokens_per_day: an admin's
                       override of the defaults (NULL keeps the default)

Defaults (environment):
    LIMIT_QUESTIONS_PER_MINUTE     everyone but admins    (10)
    LIMIT_TOKENS_PER_DAY                                  (500000)
    ADMIN_QUESTIONS_PER_MINUTE     admins                 (30)
    ADMIN_TOKENS_PER_DAY                                  (5000000)
    GLOBAL_TOKENS_PER_DAY          all users together; 0 = no cap (0)
0 for a per-user limit means no limit.

Limits protect spend, not the app: if the counters can't be read the
question goes ahead (and the error is logged).
"""
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

import identity
from common import config

log = logging.getLogger(__name__)
PRUNE_MINUTES = 60
SCHEMA_LOCK = 72616711   # pg_advisory_xact_lock: two first requests create the tables once

_schema_ready = False
_schema_lock = threading.Lock()


def _env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def defaults(role: str) -> dict:
    if role == "admin":
        return {"per_minute": _env("ADMIN_QUESTIONS_PER_MINUTE", 30),
                "tokens_per_day": _env("ADMIN_TOKENS_PER_DAY", 5_000_000)}
    return {"per_minute": _env("LIMIT_QUESTIONS_PER_MINUTE", 10),
            "tokens_per_day": _env("LIMIT_TOKENS_PER_DAY", 500_000)}


def global_tokens_per_day() -> int:
    return _env("GLOBAL_TOKENS_PER_DAY", 0)


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
        identity.ensure_schema()
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_usage (
                    user_id text NOT NULL,
                    day date NOT NULL,
                    questions integer NOT NULL DEFAULT 0,
                    tokens bigint NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day))""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_usage_minute (
                    user_id text NOT NULL,
                    minute timestamptz NOT NULL,
                    questions integer NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, minute))""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_user_limits (
                    user_id text PRIMARY KEY,
                    per_minute integer CHECK (per_minute >= 0),
                    tokens_per_day bigint CHECK (tokens_per_day >= 0),
                    updated_by text,
                    updated_at timestamptz NOT NULL DEFAULT now())""")
        _schema_ready = True


@dataclass
class Refusal:
    message: str
    retry_after: int   # seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _until_tomorrow(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()) + 1)


def _wait_text(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 2 * 3600:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600)} hours"


def limits_for(user: dict, overrides: dict | None = None) -> dict:
    """The user's limits: their override where set, else their role's default."""
    base = defaults(user.get("role", "user"))
    own = overrides or {}
    return {k: own[k] if own.get(k) is not None else base[k] for k in base}


def _overrides(connection, user_id: str) -> dict | None:
    return connection.execute("SELECT per_minute, tokens_per_day FROM app_user_limits WHERE user_id = %s",
                              (user_id,)).fetchone()


def admit(user: dict, now: datetime | None = None) -> Refusal | None:
    """Count one question for the user, or say why it must wait (nothing counted)."""
    now = now or _now()
    try:
        ensure_schema()
        with _connect() as connection:
            limits = limits_for(user, _overrides(connection, user["user_id"]))
            day, minute = now.date(), now.replace(second=0, microsecond=0)
            used = connection.execute("SELECT questions, tokens FROM app_usage WHERE user_id = %s AND day = %s",
                                      (user["user_id"], day)).fetchone() or {"questions": 0, "tokens": 0}
            if limits["tokens_per_day"] and used["tokens"] >= limits["tokens_per_day"]:
                wait = _until_tomorrow(now)
                return Refusal(f"You've used today's allowance of {limits['tokens_per_day']:,} tokens. "
                               f"It renews in {_wait_text(wait)}.", wait)
            cap = global_tokens_per_day()
            if cap:
                total = connection.execute("SELECT coalesce(sum(tokens), 0) AS n FROM app_usage WHERE day = %s",
                                           (day,)).fetchone()["n"]
                if total >= cap:
                    wait = _until_tomorrow(now)
                    return Refusal(f"The assistant is busy: today's allowance for everyone is used up. "
                                   f"Please try again in {_wait_text(wait)}.", wait)
            this_minute = connection.execute(
                "SELECT questions FROM app_usage_minute WHERE user_id = %s AND minute = %s",
                (user["user_id"], minute)).fetchone()
            if limits["per_minute"] and this_minute and this_minute["questions"] >= limits["per_minute"]:
                wait = max(1, 60 - now.second)
                return Refusal(f"That's {limits['per_minute']} questions this minute, the most at once. "
                               f"Please wait {_wait_text(wait)}.", wait)
            connection.execute("""
                INSERT INTO app_usage_minute (user_id, minute, questions) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, minute) DO UPDATE SET questions = app_usage_minute.questions + 1""",
                               (user["user_id"], minute))
            connection.execute("""
                INSERT INTO app_usage (user_id, day, questions) VALUES (%s, %s, 1)
                ON CONFLICT (user_id, day) DO UPDATE SET questions = app_usage.questions + 1""",
                               (user["user_id"], day))
            connection.execute("DELETE FROM app_usage_minute WHERE minute < %s",
                               (minute - timedelta(minutes=PRUNE_MINUTES),))
    except psycopg.Error as error:
        log.warning("usage limits unavailable, the question goes ahead: %s", error)
    return None


def charge(user_id: str | None, tokens: int, now: datetime | None = None) -> None:
    """Add a finished run's tokens to the user's day."""
    if not user_id or not tokens:
        return
    try:
        ensure_schema()
        with _connect() as connection:
            connection.execute("""
                INSERT INTO app_usage (user_id, day, tokens) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, day) DO UPDATE SET tokens = app_usage.tokens + EXCLUDED.tokens""",
                               (user_id, (now or _now()).date(), int(tokens)))
    except psycopg.Error as error:
        log.warning("could not record %s tokens for %s: %s", tokens, user_id, error)


def overview(users: list[dict], now: datetime | None = None) -> dict:
    """For the Users page: {user_id: {"today": {questions, tokens}, "limits", "override"}}."""
    ensure_schema()
    day = (now or _now()).date()
    with _connect() as connection:
        used = {r["user_id"]: r for r in connection.execute(
            "SELECT user_id, questions, tokens FROM app_usage WHERE day = %s", (day,))}
        own = {r["user_id"]: r for r in connection.execute(
            "SELECT user_id, per_minute, tokens_per_day FROM app_user_limits")}
    return {u["user_id"]: {
        "today": {"questions": used.get(u["user_id"], {}).get("questions", 0),
                  "tokens": used.get(u["user_id"], {}).get("tokens", 0)},
        "limits": limits_for(u, own.get(u["user_id"])),
        "override": {k: own[u["user_id"]][k] for k in ("per_minute", "tokens_per_day")} if u["user_id"] in own else None,
    } for u in users}


def set_override(user_id: str, limits: dict | None, actor_id: str) -> None:
    """limits: {"per_minute", "tokens_per_day"}, each a whole number or None
    (the role's default); None or both None removes the override."""
    ensure_schema()
    clean = {}
    for key in ("per_minute", "tokens_per_day"):
        value = (limits or {}).get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{key} must be a whole number of 0 or more, or empty for the default")
        clean[key] = value
    with _connect() as connection:
        if clean["per_minute"] is None and clean["tokens_per_day"] is None:
            connection.execute("DELETE FROM app_user_limits WHERE user_id = %s", (user_id,))
            return
        connection.execute("""
            INSERT INTO app_user_limits (user_id, per_minute, tokens_per_day, updated_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET per_minute = EXCLUDED.per_minute,
                tokens_per_day = EXCLUDED.tokens_per_day, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                           (user_id, clean["per_minute"], clean["tokens_per_day"], actor_id))

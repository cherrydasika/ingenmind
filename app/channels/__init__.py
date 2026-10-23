"""Channels (R50): people ask where they already are, through the same flow,
guardrails, budgets, sessions and feedback (answer_feedback.py) as the Ask page.

    channels         channel_id, kind (widget | slack | teams), name, config
                     (widget: allowed_origins, mode public | signed_in;
                     slack: guests; teams: app_id, tenant_id, guests), secret
                     (sealed: slack bot_token + signing_secret; teams
                     app_password), user_id (the channel's own account, for
                     public widget visitors and guests), created_at
    channel_threads  (channel_id, thread_key, user_id) → session_id: a Slack
                     thread, a Teams conversation or a widget visitor keeps
                     its session (follow-ups), until it idles out
    channel_events   (channel_id, event_id): events already handled (Slack
                     and Teams send again when a reply is slow)

Who asks: on Slack and Teams the person's IngenMind account, found by their
email; someone without one asks as the channel's own account when the
channel lets guests in, else is told to ask an admin. A public widget asks as
the widget's own account (its limits are the widget's); a signed-in one as
the visitor's account. Each turn records its channel (sessions, Langfuse
metadata, Monitoring).
"""

import re
import threading
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

KINDS = ("widget", "slack", "teams")
GUEST_PERMISSIONS = ["query_rag"]

_schema_ready = False
_schema_lock = threading.Lock()
_asker = None   # web_api's: (user, session_hint, question, channel kind) → (result, session_id)


class ChannelError(ValueError):
    """A channel that can't be added or used."""


def set_asker(fn) -> None:
    global _asker
    _asker = fn


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616750)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id text PRIMARY KEY,
                    kind text NOT NULL,
                    name text NOT NULL,
                    config jsonb NOT NULL DEFAULT '{}',
                    secret text,
                    user_id text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS channel_threads (
                    channel_id text NOT NULL REFERENCES channels (channel_id) ON DELETE CASCADE,
                    thread_key text NOT NULL,
                    user_id text NOT NULL,
                    session_id text NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (channel_id, thread_key, user_id)
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS channel_events (
                    channel_id text NOT NULL REFERENCES channels (channel_id) ON DELETE CASCADE,
                    event_id text NOT NULL,
                    seen_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (channel_id, event_id)
                )""")
        _schema_ready = True


# ---------- stored channels ----------

def _check(kind: str, settings: dict, secret: dict) -> tuple[dict, dict]:
    if kind == "widget":
        from . import widget
        return widget.check(settings), {}
    if kind == "slack":
        if not str(secret.get("bot_token") or "").startswith("xoxb-") or not secret.get("signing_secret"):
            raise ChannelError("Slack needs the app's bot token (xoxb-…) and its signing secret")
        return {"guests": bool(settings.get("guests"))}, {"bot_token": secret["bot_token"].strip(),
                                                          "signing_secret": secret["signing_secret"].strip()}
    app_id = str(settings.get("app_id") or "").strip()
    if not re.match(r"^[0-9a-f-]{36}$", app_id, re.I) or not secret.get("app_password"):
        raise ChannelError("Teams needs the bot's Microsoft App ID (a GUID) and its client secret")
    tenant = str(settings.get("tenant_id") or "").strip()
    if tenant and not re.match(r"^[0-9a-f-]{36}$", tenant, re.I):
        raise ChannelError("the tenant id is a GUID (empty for a multi-tenant bot)")
    return {"app_id": app_id, "tenant_id": tenant, "guests": bool(settings.get("guests"))}, \
        {"app_password": secret["app_password"].strip()}


def add(kind: str, name: str, settings: dict, secret: dict | None = None, created_by: str | None = None) -> dict:
    import identity
    import secrets_box
    if kind not in KINDS:
        raise ChannelError(f"kind: one of {', '.join(KINDS)}")
    name = " ".join(str(name or "").split())[:60] or kind.title()
    settings, secret = _check(kind, settings or {}, secret or {})
    try:
        sealed = secrets_box.seal(secret) if secret else None
    except secrets_box.SecretError as error:
        raise ChannelError(str(error)) from None
    channel_id = f"ch_{uuid.uuid4().hex[:10]}"
    # Its own account: public widget visitors and guests ask as it, within its limits.
    account = identity.create_user(f"{name} ({kind})"[:80], f"{channel_id}@channels.ingenmind.invalid", "user",
                                   GUEST_PERMISSIONS, created_by)
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO channels (channel_id, kind, name, config, secret, user_id) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *""", (channel_id, kind, name, Jsonb(settings), sealed, account["user_id"])).fetchone()
    return public(row)


def get(channel_id: str, with_secret: bool = False) -> dict | None:
    import secrets_box
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM channels WHERE channel_id = %s", (channel_id,)).fetchone()
    if row and with_secret:
        row["secret"] = secrets_box.open_(row["secret"]) if row["secret"] else {}
    return row


def remove(channel_id: str) -> bool:
    import identity
    found = get(channel_id)
    if not found:
        return False
    with _connect() as connection:
        connection.execute("DELETE FROM channels WHERE channel_id = %s", (channel_id,))
    identity.update_user(found["user_id"], {"status": "disabled"})   # its account can't ask any more
    return True


def listing() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return [public(r) for r in connection.execute("SELECT * FROM channels ORDER BY created_at").fetchall()]


def public(row: dict) -> dict:
    return {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k != "secret"},
            "has_secret": bool(row.get("secret"))}


def first_time(channel_id: str, event_id: str) -> bool:
    """True the first time an event is seen (a retried delivery is not handled twice)."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("DELETE FROM channel_events WHERE seen_at < now() - interval '1 day'")
        return bool(connection.execute("INSERT INTO channel_events (channel_id, event_id) VALUES (%s, %s) "
                                       "ON CONFLICT DO NOTHING", (channel_id, str(event_id)[:200])).rowcount)


# ---------- asking ----------

def person(channel: dict, email: str | None) -> dict | None:
    """Who a Slack or Teams user asks as: their active IngenMind account by email, else the
    channel's own account when it lets guests in, else None."""
    import identity
    if email:
        identity.ensure_schema()
        with _connect() as connection:
            row = connection.execute("SELECT user_id FROM app_users WHERE lower(email) = lower(%s) AND status = 'active'",
                                     (email.strip(),)).fetchone()
        if row:
            return identity.get_user(row["user_id"])
    return identity.get_user(channel["user_id"]) if channel["config"].get("guests") else None


def ask(channel: dict, thread_key: str, question: str, user: dict) -> dict:
    """The live flow's answer, as on the Ask page, in the thread's session.
    {result, session_id} or {refused: why} (limits, setup)."""
    import identity
    ensure_schema()
    if user["user_id"] == channel["user_id"]:
        user = identity.get_user(user["user_id"])     # the channel's account: still active?
    if not user or user["status"] != "active":
        return {"refused": "This channel is switched off."}
    with _connect() as connection:
        row = connection.execute("SELECT session_id FROM channel_threads WHERE channel_id = %s AND thread_key = %s "
                                 "AND user_id = %s", (channel["channel_id"], thread_key, user["user_id"])).fetchone()
    out = _asker(user, row["session_id"] if row else None, question, channel["kind"])
    if out.get("session_id"):
        with _connect() as connection:
            connection.execute("""
                INSERT INTO channel_threads (channel_id, thread_key, user_id, session_id) VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel_id, thread_key, user_id) DO UPDATE SET session_id = EXCLUDED.session_id,
                                                                            updated_at = now()""",
                               (channel["channel_id"], thread_key, user["user_id"], out["session_id"]))
    return out


def owns_turn(channel: dict, thread_key: str, user_id: str, turn_id: int) -> bool:
    """Whether an answer is of this thread's own conversation (public widget visitors share one account)."""
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute("""
            SELECT 1 FROM channel_threads c JOIN app_session_turns t ON t.session_id = c.session_id
            WHERE c.channel_id = %s AND c.thread_key = %s AND c.user_id = %s AND t.turn_id = %s""",
                                       (channel["channel_id"], thread_key, user_id, turn_id)).fetchone())


def sources_list(result: dict) -> list[tuple[str, str, str]]:
    """[("[2, 3]", url, label)] for the sources an answer cites, in citation order."""
    cited = [s for s in result.get("sources") or [] if s.get("cited")]
    return [(f"[{', '.join(map(str, sorted(s['cited'])))}]", s["url"], s.get("organisation") or s.get("title") or s["url"])
            for s in sorted(cited, key=lambda s: min(s["cited"]))]


def usage(days: int = 7) -> dict:
    """Questions per channel in the last days (the Ask page is "app")."""
    import sessions
    sessions.ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT coalesce(result->>'channel', 'app') AS channel, count(*) AS n FROM app_session_turns
            WHERE asked_at > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 2 DESC""", (days,)).fetchall()
    return {"days": days, "channels": {r["channel"]: r["n"] for r in rows}}

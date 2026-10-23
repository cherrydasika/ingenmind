"""API keys (R46): other systems call the versioned API (/api/v1/…, web_api)
without a browser session, as the user who made the key, within its scopes.

    api_keys  key_id, user_id, name, scopes, prefix (shown in lists), digest
              (SHA-256 of the whole key: never the key itself), created_at,
              expires_at, revoked_at, last_used_at

A key reads "im_<key_id>_<secret>" and is shown once, when it is made. It is
checked on every request (auth.AuthMiddleware): unknown, revoked or expired
keys are refused at once, and so are keys whose owner was deactivated. A key
only reaches /api/v1/ routes, and only those its scopes cover, which never go
beyond its owner's permissions (checked again at use: a role change narrows
every key). Rate limits (usage_limits.py) and the audit log (audit.py) apply
as for the owner, the audit entry naming the key.
"""

import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

import identity
from common import config

# scope → the permission it needs (and stands for)
SCOPES = {"ask": "query_rag", "ingest": "upload_documents", "evaluate": "run_evaluations", "admin": "manage_settings"}
PERMISSION_SCOPE = {permission: scope for scope, permission in SCOPES.items()}
MAX_DAYS = 365
NAME_CHARS = 80
KEY = re.compile(r"^im_([a-f0-9]{12})_([A-Za-z0-9_-]{32,64})$")

_schema_ready = False
_schema_lock = threading.Lock()


class ApiKeyError(ValueError):
    """A key that can't be made as asked."""


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616744)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id text PRIMARY KEY,
                    user_id text NOT NULL,
                    name text NOT NULL,
                    scopes text[] NOT NULL,
                    prefix text NOT NULL,
                    digest text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    expires_at timestamptz NOT NULL,
                    revoked_at timestamptz,
                    last_used_at timestamptz
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys (user_id, created_at)")
        _schema_ready = True


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def allowed_scopes(user: dict) -> list[str]:
    """The scopes a user may give a key: those their permissions cover."""
    return [scope for scope, permission in SCOPES.items() if identity.can(user, permission)]


def create(user: dict, name: str, scopes, days: int = 90) -> tuple[str, dict]:
    """(the key, shown once; its record). The scopes must be within the user's permissions."""
    name = " ".join(str(name or "").split())[:NAME_CHARS]
    scopes = sorted({s for s in (scopes or []) if isinstance(s, str)})
    if not name:
        raise ApiKeyError("a key needs a name (what uses it)")
    if not scopes or any(s not in SCOPES for s in scopes):
        raise ApiKeyError(f"scopes: one or more of {', '.join(SCOPES)}")
    beyond = [s for s in scopes if s not in allowed_scopes(user)]
    if beyond:
        raise ApiKeyError(f"your permissions don't cover: {', '.join(beyond)}")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ApiKeyError("days must be a whole number") from None
    if not 1 <= days <= MAX_DAYS:
        raise ApiKeyError(f"a key lasts 1–{MAX_DAYS} days")
    key_id = secrets.token_hex(6)
    key = f"im_{key_id}_{secrets.token_urlsafe(32)}"
    ensure_schema()
    with _connect() as connection:
        record = connection.execute("""
            INSERT INTO api_keys (key_id, user_id, name, scopes, prefix, digest, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                                    (key_id, user["user_id"], name, scopes, key[:15], _digest(key),
                                     datetime.now(timezone.utc) + timedelta(days=days))).fetchone()
    return key, public(record)


def check(key: str) -> tuple[dict, dict] | None:
    """(the key's record, its owner) for a key that may be used now, else None."""
    match = KEY.match(key or "")
    if not match:
        return None
    ensure_schema()
    with _connect() as connection:
        record = connection.execute("SELECT * FROM api_keys WHERE key_id = %s", (match.group(1),)).fetchone()
        if (not record or not hmac.compare_digest(record["digest"], _digest(key)) or record["revoked_at"]
                or record["expires_at"] <= datetime.now(timezone.utc)):
            return None
        connection.execute("UPDATE api_keys SET last_used_at = now() WHERE key_id = %s", (record["key_id"],))
    owner = identity.get_user(record["user_id"])
    if not owner or owner["status"] != "active":
        return None
    return public(record), owner


def permits(record: dict, owner: dict, permission) -> bool:
    """Whether this key may call a route needing `permission` (a name or a tuple, any one will do):
    a scope of the key stands for it, and the owner still has it."""
    names = permission if isinstance(permission, tuple) else (permission,)
    return any(isinstance(name, str) and PERMISSION_SCOPE.get(name, "admin") in record["scopes"]
               and identity.can(owner, name) for name in names)


def revoke(key_id: str, user_id: str | None = None) -> bool:
    """Revoke a key (user_id: only if it is theirs; None: any, for an administrator)."""
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE key_id = %s AND revoked_at IS NULL"
            + (" AND user_id = %s" if user_id else ""), (key_id, user_id) if user_id else (key_id,)).rowcount)


def list_keys(user_id: str | None = None) -> list[dict]:
    """A user's keys (None: everyone's), newest first; never the key or its digest."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM api_keys" + (" WHERE user_id = %s" if user_id else "") + " ORDER BY created_at DESC",
            (user_id,) if user_id else ()).fetchall()
    return [public(r) for r in rows]


def public(record: dict) -> dict:
    now = datetime.now(timezone.utc)
    status = ("revoked" if record["revoked_at"] else "expired" if record["expires_at"] <= now else "active")
    return {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in record.items() if k != "digest"},
            "status": status}

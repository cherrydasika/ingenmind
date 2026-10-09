"""Users and what they may do. Sign-in itself is delegated to an identity
provider (auth.py): this app never stores or sees a password. A user is a
profile plus an explicit set of permissions; a role is only a named bundle
used to fill those permissions in, and every check is on a permission.

    app_users             user_id, first_name, email, role, status, auth
                          provider and subject, theme, created/last login
    app_user_permissions  user_id, permission

Sign-in is invite-only: a person gets in when an admin has added them (by
email), or when their email is in ADMIN_EMAILS (how the first admin gets in).
Deactivated users cannot sign in; users are never hard-deleted.
"""

import os
import threading
import uuid

import psycopg
from psycopg.rows import dict_row

from common import config

# The permission catalogue: name → what it allows. Order is the UI's order.
PERMISSIONS = {
    "view_knowledge": "View the knowledge base and its sources",
    "query_rag": "Ask questions",
    "upload_documents": "Add ad-hoc documents",
    "manage_knowledge": "Run ingestion and manage sources",
    "view_sessions": "View other users' sessions",
    "view_agent_memory": "View agent memory",
    "run_evaluations": "Run evaluations",
    "manage_agents": "Build, publish and make flows live",
    "manage_users": "Add, edit and deactivate users",
    "manage_settings": "Change system settings",
}

# Roles are bundles that fill in a new user's permissions.
ROLES = {
    "admin": list(PERMISSIONS),
    "developer": ["view_knowledge", "query_rag", "upload_documents", "manage_knowledge", "view_sessions",
                  "view_agent_memory", "run_evaluations"],
    "user": ["query_rag"],
    "viewer": ["view_knowledge"],
}

STATUSES = ("active", "disabled")
THEMES = ("system", "light", "dark")

_schema_ready = False
_schema_lock = threading.Lock()


class NotInvited(PermissionError):
    """Signed in with the provider, but not a user of this app."""


class Disabled(PermissionError):
    """A deactivated user."""


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def ensure_schema() -> None:
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    user_id text PRIMARY KEY,
                    first_name text NOT NULL,
                    email text UNIQUE,
                    role text NOT NULL,
                    status text NOT NULL DEFAULT 'active',
                    auth_provider text,
                    auth_subject text,
                    theme text NOT NULL DEFAULT 'system',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    created_by text,
                    updated_at timestamptz,
                    updated_by text,
                    last_login timestamptz,
                    UNIQUE (auth_provider, auth_subject)
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_user_permissions (
                    user_id text NOT NULL REFERENCES app_users (user_id),
                    permission text NOT NULL,
                    PRIMARY KEY (user_id, permission)
                )""")
        _schema_ready = True


def reset_schema_cache() -> None:
    """For tests that switch databases."""
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def _with_permissions(connection, user: dict | None) -> dict | None:
    if not user:
        return None
    rows = connection.execute("SELECT permission FROM app_user_permissions WHERE user_id = %s ORDER BY permission",
                              (user["user_id"],)).fetchall()
    return {**user, "permissions": [r["permission"] for r in rows]}


def get_user(user_id: str) -> dict | None:
    ensure_schema()
    with _connect() as connection:
        return _with_permissions(connection, connection.execute(
            "SELECT * FROM app_users WHERE user_id = %s", (user_id,)).fetchone())


def list_users() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        users = connection.execute("SELECT * FROM app_users ORDER BY lower(first_name), created_at").fetchall()
        return [_with_permissions(connection, u) for u in users]


def _clean_permissions(permissions) -> list[str]:
    unknown = set(permissions) - set(PERMISSIONS)
    if unknown:
        raise ValueError(f"unknown permissions: {', '.join(sorted(unknown))}")
    return sorted(set(permissions))


def create_user(first_name: str, email: str, role: str, permissions: list[str] | None = None,
                created_by: str | None = None) -> dict:
    """A new active user; permissions default to the role's bundle."""
    first_name, email = (first_name or "").strip(), (email or "").strip().lower()
    if not first_name or len(first_name) > 80:
        raise ValueError("first_name is required (at most 80 characters)")
    if "@" not in email or len(email) > 254:
        raise ValueError("a valid email is required")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    granted = _clean_permissions(ROLES[role] if permissions is None else permissions)
    ensure_schema()
    user_id = f"u_{uuid.uuid4().hex[:12]}"
    with _connect() as connection:
        connection.execute(
            "INSERT INTO app_users (user_id, first_name, email, role, created_by) VALUES (%s, %s, %s, %s, %s)",
            (user_id, first_name, email, role, created_by))
        for permission in granted:
            connection.execute("INSERT INTO app_user_permissions (user_id, permission) VALUES (%s, %s)",
                               (user_id, permission))
    return get_user(user_id)


def update_user(user_id: str, changes: dict, updated_by: str | None = None) -> dict:
    """Change first_name, role, status, permissions or theme."""
    allowed = {"first_name", "role", "status", "permissions", "theme"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
    if "role" in changes and changes["role"] not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if "status" in changes and changes["status"] not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if "theme" in changes and changes["theme"] not in THEMES:
        raise ValueError(f"theme must be one of {', '.join(THEMES)}")
    if "first_name" in changes and not (changes["first_name"] or "").strip():
        raise ValueError("first_name cannot be empty")
    if "role" in changes and "permissions" not in changes:
        changes = {**changes, "permissions": ROLES[changes["role"]]}   # a new role brings its bundle
    ensure_schema()
    with _connect() as connection:
        if not connection.execute("SELECT 1 FROM app_users WHERE user_id = %s", (user_id,)).fetchone():
            raise LookupError(f"no user {user_id}")
        for column in ("first_name", "role", "status", "theme"):
            if column in changes:
                value = changes[column].strip() if column == "first_name" else changes[column]
                connection.execute(f"UPDATE app_users SET {column} = %s WHERE user_id = %s", (value, user_id))
        if "permissions" in changes:
            granted = _clean_permissions(changes["permissions"])
            connection.execute("DELETE FROM app_user_permissions WHERE user_id = %s", (user_id,))
            for permission in granted:
                connection.execute("INSERT INTO app_user_permissions (user_id, permission) VALUES (%s, %s)",
                                   (user_id, permission))
        connection.execute("UPDATE app_users SET updated_at = now(), updated_by = %s WHERE user_id = %s",
                           (updated_by, user_id))
    return get_user(user_id)


def sign_in(provider: str, subject: str, email: str | None, first_name: str | None) -> dict:
    """The user an identity provider vouched for, recording the login.
    Linked by (provider, subject); a first sign-in links by email to a user
    an admin added, or creates an admin for an ADMIN_EMAILS address.
    Raises NotInvited or Disabled."""
    email = (email or "").strip().lower()
    ensure_schema()
    with _connect() as connection:
        user = connection.execute("SELECT * FROM app_users WHERE auth_provider = %s AND auth_subject = %s",
                                  (provider, subject)).fetchone()
        if not user and email:
            user = connection.execute("SELECT * FROM app_users WHERE email = %s AND auth_subject IS NULL",
                                      (email,)).fetchone()
            if user:
                connection.execute("UPDATE app_users SET auth_provider = %s, auth_subject = %s WHERE user_id = %s",
                                   (provider, subject, user["user_id"]))
    if not user and email and email in admin_emails():
        name = (first_name or "").strip() or email.split("@")[0]
        user = create_user(name, email, "admin", created_by="ADMIN_EMAILS")
        with _connect() as connection:
            connection.execute("UPDATE app_users SET auth_provider = %s, auth_subject = %s WHERE user_id = %s",
                               (provider, subject, user["user_id"]))
    if not user:
        raise NotInvited(email or subject)
    if user["status"] != "active":
        raise Disabled(user["user_id"])
    record_login(user["user_id"])
    return get_user(user["user_id"])


def guard_change(actor: dict, user_id: str, changes: dict) -> None:
    """Refuse changes that would lock administrators out: deactivating
    yourself, removing your own manage_users, or leaving no active user who
    can manage users. Raises ValueError."""
    target = get_user(user_id)
    if not target:
        raise LookupError(f"no user {user_id}")
    status = changes.get("status", target["status"])
    permissions = changes.get("permissions")
    if permissions is None and "role" in changes:
        permissions = ROLES[changes["role"]] if changes["role"] in ROLES else target["permissions"]
    if permissions is None:
        permissions = target["permissions"]
    if user_id == actor["user_id"]:
        if status != "active":
            raise ValueError("You can't deactivate yourself")
        if "manage_users" not in permissions:
            raise ValueError("You can't remove your own permission to manage users")
    keeps_admin = status == "active" and "manage_users" in permissions
    others = [u for u in list_users() if u["user_id"] != user_id and u["status"] == "active"
              and "manage_users" in u["permissions"]]
    if not keeps_admin and not others:
        raise ValueError("At least one active user must be able to manage users")


def record_login(user_id: str) -> None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_users SET last_login = now() WHERE user_id = %s", (user_id,))


def can(user: dict | None, permission: str) -> bool:
    return bool(user) and user.get("status") == "active" and permission in user.get("permissions", [])


def public(user: dict) -> dict:
    """What the browser may know about a user."""
    return {key: user.get(key) for key in ("user_id", "first_name", "email", "role", "status", "theme",
                                           "permissions", "created_at", "last_login")}

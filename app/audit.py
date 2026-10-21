"""The audit log: who changed what, when, from where (R07).

AuditMiddleware records every state-changing /api request (anything but
GET, HEAD and OPTIONS) whose route needs a permission beyond asking
questions, so a new admin route can't be left out. Sign-in, sign-out and
confirming one's identity are recorded by auth.py. Each entry keeps the
action (the route's handler), its target (the path's IDs), which fields the
request set and their short, non-secret values, the outcome, the client
address and a request ID (also sent back as X-Request-ID).

The table is append-only: a trigger refuses UPDATE and DELETE, so the app
can't change history, even by mistake.

    audit_log  id, at, user_id, user_name, action, method, path, target,
               status, details (jsonb), ip, request_id
"""
import json
import logging
import re
import threading
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

import auth
from common import config

log = logging.getLogger(__name__)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
NOT_AUDITED = {None, "query_rag"}   # asking questions is in sessions, not the audit log
READ_ONLY = {"/api/flows/validate", "/api/flows/diff", "/api/adhoc/preview"}   # POSTs that change nothing
MAX_BODY = 64_000                   # bytes read for the field summary; the rest passes through untouched
VALUE_CHARS = 120
SECRET = re.compile(r"secret|password|passwd|token|api_?key|credential|cookie|question|answer|message|text|content|body",
                    re.IGNORECASE)
SCHEMA_LOCK = 72616712

_schema_ready = False
_schema_lock = threading.Lock()


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
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id bigserial PRIMARY KEY,
                    at timestamptz NOT NULL DEFAULT now(),
                    user_id text,
                    user_name text,
                    action text NOT NULL,
                    method text,
                    path text,
                    target text,
                    status integer,
                    details jsonb NOT NULL DEFAULT '{}'::jsonb,
                    ip text,
                    request_id text)""")
            connection.execute("CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log (at DESC)")
            connection.execute("""
                CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'the audit log is append-only';
                END $$ LANGUAGE plpgsql""")
            connection.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
            connection.execute("""
                CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
                FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()""")
        _schema_ready = True


def summarise(raw: bytes) -> dict:
    """Which fields a JSON or form body set, with short non-secret values."""
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        try:
            from urllib.parse import parse_qs
            body = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(raw.decode("utf-8")).items()}
        except UnicodeDecodeError:
            return {"body": f"{len(raw)} bytes"}
    if not isinstance(body, dict):
        return {"body": type(body).__name__}
    values = {}
    for key, value in body.items():
        if SECRET.search(key):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            values[key] = value
        elif isinstance(value, str) and len(value) <= VALUE_CHARS:
            values[key] = value
        elif isinstance(value, list) and all(isinstance(v, (str, int)) for v in value) and len(value) <= 20:
            values[key] = value
    return {"fields": sorted(body), "values": values}


def record(action: str, user: dict | None = None, *, method: str | None = None, path: str | None = None,
           target: str | None = None, status: int | None = None, details: dict | None = None,
           ip: str | None = None, request_id: str | None = None) -> None:
    """Append one entry. Never raises: the action already happened (logged instead)."""
    row = ((user or {}).get("user_id"), (user or {}).get("first_name"), action, method, path, target, status,
           Jsonb(details or {}), ip, request_id)
    for attempt in (1, 2):
        try:
            ensure_schema()
            with _connect() as connection:
                connection.execute("""
                    INSERT INTO audit_log (user_id, user_name, action, method, path, target, status, details, ip,
                                           request_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", row)
            return
        except psycopg.errors.UndefinedTable:
            reset_schema_cache()   # the database was recreated since the table was made: make it again
            if attempt == 2:
                log.error("audit log entry lost (%s %s): no audit_log table", action, target)
        except Exception as error:
            log.error("audit log entry lost (%s %s by %s): %s", action, target, (user or {}).get("user_id"), error)
            return


def client_ip(scope_or_request) -> str | None:
    scope = getattr(scope_or_request, "scope", scope_or_request)
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for")
    client = scope.get("client")
    direct = client[0] if client else None
    return f"{forwarded.split(',')[0].strip()} (via {direct})" if forwarded else direct


def entries(user_id: str | None = None, action: str | None = None, since: str | None = None,
            until: str | None = None, text: str | None = None, limit: int = 500) -> tuple[list[dict], list[str]]:
    """The newest entries matching the filters, and every action name there is."""
    ensure_schema()
    clauses, args = [], []
    for column, value in (("user_id", user_id), ("action", action)):
        if value:
            clauses.append(f"{column} = %s")
            args.append(value)
    if since:
        clauses.append("at >= %s::date")
        args.append(since)
    if until:
        clauses.append("at < %s::date + 1")
        args.append(until)
    if text:
        clauses.append("(path ILIKE %s OR target ILIKE %s OR details::text ILIKE %s OR user_name ILIKE %s)")
        args += [f"%{text}%"] * 4
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as connection:
        rows = connection.execute(f"SELECT * FROM audit_log {where} ORDER BY at DESC, id DESC LIMIT %s",
                                  (*args, max(1, min(int(limit), 5000)))).fetchall()
        actions = [r["action"] for r in connection.execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()]
    return rows, actions


class AuditMiddleware:
    """Inside AuthMiddleware (the user is known) and around the routes (the
    handler and path IDs are known once the router has run)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        if scope["path"] in READ_ONLY or auth.required_permission(scope["method"], scope["path"]) in NOT_AUDITED:
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex[:16]
        body = bytearray()
        status = {"code": None}

        async def receive_and_keep():
            message = await receive()
            if message["type"] == "http.request" and len(body) < MAX_BODY:
                body.extend(message.get("body", b"")[:MAX_BODY - len(body)])
            return message

        async def send_and_note(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive_and_keep, send_and_note)
        finally:
            endpoint = scope.get("endpoint")
            params = scope.get("path_params") or {}
            user = (scope.get("state") or {}).get("user")
            code = status["code"] or 500
            action = getattr(endpoint, "__name__", None) or ("refused" if code in (401, 403) else "unknown")
            await run_in_threadpool(
                record, action, user, method=scope["method"], path=scope["path"],
                target=", ".join(f"{k}={v}" for k, v in params.items()) or None, status=code,
                details=summarise(bytes(body)), ip=client_ip(scope), request_id=request_id)

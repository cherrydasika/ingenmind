"""Outbound webhooks (R47): other systems react to what happens here.

    webhooks            hook_id, name, url, events, secret (sealed with
                        INTEGRATIONS_KEY: it signs every delivery), trusted,
                        enabled, created_at
    webhook_deliveries  delivery_id, hook_id, event, payload, status (pending |
                        delivered | failed), attempts, last_code, last_error,
                        next_attempt_at, created_at, delivered_at

emit(event, data, private=...) queues one delivery per enabled hook that wants
the event and sends it at once in the background; a failed send is retried
with backoff (RETRY_AFTER) by the sweeper (web_api's lifespan), then given up.
Payloads carry ids, numbers and labels only; `private` (question and answer
text, research tasks) is added only for hooks marked trusted.

Each request is a POST of the JSON payload with:
    X-IngenMind-Event       the event
    X-IngenMind-Delivery    the delivery id (the same on every retry)
    X-IngenMind-Timestamp   seconds since the epoch
    X-IngenMind-Signature   sha256=<hex HMAC-SHA256 of "<timestamp>.<body>" with the hook's secret>
"""

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import secrets_box
from common import config

EVENTS = {
    "answer.failed_review": "An answer a user asked for failed the answer check",
    "knowledge.gap": "Research couldn't close a knowledge gap (a page or live data is missing)",
    "ingestion.finished": "An ingestion job finished",
    "evaluation.regressed": "An evaluation run passed fewer questions than the version's previous run",
    "version.live": "A flow version was made live",
    "ping": "A test delivery (Send test)",
}
RETRY_AFTER = (60, 300, 1800, 7200)        # seconds before each retry; then it's failed
TIMEOUT = 10
MAX_LOG = 50

_schema_ready = False
_schema_lock = threading.Lock()


class WebhookError(ValueError):
    """A hook that can't be made as asked."""


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
            connection.execute("SELECT pg_advisory_xact_lock(72616747)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS webhooks (
                    hook_id text PRIMARY KEY,
                    name text NOT NULL,
                    url text NOT NULL,
                    events text[] NOT NULL,
                    secret text NOT NULL,
                    trusted boolean NOT NULL DEFAULT false,
                    enabled boolean NOT NULL DEFAULT true,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id text PRIMARY KEY,
                    hook_id text NOT NULL REFERENCES webhooks ON DELETE CASCADE,
                    event text NOT NULL,
                    payload jsonb NOT NULL,
                    status text NOT NULL DEFAULT 'pending',
                    attempts int NOT NULL DEFAULT 0,
                    last_code int,
                    last_error text,
                    next_attempt_at timestamptz NOT NULL DEFAULT now(),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    delivered_at timestamptz
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_due ON webhook_deliveries "
                               "(status, next_attempt_at)")
        _schema_ready = True


# ---------- hooks ----------

def create(name: str, url: str, events: list[str], trusted: bool = False) -> tuple[str, dict]:
    """(the signing secret, shown once; the hook)."""
    name = " ".join(str(name or "").split())[:80]
    url = (url or "").strip()
    events = sorted({e for e in events or [] if isinstance(e, str)})
    if not name:
        raise WebhookError("a webhook needs a name")
    if not (url.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1|[a-z0-9-]+)(:\d+)?/", url)):
        raise WebhookError("the URL must be https:// (http:// only to localhost or a service on this network)")
    unknown = [e for e in events if e not in EVENTS]
    if not events or unknown:
        raise WebhookError(f"events: one or more of {', '.join(EVENTS)}")
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    try:
        sealed = secrets_box.seal({"secret": secret})
    except secrets_box.SecretError as error:
        raise WebhookError(str(error)) from None
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO webhooks (hook_id, name, url, events, secret, trusted) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *""", (f"wh_{uuid.uuid4().hex[:12]}", name, url, events, sealed, bool(trusted))).fetchone()
    return secret, public(row)


def remove(hook_id: str) -> bool:
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute("DELETE FROM webhooks WHERE hook_id = %s", (hook_id,)).rowcount)


def hooks() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return [public(r) for r in connection.execute("SELECT * FROM webhooks ORDER BY created_at").fetchall()]


def deliveries(hook_id: str | None = None, limit: int = MAX_LOG) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT delivery_id, hook_id, event, status, attempts, last_code, last_error, created_at, delivered_at, "
            "next_attempt_at FROM webhook_deliveries" + (" WHERE hook_id = %s" if hook_id else "")
            + " ORDER BY created_at DESC LIMIT %s", ((hook_id, limit) if hook_id else (limit,))).fetchall()
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]


def public(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k != "secret"}


# ---------- events ----------

def sign(secret: str, timestamp: int, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, tolerance: int = 300) -> bool:
    """For a receiver: the signature matches and the timestamp is recent."""
    try:
        fresh = abs(time.time() - int(timestamp)) <= tolerance
    except (TypeError, ValueError):
        return False
    return fresh and hmac.compare_digest(sign(secret, int(timestamp), body), signature or "")


def emit(event: str, data: dict, private: dict | None = None, only: str | None = None) -> list[str]:
    """Queue this event for every enabled hook that wants it (only: one hook, for a test) and send at once
    in the background. Never raises: what happened already happened."""
    try:
        ensure_schema()
        with _connect() as connection:
            rows = connection.execute(
                "SELECT hook_id, trusted FROM webhooks WHERE enabled AND %s = ANY(events)"
                + (" AND hook_id = %s" if only else ""), (event, only) if only else (event,)).fetchall()
            ids = []
            for row in rows:
                delivery_id = f"wd_{uuid.uuid4().hex}"
                payload = {"id": delivery_id, "event": event, "created_at": datetime.now(timezone.utc).isoformat(),
                           "data": {**data, **(private or {} if row["trusted"] else {})}}
                connection.execute("INSERT INTO webhook_deliveries (delivery_id, hook_id, event, payload) "
                                   "VALUES (%s, %s, %s, %s)", (delivery_id, row["hook_id"], event, Jsonb(payload)))
                ids.append(delivery_id)
    except Exception as error:
        print(f"webhook {event} not queued: {error}")
        return []
    if ids:
        _later(lambda: [deliver(i) for i in ids])
    return ids


def _later(work) -> None:
    """Background sending (tests replace this to send inline)."""
    threading.Thread(target=work, name="webhook-send", daemon=True).start()


def _post(url: str, body: bytes, headers: dict) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return reply.status
    except urllib.error.HTTPError as error:
        return error.code


def deliver(delivery_id: str, post=_post) -> str:
    """Send one queued delivery; its new status (delivered | pending, for a retry | failed)."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w USING (hook_id)
            WHERE d.delivery_id = %s AND d.status = 'pending'""", (delivery_id,)).fetchone()
    if row is None:
        return "gone"
    body = json.dumps(row["payload"], separators=(",", ":")).encode()
    timestamp = int(time.time())
    code, error = None, None
    try:
        secret = secrets_box.open_(row["secret"])["secret"]
        code = post(row["url"], body, {"Content-Type": "application/json", "User-Agent": "IngenMind-Webhooks/1",
                                       "X-IngenMind-Event": row["event"], "X-IngenMind-Delivery": delivery_id,
                                       "X-IngenMind-Timestamp": str(timestamp),
                                       "X-IngenMind-Signature": sign(secret, timestamp, body)})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    attempts = row["attempts"] + 1
    if code is not None and 200 <= code < 300:
        status, wait = "delivered", None
    else:
        status = "pending" if attempts <= len(RETRY_AFTER) else "failed"
        wait = RETRY_AFTER[attempts - 1] if status == "pending" else None
    with _connect() as connection:
        connection.execute("""
            UPDATE webhook_deliveries SET status = %s, attempts = %s, last_code = %s, last_error = %s,
                next_attempt_at = %s, delivered_at = CASE WHEN %s = 'delivered' THEN now() ELSE NULL END
            WHERE delivery_id = %s""", (status, attempts, code, error or (None if status == "delivered" else f"HTTP {code}"),
                                        datetime.now(timezone.utc) + timedelta(seconds=wait or 0), status, delivery_id))
    return status


def due() -> list[str]:
    """Retries whose time has come (each claimed by pushing its next attempt out a little)."""
    ensure_schema()
    with _connect() as connection:
        return [r["delivery_id"] for r in connection.execute("""
            UPDATE webhook_deliveries SET next_attempt_at = now() + interval '2 minutes'
            WHERE delivery_id IN (SELECT delivery_id FROM webhook_deliveries
                                  WHERE status = 'pending' AND attempts > 0 AND next_attempt_at <= now()
                                  ORDER BY next_attempt_at LIMIT 50 FOR UPDATE SKIP LOCKED)
            RETURNING delivery_id""").fetchall()]


def sweep_forever(every: float = 30) -> None:
    """The retry loop (web_api starts it)."""
    while True:
        time.sleep(every)
        try:
            for delivery_id in due():
                deliver(delivery_id)
        except Exception as error:   # the database may be restarting
            print(f"webhook retries skipped: {error}")

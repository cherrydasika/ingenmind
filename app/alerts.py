"""Alerts (R55): tell admins when something needs them, instead of waiting for
someone to open a dashboard.

    alert_rules     one row per rule kind (RULES): enabled, threshold, window,
                    muted_until, state (ok | firing), value and when last checked
    alert_settings  where alerts go: a Slack incoming-webhook URL (sealed with
                    INTEGRATIONS_KEY), email addresses
    alert_events    the history: fired / resolved / test, the value and
                    threshold, the message, and what each channel did

The checker (check(), every CHECK_EVERY seconds from web_api's lifespan)
measures each enabled rule over its window from the run records (R53), the
ingestion queue and the re-crawl. A rule fires when it goes from ok to
breached and resolves when it clears, so a lasting problem is one alert, not
one every few minutes. A muted rule still records what happens; it just
doesn't send. An evaluation regression is an event (flows/evals.py), not a
measurement. Channels: Slack, email (SMTP_* settings) and the R47 webhooks
(event alert.fired). Messages carry numbers and labels only, never questions.
"""

import json
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import secrets_box
from common import config

CHECK_EVERY = int(os.environ.get("ALERT_CHECK_SECONDS", "300"))
TIMEOUT = 10
HISTORY = 100

# kind: what it measures, how a value breaches (above / below), its default threshold and window (minutes),
# and the fewest runs a rate needs before it means anything.
RULES = {
    "pass_rate": {"label": "Answer pass rate", "breach": "below", "threshold": 0.8, "window": 1440, "min_runs": 10,
                  "unit": "ratio", "what": "Share of users' checked answers that passed the answer check"},
    "daily_cost": {"label": "Cost today", "breach": "above", "threshold": 20.0, "window": None, "unit": "usd",
                   "what": "Approximate cost of every run since midnight (UTC)"},
    "error_rate": {"label": "Error rate", "breach": "above", "threshold": 0.05, "window": 60, "min_runs": 10,
                   "unit": "ratio", "what": "Share of users' runs that failed (timeouts included)"},
    "p95_latency": {"label": "Slowest 5% response time", "breach": "above", "threshold": 60.0, "window": 60,
                    "min_runs": 5, "unit": "seconds", "what": "95th-percentile time of users' runs"},
    "ingestion_failures": {"label": "Ingestion failures", "breach": "above", "threshold": 0, "window": 1440,
                           "unit": "count", "what": "Ingestion jobs that failed"},
    "guardrail_spike": {"label": "Guardrail blocks", "breach": "above", "threshold": 0.3, "window": 60, "min_runs": 10,
                        "unit": "ratio", "what": "Share of users' questions or answers a guardrail blocked"},
    "stale_sources": {"label": "Stale sources", "breach": "above", "threshold": 0, "window": None, "unit": "count",
                      "what": "Pages the re-crawl flags stale (gone, failing or overdue)"},
    "eval_regression": {"label": "Evaluation regression", "breach": "event", "threshold": None, "window": None,
                        "unit": "ratio", "what": "An evaluation run passed fewer questions than the previous run"},
}

_schema_ready = False
_schema_lock = threading.Lock()
_check_lock = threading.Lock()


class AlertError(ValueError):
    """Settings that can't be saved as given."""


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
            connection.execute("SELECT pg_advisory_xact_lock(72616755)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_rules (
                    kind text PRIMARY KEY,
                    enabled boolean NOT NULL DEFAULT true,
                    threshold real,
                    window_minutes int,
                    muted_until timestamptz,
                    state text NOT NULL DEFAULT 'ok',
                    value real,
                    checked_at timestamptz
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_settings (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    slack text,
                    emails text[] NOT NULL DEFAULT '{}',
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_events (
                    id bigserial PRIMARY KEY,
                    kind text NOT NULL,
                    status text NOT NULL,
                    value real,
                    threshold real,
                    message text NOT NULL,
                    details jsonb NOT NULL DEFAULT '{}',
                    muted boolean NOT NULL DEFAULT false,
                    sent jsonb NOT NULL DEFAULT '{}',
                    at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS alert_events_recent ON alert_events (at DESC)")
            for kind, rule in RULES.items():
                connection.execute("INSERT INTO alert_rules (kind, threshold, window_minutes) VALUES (%s, %s, %s) "
                                   "ON CONFLICT (kind) DO NOTHING", (kind, rule["threshold"], rule["window"]))
        _schema_ready = True


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


# ---------- rules ----------

def rules() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = {r["kind"]: r for r in connection.execute("SELECT * FROM alert_rules").fetchall()}
    now = datetime.now(timezone.utc)
    return [{"kind": kind, **{k: v for k, v in rule.items() if k not in ("threshold", "window")},
             "default_threshold": rule["threshold"], "default_window": rule["window"],
             "enabled": rows[kind]["enabled"], "threshold": rows[kind]["threshold"], "window": rows[kind]["window_minutes"],
             "muted_until": _iso(rows[kind]["muted_until"]) if rows[kind]["muted_until"] and rows[kind]["muted_until"] > now else None,
             "state": rows[kind]["state"], "value": rows[kind]["value"], "checked_at": _iso(rows[kind]["checked_at"])}
            for kind, rule in RULES.items()]


def update_rule(kind: str, enabled: bool | None = None, threshold: float | None = None, window: int | None = None,
                mute_minutes: int | None = None) -> dict:
    """Change a rule; mute_minutes 0 unmutes it."""
    if kind not in RULES:
        raise AlertError(f"no rule {kind!r}")
    rule, changes = RULES[kind], {}
    if enabled is not None:
        changes["enabled"] = bool(enabled)
    if threshold is not None:
        if rule["threshold"] is None or not isinstance(threshold, (int, float)) or threshold < 0 \
                or (rule["unit"] == "ratio" and threshold > 1):
            raise AlertError("the threshold must be a number ≥ 0 (a ratio from 0 to 1)")
        changes["threshold"] = float(threshold)
    if window is not None:
        if rule["window"] is None or not isinstance(window, int) or not 5 <= window <= 10080:
            raise AlertError("the window is 5 to 10080 minutes (a week)")
        changes["window_minutes"] = window
    if mute_minutes is not None:
        if not isinstance(mute_minutes, int) or not 0 <= mute_minutes <= 10080:
            raise AlertError("mute for 0 (unmute) to 10080 minutes")
        changes["muted_until"] = datetime.now(timezone.utc) + timedelta(minutes=mute_minutes) if mute_minutes else None
    ensure_schema()
    if changes:
        with _connect() as connection:
            connection.execute(f"UPDATE alert_rules SET {', '.join(f'{k} = %s' for k in changes)} WHERE kind = %s",
                               (*changes.values(), kind))
    return next(r for r in rules() if r["kind"] == kind)


# ---------- measuring ----------

def _users_runs(begin: datetime, end: datetime) -> dict:
    from operations import _Query, _summary
    with _connect() as connection:
        summary = _summary(_Query(connection, begin, end, {"source": "users"}))
        row = connection.execute("""
            SELECT count(*) FILTER (WHERE evaluated) AS evaluated, count(*) FILTER (WHERE passed) AS passed
            FROM agent_flow_runs WHERE source = 'users' AND started_at >= %s AND started_at < %s""", (begin, end)).fetchone()
    return {**summary, **row}


def measure(kind: str, window: int | None, now: datetime | None = None) -> tuple[float | None, dict]:
    """(the rule's value now, or None when there isn't enough to judge; details for the message)."""
    now = now or datetime.now(timezone.utc)
    begin = now - timedelta(minutes=window or 0)
    min_runs = RULES[kind].get("min_runs", 0)
    if kind in ("pass_rate", "error_rate", "p95_latency", "guardrail_spike"):
        s = _users_runs(begin, now)
        if kind == "pass_rate":
            return (round(s["passed"] / s["evaluated"], 4) if s["evaluated"] >= min_runs and s["evaluated"] else None,
                    {"checked": s["evaluated"], "passed": s["passed"]})
        if s["runs"] < min_runs:
            return None, {"runs": s["runs"]}
        if kind == "error_rate":
            return s["error_rate"], {"runs": s["runs"], "failed": s["failed"], "timed_out": s["timed_out"]}
        if kind == "p95_latency":
            return s["p95"], {"runs": s["runs"], "p50": s["p50"]}
        blocked = s["input_blocked"] + s["output_blocked"]
        return round(blocked / s["runs"], 4), {"runs": s["runs"], "blocked": blocked}
    if kind == "daily_cost":
        from operations import _Query, _summary
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with _connect() as connection:
            s = _summary(_Query(connection, midnight, now + timedelta(seconds=1), None))
        return s["cost"] or 0.0, {"runs": s["runs"]}
    if kind == "ingestion_failures":
        try:
            with _connect() as connection:
                row = connection.execute("""
                    SELECT count(*) AS failed, array_agg(DISTINCT kind) AS kinds FROM ingestion_jobs
                    WHERE status = 'failed' AND coalesce(finished_at, created_at) >= %s""", (begin,)).fetchone()
        except psycopg.errors.UndefinedTable:
            return 0, {}
        return row["failed"], {"kinds": [k for k in row["kinds"] or [] if k]}
    if kind == "stale_sources":
        import recrawl
        stale = [r for r in recrawl.freshness(now) if r["stale"]]
        return len(stale), {"pages": len(stale)}
    return None, {}


def _breached(kind: str, value, threshold) -> bool:
    if value is None or threshold is None:
        return False
    return value < threshold if RULES[kind]["breach"] == "below" else value > threshold


def _shown(kind: str, value) -> str:
    unit = RULES[kind]["unit"]
    if value is None:
        return "—"
    return {"ratio": f"{value * 100:.1f}%", "usd": f"${value:,.2f}", "seconds": f"{value:.1f} s"}.get(unit, f"{value:g}")


def _message(kind: str, status: str, value, threshold, window) -> str:
    rule = RULES[kind]
    over = f" in the last {window} min" if window else ""
    if status == "resolved":
        return f"Resolved: {rule['label']} is {_shown(kind, value)}{over} (threshold {_shown(kind, threshold)})."
    if rule["breach"] == "event":
        return f"{rule['label']}: {rule['what'].lower()}."
    if status == "test":   # the value now, whether or not it breaches
        return (f"Test alert: {rule['label']} is {_shown(kind, value)}{over} "
                f"(alerts when {rule['breach']} {_shown(kind, threshold)}).")
    return f"{rule['label']} is {_shown(kind, value)}{over}: {rule['breach']} the threshold of {_shown(kind, threshold)}."


def check(now: datetime | None = None) -> list[dict]:
    """Measure every enabled rule; fire the ones newly breached, resolve the ones that cleared. The events."""
    import operations
    ensure_schema()
    operations.ensure_schema()
    now = now or datetime.now(timezone.utc)
    events = []
    with _check_lock:
        for rule in rules():
            kind = rule["kind"]
            if not rule["enabled"] or RULES[kind]["breach"] == "event":
                continue
            try:
                value, details = measure(kind, rule["window"], now)
            except Exception as error:   # one rule's data being unavailable doesn't stop the others
                print(f"alert {kind} not measured: {error}", file=sys.stderr)
                continue
            breached = _breached(kind, value, rule["threshold"])
            status = "fired" if breached and rule["state"] == "ok" else \
                "resolved" if not breached and rule["state"] == "firing" and value is not None else None
            with _connect() as connection:
                connection.execute("UPDATE alert_rules SET value = %s, checked_at = %s, state = %s WHERE kind = %s",
                                   (value, now, "firing" if breached else ("ok" if value is not None else rule["state"]),
                                    kind))
            if status:
                events.append(_raise(kind, status, value, rule["threshold"], rule["window"], details,
                                     muted=bool(rule["muted_until"])))
    return events


def event(kind: str, value: float | None, details: dict) -> dict | None:
    """An alert that is an event, not a measurement (an evaluation regression). Never raises."""
    try:
        rule = next(r for r in rules() if r["kind"] == kind)
        if not rule["enabled"]:
            return None
        return _raise(kind, "fired", value, None, None, details, muted=bool(rule["muted_until"]))
    except Exception as error:
        print(f"alert {kind} not raised: {error}", file=sys.stderr)
        return None


def test(kind: str) -> dict:
    """Send this rule as a test alert to every channel, with its value now: is it wired up?"""
    if kind not in RULES:
        raise AlertError(f"no rule {kind!r}")
    rule = next(r for r in rules() if r["kind"] == kind)
    try:
        value, details = measure(kind, rule["window"]) if RULES[kind]["breach"] != "event" else (None, {})
    except Exception as error:
        value, details = None, {"error": f"{type(error).__name__}"}
    return _raise(kind, "test", value, rule["threshold"], rule["window"], details, muted=False)


def _raise(kind: str, status: str, value, threshold, window, details: dict, muted: bool) -> dict:
    message = _message(kind, status, value, threshold, window)
    if status == "test" and RULES[kind]["breach"] == "event":
        message = f"Test alert: {message}"
    alert = {"kind": kind, "label": RULES[kind]["label"], "status": status, "value": value, "threshold": threshold,
             "window_minutes": window, "message": message, "details": details,
             "at": datetime.now(timezone.utc).isoformat()}
    sent = {} if muted else send(alert)
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO alert_events (kind, status, value, threshold, message, details, muted, sent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (kind, status, value, threshold, message, Jsonb(details), muted, Jsonb(sent))).fetchone()
    return {**alert, "id": row["id"], "muted": muted, "sent": sent}


def history(limit: int = HISTORY) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM alert_events ORDER BY at DESC, id DESC LIMIT %s", (limit,)).fetchall()
    return [{**r, "at": _iso(r["at"]), "label": RULES.get(r["kind"], {}).get("label", r["kind"])} for r in rows]


# ---------- channels ----------

def settings() -> dict:
    """Where alerts go (the Slack URL is never shown back, only whether one is set) and whether email can send."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT slack, emails FROM alert_settings").fetchone() or {"slack": None, "emails": []}
    return {"slack": bool(row["slack"]), "emails": list(row["emails"]), "email_ready": bool(_smtp()["host"]),
            "email_from": _smtp()["sender"], "webhook_event": "alert.fired"}


def save_settings(slack: str | None = None, emails: list[str] | None = None, clear_slack: bool = False) -> dict:
    changes = {}
    if clear_slack:
        changes["slack"] = None
    elif slack:
        slack = slack.strip()
        # http:// only to a service on this network (a stand-in, for testing), as webhooks allow
        if not (slack.startswith("https://hooks.slack.com/")
                or re.match(r"^http://(localhost|127\.0\.0\.1|[a-z0-9-]+)(:\d+)?/", slack)):
            raise AlertError("a Slack incoming-webhook URL starts with https://hooks.slack.com/")
        try:
            changes["slack"] = secrets_box.seal({"url": slack})
        except secrets_box.SecretError as error:
            raise AlertError(str(error)) from None
    if emails is not None:
        cleaned = sorted({e.strip().lower() for e in emails if isinstance(e, str) and e.strip()})
        if any("@" not in e or " " in e or len(e) > 254 for e in cleaned) or len(cleaned) > 20:
            raise AlertError("emails: up to 20 addresses")
        changes["emails"] = cleaned
    ensure_schema()
    if changes:
        with _connect() as connection:
            connection.execute("INSERT INTO alert_settings (singleton) VALUES (true) ON CONFLICT DO NOTHING")
            connection.execute(f"UPDATE alert_settings SET {', '.join(f'{k} = %s' for k in changes)}, updated_at = now()",
                               tuple(changes.values()))
    return settings()


def _smtp() -> dict:
    return {"host": os.environ.get("SMTP_HOST", "").strip(), "port": int(os.environ.get("SMTP_PORT", "587") or 587),
            "user": os.environ.get("SMTP_USER", "").strip(), "password": os.environ.get("SMTP_PASSWORD", ""),
            "tls": os.environ.get("SMTP_TLS", "starttls").strip().lower(),
            "sender": os.environ.get("ALERT_EMAIL_FROM", "").strip() or "ingenmind-alerts@localhost"}


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return reply.status
    except urllib.error.HTTPError as error:
        return error.code


def _slack_text(alert: dict) -> dict:
    icon = {"fired": ":rotating_light:", "resolved": ":white_check_mark:", "test": ":test_tube:"}[alert["status"]]
    return {"text": f"{icon} IngenMind · {alert['message']}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} *{alert['label']}*\n{alert['message']}"}},
                       {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{alert['status']} · {alert['at'][:16]} UTC"}]}]}


def _email(alert: dict, to: list[str]) -> None:
    smtp = _smtp()
    message = EmailMessage()
    message["Subject"] = f"[IngenMind] {({'resolved': 'Resolved', 'test': 'Test'}).get(alert['status'], 'Alert')}: {alert['label']}"
    message["From"], message["To"] = smtp["sender"], ", ".join(to)
    details = "\n".join(f"  {k}: {v}" for k, v in alert["details"].items())
    message.set_content(f"{alert['message']}\n\n{details}\n\n{alert['at']}\nMonitoring → Alerts shows the history.\n")
    secure = smtp["tls"] == "ssl"
    with (smtplib.SMTP_SSL if secure else smtplib.SMTP)(smtp["host"], smtp["port"], timeout=TIMEOUT) as server:
        if smtp["tls"] == "starttls":
            server.starttls()
        if smtp["user"]:
            server.login(smtp["user"], smtp["password"])
        server.send_message(message)


def send(alert: dict, post=None) -> dict:
    """Deliver to Slack, email and the alert.fired webhooks; what each did (ok, or the error)."""
    post = post or _post
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT slack, emails FROM alert_settings").fetchone() or {"slack": None, "emails": []}
    sent = {}
    if row["slack"]:
        try:
            code = post(secrets_box.open_(row["slack"])["url"], json.dumps(_slack_text(alert)).encode())
            sent["slack"] = "ok" if 200 <= code < 300 else f"HTTP {code}"
        except Exception as error:
            sent["slack"] = f"{type(error).__name__}: {error}"[:200]
    if row["emails"]:
        if not _smtp()["host"]:
            sent["email"] = "not sent: set SMTP_HOST on the server"
        else:
            try:
                _email(alert, list(row["emails"]))
                sent["email"] = "ok"
            except Exception as error:
                sent["email"] = f"{type(error).__name__}: {error}"[:200]
    import webhooks
    queued = webhooks.emit("alert.fired", {k: alert[k] for k in ("kind", "label", "status", "value", "threshold",
                                                                 "window_minutes", "message", "details", "at")})
    if queued:
        sent["webhooks"] = len(queued)
    return sent


def loop(every: float = CHECK_EVERY) -> None:
    """The checker (web_api starts it). One instance checks at a time (an advisory lock)."""
    while True:
        time.sleep(every)
        try:
            with _connect() as connection:
                if not connection.execute("SELECT pg_try_advisory_lock(72616756) AS ok").fetchone()["ok"]:
                    continue
                try:
                    check()
                finally:
                    connection.execute("SELECT pg_advisory_unlock(72616756)")
        except Exception as error:   # the database may be restarting
            print(f"alert check skipped: {error}", file=sys.stderr)

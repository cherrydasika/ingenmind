"""Data lifecycle (R59): how long each kind of data is kept, and a person's
data exported or deleted everywhere.

    data_retention       kind (sessions | traces | feedback | memory | audit),
                         days (NULL: kept until deleted), updated_by/at
    data_retention_runs  when retention was last applied and what it deleted

Where a person's data lives (USER_DATA, the one list both export and delete
read; a table not created on this install is skipped):
    PostgreSQL        their account and permissions, sessions and questions
                      (with the feedback, knowledge gaps and learnings kept per
                      turn: deleted with it), remembered facts and consent,
                      limits and usage, API keys, channel threads, durable runs
                      and questions waiting on a person
    agent memory      each session's agent conversations: agent_sessions
                      locally, AgentCore Memory (events and long-term records
                      of the session's actor) on AWS
    Langfuse          the traces of their questions (by the trace ids kept with
                      each turn, and every observation tagged with their user id)

Deleting removes all of it. Rows that only mention them as the person who
did something (who added a URL, decided a recommendation, set the system up,
answered a question, and their run metrics) keep the row and lose the name;
the audit log keeps the action as "deleted user" with their id, name, email
and IP removed. A channel's own account can't be deleted while the channel
exists, nor the last active admin.

Retention (apply, daily from web_api's lifespan, or Apply now): sessions
idle longer than its days go with their questions, durable runs and agent
conversations; traces older than it are deleted from Langfuse; feedback,
remembered facts and agent conversations (memory) and audit entries older
than theirs are deleted. AgentCore Memory expires events by itself (its event
expiry, set when the memory was created).
"""

import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

KINDS = {
    "sessions": "Sessions and their questions and answers, durable runs and questions waiting on a person",
    "traces": "Langfuse traces of each run (prompts, answers, timings)",
    "feedback": "Thumbs up or down on answers, with reasons and comments",
    "memory": "What the assistant remembers: facts about a person and each session's agent conversations",
    "audit": "The audit log of admin and sign-in actions",
}
MIN_DAYS = 1
APPLY_EVERY = 24 * 3600
CHECK_EVERY = 3600
DELETED = "deleted-user"
NOT_EXPORTED = ("digest", "secret", "embedding")   # key hashes, sealed secrets, vectors: never in an export

# (table, column, what to do): delete their rows, or keep the row without them (anonymise)
USER_DATA = (
    ("answer_feedback", "user_id", "delete"),
    ("app_session_turns", "user_id", "delete"),        # feedback, gaps and learnings go with each turn
    ("channel_threads", "user_id", "delete"),
    ("app_sessions", "user_id", "delete"),
    ("app_user_facts", "user_id", "delete"),
    ("app_user_memory_consent", "user_id", "delete"),
    ("app_user_limits", "user_id", "delete"),
    ("app_usage", "user_id", "delete"),
    ("app_usage_minute", "user_id", "delete"),
    ("api_keys", "user_id", "delete"),
    ("human_requests", "user_id", "delete"),
    ("agent_run_records", "user_id", "delete"),
    ("app_user_permissions", "user_id", "delete"),
    ("agent_flow_runs", "user_id", "anonymise"),
    ("human_requests", "answered_by", "anonymise"),
    ("app_user_limits", "updated_by", "anonymise"),
    ("kb_urls", "added_by", "anonymise"),
    ("use_recommendations", "decided_by", "anonymise"),
    ("app_knowledge_system", "set_up_by", "anonymise"),
    ("app_knowledge_system_events", "user_id", "anonymise"),
    ("app_users", "created_by", "anonymise"),
    ("app_users", "updated_by", "anonymise"),
    ("app_users", "user_id", "delete"),
)

_schema_ready = False
_schema_lock = threading.Lock()


class Refused(ValueError):
    """The person can't be deleted (yet): the reason says why."""


def _connect(**kwargs):
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                           dbname=config.PGDATABASE, row_factory=dict_row, **kwargs)


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
            connection.execute("SELECT pg_advisory_xact_lock(72616760)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS data_retention (
                    kind text PRIMARY KEY,
                    days integer CHECK (days IS NULL OR days >= 1),
                    updated_by text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS data_retention_runs (
                    id bigserial PRIMARY KEY,
                    at timestamptz NOT NULL DEFAULT now(),
                    deleted jsonb NOT NULL DEFAULT '{}',
                    errors jsonb NOT NULL DEFAULT '[]'
                )""")
        _schema_ready = True


def _exists(connection, table: str) -> bool:
    return bool(connection.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"])


def _columns(connection, table: str) -> list[str]:
    return [r["column_name"] for r in connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (table,))]


def _ref(user_id: str) -> str:
    """For the audit entry of a deletion: which account it was, without naming it."""
    return "user:" + hashlib.sha256(user_id.encode()).hexdigest()[:12]


# ---------- What a person has ----------

def _session_ids(connection, user_id: str) -> list[str]:
    ids = []
    for table in ("app_sessions", "channel_threads", "agent_run_records", "human_requests"):
        if _exists(connection, table):
            ids += [r["session_id"] for r in connection.execute(
                sql.SQL("SELECT DISTINCT session_id FROM {} WHERE user_id = %s AND session_id IS NOT NULL").format(
                    sql.Identifier(table)), (user_id,))]
    return list(dict.fromkeys(ids))


def actors(session_ids: list[str]) -> list[str]:
    """The agent-memory actors of these sessions (agent._actor_id)."""
    import agent
    return [agent._actor_id(s) for s in session_ids]


def _trace_ids(connection, user_id: str) -> list[str]:
    if not _exists(connection, "app_session_turns"):
        return []
    return [r["trace_id"] for r in connection.execute(
        "SELECT trace_id FROM app_session_turns WHERE user_id = %s AND trace_id IS NOT NULL", (user_id,))]


def export(user_id: str) -> dict:
    """Everything kept about a person, as JSON-ready data (key hashes, sealed secrets and vectors left out)."""
    with _connect() as connection:
        user = connection.execute("SELECT * FROM app_users WHERE user_id = %s", (user_id,)).fetchone() \
            if _exists(connection, "app_users") else None
        if not user:
            raise LookupError("no such user")
        out = {"exported_at": datetime.now(timezone.utc).isoformat(), "user_id": user_id, "tables": {}}
        for table, column, action in USER_DATA:
            if action != "delete" or not _exists(connection, table):
                continue
            keep = [c for c in _columns(connection, table) if not any(word in c for word in NOT_EXPORTED)]
            rows = connection.execute(sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
                sql.SQL(", ").join(map(sql.Identifier, keep)), sql.Identifier(table), sql.Identifier(column)),
                (user_id,)).fetchall()
            if rows:
                out["tables"].setdefault(table, []).extend(rows)
        for table in ("knowledge_gaps", "use_learnings"):          # kept per turn
            if _exists(connection, table) and _exists(connection, "app_session_turns"):
                keep = [c for c in _columns(connection, table) if c != "embedding"]
                rows = connection.execute(sql.SQL(
                    "SELECT {} FROM {} WHERE turn_id IN (SELECT turn_id FROM app_session_turns WHERE user_id = %s)"
                ).format(sql.SQL(", ").join(sql.Identifier(table, c) for c in keep), sql.Identifier(table)),
                    (user_id,)).fetchall()
                if rows:
                    out["tables"][table] = rows
        if _exists(connection, "agent_sessions"):
            rows = connection.execute("SELECT * FROM agent_sessions WHERE actor = ANY(%s)",
                                      (actors(_session_ids(connection, user_id)),)).fetchall()
            if rows:
                out["tables"]["agent_sessions"] = rows
    out["elsewhere"] = {"langfuse_traces": _trace_ids_in(out),
                        "note": "Langfuse traces and AgentCore Memory are deleted with the account; ask an admin for a "
                                "copy of a trace."}
    return json.loads(json.dumps(out, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))


def _trace_ids_in(export_data: dict) -> list[str]:
    return [t["trace_id"] for t in export_data["tables"].get("app_session_turns", []) if t.get("trace_id")]


# ---------- Elsewhere: Langfuse and AgentCore Memory ----------

def _langfuse():
    from tracing import get_langfuse_client
    return get_langfuse_client().api


def langfuse_trace_ids(user_id: str | None = None, before: datetime | None = None, limit: int = 5000) -> list[str]:
    """Trace ids from Langfuse's observations (v4 has no trace listing): a user's, or every one started before."""
    api, ids, cursor = _langfuse(), [], None
    while len(ids) < limit:
        page = api.observations_v_2.get_many(user_id=user_id, to_start_time=before, fields="core", limit=1000,
                                             cursor=cursor)
        ids += [o["traceId"] if isinstance(o, dict) else o.trace_id for o in page.data]
        cursor = getattr(getattr(page, "meta", None), "cursor", None)
        if not cursor or not page.data:
            break
    return list(dict.fromkeys(ids))


def langfuse_delete(trace_ids: list[str]) -> int:
    api = _langfuse()
    trace_ids = list(dict.fromkeys(trace_ids))
    for start in range(0, len(trace_ids), 500):
        api.trace.delete_multiple(trace_ids=trace_ids[start:start + 500])
    return len(trace_ids)


def agentcore_forget(actor_ids: list[str]) -> int:
    """Delete AgentCore Memory's events and long-term records for these actors; 0 with the local runtime
    (its agent conversations are agent_sessions, deleted in PostgreSQL)."""
    import agent
    import agent_runtime
    if agent_runtime.RUNTIME == "local" or not actor_ids:
        return 0
    memory_id = (agent.describe().get("harness") or {}).get("memory")
    if not memory_id:
        return 0
    client = agent._session().client("bedrock-agentcore")
    deleted = 0
    for actor in actor_ids:
        try:
            sessions = client.list_sessions(memoryId=memory_id, actorId=actor, maxResults=100)["sessionSummaries"]
        except client.exceptions.ResourceNotFoundException:
            sessions = []
        for session in sessions:
            for event in client.list_events(memoryId=memory_id, actorId=actor, sessionId=session["sessionId"],
                                            maxResults=100).get("events", []):
                client.delete_event(memoryId=memory_id, actorId=actor, sessionId=session["sessionId"],
                                    eventId=event["eventId"])
                deleted += 1
        for record in client.list_memory_records(memoryId=memory_id, namespace=f"/actors/{actor}/",
                                                 maxResults=100).get("memoryRecordSummaries", []):
            client.delete_memory_record(memoryId=memory_id, memoryRecordId=record["memoryRecordId"])
            deleted += 1
    return deleted


# ---------- Deleting a person ----------

def _check_deletable(connection, user: dict) -> None:
    if _exists(connection, "channels") and connection.execute(
            "SELECT 1 FROM channels WHERE user_id = %s", (user["user_id"],)).fetchone():
        raise Refused("this is a channel's own account: delete the channel first")
    if user["role"] == "admin" and user["status"] == "active" and connection.execute(
            "SELECT count(*) AS n FROM app_users WHERE role = 'admin' AND status = 'active' AND user_id <> %s",
            (user["user_id"],)).fetchone()["n"] == 0:
        raise Refused("this is the only active admin: make someone else an admin first")


def _scrub_audit(connection, user: dict) -> int:
    """Keep the audit log's actions, without the person: their id, name, email and IP replaced."""
    if not _exists(connection, "audit_log"):
        return 0
    import audit
    connection.execute(audit.MAINTENANCE)
    touched = connection.execute("""
        UPDATE audit_log SET user_id = CASE WHEN user_id = %(id)s THEN %(gone)s ELSE user_id END,
            user_name = CASE WHEN user_id = %(id)s THEN 'Deleted user' ELSE user_name END,
            ip = CASE WHEN user_id = %(id)s THEN NULL ELSE ip END,
            path = replace(path, %(id)s, %(gone)s), target = replace(target, %(id)s, %(gone)s),
            details = replace(replace(details::text, %(id)s, %(gone)s), %(email)s, %(gone)s)::jsonb
        WHERE user_id = %(id)s OR path LIKE %(like)s OR target LIKE %(like)s
           OR details::text LIKE %(like)s OR details::text LIKE %(like_email)s""",
        {"id": user["user_id"], "gone": DELETED, "email": user.get("email") or user["user_id"],
         "like": f"%{user['user_id']}%", "like_email": f"%{user.get('email') or user['user_id']}%"}).rowcount
    return touched


def delete_user(user_id: str, by: dict | None, langfuse=None, agentcore=None) -> dict:
    """Delete a person's data everywhere (the module docstring says what that means); what was removed.
    langfuse: (user id, known trace ids) → traces deleted; agentcore: actors → items deleted. A failure
    there is reported (and nothing in PostgreSQL deleted, so it can be tried again)."""
    langfuse = langfuse or (lambda uid, known: langfuse_delete(known + langfuse_trace_ids(uid)))
    agentcore = agentcore or agentcore_forget
    with _connect() as connection:
        user = connection.execute("SELECT * FROM app_users WHERE user_id = %s", (user_id,)).fetchone()
        if not user:
            raise LookupError("no such user")
        _check_deletable(connection, user)
        session_ids = _session_ids(connection, user_id)
        known_traces = _trace_ids(connection, user_id)
    result = {"user": _ref(user_id), "postgres": {}, "langfuse_traces": 0, "agentcore_items": 0}
    # Outside first: if Langfuse or AgentCore can't be reached nothing is deleted, and it can be retried.
    result["langfuse_traces"] = langfuse(user_id, known_traces)
    result["agentcore_items"] = agentcore(actors(session_ids))
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616761)")
        deleted = result["postgres"]
        if _exists(connection, "agent_sessions"):
            deleted["agent_sessions"] = connection.execute("DELETE FROM agent_sessions WHERE actor = ANY(%s)",
                                                           (actors(session_ids),)).rowcount
        for table, column, action in USER_DATA:
            if not _exists(connection, table) or column not in _columns(connection, table):
                continue
            query = (sql.SQL("DELETE FROM {t} WHERE {c} = %s") if action == "delete"
                     else sql.SQL("UPDATE {t} SET {c} = NULL WHERE {c} = %s"))
            count = connection.execute(query.format(t=sql.Identifier(table), c=sql.Identifier(column)),
                                       (user_id,)).rowcount
            if count:
                deleted[f"{table}.{column}" if action == "anonymise" else table] = count
        deleted["audit_log (anonymised)"] = _scrub_audit(connection, user)
    import audit
    audit.record("user_data_deleted", by if by and by.get("user_id") != user_id else None,
                 target=result["user"], details={k: v for k, v in result.items() if k != "user"})
    return result


# ---------- Retention ----------

def settings() -> dict:
    ensure_schema()
    with _connect() as connection:
        rows = {r["kind"]: r for r in connection.execute("SELECT * FROM data_retention")}
        last = connection.execute("SELECT * FROM data_retention_runs ORDER BY at DESC LIMIT 1").fetchone()
    return {"kinds": [{"kind": k, "what": what, "days": (rows.get(k) or {}).get("days"),
                       "updated_at": (rows.get(k) or {}).get("updated_at") and rows[k]["updated_at"].isoformat()}
                      for k, what in KINDS.items()],
            "last_applied": last and {"at": last["at"].isoformat(), "deleted": last["deleted"], "errors": last["errors"]}}


def save_settings(days: dict, user_id: str | None) -> dict:
    """days: kind → whole days to keep (≥ MIN_DAYS), or None to keep until deleted."""
    ensure_schema()
    for kind, value in days.items():
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < MIN_DAYS):
            raise ValueError(f"{kind}: a whole number of days (at least {MIN_DAYS}), or empty to keep it")
    with _connect() as connection:
        for kind, value in days.items():
            connection.execute("""
                INSERT INTO data_retention (kind, days, updated_by) VALUES (%s, %s, %s)
                ON CONFLICT (kind) DO UPDATE SET days = EXCLUDED.days, updated_by = EXCLUDED.updated_by,
                    updated_at = now()""", (kind, value, user_id))
    return settings()


def _old_sessions(connection, cutoff: datetime) -> dict[str, int]:
    out = {}
    if not _exists(connection, "app_sessions"):
        return out
    old = [r["session_id"] for r in connection.execute(
        "SELECT session_id FROM app_sessions WHERE coalesce(last_activity, started_at) < %s", (cutoff,))]
    if _exists(connection, "agent_sessions"):
        out["agent_sessions"] = connection.execute("DELETE FROM agent_sessions WHERE actor = ANY(%s)",
                                                   (actors(old),)).rowcount
    for table in ("app_session_turns", "channel_threads", "app_sessions"):
        if _exists(connection, table):
            out[table] = connection.execute(sql.SQL("DELETE FROM {} WHERE session_id = ANY(%s)").format(
                sql.Identifier(table)), (old,)).rowcount
    # Durable runs and questions waiting on a person: of those sessions, or old themselves; never unfinished ones.
    if _exists(connection, "agent_run_records"):
        out["agent_run_records"] = connection.execute(
            "DELETE FROM agent_run_records WHERE (session_id = ANY(%s) OR coalesce(finished_at, updated_at) < %s) "
            "AND status NOT IN ('running', 'waiting', 'interrupted')", (old, cutoff)).rowcount
    if _exists(connection, "human_requests"):
        out["human_requests"] = connection.execute(
            "DELETE FROM human_requests WHERE (session_id = ANY(%s) OR created_at < %s) AND status <> 'open'",
            (old, cutoff)).rowcount
    return out


def apply(now: datetime | None = None, langfuse=None) -> dict:
    """Delete what is older than its kind's retention; recorded in data_retention_runs. langfuse: cutoff → traces
    deleted (default: the ones Langfuse lists before it)."""
    now = now or datetime.now(timezone.utc)
    langfuse = langfuse or (lambda cutoff: langfuse_delete(langfuse_trace_ids(before=cutoff)))
    days = {k["kind"]: k["days"] for k in settings()["kinds"] if k["days"]}
    deleted, errors = {}, []
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616762)")
        if "sessions" in days:
            deleted["sessions"] = _old_sessions(connection, now - timedelta(days=days["sessions"]))
        if "feedback" in days and _exists(connection, "answer_feedback"):
            deleted["feedback"] = {"answer_feedback": connection.execute(
                "DELETE FROM answer_feedback WHERE created_at < %s", (now - timedelta(days=days["feedback"]),)).rowcount}
        if "memory" in days:
            cutoff = now - timedelta(days=days["memory"])
            deleted["memory"] = {table: connection.execute(sql.SQL("DELETE FROM {} WHERE updated_at < %s").format(
                sql.Identifier(table)), (cutoff,)).rowcount
                for table in ("app_user_facts", "agent_sessions") if _exists(connection, table)}
        if "audit" in days and _exists(connection, "audit_log"):
            import audit
            connection.execute(audit.MAINTENANCE)
            deleted["audit"] = {"audit_log": connection.execute(
                "DELETE FROM audit_log WHERE at < %s", (now - timedelta(days=days["audit"]),)).rowcount}
    if "traces" in days:
        try:
            deleted["traces"] = {"langfuse": langfuse(now - timedelta(days=days["traces"]))}
        except Exception as error:      # Langfuse down: tried again at the next run
            errors.append(f"traces: {error}")
    with _connect() as connection:
        connection.execute("INSERT INTO data_retention_runs (at, deleted, errors) VALUES (%s, %s, %s)",
                           (now, Jsonb(deleted), Jsonb(errors)))
    if deleted.get("audit", {}).get("audit_log"):
        import audit
        audit.record("audit_log_pruned", None, target=f"older than {days['audit']} days",
                     details={"deleted": deleted["audit"]["audit_log"]})
    return {"at": now.isoformat(), "deleted": deleted, "errors": errors}


def due(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    ensure_schema()
    with _connect() as connection:
        last = connection.execute("SELECT max(at) AS at FROM data_retention_runs").fetchone()["at"]
        any_set = connection.execute("SELECT 1 FROM data_retention WHERE days IS NOT NULL").fetchone()
    return bool(any_set) and (last is None or last <= now - timedelta(seconds=APPLY_EVERY))


def loop(stop: threading.Event | None = None, every: float = CHECK_EVERY) -> None:
    """Apply retention once a day (one instance: the advisory lock)."""
    while not (stop and stop.is_set()):
        try:
            with _connect(autocommit=True) as connection:
                if connection.execute("SELECT pg_try_advisory_lock(72616763) AS ok").fetchone()["ok"]:
                    try:
                        if due():
                            apply()
                    finally:
                        connection.execute("SELECT pg_advisory_unlock(72616763)")
        except Exception as error:
            print(f"data retention: {error}", file=sys.stderr)
        if stop:
            stop.wait(every)
        else:
            time.sleep(every)


def privacy() -> dict:
    """For the Privacy page: what is stored, where, and for how long."""
    kinds = settings()
    return {"kinds": kinds["kinds"], "last_applied": kinds["last_applied"], "where": [
        {"what": "Your account", "where": "PostgreSQL", "detail": "name, email, role, permissions, sign-in provider, theme, last sign-in"},
        {"what": "Your questions and answers", "where": "PostgreSQL", "detail": "each session's questions (personal details masked before the agents see them), answers, sources and checks; feedback you gave"},
        {"what": "What the assistant remembers", "where": "PostgreSQL, AgentCore Memory on AWS", "detail": "facts you agreed it may remember, and each session's agent conversations"},
        {"what": "Traces", "where": "Langfuse", "detail": "each run's steps, prompts and answers (personal details masked), for debugging and quality checks"},
        {"what": "Usage", "where": "PostgreSQL", "detail": "questions and tokens per day and minute, for limits"},
        {"what": "The audit log", "where": "PostgreSQL", "detail": "admin actions and sign-ins, with who, when and from which IP"},
    ]}

"""Durable runs (R30): every run's LangGraph state is checkpointed in
PostgreSQL, and a record of the run keeps what the graph state doesn't (the
searches behind its citations, tool calls, checks, usage), so a run cut short
by a restart can be picked up where it stopped, and every run has a history.

    saver()            the LangGraph checkpointer (PostgresSaver; MemorySaver when
                       DURABLE_RUNS=false or the database is unreachable)
    begin(...)         a run starts: its record, with what it needs to resume
    snapshot(run)      after each graph step: the run's own state, as JSON
    finish(...)        done / failed / cancelled, with the public result
    recover()          at startup: runs left "running" by a dead process become
                       "interrupted"; those asked on the Ask page in the last
                       RESUME_MINUTES are resumed in the background
    prune()            records and checkpoints older than RETENTION_DAYS

    agent_run_records  run_id, source, session_id, user_id, flow_id, version,
                       spec, question (as the agents got it), asked (as the
                       user asked it), read_as, status, last_node, steps,
                       seconds, input_tokens, output_tokens, snapshot, result,
                       error, resumes, started_at, updated_at, finished_at

Finished runs drop their checkpoints (the record stays); interrupted and
failed ones keep them until resumed or pruned.
"""

import json
import logging
import os
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

log = logging.getLogger(__name__)

ENABLED = os.environ.get("DURABLE_RUNS", "true").strip().lower() not in ("0", "false", "no")
RETENTION_DAYS = float(os.environ.get("DURABLE_RETENTION_DAYS", "7"))
RESUME_MINUTES = float(os.environ.get("DURABLE_RESUME_MINUTES", "30"))
FINAL = ("done", "failed", "cancelled")
# The run's own state kept with each snapshot (agent._Run attributes).
SNAPSHOT_FIELDS = ("searches", "calls", "evaluations", "rewrites", "contracts", "web_searches", "research",
                   "candidates", "data_candidates", "budgets", "role_limits", "known_sources", "runs", "path",
                   "agent_runs", "trace_id", "budget_hit", "question", "user_facts")

_lock = threading.Lock()
_saver = None
_pool = None
_ready = False
LIVE: dict[str, object] = {}      # run_id → the _Run running in this process (for cancelling)


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset() -> None:
    """For tests that switch databases."""
    global _saver, _pool, _ready, _setup_done
    with _lock:
        if _pool is not None:
            _pool.close()
        _saver = _pool = None
        _ready = _setup_done = False


def saver():
    """The checkpointer every compiled graph uses."""
    global _saver, _pool
    with _lock:
        if _saver is not None:
            return _saver
        if ENABLED:
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    f"host={config.PGHOST} port={config.PGPORT} user={config.PGUSER} "
                    f"password={config.PGPASSWORD} dbname={config.PGDATABASE}",
                    min_size=1, max_size=8, kwargs={"autocommit": True, "prepare_threshold": 0}, open=True)
                _saver = PostgresSaver(_pool)    # its tables: ensure_setup(), before the first run
                return _saver
            except Exception as error:
                log.warning("durable runs unavailable, checkpoints kept in memory: %s", error)
                if _pool is not None:
                    _pool.close()
                    _pool = None
        from langgraph.checkpoint.memory import MemorySaver
        _saver = MemorySaver()
        return _saver


_setup_done = False


def ensure_setup() -> None:
    """The checkpointer's tables, once per process, before the first run that needs them; never at
    import: setup() creates indexes CONCURRENTLY, which waits for every open transaction to end,
    including one its caller may hold (the demo seed makes its flow live inside one). Two processes
    starting at once take turns with a session lock, outside any transaction."""
    global _setup_done
    if _setup_done or not durable():
        return
    checkpointer = saver()                 # before _lock: saver() takes it too
    with _lock:
        if _setup_done:
            return
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=config.PGDATABASE, autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(72616740)")
            try:
                checkpointer.setup()
            finally:
                connection.execute("SELECT pg_advisory_unlock(72616740)")
        _setup_done = True


def durable() -> bool:
    return type(saver()).__name__ == "PostgresSaver"


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616741)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_run_records (
                run_id text PRIMARY KEY,
                source text NOT NULL,
                session_id text NOT NULL DEFAULT '',
                user_id text,
                flow_id text NOT NULL,
                version text NOT NULL,
                spec jsonb NOT NULL,
                question text NOT NULL,
                asked text NOT NULL,
                read_as text,
                status text NOT NULL DEFAULT 'running',
                last_node text,
                steps int NOT NULL DEFAULT 0,
                seconds real NOT NULL DEFAULT 0,
                input_tokens int NOT NULL DEFAULT 0,
                output_tokens int NOT NULL DEFAULT 0,
                snapshot jsonb,
                result jsonb,
                error text,
                resumes int NOT NULL DEFAULT 0,
                started_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz
            )""")
        connection.execute("CREATE INDEX IF NOT EXISTS agent_run_records_status ON agent_run_records (status, started_at)")
    _ready = True


def _safe(work, what: str):
    """Records are a history and a way back: a run never fails for them."""
    try:
        ensure_schema()
        return work()
    except Exception as error:
        log.warning("durable run %s failed: %s", what, error)
        return None


def begin(run_id: str, *, source: str, session_id: str, user_id: str | None, flow_id: str, version,
          spec: dict, question: str, asked: str, read_as: str | None) -> None:
    ensure_setup()

    def work():
        with _connect() as connection:
            connection.execute("""
                INSERT INTO agent_run_records (run_id, source, session_id, user_id, flow_id, version, spec, question,
                                               asked, read_as)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET status = 'running', updated_at = now(),
                    resumes = agent_run_records.resumes + 1""",
                               (run_id, source, session_id or "", user_id, flow_id, str(version), Jsonb(spec),
                                question, asked, read_as))
    _safe(work, "start")


def _plain(value):
    """JSON-safe: sets as lists, and no chunk vectors (big, and only for plots)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != "vector"}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def state_of(run) -> dict:
    with run.lock:
        out = {name: _plain(getattr(run, name, None)) for name in SNAPSHOT_FIELDS}
    return json.loads(json.dumps(out, default=str))


def restore(run, state: dict) -> None:
    """Put a snapshot back on a fresh run (agent._Run)."""
    for name in SNAPSHOT_FIELDS:
        if name in (state or {}) and state[name] is not None:
            value = state[name]
            if name in ("candidates", "data_candidates", "budgets", "known_sources"):
                value = {int(k): v for k, v in value.items()}     # delegation numbers come back as strings
            setattr(run, name, value)


def snapshot(run, node: str | None = None) -> None:
    run_id = getattr(run, "run_id", None)
    if not run_id:
        return
    usage = [sum(r.get(k, 0) for r in getattr(run, "runs", [])) for k in ("input_tokens", "output_tokens")]

    def work():
        with _connect() as connection:
            connection.execute("""
                UPDATE agent_run_records SET snapshot = %s, last_node = coalesce(%s, last_node), steps = steps + 1,
                    seconds = %s, input_tokens = %s, output_tokens = %s, updated_at = now()
                WHERE run_id = %s AND status = 'running'""",
                               (Jsonb(state_of(run)), node, round(time.monotonic() - run.started, 3), *usage, run_id))
    _safe(work, "snapshot")


def finish(run_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    def work():
        with _connect() as connection:
            connection.execute("""
                UPDATE agent_run_records SET status = %s, result = %s, error = %s, finished_at = now(),
                    updated_at = now(), snapshot = CASE WHEN %s = 'done' THEN NULL ELSE snapshot END
                WHERE run_id = %s""",
                               (status, Jsonb(_public(result)) if result is not None else None, error, status, run_id))
        if status == "done":
            saver().delete_thread(run_id)          # the record keeps the history
    _safe(work, "finish")
    LIVE.pop(run_id, None)


def _public(result: dict) -> dict:
    keep = ("answer", "question", "read_as", "sources", "citations", "answer_check", "guardrails", "usage",
            "total", "flow", "status", "rounds", "budget")
    return json.loads(json.dumps({k: result.get(k) for k in keep if k in result}, default=str))


def get(run_id: str) -> dict | None:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("SELECT * FROM agent_run_records WHERE run_id = %s", (run_id,)).fetchone()


def list_runs(limit: int = 100, source: str | None = None) -> list[dict]:
    ensure_schema()
    where, params = ("WHERE source = %s", (source,)) if source else ("", ())
    with _connect() as connection:
        return connection.execute(f"""
            SELECT run_id, source, session_id, user_id, flow_id, version, asked, read_as, status, last_node, steps,
                   seconds, input_tokens, output_tokens, error, resumes, started_at, updated_at, finished_at,
                   result->>'answer' AS answer
            FROM agent_run_records {where} ORDER BY started_at DESC LIMIT %s""", (*params, limit)).fetchall()


def cancel(run_id: str) -> bool:
    """Stop a run: a live one at its next step (as Cancel on the Ask page does); one
    interrupted or failed is closed, its checkpoints dropped."""
    live = LIVE.get(run_id)
    if live is not None and getattr(live, "cancelled", None) is not None:
        live.cancelled.set()
    ensure_schema()
    with _connect() as connection:
        changed = connection.execute("""
            UPDATE agent_run_records SET status = 'cancelled', finished_at = now(), updated_at = now()
            WHERE run_id = %s AND status NOT IN ('done', 'cancelled')""", (run_id,)).rowcount
    if changed and live is None:
        saver().delete_thread(run_id)
    return bool(changed)


def mark_interrupted() -> list[dict]:
    """At startup: the runs a dead process left running."""
    ensure_schema()
    with _connect() as connection:
        return connection.execute("""
            UPDATE agent_run_records SET status = 'interrupted', updated_at = now()
            WHERE status = 'running' RETURNING run_id, source, started_at""").fetchall()


def resumable(run_id: str) -> bool:
    record = get(run_id)
    return bool(record and record["status"] in ("interrupted", "failed") and record["snapshot"] is not None)


def recover(resume) -> list[str]:
    """Mark interrupted runs and resume, in the background, those asked on the
    Ask page in the last RESUME_MINUTES (resume(run_id) does one)."""
    if not durable():
        return []
    rows = _safe(mark_interrupted, "recovery") or []
    recent = [r["run_id"] for r in rows if r["source"] == "users"
              and time.time() - r["started_at"].timestamp() < RESUME_MINUTES * 60]
    if recent:
        def work():
            for run_id in recent:
                try:
                    resume(run_id)
                except Exception as error:
                    log.warning("could not resume run %s: %s", run_id, error)
        threading.Thread(target=work, name="durable-recover", daemon=True).start()
    return recent


def prune() -> int:
    """Records (and any checkpoints) older than RETENTION_DAYS."""
    def work():
        with _connect() as connection:
            old = [r["run_id"] for r in connection.execute(
                "DELETE FROM agent_run_records WHERE started_at < now() - make_interval(secs => %s) RETURNING run_id",
                (RETENTION_DAYS * 86400,)).fetchall()]
        for run_id in old:
            saver().delete_thread(run_id)
        return len(old)
    return _safe(work, "prune") or 0

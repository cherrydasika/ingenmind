"""Saved flows in Postgres: one working draft per flow plus immutable
published versions. The built-in default flow (default_flow.json) is version
0 of its id and is never stored; the agent runtime still runs it — running
saved versions comes later.

    agent_flows          id, name, draft (jsonb, null when none), published_version
    agent_flow_versions  flow_id, version, spec (jsonb), note, published_at
    agent_live_flow      the one flow version users get (none: the built-in flow)
    agent_flow_runs      one row of numbers per run, for per-flow metrics
"""

import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

from .spec import FlowSpec

_schema_ready = False
_schema_lock = threading.Lock()


class FlowNotFound(LookupError):
    pass


class FlowConflict(ValueError):
    pass


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
                CREATE TABLE IF NOT EXISTS agent_flows (
                    id text PRIMARY KEY,
                    name text NOT NULL,
                    draft jsonb,
                    draft_updated_at timestamptz,
                    published_version int,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_flow_versions (
                    flow_id text NOT NULL REFERENCES agent_flows (id) ON DELETE CASCADE,
                    version int NOT NULL,
                    spec jsonb NOT NULL,
                    note text NOT NULL DEFAULT '',
                    published_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (flow_id, version)
                )""")
            # Which flow version users get (the Retrieval page); no row: the built-in flow.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_live_flow (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    flow_id text NOT NULL,
                    version int NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
            # One row per run, for per-flow metrics: numbers and flags only,
            # never the question, the answer or anything the agents saw.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_flow_runs (
                    id bigserial PRIMARY KEY,
                    flow_id text NOT NULL,
                    version text NOT NULL,
                    source text NOT NULL,
                    started_at timestamptz NOT NULL DEFAULT now(),
                    seconds real NOT NULL,
                    rounds int NOT NULL DEFAULT 0,
                    tasks int NOT NULL DEFAULT 0,
                    input_tokens int NOT NULL DEFAULT 0,
                    output_tokens int NOT NULL DEFAULT 0,
                    input_blocked boolean NOT NULL DEFAULT false,
                    output_blocked boolean NOT NULL DEFAULT false,
                    evaluated boolean NOT NULL DEFAULT false,
                    passed boolean,
                    overall real,
                    failed boolean NOT NULL DEFAULT false
                )""")
            connection.execute("""
                CREATE INDEX IF NOT EXISTS agent_flow_runs_recent ON agent_flow_runs (flow_id, started_at DESC)""")
        _schema_ready = True


def reset_schema_cache() -> None:
    """Tests point config at a fresh database."""
    global _schema_ready
    _schema_ready = False


def _iso(value):
    return value.isoformat() if value else None


def list_flows(builtin: FlowSpec) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT id, name, draft IS NOT NULL AS has_draft, draft_updated_at, published_version
            FROM agent_flows ORDER BY id""").fetchall()
    flows = {r["id"]: {"id": r["id"], "name": r["name"], "has_draft": r["has_draft"],
                       "draft_updated_at": _iso(r["draft_updated_at"]),
                       "published_version": r["published_version"], "builtin": False} for r in rows}
    stored = flows.get(builtin.id)
    flows[builtin.id] = {"id": builtin.id, "name": stored["name"] if stored else builtin.name,
                         "has_draft": bool(stored and stored["has_draft"]),
                         "draft_updated_at": stored["draft_updated_at"] if stored else None,
                         "published_version": (stored or {}).get("published_version") or 0, "builtin": True}
    return sorted(flows.values(), key=lambda f: (not f["builtin"], f["id"]))


def get_flow(flow_id: str, builtin: FlowSpec) -> dict:
    """The flow to edit: its draft, else its latest published version, else the built-in."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_flows WHERE id = %s", (flow_id,)).fetchone()
        versions = connection.execute("""
            SELECT version, note, published_at FROM agent_flow_versions
            WHERE flow_id = %s ORDER BY version DESC""", (flow_id,)).fetchall()
        latest = None
        if row and row["published_version"]:
            latest = connection.execute("""
                SELECT spec FROM agent_flow_versions WHERE flow_id = %s AND version = %s""",
                (flow_id, row["published_version"])).fetchone()
    is_builtin = flow_id == builtin.id
    if row is None and not is_builtin:
        raise FlowNotFound(flow_id)
    history = [{"version": v["version"], "note": v["note"], "published_at": _iso(v["published_at"])} for v in versions]
    if is_builtin:
        history.append({"version": 0, "note": "Built-in (app/flows/default_flow.json)", "published_at": None})
    if row and row["draft"] is not None:
        spec, source = row["draft"], "draft"
    elif latest:
        spec, source = latest["spec"], "published"
    elif is_builtin:
        spec, source = builtin.model_dump(mode="json"), "builtin"
    else:
        raise FlowNotFound(flow_id)
    return {"flow": spec, "source": source, "builtin": is_builtin,
            "published_version": (row["published_version"] if row else None) or 0,
            "draft_updated_at": _iso(row["draft_updated_at"]) if row else None, "versions": history}


def get_version(flow_id: str, version: int, builtin: FlowSpec) -> dict:
    if version == 0 and flow_id == builtin.id:
        return builtin.model_dump(mode="json")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            SELECT spec FROM agent_flow_versions WHERE flow_id = %s AND version = %s""",
            (flow_id, version)).fetchone()
    if row is None:
        raise FlowNotFound(f"{flow_id} v{version}")
    return row["spec"]


def create_flow(spec: FlowSpec, builtin: FlowSpec) -> None:
    if spec.id == builtin.id:
        raise FlowConflict(f"{spec.id!r} is the built-in flow")
    ensure_schema()
    with _connect() as connection:
        inserted = connection.execute("""
            INSERT INTO agent_flows (id, name, draft, draft_updated_at) VALUES (%s, %s, %s, now())
            ON CONFLICT (id) DO NOTHING RETURNING id""",
            (spec.id, spec.name, Jsonb(spec.model_dump(mode="json")))).fetchone()
    if inserted is None:
        raise FlowConflict(f"a flow called {spec.id!r} already exists")


def save_draft(spec: FlowSpec, builtin: FlowSpec) -> None:
    ensure_schema()
    with _connect() as connection:
        if spec.id == builtin.id:
            connection.execute("""
                INSERT INTO agent_flows (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING""",
                (spec.id, spec.name))
        updated = connection.execute("""
            UPDATE agent_flows SET draft = %s, name = %s, draft_updated_at = now() WHERE id = %s RETURNING id""",
            (Jsonb(spec.model_dump(mode="json")), spec.name, spec.id)).fetchone()
    if updated is None:
        raise FlowNotFound(spec.id)


def discard_draft(flow_id: str) -> None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE agent_flows SET draft = NULL, draft_updated_at = NULL WHERE id = %s", (flow_id,))


def publish(flow_id: str, note: str, check) -> int:
    """Freeze the draft as the next version. `check(spec)` raises if the
    draft must not be published (validation or compile errors)."""
    ensure_schema()
    with _connect() as connection, connection.transaction():
        row = connection.execute("SELECT * FROM agent_flows WHERE id = %s FOR UPDATE", (flow_id,)).fetchone()
        if row is None or row["draft"] is None:
            raise FlowConflict("there is no saved draft to publish")
        version = (row["published_version"] or 0) + 1
        spec = FlowSpec.model_validate({**row["draft"], "version": version})
        check(spec)
        connection.execute("""
            INSERT INTO agent_flow_versions (flow_id, version, spec, note) VALUES (%s, %s, %s, %s)""",
            (flow_id, version, Jsonb(spec.model_dump(mode="json")), note[:500]))
        connection.execute("""
            UPDATE agent_flows SET published_version = %s, draft = NULL, draft_updated_at = NULL
            WHERE id = %s""", (version, flow_id))
    return version


def live_pointer() -> dict:
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT flow_id, version, updated_at FROM agent_live_flow").fetchone()
    return {"flow_id": row["flow_id"], "version": row["version"], "updated_at": _iso(row["updated_at"])} if row else None


def live(builtin: FlowSpec) -> dict:
    """{"flow", "version"}: the version users get."""
    pointer = live_pointer()
    if pointer is None or (pointer["flow_id"] == builtin.id and pointer["version"] == 0):
        return {"flow": builtin.model_dump(mode="json"), "version": 0}
    return {"flow": get_version(pointer["flow_id"], pointer["version"], builtin), "version": pointer["version"]}


def set_live(flow_id: str, version: int, builtin: FlowSpec, check) -> None:
    """Make a published version (or the built-in, version 0) what users get.
    `check(spec)` raises when it cannot run."""
    check(FlowSpec.model_validate(get_version(flow_id, version, builtin)))
    ensure_schema()
    with _connect() as connection:
        if flow_id == builtin.id and version == 0:
            connection.execute("DELETE FROM agent_live_flow")
        else:
            connection.execute("""
                INSERT INTO agent_live_flow (singleton, flow_id, version) VALUES (true, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET flow_id = EXCLUDED.flow_id, version = EXCLUDED.version,
                                                      updated_at = now()""", (flow_id, version))


# Each run field and its value when a run does not report it (None: unknown).
RUN_FIELDS = {"seconds": 0.0, "rounds": 0, "tasks": 0, "input_tokens": 0, "output_tokens": 0,
              "input_blocked": False, "output_blocked": False, "evaluated": False, "passed": None,
              "overall": None, "failed": False}


def record_run(flow_id: str, version, source: str, metrics: dict) -> None:
    """Store one run's numbers (RUN_FIELDS); anything else in `metrics` is ignored."""
    ensure_schema()
    columns = list(RUN_FIELDS)
    values = [default if metrics.get(k) is None else metrics[k] for k, default in RUN_FIELDS.items()]
    with _connect() as connection:
        connection.execute(
            f"INSERT INTO agent_flow_runs (flow_id, version, source, {', '.join(columns)}) "
            f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(columns))})",
            (flow_id, str(version), source[:20], *values))


def metrics(flow_id: str, days: int = 30) -> list[dict]:
    """Per version and source over the last `days`: runs, pass and block rates, time and tokens."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT version, source, count(*) AS runs,
                   count(*) FILTER (WHERE failed) AS failed,
                   count(*) FILTER (WHERE input_blocked) AS input_blocked,
                   count(*) FILTER (WHERE output_blocked) AS output_blocked,
                   count(*) FILTER (WHERE evaluated) AS evaluated,
                   count(*) FILTER (WHERE passed) AS passed,
                   avg(overall) FILTER (WHERE evaluated) AS overall,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS p50_seconds,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds) AS p90_seconds,
                   avg(rounds) AS rounds, avg(input_tokens + output_tokens) AS tokens,
                   max(started_at) AS last_run
            FROM agent_flow_runs
            WHERE flow_id = %s AND started_at > now() - make_interval(days => %s)
            GROUP BY version, source
            ORDER BY max(started_at) DESC""", (flow_id, days)).fetchall()
    out = []
    for r in rows:
        out.append({
            "version": r["version"], "source": r["source"], "runs": r["runs"], "failed": r["failed"],
            "input_blocked": r["input_blocked"], "output_blocked": r["output_blocked"],
            "evaluated": r["evaluated"], "passed": r["passed"],
            "pass_rate": round(r["passed"] / r["evaluated"], 3) if r["evaluated"] else None,
            "overall": round(r["overall"], 3) if r["overall"] is not None else None,
            "p50_seconds": round(r["p50_seconds"], 2), "p90_seconds": round(r["p90_seconds"], 2),
            "rounds": round(float(r["rounds"]), 2), "tokens": round(float(r["tokens"])),
            "last_run": _iso(r["last_run"])})
    return out

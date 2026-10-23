"""Operations (R53): is the system healthy and what does it cost, from the run
records in PostgreSQL (not Langfuse):

    agent_flow_runs        one row per run: seconds, tokens, failed, timed out,
                           blocked, budget hit; who asked, the channel, the model
    agent_flow_run_agents  per run, each agent's model, seconds and tokens
    answer_cache_stats     cache hits and misses per day (R19)
    ingestion_jobs         the ingestion worker's queue

Cost is approximate: tokens × the list price of their model (model_prices.json).
An agent's tokens are priced at its own model; the rest of a run's tokens (the
guardrails, the evaluators, question rewrites) at the run's model. Runs recorded
before R53 have no model: they are priced at the current one.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import analytics
import slo
from common import config

FILTERS = {"flow": "flow_id", "model": "run_model", "channel": "channel", "user": "user_id", "source": "source"}
TOP = 10
RECENT_FAILURES = 10


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def ensure_schema() -> None:
    import answer_cache
    import identity
    from flows import store
    store.ensure_schema()
    identity.ensure_schema()
    answer_cache.ensure_schema()


def _prices() -> tuple[dict, str]:
    import llm
    prices = json.loads(Path(__file__).with_name("model_prices.json").read_text())["models"]
    return {m: {"input": p.get("input"), "output": p.get("output")} for m, p in prices.items()}, llm.MODEL


# The runs in the period that match the filters, each with its cost (CTE `cost`).
_RUNS = """
WITH prices AS (SELECT key AS model, (value->>'input')::float8 AS pin, (value->>'output')::float8 AS pout
                FROM jsonb_each(%(prices)s)),
runs AS (SELECT * FROM (SELECT r.*, coalesce(r.model, %(current)s) AS run_model FROM agent_flow_runs r
                        WHERE r.started_at >= %(begin)s AND r.started_at < %(end)s) r WHERE {where}),
agents AS (SELECT a.*, coalesce(a.model, r.run_model) AS agent_model, r.started_at
           FROM agent_flow_run_agents a JOIN runs r ON r.id = a.run_id),
spent AS (SELECT run_id, sum(input_tokens) AS i, sum(output_tokens) AS o FROM agents GROUP BY 1),
lines AS (SELECT run_id, agent_model AS model, input_tokens AS i, output_tokens AS o FROM agents
          UNION ALL
          SELECT r.id, r.run_model, greatest(r.input_tokens - coalesce(s.i, 0), 0), greatest(r.output_tokens - coalesce(s.o, 0), 0)
          FROM runs r LEFT JOIN spent s ON s.run_id = r.id),
priced AS (SELECT l.*, (l.i * p.pin + l.o * p.pout) / 1e6 AS cost FROM lines l LEFT JOIN prices p ON p.model = l.model),
cost AS (SELECT run_id, sum(cost) AS cost FROM priced GROUP BY 1)
"""

# The health numbers of a group of runs (FROM runs r LEFT JOIN cost c ON c.run_id = r.id).
_HEALTH = """count(*) AS runs, count(*) FILTER (WHERE failed) AS failed, count(*) FILTER (WHERE timed_out) AS timed_out,
             count(*) FILTER (WHERE input_blocked) AS input_blocked, count(*) FILTER (WHERE output_blocked) AS output_blocked,
             count(*) FILTER (WHERE budget_hit) AS budget_hit,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS p50,
             percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds) AS p95,
             coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens,
             sum(c.cost) AS cost"""


def _health(row: dict) -> dict:
    runs = row["runs"] or 0
    rate = lambda n: round(n / runs, 4) if runs else None
    return {"runs": runs, "failed": row["failed"], "timed_out": row["timed_out"], "error_rate": rate(row["failed"]),
            "timeout_rate": rate(row["timed_out"]), "input_blocked": row["input_blocked"],
            "output_blocked": row["output_blocked"], "budget_hit": row["budget_hit"],
            "p50": round(row["p50"], 2) if row["p50"] is not None else None,
            "p95": round(row["p95"], 2) if row["p95"] is not None else None,
            "input_tokens": int(row["input_tokens"]), "output_tokens": int(row["output_tokens"]),
            "cost": round(row["cost"], 4) if row["cost"] is not None else None}


def _latency(row: dict) -> dict:
    return {"p50": round(row["p50"], 2) if row["p50"] is not None else None,
            "p95": round(row["p95"], 2) if row["p95"] is not None else None}


def _filters(given: dict | None) -> tuple[str, dict]:
    """The WHERE over `runs` for the filters given ({"flow": …, "model": …}); unknown ones are ignored."""
    clauses, params = ["true"], {}
    for name, value in (given or {}).items():
        if name in FILTERS and value:
            clauses.append(f"{FILTERS[name]} = %(f_{name})s")
            params[f"f_{name}"] = str(value)
    return " AND ".join(clauses), params


class _Query:
    """Queries over the period's filtered runs, on one connection."""

    def __init__(self, connection, begin: datetime, end: datetime, filters: dict | None):
        prices, current = _prices()
        where, params = _filters(filters)
        self.connection, self.prefix = connection, _RUNS.format(where=where)
        self.params = {"prices": Jsonb(prices), "current": current, "begin": begin, "end": end, **params}

    def all(self, sql: str, **extra) -> list[dict]:
        return self.connection.execute(self.prefix + sql, {**self.params, **extra}).fetchall()


def _summary(q: _Query) -> dict:
    [row] = q.all(f"SELECT {_HEALTH} FROM runs r LEFT JOIN cost c ON c.run_id = r.id")
    return _health(row)


def _by(q: _Query, column: str, order: str = "runs DESC", limit: int | None = None) -> list[dict]:
    rows = q.all(f"SELECT {column} AS key, {_HEALTH} FROM runs r LEFT JOIN cost c ON c.run_id = r.id "
                 f"GROUP BY 1 ORDER BY {order}, 1 {'LIMIT %(limit)s' if limit else ''}", limit=limit)
    return [{"key": r["key"], **_health(r)} for r in rows]


def _agents(q: _Query) -> list[dict]:
    """Per agent (role): how often it ran, its model time per run (p50, p95), tokens and cost."""
    rows = q.all("""
        SELECT a.role, count(DISTINCT a.run_id) AS runs, sum(a.turns) AS turns,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY a.seconds) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY a.seconds) AS p95,
               sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens,
               sum((a.input_tokens * p.pin + a.output_tokens * p.pout) / 1e6) AS cost,
               array_agg(DISTINCT a.agent_model) AS models
        FROM agents a LEFT JOIN prices p ON p.model = a.agent_model GROUP BY 1 ORDER BY p95 DESC NULLS LAST, 1""")
    return [{"role": r["role"], "runs": r["runs"], "turns": int(r["turns"]), **_latency(r),
             "input_tokens": int(r["input_tokens"]), "output_tokens": int(r["output_tokens"]),
             "cost": round(r["cost"], 4) if r["cost"] is not None else None,
             "models": sorted(m for m in r["models"] if m)} for r in rows]


def _models(q: _Query) -> list[dict]:
    """Per model: its tokens and cost (every line priced at it), and its agents' model time per call."""
    rows = q.all("""
        SELECT l.model, sum(l.i) AS input_tokens, sum(l.o) AS output_tokens, sum(l.cost) AS cost,
               bool_or(p.model IS NOT NULL) AS priced,
               (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY a.seconds) FROM agents a WHERE a.agent_model = l.model) AS p50,
               (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY a.seconds) FROM agents a WHERE a.agent_model = l.model) AS p95
        FROM priced l LEFT JOIN prices p ON p.model = l.model
        WHERE l.i > 0 OR l.o > 0 GROUP BY l.model ORDER BY cost DESC NULLS LAST, l.model""")
    return [{"model": r["model"], "input_tokens": int(r["input_tokens"]), "output_tokens": int(r["output_tokens"]),
             "cost": round(r["cost"], 4) if r["cost"] is not None else None, "priced": r["priced"], **_latency(r)}
            for r in rows]


def _days(q: _Query) -> list[dict]:
    rows = q.all(f"""
        SELECT date_trunc('day', started_at AT TIME ZONE 'UTC')::date AS key, {_HEALTH}
        FROM runs r LEFT JOIN cost c ON c.run_id = r.id GROUP BY 1 ORDER BY 1""")
    by_model = q.all("""
        SELECT date_trunc('day', r.started_at AT TIME ZONE 'UTC')::date AS day, l.model, sum(l.cost) AS cost
        FROM priced l JOIN runs r ON r.id = l.run_id WHERE l.cost > 0 GROUP BY 1, 2""")
    costs: dict = {}
    for r in by_model:
        costs.setdefault(r["day"], {})[r["model"]] = round(r["cost"], 4)
    return [{"day": r["key"].isoformat(), **_health(r), "cost_by_model": costs.get(r["key"], {})} for r in rows]


def _users(q: _Query) -> list[dict]:
    rows = _by(q, "user_id", order="cost DESC NULLS LAST, runs DESC", limit=TOP)
    ids = [r["key"] for r in rows if r["key"]]
    names = {u["user_id"]: u for u in q.connection.execute(
        "SELECT user_id, first_name, email FROM app_users WHERE user_id = ANY(%s)", (ids,)).fetchall()} if ids else {}
    return [{**r, "name": (names.get(r["key"]) or {}).get("first_name"), "email": (names.get(r["key"]) or {}).get("email")}
            for r in rows]


def _options(connection, begin: datetime, end: datetime) -> dict:
    """The values each filter can take in the period (whatever the other filters)."""
    _, current = _prices()
    out = {}
    for name, column in FILTERS.items():
        expression = "coalesce(model, %(current)s)" if column == "run_model" else column
        rows = connection.execute(f"""
            SELECT DISTINCT {expression} AS v FROM agent_flow_runs
            WHERE started_at >= %(begin)s AND started_at < %(end)s AND {expression} IS NOT NULL ORDER BY 1""",
            {"begin": begin, "end": end, "current": current}).fetchall()
        out[name] = [r["v"] for r in rows]
    if out["user"]:
        names = {u["user_id"]: u["first_name"] for u in connection.execute(
            "SELECT user_id, first_name FROM app_users WHERE user_id = ANY(%s)", (out["user"],)).fetchall()}
        out["user"] = [{"id": u, "name": names.get(u) or u} for u in out["user"]]
    return out


def cache(connection, begin: datetime, end: datetime) -> dict:
    """Answer-cache hits and misses (R19) in the period: kept per day, for every flow and channel."""
    row = connection.execute("""
        SELECT coalesce(sum(hits), 0) AS hits, coalesce(sum(misses), 0) AS misses FROM answer_cache_stats
        WHERE day >= %s AND day < %s""", (begin.date(), end.date())).fetchone()
    looked = row["hits"] + row["misses"]
    return {"hits": row["hits"], "misses": row["misses"], "hit_rate": round(row["hits"] / looked, 4) if looked else None}


def ingestion(connection, begin: datetime, end: datetime) -> dict:
    """The ingestion worker's queue now, and its jobs that ended in the period."""
    try:
        with connection.transaction():
            now = connection.execute("""
                SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
                       count(*) FILTER (WHERE status = 'running') AS running,
                       extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'queued')) AS oldest_queued_seconds
                FROM ingestion_jobs""").fetchone()
            ended = connection.execute("""
                SELECT count(*) FILTER (WHERE status = 'success') AS succeeded, count(*) FILTER (WHERE status = 'failed') AS failed
                FROM ingestion_jobs WHERE coalesce(finished_at, created_at) >= %s AND coalesce(finished_at, created_at) < %s""",
                (begin, end)).fetchone()
            failures = connection.execute("""
                SELECT kind, created_at, finished_at, left(coalesce(error, ''), 300) AS error FROM ingestion_jobs
                WHERE status = 'failed' AND coalesce(finished_at, created_at) >= %s AND coalesce(finished_at, created_at) < %s
                ORDER BY coalesce(finished_at, created_at) DESC LIMIT %s""", (begin, end, RECENT_FAILURES)).fetchall()
    except psycopg.errors.UndefinedTable:      # nothing ingested yet
        return {"queued": 0, "running": 0, "oldest_queued_seconds": None, "succeeded": 0, "failed": 0, "failures": []}
    oldest = now["oldest_queued_seconds"]
    return {"queued": now["queued"], "running": now["running"],
            "oldest_queued_seconds": round(float(oldest)) if oldest is not None else None,
            "succeeded": ended["succeeded"], "failed": ended["failed"],
            "failures": [{"kind": f["kind"], "at": (f["finished_at"] or f["created_at"]).isoformat(), "error": f["error"]}
                         for f in failures]}


def _slowest(q: _Query) -> dict:
    """Yesterday's slowest flow, agent and model by their 95th-percentile time."""
    flows = [r for r in _by(q, "flow_id", order="p95 DESC NULLS LAST") if r["p95"] is not None]
    agents = [a for a in _agents(q) if a["p95"] is not None]
    models = sorted((m for m in _models(q) if m["p95"] is not None), key=lambda m: -m["p95"])
    pick = lambda rows, key: {"name": rows[0][key], "p95": rows[0]["p95"]} if rows else None
    return {"flow": pick(flows, "key"), "agent": pick(agents, "role"), "model": pick(models, "model")}


def report(start: str | None = None, end: str | None = None, filters: dict | None = None) -> dict:
    """The Operations page: the period's health and cost, broken down, and yesterday's at a glance."""
    begin, finish = analytics.period(start, end)
    ensure_schema()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with _connect() as connection:
        q = _Query(connection, begin, finish, filters)
        y = _Query(connection, today - timedelta(days=1), today, filters)
        prices, current = _prices()
        models = _models(q)
        return {
            "from": begin.date().isoformat(), "to": (finish - timedelta(days=1)).date().isoformat(),
            "filters": {k: v for k, v in (filters or {}).items() if k in FILTERS and v},
            "options": _options(connection, begin, finish),
            "current_model": current, "unpriced": sorted(m["model"] for m in models if not m["priced"]),
            "yesterday": {"day": (today - timedelta(days=1)).date().isoformat(), **_summary(y), "slowest": _slowest(y),
                          "cache": cache(connection, today - timedelta(days=1), today)},
            "summary": _summary(q), "cache": cache(connection, begin, finish),
            "days": _days(q), "flows": _by(q, "flow_id", order="p95 DESC NULLS LAST"),
            "agents": _agents(q), "models": models,
            "channels": _by(q, "coalesce(channel, 'unknown')", order="cost DESC NULLS LAST, runs DESC"),
            "sources": _by(q, "source"), "users": _users(q), "ingestion": ingestion(connection, begin, finish),
            "slo": slo.report()}

"""Service-level objectives and their error budgets (R61), from real users'
runs (agent_flow_runs, source 'users') over the last WINDOW_DAYS:

    availability  runs that finished without an error or a timeout
    latency       runs answered within SLO_LATENCY_SECONDS
    pass rate     checked answers that passed the answer check

Each has a target (environment: SLO_AVAILABILITY, SLO_LATENCY_TARGET,
SLO_PASS_RATE). The error budget is the share of runs allowed to miss
((1 − target) × runs); "left" is how much of it remains, negative once
spent. The burn rate is the last day's misses against what the budget allows
per day: above 1 the budget runs out before the window ends.

The Operations page shows them; loadtest/README.md has the measurements the
defaults come from.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from common import config

WINDOW_DAYS = 30


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


LATENCY_SECONDS = _float("SLO_LATENCY_SECONDS", 30)
OBJECTIVES = {
    "availability": {"target": _float("SLO_AVAILABILITY", 0.99),
                     "what": "Questions answered without an error or a timeout"},
    "latency": {"target": _float("SLO_LATENCY_TARGET", 0.95),
                "what": f"Questions answered within {LATENCY_SECONDS:g} seconds"},
    "pass_rate": {"target": _float("SLO_PASS_RATE", 0.80),
                  "what": "Checked answers that passed the answer check"},
}


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def _counts(connection, since: datetime, until: datetime) -> dict:
    row = connection.execute("""
        SELECT count(*) AS runs,
               count(*) FILTER (WHERE failed OR timed_out) AS errors,
               count(*) FILTER (WHERE seconds > %s) AS slow,
               count(*) FILTER (WHERE evaluated) AS checked,
               count(*) FILTER (WHERE evaluated AND NOT passed) AS not_passed
        FROM agent_flow_runs WHERE source = 'users' AND started_at >= %s AND started_at < %s""",
        (LATENCY_SECONDS, since, until)).fetchone()
    return {"availability": (row["runs"], row["errors"]), "latency": (row["runs"], row["slow"]),
            "pass_rate": (row["checked"], row["not_passed"])}


def report(now: datetime | None = None) -> dict:
    """Each objective: target, what it measured over the window, its error budget and the last day's burn rate."""
    from flows import store
    store.ensure_schema()
    now = now or datetime.now(timezone.utc)
    with _connect() as connection:
        window = _counts(connection, now - timedelta(days=WINDOW_DAYS), now)
        day = _counts(connection, now - timedelta(days=1), now)
    out = []
    for name, objective in OBJECTIVES.items():
        target = objective["target"]
        total, bad = window[name]
        day_total, day_bad = day[name]
        allowed = (1 - target) * total
        out.append({
            "name": name, "what": objective["what"], "target": target, "events": total, "misses": bad,
            "achieved": round(1 - bad / total, 4) if total else None,
            "budget": round(allowed, 1), "left": round(1 - bad / allowed, 3) if allowed else None,
            "burn_rate": round((day_bad / day_total) / (1 - target), 2) if day_total and target < 1 else None,
            "met": (1 - bad / total) >= target if total else None})
    return {"window_days": WINDOW_DAYS, "latency_seconds": LATENCY_SECONDS, "objectives": out}

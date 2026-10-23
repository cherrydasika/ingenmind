"""Scheduled evaluations and the promotion gate (R56).

Quality changes when content, models or providers change, even with no
deploy, so chosen question sets run every night on the live flow, and a
version must match or beat the live one on the gate sets before it goes live.

    agent_eval_schedule  (one row) enabled, the nightly sets, the hour (UTC),
                         the gate sets (none: every set both versions ran),
                         the tolerance, the day it last ran

gate(): for each gate set, the candidate's newest finished run against the
live version's; worse by more than the tolerance (or never run on a set the
live version was measured on) blocks it, unless the admin overrides with a
reason (the web API keeps it in the audit log). The red-team set has its own
check (R14), and the built-in flow (version 0) can always be gone back to.
A nightly run that drops raises the evaluation-regression alert (R55, evals._finish).
"""

import sys
import threading
import time
from datetime import datetime, timezone

from . import evals, store

CHECK_EVERY = 600
_ready = False
_lock = threading.Lock()


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    evals.ensure_schema()
    with _lock, store._connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616757)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_eval_schedule (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                enabled boolean NOT NULL DEFAULT false,
                sets text[] NOT NULL DEFAULT '{}',
                hour int NOT NULL DEFAULT 2,
                gate_sets text[] NOT NULL DEFAULT '{}',
                tolerance real NOT NULL DEFAULT 0,
                last_run date,
                updated_at timestamptz NOT NULL DEFAULT now()
            )""")
        connection.execute("INSERT INTO agent_eval_schedule (singleton) VALUES (true) ON CONFLICT DO NOTHING")
    _ready = True


def reset_schema_cache() -> None:
    global _ready
    _ready = False


def schedule() -> dict:
    ensure_schema()
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM agent_eval_schedule").fetchone()
    return {"enabled": row["enabled"], "sets": list(row["sets"]), "hour": row["hour"], "gate_sets": list(row["gate_sets"]),
            "tolerance": row["tolerance"], "last_run": row["last_run"].isoformat() if row["last_run"] else None}


def save_schedule(enabled=None, sets=None, hour=None, gate_sets=None, tolerance=None) -> dict:
    known = {s["id"] for s in evals.list_sets()}
    changes = {}
    if enabled is not None:
        changes["enabled"] = bool(enabled)
    for name, value in (("sets", sets), ("gate_sets", gate_sets)):
        if value is not None:
            if not isinstance(value, list) or not all(isinstance(v, str) and v in known for v in value) or len(value) > 10:
                raise ValueError(f"{name}: up to 10 known question sets")
            changes[name] = sorted(set(value))
    if hour is not None:
        if not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError("hour: 0 to 23 (UTC)")
        changes["hour"] = hour
    if tolerance is not None:
        if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 0.5:
            raise ValueError("tolerance: 0 to 0.5 (a share of the pass rate)")
        changes["tolerance"] = float(tolerance)
    ensure_schema()
    if changes:
        with store._connect() as connection:
            connection.execute(f"UPDATE agent_eval_schedule SET {', '.join(f'{k} = %s' for k in changes)}, "
                               "updated_at = now()", tuple(changes.values()))
    return schedule()


# ---------- the gate ----------

def _newest(connection, flow_id: str, version) -> dict[str, dict]:
    """Per set: the newest finished run of this version (memory on: the way users get it)."""
    rows = connection.execute("""
        SELECT DISTINCT ON (set_id) id, set_id, set_name, summary, finished_at FROM agent_eval_runs
        WHERE flow_id = %s AND version = %s AND status = 'done' AND set_id <> %s
          AND coalesce(options->>'memory', 'true') <> 'false'
        ORDER BY set_id, finished_at DESC""", (flow_id, str(version), evals.RED_TEAM)).fetchall()
    out = {}
    for r in rows:
        summary = r["summary"] or {}
        # A run whose answers were never checked (none got through) passed nothing: 0, not "unknown".
        rate = summary.get("pass_rate") if summary.get("pass_rate") is not None else (0.0 if summary.get("questions") else None)
        if rate is not None:
            out[r["set_id"]] = {**r, "summary": {**summary, "pass_rate": rate}}
    return out


def gate(flow_id: str, version: int, live: dict) -> dict:
    """{"passed", "checks": [{set, candidate, live, ok}], "tolerance"}: the candidate against the live version."""
    settings = schedule()
    if version == 0 or (live["flow_id"] == flow_id and live["version"] == version):
        return {"passed": True, "checks": [], "tolerance": settings["tolerance"], "exempt": True}
    with store._connect() as connection:
        mine = _newest(connection, flow_id, version)
        theirs = _newest(connection, live["flow_id"], live["version"])
    sets = settings["gate_sets"] or sorted(set(mine) & set(theirs))
    checks = []
    for set_id in sets:
        candidate, current = mine.get(set_id), theirs.get(set_id)
        if current is None:          # nothing to compare against: the live version was never measured on it
            continue
        name = (candidate or current)["set_name"]
        if candidate is None:
            checks.append({"set_id": set_id, "set_name": name, "candidate": None, "live": current["summary"]["pass_rate"],
                           "ok": False, "why": "not run on this version"})
            continue
        ours, theirs_rate = candidate["summary"]["pass_rate"], current["summary"]["pass_rate"]
        checks.append({"set_id": set_id, "set_name": name, "candidate": ours, "live": theirs_rate,
                       "candidate_run": candidate["id"], "live_run": current["id"],
                       "ok": ours >= theirs_rate - settings["tolerance"] - 1e-9})
    return {"passed": all(c["ok"] for c in checks), "checks": checks, "tolerance": settings["tolerance"],
            "live": live, "configured": bool(settings["gate_sets"])}


def describe(result: dict, version: int) -> str:
    worse = [c for c in result["checks"] if not c["ok"]]
    parts = [f"{c['set_name']}: not run on v{version}" if c["candidate"] is None else
             f"{c['set_name']}: {round(c['candidate'] * 100)}% vs live {round(c['live'] * 100)}%" for c in worse]
    return (f"v{version} does worse than the live version (v{result['live']['version']}) on the gate sets: "
            f"{'; '.join(parts)}. Run it again, fix it, or make it live anyway with a reason.")


# ---------- nightly runs and trends ----------

def due(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    s = schedule()
    return s["enabled"] and bool(s["sets"]) and now.hour >= s["hour"] and s["last_run"] != now.date().isoformat()


def run_nightly(live_target, execute, now: datetime | None = None) -> list[str]:
    """Queue the scheduled sets on the live flow, once a day (claimed in the database: one instance runs it).
    live_target() → (flow_id, version label, flow JSON)."""
    now = now or datetime.now(timezone.utc)
    ensure_schema()
    with store._connect() as connection:
        claimed = connection.execute("""
            UPDATE agent_eval_schedule SET last_run = %s
            WHERE enabled AND cardinality(sets) > 0 AND %s >= hour AND last_run IS DISTINCT FROM %s
            RETURNING sets""", (now.date(), now.hour, now.date())).fetchone()
    if not claimed:
        return []
    flow_id, label, spec = live_target()
    ids = []
    for set_id in claimed["sets"]:
        try:
            ids += evals.start(set_id, flow_id, [(label, spec)], execute)
        except LookupError as error:      # a set removed since it was scheduled
            print(f"nightly evaluation skipped: {error}", file=sys.stderr)
    if ids:
        with store._connect() as connection:
            connection.execute("UPDATE agent_eval_runs SET options = options || '{\"scheduled\": true}' "
                               "WHERE id = ANY(%s)", (ids,))
    return ids


def loop(live_target, execute, every: float = CHECK_EVERY) -> None:
    """The nightly scheduler (web_api starts it)."""
    while True:
        time.sleep(every)
        try:
            if due():
                run_nightly(live_target, execute)
        except Exception as error:   # the database may be restarting
            print(f"nightly evaluations skipped: {error}", file=sys.stderr)


def trends(days: int = 90) -> list[dict]:
    """Per set: its finished runs over time (pass rate, which flow version, scheduled or not), oldest first."""
    evals.ensure_schema()
    with store._connect() as connection:
        rows = connection.execute("""
            SELECT id, set_id, set_name, flow_id, version, finished_at, summary, options FROM agent_eval_runs
            WHERE status = 'done' AND finished_at > now() - make_interval(days => %s)
            ORDER BY finished_at""", (days,)).fetchall()
    sets: dict[str, dict] = {}
    for r in rows:
        summary = r["summary"] or {}
        rate = summary.get("pass_rate") if r["set_id"] != evals.RED_TEAM else (
            round(summary["expectations_met"] / summary["expectations"], 3) if summary.get("expectations") else None)
        if rate is None:
            continue
        sets.setdefault(r["set_id"], {"set_id": r["set_id"], "set_name": r["set_name"], "runs": []})["runs"].append({
            "run_id": r["id"], "at": r["finished_at"].isoformat(), "flow_id": r["flow_id"], "version": r["version"],
            "rate": rate, "scheduled": bool((r["options"] or {}).get("scheduled"))})
    return sorted(sets.values(), key=lambda s: s["set_name"])

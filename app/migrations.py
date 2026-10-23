"""Database migrations on start (R62).

Every module owns its tables and brings them up to date idempotently
(`ensure_schema`: CREATE … IF NOT EXISTS, ADD COLUMN IF NOT EXISTS). On
start the web app runs them all, before it serves anything:

    1. waits for PostgreSQL (up to MIGRATE_WAIT_SECONDS, default 60)
    2. refuses to start if the database was last migrated by a newer
       release than this one (a downgrade; ALLOW_DOWNGRADE=true overrides,
       after restoring a backup taken before the upgrade)
    3. runs every module's schema setup; one that depends on another's
       tables is retried after the rest, so the list needs no ordering
    4. records the version, the time and how long it took
       (ingenmind_schema_versions)

Several web workers starting at once take turns (an advisory lock). Version
"dev" (a checkout, not a release) is recorded but never refused or refusing.
See docs/upgrading.md.
"""

import importlib
import os
import re
import sys
import time

import psycopg

from common import config

VERSION = (os.environ.get("INGENMIND_APP_VERSION", "").strip() or "dev").removeprefix("v")
LOCK = 72_616_799     # distinct from the modules' own schema locks (72_616_701–704)
MODULES = (
    "identity", "audit", "sessions", "api_keys", "usage_limits", "knowledge_system", "knowledge_collections",
    "flows.store", "flows.evals", "flows.eval_gate", "flows.search_eval", "durable", "human", "agent_memory",
    "user_memory", "answer_cache", "answer_feedback", "knowledge_gaps", "use_learnings", "analytics", "operations",
    "alerts", "webhooks", "channels", "connectors", "mcp_integration", "openapi_tools", "recrawl", "source_profiles",
    "data_lifecycle", "initialization.plan", "initialization.blueprint", "initialization.content",
    "initialization.conversation", "initialization.evaluation", "initialization.labels",
    "initialization.live_sources", "initialization.sources", "initialization.sources_run",
)


class Refused(Exception):
    """The database is newer than this release."""


def parse(version: str) -> tuple | None:
    """(1, 2, 3, 1, 'rc.1') style key for comparing, or None for dev builds. A release sorts after its pre-releases."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?", version or "")
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, m[4] or ""


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, autocommit=True)


def _wait(seconds: float):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return _connect()
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _steps() -> list[tuple[str, callable]]:
    from common import storage
    steps = [("documents", lambda: storage.ensure_collection(storage.get_client()))]
    for name in MODULES:
        steps.append((name, importlib.import_module(name).ensure_schema))
    return steps


def newest(connection) -> str | None:
    """The newest release that migrated this database (dev builds aside)."""
    versions = [r[0] for r in connection.execute("SELECT version FROM ingenmind_schema_versions")]
    released = [v for v in versions if parse(v)]
    return max(released, key=parse) if released else None


def run(version: str = VERSION, wait: float | None = None) -> dict:
    """Bring the database up to this release's schema. Raises Refused on a downgrade."""
    started = time.monotonic()
    wait = float(os.environ.get("MIGRATE_WAIT_SECONDS", "") or 60) if wait is None else wait
    with _wait(wait) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS ingenmind_schema_versions (
                id bigserial PRIMARY KEY, version text NOT NULL, migrated_at timestamptz NOT NULL DEFAULT now(),
                seconds real NOT NULL, steps int NOT NULL)""")
            previous = newest(connection)
            if (parse(version) and previous and parse(previous) > parse(version)
                    and os.environ.get("ALLOW_DOWNGRADE", "").lower() not in ("1", "true", "yes")):
                raise Refused(f"this database was migrated by IngenMind {previous}, newer than this release "
                              f"({version}). Run {previous} or newer, or restore the backup taken before upgrading "
                              "and set ALLOW_DOWNGRADE=true (docs/upgrading.md).")
            pending, errors = _steps(), {}
            while pending:                      # a step needing another's tables runs again after the rest
                failed = []
                for name, step in pending:
                    try:
                        step()
                    except Exception as exc:    # noqa: BLE001 - reported below if it never succeeds
                        failed.append((name, step))
                        errors[name] = exc
                if len(failed) == len(pending):
                    raise RuntimeError("database migration failed: " + "; ".join(
                        f"{name}: {errors[name]}" for name, _ in failed)) from errors[failed[0][0]]
                pending = failed
            seconds = round(time.monotonic() - started, 2)
            steps = len(MODULES) + 1
            connection.execute("INSERT INTO ingenmind_schema_versions (version, seconds, steps) VALUES (%s, %s, %s)",
                               (version, seconds, steps))
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    print(f"database migrated to {version} ({steps} steps, {seconds}s; previously {previous or 'new'})",
          file=sys.stderr)
    return {"version": version, "previous": previous, "seconds": seconds, "steps": steps}


if __name__ == "__main__":          # docker compose exec webapp python /app/migrations.py
    sys.path.insert(0, "/app/dags")
    run()

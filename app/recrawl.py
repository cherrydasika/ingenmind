"""Scheduled re-crawl, change detection and freshness (R49).

    source_schedules  source_url, every (daily | weekly | off), last_checked_at,
                      last_changed_at, etag, last_modified, status (unchanged |
                      changed | failed | gone | new), diff ({added, removed,
                      kept, sample_added, sample_removed}), failures, error

Every web page in the knowledge base has a schedule (weekly unless an admin
changes it on the Ingestion tab). The worker checks what is due every
CHECK_EVERY seconds (ingestion_worker.main):

1. a conditional GET (ETag / Last-Modified, scraping.check_changed): 304 means
   unchanged, and its lifetime is renewed;
2. otherwise the page is ingested again (common.ingest.ingest_url, its labels
   kept): an unchanged text only renews it; a changed one replaces the page's
   chunks, re-embedding only the chunks whose text changed, and the old
   chunks are removed;
3. the change is recorded as a diff of chunk texts.

A page that is gone (404/410) or keeps failing is flagged stale; so is one not
checked for twice its interval. (R55 alerts on a key source that disappears
or changes a lot.)
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config, ingest, scraping, storage

INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "off": None}
DEFAULT_EVERY = "weekly"
CHECK_EVERY = 600            # seconds between the worker's looks at what is due
PER_ROUND = 20               # pages checked per look at most
STALE_FAILURES = 3
SAMPLE_CHARS = 160

_schema_ready = False
_schema_lock = threading.Lock()


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
            connection.execute("SELECT pg_advisory_xact_lock(72616749)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS source_schedules (
                    source_url text PRIMARY KEY,
                    every text NOT NULL DEFAULT 'weekly',
                    last_checked_at timestamptz,
                    last_changed_at timestamptz,
                    etag text,
                    last_modified text,
                    status text NOT NULL DEFAULT 'new',
                    diff jsonb,
                    failures int NOT NULL DEFAULT 0,
                    error text
                )""")
        _schema_ready = True


def _web_sources(client) -> list[str]:
    return [row["payload"]["source_url"] for row in storage.source_summary(client)
            if str(row["payload"].get("source_url", "")).startswith(("http://", "https://"))]


def sync(client=None) -> int:
    """Every web page in the knowledge base gets a schedule (the default); pages no longer there lose theirs."""
    client = client or storage.get_client()
    urls = _web_sources(client)
    ensure_schema()
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO source_schedules (source_url, every) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               [(u, DEFAULT_EVERY) for u in urls])
        connection.execute("DELETE FROM source_schedules WHERE NOT (source_url = ANY(%s))", (urls,))
    return len(urls)


def set_every(url: str, every: str) -> None:
    if every not in INTERVALS:
        raise ValueError(f"every: one of {', '.join(INTERVALS)}")
    ensure_schema()
    with _connect() as connection:
        if not connection.execute("UPDATE source_schedules SET every = %s WHERE source_url = %s", (every, url)).rowcount:
            raise ValueError("no such source")


def due(now: datetime | None = None, limit: int = PER_ROUND) -> list[str]:
    now = now or datetime.now(timezone.utc)
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT source_url, every, last_checked_at FROM source_schedules WHERE every <> 'off' "
                                  "ORDER BY last_checked_at NULLS FIRST").fetchall()
    return [r["source_url"] for r in rows
            if r["last_checked_at"] is None or r["last_checked_at"] + INTERVALS[r["every"]] <= now][:limit]


def _diff(before: list[str], after: list[str]) -> dict:
    old, new = set(before), set(after)
    added, removed = [c for c in after if c not in old], [c for c in before if c not in new]
    return {"added": len(added), "removed": len(removed), "kept": len(new & old),
            "sample_added": added[0][:SAMPLE_CHARS] if added else None,
            "sample_removed": removed[0][:SAMPLE_CHARS] if removed else None}


def check(url: str, client=None, conditional=scraping.check_changed, ingest_url=ingest.ingest_url) -> dict:
    """One page now: unchanged, changed (re-ingested, its old chunks removed), gone or failed."""
    client = client or storage.get_client()
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM source_schedules WHERE source_url = %s", (url,)).fetchone() or {}
    existing = storage.get_existing_metadata(client, url) or {}
    update = {"last_checked_at": datetime.now(timezone.utc), "error": None}
    try:
        head = conditional(url, row.get("etag"), row.get("last_modified"))
        update.update(etag=head.get("etag"), last_modified=head.get("last_modified"))
        if head["status"] in (404, 410):
            update.update(status="gone", failures=(row.get("failures") or 0) + 1, error=f"HTTP {head['status']}")
        elif head["status"] == 304:
            storage.refresh_expiry(client, url, existing.get("ttl_days") or config.DEFAULT_TTL_DAYS)
            update.update(status="unchanged", failures=0)
        else:
            before = [c["text"] for c in storage.page_chunks(client, url)]
            result = ingest_url(client, {"url": url, "ttl_days": existing.get("ttl_days") or config.DEFAULT_TTL_DAYS,
                                         "keep_payload": True, "recheck": True})
            if result["status"] == "failed":
                update.update(status="failed", failures=(row.get("failures") or 0) + 1, error=result.get("error"))
            elif result["status"] == "updated":
                after = [c["text"] for c in storage.page_chunks(client, url)]
                update.update(status="changed", failures=0, last_changed_at=update["last_checked_at"],
                              diff=Jsonb({**_diff(before, after), "embedded": result.get("embedded")}))
            else:                                   # unchanged_ttl_refreshed, empty
                update.update(status="unchanged" if result["status"] != "empty" else "gone", failures=0)
    except Exception as error:
        update.update(status="failed", failures=(row.get("failures") or 0) + 1, error=f"{type(error).__name__}: {error}"[:300])
    columns = ", ".join(f"{k} = %s" for k in update)
    with _connect() as connection:
        connection.execute(f"INSERT INTO source_schedules (source_url) VALUES (%s) ON CONFLICT DO NOTHING", (url,))
        connection.execute(f"UPDATE source_schedules SET {columns} WHERE source_url = %s", (*update.values(), url))
    return {"url": url, **{k: (v.obj if isinstance(v, Jsonb) else v) for k, v in update.items()}}


def run_due(client=None, **ports) -> list[dict]:
    """What is due, one page after another (the worker's round)."""
    client = client or storage.get_client()
    sync(client)
    return [check(url, client, **ports) for url in due()]


def freshness(now: datetime | None = None) -> list[dict]:
    """Per web page: its schedule, last check and change, and whether it is stale (for the Ingestion tab)."""
    now = now or datetime.now(timezone.utc)
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM source_schedules ORDER BY source_url").fetchall()
    out = []
    for r in rows:
        interval = INTERVALS[r["every"]]
        overdue = bool(interval) and r["last_checked_at"] is not None and r["last_checked_at"] + 2 * interval < now
        stale = r["status"] == "gone" or r["failures"] >= STALE_FAILURES or overdue
        out.append({**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}, "stale": stale})
    return out


def loop(stop: threading.Event, every: float = CHECK_EVERY) -> None:
    """The worker's background round (it runs beside the ingestion jobs)."""
    while not stop.wait(every):
        try:
            changed = [r for r in run_due() if r.get("status") == "changed"]
            if changed:
                print(f"re-crawl: {len(changed)} changed page(s) re-ingested")
        except Exception as error:   # the database or the network may be down: next round
            print(f"re-crawl round skipped: {error}")

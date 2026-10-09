"""The ingestion plan (epic #27, #22): the content the user chose, as a saved,
versioned list of pages that they review and approve with Build RAG.

Built in code from the selected sections of #21, less the pages unticked in
them; no model call. Each page carries its source, section, knowledge areas
and TTL. Nothing is fetched here: ingestion starts only after approval.

    kb_ingestion_plans       one row per version: draft | approved | superseded,
                             the page limit, totals, who approved it and when
    kb_ingestion_plan_pages  the version's pages, in order, with their ingestion
                             status (pending until the build reads them)

TTL per section, from its pages' dates and kind (the user may set it on the
review):
    often changing (half its dated pages changed within FRESH_DAYS,
    or flagged live)                                   TTL_OFTEN
    PDFs only, or nothing changed for STALE_DAYS       TTL_RARE
    otherwise                                          config.DEFAULT_TTL_DAYS
One recently edited page does not make a section "often changing" (a help
section with one page edited last week and the rest months old is not).

Reviewing a changed selection makes a new draft version; an unchanged one
reuses the draft. Approving supersedes the earlier approved version; the
review shows what it adds and removes against that version.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import knowledge_system
from common import config, scraping, storage

from . import content, sources, state

PLAN_PAGE_LIMIT = int(os.environ.get("PLAN_PAGE_LIMIT", "500"))
TTL_OFTEN, TTL_RARE = 14, 180
FRESH_DAYS, STALE_DAYS = 30, 365
TTL_RANGE = (1, 365)
SECONDS_PER_PAGE = 2.5      # the polite delay and the work, for the review's estimate

_schema_lock = threading.Lock()
_schema_ready = False


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    content.ensure_schema()
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("ALTER TABLE kb_content ADD COLUMN IF NOT EXISTS ttl_days int")
            # Versions are never reused, so a review of a dropped draft cannot pass for the current one.
            connection.execute("CREATE SEQUENCE IF NOT EXISTS kb_ingestion_plan_versions")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb_ingestion_plans (
                    version int PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    status text NOT NULL CHECK (status IN ('draft', 'approved', 'superseded')),
                    fingerprint text NOT NULL,
                    page_limit int NOT NULL,
                    totals jsonb NOT NULL DEFAULT '{}',
                    approved_by text,
                    approved_at timestamptz,
                    build jsonb
                )""")
            # Columns added after the table first shipped (CREATE TABLE IF NOT EXISTS keeps an older table).
            connection.execute("ALTER TABLE kb_ingestion_plans ADD COLUMN IF NOT EXISTS build jsonb")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb_ingestion_plan_pages (
                    version int NOT NULL REFERENCES kb_ingestion_plans (version) ON DELETE CASCADE,
                    position int NOT NULL,
                    url text NOT NULL,
                    source_id bigint NOT NULL,
                    content_id bigint NOT NULL,
                    section text NOT NULL,
                    areas jsonb NOT NULL DEFAULT '[]',
                    kind text NOT NULL,
                    ttl_days int NOT NULL,
                    status text NOT NULL DEFAULT 'pending',
                    chunks int NOT NULL DEFAULT 0,
                    error text,
                    ingested_at timestamptz,
                    PRIMARY KEY (version, url)
                )""")
        _schema_ready = True


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


# ---------- TTL ----------

def _date(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def suggested_ttl(section: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    dates = [d for d in (_date(u.get("lastmod")) for u in section.get("urls") or []) if d]
    if not dates and _date(section.get("lastmod")):
        dates = [_date(section["lastmod"])]           # only the section's newest date is known
    newest = max(dates) if dates else None
    recent = sum(now - d <= timedelta(days=FRESH_DAYS) for d in dates)
    if "live" in (section.get("flags") or []) or (dates and recent * 2 >= len(dates)):
        return TTL_OFTEN
    if (section["pdf_count"] and section["pdf_count"] == section["url_count"]) \
            or (newest and now - newest > timedelta(days=STALE_DAYS)):
        return TTL_RARE
    return int(config.DEFAULT_TTL_DAYS)


def section_ttl(section: dict) -> int:
    return section.get("ttl_days") or suggested_ttl(section)


def set_ttl(content_id: int, ttl_days: int | None) -> dict:
    """The user's TTL for a section (None: back to the suggestion)."""
    if knowledge_system.status()["state"] != state.AWAITING_CONTENT_SELECTION:
        raise state.TransitionNotAllowed("the plan can be changed while content is being chosen")
    if ttl_days is not None and not (isinstance(ttl_days, int) and TTL_RANGE[0] <= ttl_days <= TTL_RANGE[1]):
        raise ValueError(f"ttl_days must be a whole number of days from {TTL_RANGE[0]} to {TTL_RANGE[1]}")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("UPDATE kb_content SET ttl_days = %s, updated_at = now() WHERE content_id = %s "
                                 "RETURNING content_id", (ttl_days, content_id)).fetchone()
    if row is None:
        raise LookupError("no such section")
    return content.get_section(content_id)


# ---------- building the plan ----------

def planned_pages() -> list[dict]:
    """Every page of the selected sections, less the unticked ones, each URL once."""
    ensure_schema()
    chosen = {s["source_id"]: s for s in sources.list_sources() if s["status"] == "selected"}
    out, seen = [], set()
    for section in content.list_sections(with_urls=True):
        if section["status"] != "selected" or section["source_id"] not in chosen:
            continue
        excluded = set(section["excluded_urls"] or [])
        ttl = section_ttl(section)
        for page in section["urls"]:
            url = page["url"]
            if url in excluded or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "source_id": section["source_id"], "content_id": section["content_id"],
                          "section": section["name"], "areas": section["areas"] or [],
                          "kind": "pdf" if scraping.is_pdf(url) else "html", "ttl_days": ttl})
    return out


def _fingerprint(pages: list[dict]) -> str:
    keys = [(p["url"], p["content_id"], p["ttl_days"]) for p in pages]
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()


def _totals(pages: list[dict]) -> dict:
    return {"pages": len(pages), "pdfs": sum(p["kind"] == "pdf" for p in pages),
            "sites": len({p["source_id"] for p in pages}), "sections": len({p["content_id"] for p in pages}),
            "minutes": round(len(pages) * SECONDS_PER_PAGE / 60)}


def latest(status: str | None = None) -> dict | None:
    ensure_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM kb_ingestion_plans WHERE (%(s)s::text IS NULL OR status = %(s)s) "
            "ORDER BY version DESC LIMIT 1", {"s": status}).fetchone()
    return _iso(row) if row else None


def pages(version: int, status: str | None = None) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM kb_ingestion_plan_pages WHERE version = %(v)s AND (%(s)s::text IS NULL OR status = %(s)s) "
            "ORDER BY position", {"v": version, "s": status}).fetchall()
    return [_iso(r) for r in rows]


def draft() -> dict:
    """The draft plan for the current selection: the existing draft when nothing
    changed, else a new version (an older draft is dropped)."""
    planned = planned_pages()
    fingerprint = _fingerprint(planned)
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616722)")
        row = connection.execute("SELECT * FROM kb_ingestion_plans WHERE status = 'draft' ORDER BY version DESC "
                                 "LIMIT 1").fetchone()
        if row and row["fingerprint"] == fingerprint:
            return _iso(row)
        connection.execute("DELETE FROM kb_ingestion_plans WHERE status = 'draft'")
        version = connection.execute("SELECT nextval('kb_ingestion_plan_versions') AS v").fetchone()["v"]
        row = connection.execute("""
            INSERT INTO kb_ingestion_plans (version, status, fingerprint, page_limit, totals)
            VALUES (%s, 'draft', %s, %s, %s) RETURNING *""",
            (version, fingerprint, PLAN_PAGE_LIMIT, Jsonb(_totals(planned)))).fetchone()
        with connection.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO kb_ingestion_plan_pages (version, position, url, source_id, content_id, section, areas,
                                                     kind, ttl_days)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(version, i, p["url"], p["source_id"], p["content_id"], p["section"], Jsonb(p["areas"]), p["kind"],
                  p["ttl_days"]) for i, p in enumerate(planned)])
    return _iso(row)


def _stored_chunks(urls: list[str]) -> int | None:
    """How many chunks the knowledge base holds for these pages (None if unknown)."""
    if not urls:
        return 0
    try:
        client = storage.get_client()
        with client.connection() as connection:
            return connection.execute(f"SELECT count(*) AS n FROM {client.table} WHERE source_url = ANY(%s)",
                                      (urls,)).fetchone()["n"]
    except Exception:
        return None


def differences(version: int) -> dict | None:
    """What this version adds and removes against the approved plan (None: there is none)."""
    approved = latest("approved")
    if not approved or approved["version"] == version:
        return None
    before = {p["url"] for p in pages(approved["version"])}
    after = {p["url"] for p in pages(version)}
    removed = sorted(before - after)
    return {"against": approved["version"], "added": len(after - before), "removed": len(removed),
            "removed_urls": removed[:50], "removed_chunks": _stored_chunks(removed)}


def review() -> dict:
    """What the Build step shows before approval: the draft by site and section, totals,
    differences from the approved plan, areas with no content, whether it can be approved."""
    plan = draft()
    listed = pages(plan["version"])
    names = {s["source_id"]: s["name"] for s in sources.list_sources()}
    sections = {c["content_id"]: c for c in content.list_sections(with_urls=True)}
    groups: dict[int, dict] = {}
    for p in listed:
        g = groups.setdefault(p["content_id"], {
            "content_id": p["content_id"], "site": names.get(p["source_id"], "?"), "section": p["section"],
            "pages": 0, "pdfs": 0, "ttl_days": p["ttl_days"], "areas": p["areas"],
            "ttl_suggested": suggested_ttl(sections[p["content_id"]]) if p["content_id"] in sections else None})
        g["pages"] += 1
        g["pdfs"] += p["kind"] == "pdf"
    covered = {a for p in listed for a in p["areas"]}
    bp = content._confirmed_blueprint()
    uncovered = [a["name"] for a in bp["knowledge_areas"]
                 if a["knowledge_class"] in sources.SOURCED_CLASSES and a["key"] not in covered]
    problem = ("choose at least one section" if not listed else
               f"the plan has {len(listed)} pages, more than the limit of {plan['page_limit']}: choose fewer"
               if len(listed) > plan["page_limit"] else None)
    return {"plan": plan, "sections": list(groups.values()), "uncovered": uncovered,
            "differences": differences(plan["version"]), "problem": problem}


# ---------- approval ----------

def approve(version: int, user_id: str | None) -> dict:
    """Build RAG: the draft becomes the approved plan (if it is still the
    current selection's), recorded with who and when; setup moves on."""
    if knowledge_system.status()["state"] != state.AWAITING_CONTENT_SELECTION:
        raise state.TransitionNotAllowed("a plan can be approved once the content is chosen")
    current = review()
    plan = current["plan"]
    if plan["version"] != version:
        raise ValueError("the selection changed since this plan was shown: review it again")
    if current["problem"]:
        raise ValueError(current["problem"])
    with _connect() as connection:
        connection.execute("UPDATE kb_ingestion_plans SET status = 'superseded' WHERE status = 'approved'")
        row = connection.execute("""
            UPDATE kb_ingestion_plans SET status = 'approved', approved_by = %s, approved_at = now()
            WHERE version = %s AND status = 'draft' RETURNING *""", (user_id, version)).fetchone()
    if row is None:
        raise ValueError("that plan is no longer a draft")
    state.transition(state.INGESTION_APPROVED, user_id, expected=state.AWAITING_CONTENT_SELECTION,
                     plan_version=version, pages=plan["totals"]["pages"], differences=current["differences"])
    return _iso(row)


def set_build(version: int, summary: dict) -> None:
    """The check after the build (build.py), kept with the plan."""
    with _connect() as connection:
        connection.execute("UPDATE kb_ingestion_plans SET build = %s WHERE version = %s", (Jsonb(summary), version))


def back_to_content(user_id: str | None = None) -> None:
    state.transition(state.AWAITING_CONTENT_SELECTION, user_id, reason="change the content")


def view() -> dict:
    """The approved plan and its pages' progress (the build itself is #22 phase 3)."""
    plan = latest("approved")
    if not plan:
        return {"plan": None, "progress": {}}
    with _connect() as connection:
        rows = connection.execute("SELECT status, count(*) AS n, sum(chunks) AS chunks FROM kb_ingestion_plan_pages "
                                  "WHERE version = %s GROUP BY status", (plan["version"],)).fetchall()
    return {"plan": plan, "progress": {r["status"]: r["n"] for r in rows},
            "chunks": sum(r["chunks"] or 0 for r in rows)}

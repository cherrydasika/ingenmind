"""Live data sources in setup (R41): the blueprint's live knowledge areas
(departures, flight status: answered by live tools, not pages) get their own
data-source research during the Sources step, beside the site discovery, and
each records the live tool it needs.

    app_live_sources  area_key, area_name, status (researching | ready |
                      failed), report_id (its data-source report,
                      source_profiles.py), recommendation (the judge's, as
                      research.judge_sources leaves it), tool (the tool the
                      area needs: its name, whether it exists here, the
                      method), existing (tools here that may already answer
                      it), error, updated_at

Per area: two web searches, the best few distinct sites fetched (one read-only
GET each), profiled and judged as in research (research.profile_sources,
judge_sources), and saved as a data-source report: the Knowledge base page
lists it, and an API tool can be drafted from it (R40) or imported from its
OpenAPI spec (R39). Nothing is built or registered.
"""

import threading
from datetime import datetime
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import api_tools
import research
import source_profiles
from common import config

from . import sources

MAX_AREAS = 4
MAX_CANDIDATES = 4
RESULTS_PER_SEARCH = 5

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
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_live_sources (
                    area_key text PRIMARY KEY,
                    area_name text NOT NULL,
                    status text NOT NULL,
                    report_id text,
                    recommendation jsonb,
                    tool jsonb,
                    existing jsonb NOT NULL DEFAULT '[]',
                    error text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def live_areas(bp: dict) -> list[dict]:
    return [a for a in bp["knowledge_areas"] if a["knowledge_class"] not in sources.SOURCED_CLASSES][:MAX_AREAS]


def existing_tools(area: dict) -> list[str]:
    """Tools here whose name or title shares a word with the area (weather → get_weather)."""
    words = {w for w in f"{area['name']} {area.get('description', '')}".lower().replace("_", " ").split() if len(w) > 3}
    return [name for name, tool in api_tools.all_tools().items()
            if words & {w for w in f"{name} {tool.title}".lower().replace("_", " ").split() if len(w) > 3}]


def _save(area: dict, **fields) -> None:
    ensure_schema()
    columns = {"status": fields.get("status"), "report_id": fields.get("report_id"),
               "recommendation": Jsonb(fields["recommendation"]) if fields.get("recommendation") else None,
               "tool": Jsonb(fields["tool"]) if fields.get("tool") else None,
               "existing": Jsonb(fields.get("existing") or []), "error": fields.get("error")}
    with _connect() as connection:
        connection.execute("""
            INSERT INTO app_live_sources (area_key, area_name, status, report_id, recommendation, tool, existing, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (area_key) DO UPDATE SET area_name = EXCLUDED.area_name, status = EXCLUDED.status,
                report_id = EXCLUDED.report_id, recommendation = EXCLUDED.recommendation, tool = EXCLUDED.tool,
                existing = EXCLUDED.existing, error = EXCLUDED.error, updated_at = now()""",
                           (area["key"], area["name"], *columns.values()))


def research_area(area: dict, bp: dict, search=research.web_search, fetch=research.fetch_source,
                  profiler=research.profile_with_llm, judge=research.judge_with_llm) -> dict:
    """Find, profile and judge the data sources for one live area; saved as a data-source report."""
    flow = bp.get("flow") or {}
    country = (flow.get("search_country") or "").strip()
    brief = flow.get("brief") or bp.get("purpose") or ""
    task = f"{area['name']}: {area.get('description') or ''}".strip(": ")
    hosts, candidates, fetched = set(), [], {}
    for query in (f"{area['name']} API {country}", f"{area['name']} open data feed {country}"):
        for result in search(query.strip(), RESULTS_PER_SEARCH, country=country or None):
            host = urlparse(result.get("url") or "").netloc.lower().removeprefix("www.")
            if not host or host in hosts or len(candidates) >= MAX_CANDIDATES:
                continue
            page = fetch(result["url"])
            fetched[result["url"]] = page
            if page.get("error") or not page.get("text"):
                continue
            hosts.add(host)
            candidates.append({"name": (result.get("title") or host)[:120], "provider": host, "urls": [result["url"]]})
    profiled = research.profile_sources(task, candidates, fetched, brief=brief, profiler=profiler, fetch=fetch)
    judged = research.judge_sources(task, profiled["profiles"], brief=brief, judge=judge)
    recommendation = judged["recommendation"]
    report_id = source_profiles.save_report(task, [f"Live data for: {area['name']}"], brief, profiled["profiles"],
                                            recommendation)
    sketch = recommendation.get("integration") or {}
    name = sketch.get("tool_name") or ""
    tool = {"name": name, "exists": bool(name) and name in api_tools.all_tools(), "method": recommendation.get("method"),
            "endpoint": sketch.get("endpoint") or ""}
    return {"report_id": report_id, "recommendation": recommendation, "tool": tool}


def research_all(bp: dict, **ports) -> list[dict]:
    """Every live area (at most MAX_AREAS), one after another; a failure is recorded for that area only."""
    out = []
    for area in live_areas(bp):
        existing = existing_tools(area)
        _save(area, status="researching", existing=existing)
        try:
            found = research_area(area, bp, **ports)
            _save(area, status="ready", existing=existing, **found)
            out.append({"area": area["key"], "status": "ready", **found})
        except Exception as error:
            _save(area, status="failed", existing=existing, error=f"{type(error).__name__}: {error}"[:500])
            out.append({"area": area["key"], "status": "failed"})
    return out


def view(bp: dict) -> list[dict]:
    """Per live area: its research and the tool it needs, for the Sources step."""
    ensure_schema()
    with _connect() as connection:
        rows = {r["area_key"]: r for r in connection.execute("SELECT * FROM app_live_sources").fetchall()}
    out = []
    for area in live_areas(bp):
        row = rows.get(area["key"]) or {}
        recommendation = row.get("recommendation") or {}
        out.append({"key": area["key"], "name": area["name"], "status": row.get("status") or "not researched",
                    "report_id": row.get("report_id"), "recommended": recommendation.get("recommended"),
                    "method": recommendation.get("method"), "rationale": recommendation.get("rationale"),
                    "tool": row.get("tool"), "existing": row.get("existing") or [], "error": row.get("error"),
                    "updated_at": row["updated_at"].isoformat() if isinstance(row.get("updated_at"), datetime) else None})
    return out

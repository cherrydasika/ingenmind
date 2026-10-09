"""Content selection (epic #27, #21: approval 2): each chosen site's sections
mapped to the blueprint, and the user's choice of exactly what goes in.

For every source chosen in #20, in the background (site_map.analyse_site
reads it politely), then one forced-tool call maps its sections to the
blueprint's knowledge areas, recommends the relevant ones and flags low-value
content. Rules in code: flagged sections and those under MIN_RELEVANCE are not
recommended; areas outside the blueprint are dropped. Recommended sections
are suggestions: the user ticks each, and may untick single pages in it.

    kb_site_analyses  one row per chosen source: status (analysing | ready |
                      blocked | failed), how it was read, robots.txt, requests
    kb_content        one row per section: its pages, the mapping, the user's
                      choice (candidate | selected | removed) and the pages
                      they excluded

When every chosen site is analysed, setup moves from ANALYSING_SOURCES to
AWAITING_CONTENT_SELECTION. Analysing again keeps the user's choices for the
sections it finds again. Nothing is ingested here: that is the ingestion plan
(#22).
"""

import re
import threading
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

import knowledge_system
import llm
import structured
from common import config
from tracing import observation

from . import blueprint, site_map, sources, state

FLAGS = ("terms", "privacy", "cookies", "news", "press", "careers", "corporate", "events", "archive", "search",
         "live", "login", "shop")
MIN_RELEVANCE = 0.2
LARGE_SECTION = 200            # a chosen section above this many pages is flagged on the page
STALE_MINUTES = 20
SAMPLE_PATHS = 4


class SectionAssessment(BaseModel):
    key: str = Field(description="The section's key, exactly as given")
    areas: list[str] = Field(default_factory=list, description="Keys of the knowledge areas it serves")
    relevance: float = Field(ge=0, le=1)
    recommended: bool
    flags: list[str] = Field(default_factory=list, description=f"Low-value kinds of content: {', '.join(FLAGS)}")
    reason: str = Field(description="One line on what it holds and why it fits or not")

    @field_validator("areas", "flags", mode="before")
    @classmethod
    def _list(cls, value):
        return [str(v).strip().lower() for v in structured.as_list(value) if str(v).strip()]

    @field_validator("relevance", mode="before")
    @classmethod
    def _relevance(cls, value):
        try:
            return min(max(float(value), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0


class SectionAssessments(BaseModel):
    sections: list[SectionAssessment]


MAP_PROMPT = (
    "You are the Source Analysis agent. A knowledge system is being set up from the Domain Blueprint below, and the "
    "user chose this site as a source. For each of the site's sections (named, with its page count and sample "
    "page paths) say which of the blueprint's knowledge areas it serves (their keys), its relevance (0 to 1), "
    "whether to recommend reading it into the knowledge base, one line on why, and any flags for low-value "
    f"content ({', '.join(FLAGS)}). Recommend the sections whose stable pages answer the knowledge areas; do not "
    "recommend news, press, careers, corporate reports, terms and conditions, privacy or cookie pages, search "
    "results, login or shop pages, or pages that only show live or changing information. Judge from the names and "
    "paths given; do not guess at what you cannot see. In the reason, name knowledge areas by their names, not "
    "their keys. Report through the record_section_assessments tool only."
)


def map_with_llm(bp: dict, site: dict, found: list[dict]) -> tuple[SectionAssessments, dict]:
    areas = "\n".join(f"- {a['key']}: {a['name']} — {a['description']}" for a in bp["knowledge_areas"]
                      if a["knowledge_class"] in sources.SOURCED_CLASSES)
    blocks = []
    for s in found:
        paths = ", ".join(urlparse(u).path for u in s["sample"][:SAMPLE_PATHS])
        blocks.append(f"SECTION {s['key']}: {s['name']} — {s['url_count']} pages"
                      + (f", {s['pdf_count']} PDFs" if s["pdf_count"] else "") + f"; e.g. {paths}")
    prompt = (f"DOMAIN BLUEPRINT: {bp['name']} — {bp['purpose']}\nKnowledge areas:\n{areas}\n\n"
              f"SITE: {site['name']} ({site['host']})\n\n" + "\n".join(blocks))
    with observation(as_type="generation", name="section_mapper", model=llm.MODEL,
                     input=[{"role": "system", "content": MAP_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=MAP_PROMPT, name="record_section_assessments",
                              description="Record one assessment per section.",
                              schema=SectionAssessments.model_json_schema(), max_tokens=6000, temperature=0)
        raw = reply.tool_input if isinstance(reply.tool_input, dict) else {}
        items = [i for i in structured.as_list(raw.get("sections")) if isinstance(i, dict)]
        result = SectionAssessments(sections=[SectionAssessment.model_validate(i) for i in items])
        gen.update(output=result.model_dump(), usage_details=reply.usage)
    return result, reply.usage


def apply_rules(bp: dict, found: list[dict], assessed: list[SectionAssessment]) -> list[dict]:
    """The sections with their mapping, after the rules in the module docstring."""
    named = {a["key"]: a["name"] for a in bp["knowledge_areas"]}
    valid = {a["key"] for a in bp["knowledge_areas"] if a["knowledge_class"] in sources.SOURCED_CLASSES}
    by_key = {a.key: a for a in assessed}

    def readable(reason: str) -> str:
        """Area keys the model still wrote ("serves delay_compensation") become their names."""
        for key in sorted(named, key=len, reverse=True):
            reason = re.sub(rf"\b{re.escape(key)}\b", named[key], reason)
        return reason
    out = []
    for s in found:
        a = by_key.get(s["key"])
        flags = [f for f in (a.flags if a else []) if f in FLAGS]
        relevance = a.relevance if a else 0.0
        recommended = bool(a and a.recommended) and not flags and relevance >= MIN_RELEVANCE
        out.append({**s, "areas": [k for k in (a.areas if a else []) if k in valid], "relevance": round(relevance, 2),
                    "recommended": recommended, "flags": flags,
                    "reason": readable(a.reason if a else "") or "Not assessed"})
    out.sort(key=lambda s: (not s["recommended"], s["key"] == site_map.OTHER, -s["relevance"], -s["url_count"]))
    return out


# ---------- storage ----------

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
                CREATE TABLE IF NOT EXISTS kb_site_analyses (
                    source_id bigint PRIMARY KEY,
                    status text NOT NULL,
                    how text,
                    robots jsonb NOT NULL DEFAULT '{}',
                    requests int NOT NULL DEFAULT 0,
                    page_count int NOT NULL DEFAULT 0,
                    error text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb_content (
                    content_id bigserial PRIMARY KEY,
                    source_id bigint NOT NULL,
                    key text NOT NULL,
                    name text NOT NULL,
                    path_prefix text,
                    urls jsonb NOT NULL DEFAULT '[]',
                    url_count int NOT NULL DEFAULT 0,
                    pdf_count int NOT NULL DEFAULT 0,
                    lastmod text,
                    areas jsonb NOT NULL DEFAULT '[]',
                    relevance double precision NOT NULL DEFAULT 0,
                    recommended boolean NOT NULL DEFAULT false,
                    flags jsonb NOT NULL DEFAULT '[]',
                    reason text NOT NULL DEFAULT '',
                    status text NOT NULL DEFAULT 'candidate',
                    excluded_urls jsonb NOT NULL DEFAULT '[]',
                    position int NOT NULL DEFAULT 0,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (source_id, key)
                )""")
        _schema_ready = True


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def set_analysis(source_id: int, status: str, analysis: dict | None = None) -> None:
    ensure_schema()
    a = analysis or {}
    with _connect() as connection:
        connection.execute("""
            INSERT INTO kb_site_analyses (source_id, status, how, robots, requests, page_count, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET status = EXCLUDED.status, how = EXCLUDED.how,
                robots = EXCLUDED.robots, requests = EXCLUDED.requests, page_count = EXCLUDED.page_count,
                error = EXCLUDED.error, updated_at = now()""",
            (source_id, status, a.get("how"), Jsonb(a.get("robots") or {}), len(a.get("requests") or []),
             len(a.get("pages") or []), a.get("error")))


def save_sections(source_id: int, mapped: list[dict]) -> None:
    """Replace a site's sections; the user's choice for a section found again stays."""
    ensure_schema()
    with _connect() as connection:
        keys = [s["key"] for s in mapped]
        connection.execute("DELETE FROM kb_content WHERE source_id = %s AND NOT (key = ANY(%s))", (source_id, keys))
        for position, s in enumerate(mapped):
            urls = [{"url": u["url"], "lastmod": u.get("lastmod")} for u in s["urls"]]
            connection.execute("""
                INSERT INTO kb_content (source_id, key, name, path_prefix, urls, url_count, pdf_count, lastmod, areas,
                                        relevance, recommended, flags, reason, position)
                VALUES (%(source_id)s, %(key)s, %(name)s, %(path_prefix)s, %(urls)s, %(url_count)s, %(pdf_count)s,
                        %(lastmod)s, %(areas)s, %(relevance)s, %(recommended)s, %(flags)s, %(reason)s, %(position)s)
                ON CONFLICT (source_id, key) DO UPDATE SET name = EXCLUDED.name, path_prefix = EXCLUDED.path_prefix,
                    urls = EXCLUDED.urls, url_count = EXCLUDED.url_count, pdf_count = EXCLUDED.pdf_count,
                    lastmod = EXCLUDED.lastmod, areas = EXCLUDED.areas, relevance = EXCLUDED.relevance,
                    recommended = EXCLUDED.recommended, flags = EXCLUDED.flags, reason = EXCLUDED.reason,
                    position = EXCLUDED.position, updated_at = now()""",
                {**s, "source_id": source_id, "urls": Jsonb(urls), "areas": Jsonb(s["areas"]),
                 "flags": Jsonb(s["flags"]), "position": position})


def analyses() -> dict[int, dict]:
    ensure_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE kb_site_analyses SET status = 'failed', error = 'interrupted: please analyse again' "
            "WHERE status = 'analysing' AND updated_at < now() - make_interval(mins => %s)", (STALE_MINUTES,))
        return {r["source_id"]: _iso(r) for r in connection.execute("SELECT * FROM kb_site_analyses").fetchall()}


def list_sections(with_urls: bool = False) -> list[dict]:
    """Every section, in its site's order; without the page lists unless asked."""
    ensure_schema()
    columns = "*" if with_urls else ("content_id, source_id, key, name, path_prefix, url_count, pdf_count, lastmod, "
                                     "areas, relevance, recommended, flags, reason, status, excluded_urls, "
                                     "position, updated_at, jsonb_path_query_array(urls, '$[0 to 4].url') AS sample")
    with _connect() as connection:
        rows = connection.execute(f"SELECT {columns} FROM kb_content ORDER BY source_id, position").fetchall()
    return [_iso(r) for r in rows]


def get_section(content_id: int) -> dict:
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM kb_content WHERE content_id = %s", (content_id,)).fetchone()
    if row is None:
        raise LookupError("no such section")
    return _iso(row)


def set_status(content_id: int, status: str) -> dict:
    if status not in sources.STATUSES:
        raise ValueError(f"status must be one of {', '.join(sources.STATUSES)}")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("UPDATE kb_content SET status = %s, updated_at = now() WHERE content_id = %s "
                                 "RETURNING content_id", (status, content_id)).fetchone()
    if row is None:
        raise LookupError("no such section")
    return get_section(content_id)


def set_excluded(content_id: int, excluded: list[str]) -> dict:
    """The section's pages the user unticked (only its own pages are kept)."""
    section = get_section(content_id)
    own = {u["url"] for u in section["urls"]}
    keep = [u for u in dict.fromkeys(excluded or []) if u in own]
    with _connect() as connection:
        connection.execute("UPDATE kb_content SET excluded_urls = %s, updated_at = now() WHERE content_id = %s",
                           (Jsonb(keep), content_id))
    return get_section(content_id)


def chosen_pages(section: dict) -> int:
    return section["url_count"] - len(section["excluded_urls"] or [])


# ---------- the run ----------

def _start_thread(work) -> None:
    """Background work (tests replace this to run inline)."""
    threading.Thread(target=work, name="content-analysis", daemon=True).start()


def _confirmed_blueprint() -> dict:
    version = knowledge_system.status()["blueprint_version"]
    row = blueprint.get(version) if version else None
    if not row or not row["confirmed_at"]:
        raise ValueError("there is no confirmed blueprint")
    return row["blueprint"]


def analyse_one(bp: dict, site: dict, analyse: Callable = site_map.analyse_site, mapper: Callable = map_with_llm) -> None:
    """Read one chosen site, map its sections, store them."""
    set_analysis(site["source_id"], "analysing")
    with observation(as_type="span", name="site_analysis", input={"site": site["host"]}) as span:
        try:
            found = analyse(site["base_url"])
            if found["status"] == "ready" and found["sections"]:
                assessed, _ = mapper(bp, site, found["sections"])
                save_sections(site["source_id"], apply_rules(bp, found["sections"], assessed.sections))
            elif found["status"] == "ready":
                found = {**found, "status": "failed", "error": "no pages found on the site"}
        except Exception as error:   # the user sees why and can analyse again
            found = {"status": "failed", "error": f"{type(error).__name__}: {error}"[:500]}
        set_analysis(site["source_id"], found["status"], found)
        span.update(output={"status": found["status"], "sections": len(found.get("sections") or []),
                            "requests": len(found.get("requests") or [])})


def start(analyse: Callable = site_map.analyse_site, mapper: Callable = map_with_llm, only: int | None = None) -> None:
    """Analyse every chosen site (or `only` one) in the background; when none is
    left analysing, setup moves on to choosing content."""
    current = knowledge_system.status()["state"]
    if current not in (state.ANALYSING_SOURCES, state.AWAITING_CONTENT_SELECTION):
        raise state.TransitionNotAllowed("the content step is not open now")
    bp = _confirmed_blueprint()
    chosen = [s for s in sources.list_sources() if s["status"] == "selected"
              and (only is None or s["source_id"] == only)]
    if not chosen:
        raise ValueError("no chosen source to analyse")
    if any(a["status"] == "analysing" for a in analyses().values()):
        raise RuntimeError("sites are already being analysed")
    for site in chosen:
        set_analysis(site["source_id"], "analysing")

    def work():
        for site in chosen:
            analyse_one(bp, site, analyse, mapper)
        if knowledge_system.status()["state"] == state.ANALYSING_SOURCES:
            state.transition(state.AWAITING_CONTENT_SELECTION, expected=state.ANALYSING_SOURCES,
                             sites=[s["host"] for s in chosen])
    _start_thread(work)


def _choosing() -> None:
    if knowledge_system.status()["state"] != state.AWAITING_CONTENT_SELECTION:
        raise state.TransitionNotAllowed("content can be chosen once the sites are analysed")


def choose(content_id: int, status: str) -> dict:
    _choosing()
    return set_status(content_id, status)


def exclude(content_id: int, excluded: list[str]) -> dict:
    _choosing()
    return set_excluded(content_id, excluded)


def back_to_sources(user_id: str | None = None) -> None:
    state.transition(state.AWAITING_SOURCE_SELECTION, user_id, reason="change the sources")


def view() -> dict:
    """What the Content step shows: each chosen site's analysis and sections, totals, coverage."""
    try:
        bp = _confirmed_blueprint()
    except ValueError:
        return {"sites": [], "coverage": [], "pages_chosen": 0}
    done = analyses()
    sections = list_sections()
    chosen = [s for s in sources.list_sources() if s["status"] == "selected"]
    sites = [{**{k: s[k] for k in ("source_id", "name", "host", "base_url")},
              "analysis": done.get(s["source_id"]),
              "sections": [{**c, "large": c["url_count"] > LARGE_SECTION} for c in sections
                           if c["source_id"] == s["source_id"] and c["status"] != "removed"]}
             for s in chosen]
    selected = [c for c in sections if c["status"] == "selected"]
    names = {s["source_id"]: s["name"] for s in chosen}
    coverage = [{"key": a["key"], "name": a["name"],
                 "sections": [f"{names.get(c['source_id'], '?')}: {c['name']}" for c in selected
                              if a["key"] in (c["areas"] or [])]}
                for a in bp["knowledge_areas"] if a["knowledge_class"] in sources.SOURCED_CLASSES]
    return {"sites": sites, "coverage": coverage, "pages_chosen": sum(chosen_pages(c) for c in selected),
            "large_section": LARGE_SECTION}

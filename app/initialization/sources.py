"""Source discovery and the source registry (epic #27, #20: approval 1).

From the confirmed Domain Blueprint the setup agent finds the sites that
could feed the knowledge base; the user then chooses which to trust.
Nothing is scraped: sites are judged from the blueprint and search results.

1. seeds    the blueprint's organisations that have a website, with the
            areas whose evidence pages are on their site
2. search   one search per static or structured knowledge area, naming the
            domain and the region (at most MAX_SEARCHES); results grouped by
            site
3. assess   one forced-tool call: per site its name, kind, authority,
            relevance, the areas it covers, a reason, whether to recommend it
4. rules    in code: official domains and the blueprint's regulators,
            government bodies and operators are high authority, and those
            organisations are recommended (the blueprint found them on fetched
            pages); forums and social media are never recommended; with
            authoritative_only, low authority is not recommended; a searched
            site below MIN_RELEVANCE is dropped; at most MAX_SITES kept

    kb_sources  one row per site (host): what discovery or the user said
                about it, and its status (candidate | selected | removed)
"""

import re
import threading
from datetime import datetime
from typing import Callable, Literal
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

import llm
import research
import structured
from common import config
from tracing import observation

MAX_SEARCHES = 8
RESULTS_PER_SEARCH = 5
MAX_SITES = 15
MIN_RELEVANCE = 0.15       # a searched site below this is noise (another domain's pages)
SOURCED_CLASSES = ("STATIC_KNOWLEDGE", "STRUCTURED_DATA")   # live areas are answered by tools
HIGH_ROLES = ("regulator", "government", "operator")
AUTHORITY_RANK = {"high": 2, "medium": 1, "low": 0}
# Never recommended: forums, social media, question-and-answer and review sites.
NOT_SOURCES = re.compile(r"(^|\.)(reddit|quora|facebook|twitter|x|instagram|tiktok|youtube|linkedin|pinterest|"
                         r"medium|stackexchange|stackoverflow|tripadvisor|trustpilot)\.com$|forum", re.IGNORECASE)
STATUSES = ("candidate", "selected", "removed")


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _root(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme or 'https'}://{parts.hostname}" if parts.hostname else url


class SiteAssessment(BaseModel):
    host: str = Field(description="The site's host, exactly as given")
    name: str = Field(description="The site's or organisation's name")
    kind: Literal["website", "documents", "api"] = "website"
    authority: Literal["high", "medium", "low"]
    relevance: float = Field(ge=0, le=1, description="How much of the knowledge areas it can cover")
    areas: list[str] = Field(default_factory=list, description="Keys of the knowledge areas it covers")
    reason: str = Field(description="One line: why it is worth using (or not)")
    recommended: bool

    @field_validator("authority", mode="before")
    @classmethod
    def _authority(cls, value):
        value = str(value or "").strip().lower()
        return value if value in AUTHORITY_RANK else "low"

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value):
        value = str(value or "").strip().lower()
        return value if value in ("website", "documents", "api") else "website"

    @field_validator("relevance", mode="before")
    @classmethod
    def _relevance(cls, value):
        try:
            return min(max(float(value), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0

    @field_validator("areas", mode="before")
    @classmethod
    def _areas(cls, value):
        return [str(v).strip() for v in structured.as_list(value) if str(v).strip()]


class SiteAssessments(BaseModel):
    sites: list[SiteAssessment]


ASSESS_PROMPT = (
    "You are the Source Discovery agent. A knowledge system is being set up from the Domain Blueprint below. Judge "
    "each candidate site — from what the blueprint and the search results say, not from outside knowledge of the "
    "site — as a source of its static knowledge: its name; its kind (website, documents for a library of PDFs or "
    "publications, api); its authority (high: government, regulators, the operators or the official body for the "
    "domain; medium: established industry or consumer bodies and well-known publishers; low: resellers, blogs, "
    "aggregators, forums); its relevance to the knowledge areas (0 to 1); the keys of the areas it covers; one "
    "line on why; and whether you recommend it. Recommend the authoritative sites that between them cover the "
    "areas; prefer an organisation's own site to a site that repeats it. Report through the "
    "record_site_assessments tool only."
)


def assess_with_llm(summary: str, candidates: list[dict]) -> tuple[SiteAssessments, dict]:
    blocks = []
    for c in candidates:
        lines = [f"SITE {c['host']} ({c['name'] or 'name unknown'}; "
                 f"{'named in the blueprint as ' + c['role'] if c.get('role') else 'found by search'})"]
        lines += [f"- [{r['area']}] {r['title'] or ''}: {(r.get('snippet') or '')[:220]}" for r in c["results"][:4]]
        blocks.append("\n".join(lines))
    prompt = summary + "\n\nCANDIDATE SITES:\n\n" + "\n\n".join(blocks)
    with observation(as_type="generation", name="source_assessor", model=llm.MODEL,
                     input=[{"role": "system", "content": ASSESS_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=ASSESS_PROMPT, name="record_site_assessments",
                              description="Record one assessment per site.", schema=SiteAssessments.model_json_schema(),
                              max_tokens=4000, temperature=0)
        raw = reply.tool_input if isinstance(reply.tool_input, dict) else {}
        items = [i for i in structured.as_list(raw.get("sites")) if isinstance(i, dict)]
        result = SiteAssessments(sites=[SiteAssessment.model_validate(i) for i in items])
        gen.update(output=result.model_dump(), usage_details=reply.usage)
    return result, reply.usage


def _summary(bp: dict) -> str:
    areas = "\n".join(f"- {a['key']}: {a['name']} — {a['description']}" for a in bp["knowledge_areas"]
                      if a["knowledge_class"] in SOURCED_CLASSES)
    req = bp.get("source_requirements") or {}
    return (f"DOMAIN BLUEPRINT: {bp['name']} — {bp['purpose']}\nRegions: {', '.join(bp['regions'])}; for "
            f"{', '.join(bp['audience'])}\nKnowledge areas to source:\n{areas}\nSource requirements: "
            f"authoritative only: {req.get('authoritative_only', True)}; government: {req.get('government', True)}; "
            f"operators: {req.get('operators', True)}; {req.get('other') or ''}")


def discover(bp: dict, search: Callable = research.web_search, assessor: Callable = assess_with_llm) -> tuple[list[dict], dict]:
    """(sites to store, research record). bp: a DomainBlueprint as a dict."""
    with observation(as_type="span", name="source_discovery", input={"blueprint": bp.get("name")}) as span:
        candidates: dict[str, dict] = {}
        roles = {}
        for org in bp.get("organisations") or []:
            if org.get("website") and (host := host_of(org["website"])):
                candidates.setdefault(host, {"host": host, "name": org["name"], "url": _root(org["website"]),
                                             "origin": "blueprint", "role": org["role"], "results": []})
                roles[host] = org["role"]
        # What the blueprint read on a seed's site counts as its evidence.
        for area in bp["knowledge_areas"]:
            if area["knowledge_class"] not in SOURCED_CLASSES:
                continue
            for url in area.get("evidence_urls") or []:
                if (host := host_of(url)) in candidates:
                    candidates[host]["results"].append({"area": area["key"], "url": url,
                                                        "title": f"Blueprint evidence for {area['name']}"})
        region = " ".join((bp.get("regions") or [])[:2])
        domain = ((bp.get("flow") or {}).get("domain") or bp.get("name") or "").strip()
        country = ((bp.get("flow") or {}).get("search_country") or "").strip() or None
        areas = [a for a in bp["knowledge_areas"] if a["knowledge_class"] in SOURCED_CLASSES][:MAX_SEARCHES]
        searches = []
        for area in areas:
            query = f"{domain} {area['name']} {region} official"
            try:
                results = search(query, max_results=RESULTS_PER_SEARCH, country=country)
            except Exception as error:
                searches.append({"query": query, "area": area["key"], "error": f"{type(error).__name__}: {error}"})
                continue
            searches.append({"query": query, "area": area["key"], "results": [r.get("url") for r in results]})
            for r in results:
                if not (host := host_of(r.get("url") or "")):
                    continue
                site = candidates.setdefault(host, {"host": host, "name": "", "url": _root(r["url"]),
                                                    "origin": "search", "role": None, "results": []})
                site["results"].append({"area": area["key"], "url": r["url"], "title": r.get("title"),
                                        "snippet": r.get("snippet")})
        assessed, _ = assessor(_summary(bp), list(candidates.values())) if candidates else (SiteAssessments(sites=[]), {})
        sites = _apply_rules(bp, candidates, {a.host.lower().removeprefix("www."): a for a in assessed.sites}, roles)
        span.update(output={"candidates": len(candidates), "kept": len(sites),
                            "recommended": sum(s["recommended"] for s in sites)})
    return sites, {"queries": [s["query"] for s in searches], "searches": searches}


def _apply_rules(bp: dict, candidates: dict, assessed: dict, roles: dict) -> list[dict]:
    valid_areas = {a["key"] for a in bp["knowledge_areas"] if a["knowledge_class"] in SOURCED_CLASSES}
    authoritative_only = (bp.get("source_requirements") or {}).get("authoritative_only", True)
    sites = []
    for host, c in candidates.items():
        a = assessed.get(host)
        if a is None and c["origin"] != "blueprint":
            continue    # the assessor left it out
        authority = a.authority if a else "medium"
        if research.OFFICIAL_DOMAINS.search(host) or roles.get(host) in HIGH_ROLES:
            authority = "high"
        areas = [k for k in (a.areas if a else []) if k in valid_areas] or sorted({r["area"] for r in c["results"]})
        relevance = a.relevance if a else 0.5
        if c["origin"] == "search" and relevance < MIN_RELEVANCE:
            continue    # another domain's pages (an airline for a rail system)
        recommended = bool(a.recommended) if a else False
        if roles.get(host) in HIGH_ROLES:
            recommended = True   # the blueprint found it on fetched pages
        if NOT_SOURCES.search(host) or (authoritative_only and authority == "low"):
            recommended = False
        sites.append({"host": host, "name": (a.name if a else "") or c["name"] or host, "base_url": c["url"],
                      "kind": a.kind if a else "website", "origin": c["origin"], "authority": authority,
                      "relevance": round(relevance, 2), "areas": areas,
                      "reason": (a.reason if a else "") or f"Named in the blueprint ({c.get('role') or 'organisation'})",
                      "recommended": recommended,
                      "evidence": [{k: r.get(k) for k in ("area", "url", "title")} for r in c["results"][:6]]})
    sites.sort(key=lambda s: (not s["recommended"], -AUTHORITY_RANK[s["authority"]], -s["relevance"], s["host"]))
    return sites[:MAX_SITES]


# ---------- the registry ----------

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
                CREATE TABLE IF NOT EXISTS kb_sources (
                    source_id bigserial PRIMARY KEY,
                    host text NOT NULL UNIQUE,
                    name text NOT NULL,
                    base_url text NOT NULL,
                    kind text NOT NULL DEFAULT 'website',
                    origin text NOT NULL,
                    authority text NOT NULL,
                    relevance double precision NOT NULL DEFAULT 0,
                    areas jsonb NOT NULL DEFAULT '[]',
                    reason text NOT NULL DEFAULT '',
                    evidence jsonb NOT NULL DEFAULT '[]',
                    recommended boolean NOT NULL DEFAULT false,
                    status text NOT NULL DEFAULT 'candidate',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def _view(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def save_discovered(sites: list[dict]) -> None:
    """Replace what an earlier discovery found; the user's choices and own sites stay."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("DELETE FROM kb_sources WHERE origin <> 'user' AND status = 'candidate'")
        for s in sites:
            connection.execute("""
                INSERT INTO kb_sources (host, name, base_url, kind, origin, authority, relevance, areas, reason,
                                        evidence, recommended)
                VALUES (%(host)s, %(name)s, %(base_url)s, %(kind)s, %(origin)s, %(authority)s, %(relevance)s,
                        %(areas)s, %(reason)s, %(evidence)s, %(recommended)s)
                ON CONFLICT (host) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                    authority = EXCLUDED.authority, relevance = EXCLUDED.relevance, areas = EXCLUDED.areas,
                    reason = EXCLUDED.reason, evidence = EXCLUDED.evidence, recommended = EXCLUDED.recommended,
                    updated_at = now()""",
                {**s, "areas": Jsonb(s["areas"]), "evidence": Jsonb(s["evidence"])})


def list_sources(include_removed: bool = False) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM kb_sources WHERE %s OR status <> 'removed' ORDER BY "
                                  "recommended DESC, CASE authority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                                  "relevance DESC, host", (include_removed,)).fetchall()
    return [_view(r) for r in rows]


def add_user_site(site: dict) -> dict:
    """The user's own site, selected; a site already listed is selected instead (whatever its origin)."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO kb_sources (host, name, base_url, kind, origin, authority, relevance, areas, reason,
                                    evidence, recommended, status)
            VALUES (%(host)s, %(name)s, %(base_url)s, %(kind)s, %(origin)s, %(authority)s, %(relevance)s,
                    %(areas)s, %(reason)s, %(evidence)s, %(recommended)s, 'selected')
            ON CONFLICT (host) DO UPDATE SET status = 'selected', updated_at = now()
            RETURNING *""", {**site, "areas": Jsonb(site["areas"]), "evidence": Jsonb(site["evidence"])}).fetchone()
    return _view(row)


def set_status(source_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("UPDATE kb_sources SET status = %s, updated_at = now() WHERE source_id = %s "
                                 "RETURNING *", (status, source_id)).fetchone()
    if row is None:
        raise LookupError("no such source")
    return _view(row)


def coverage(bp: dict, sources: list[dict]) -> list[dict]:
    """Per sourced knowledge area: the selected sites covering it."""
    selected = [s for s in sources if s["status"] == "selected"]
    return [{"key": a["key"], "name": a["name"],
             "sources": [s["name"] for s in selected if a["key"] in (s["areas"] or [])]}
            for a in bp["knowledge_areas"] if a["knowledge_class"] in SOURCED_CLASSES]

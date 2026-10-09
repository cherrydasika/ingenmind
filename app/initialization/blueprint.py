"""The Domain Blueprint: what the knowledge system covers (epic #19, #11).

From the confirmed requirements (requirements.py) the setup agent researches
the domain and writes a structured blueprint, grounded in pages it fetched:

1. plan     one forced-tool call: up to MAX_QUERIES web searches, each naming
            the region, and the search country
2. search   research.web_search; the results ranked official sites first
3. fetch    research.fetch_source on the best, at most MAX_PAGES usable pages
4. write    one forced-tool call: DomainBlueprint from the requirements and
            the pages' text
5. check    in code: evidence must be fetched pages; an organisation with no
            fetched page is dropped and listed under unknowns; the flow
            settings are cut to their fields' limits

    app_domain_blueprints  one row per version: status (researching | ready |
                           failed), the blueprint, the research behind it

Everything later reads it: source discovery (#12), chunk metadata (#15), the
flow's brief and scope, which areas go to live tools, the evaluation (#16).
"""

import json
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

from .requirements import SetupRequirements

MAX_QUERIES = 6
RESULTS_PER_QUERY = 4
MAX_PAGES = 10
PAGE_CHARS = 2500          # per page, in the writing prompt
MAX_TEXT = 600             # per fact in the blueprint
KnowledgeClass = Literal["STATIC_KNOWLEDGE", "DYNAMIC_KNOWLEDGE", "STRUCTURED_DATA", "EXTERNAL_TOOL_API"]
KNOWLEDGE_CLASSES = KnowledgeClass.__args__
OrgRole = Literal["regulator", "government", "operator", "industry body", "consumer body", "other"]
# The flow settings' limits (flows/registry.py), so the blueprint fits them.
FLOW_LIMITS = {"brief": 1000, "domain": 60, "scope": 4000, "supervisor_instructions": 2000, "search_country": 40}


def _short(value) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _items(value) -> list[str]:
    return [_short(v) for v in structured.as_list(value) if _short(v)][:20]


class KnowledgeArea(BaseModel):
    key: str = Field(description="snake_case key, e.g. refunds")
    name: str
    description: str = Field(description="What it covers, in one sentence")
    knowledge_class: KnowledgeClass = Field(description=(
        "STATIC_KNOWLEDGE: stable facts a page holds (policies, rights, facilities); DYNAMIC_KNOWLEDGE: changes "
        "constantly (live departures, delays, status); STRUCTURED_DATA: tables or datasets (timetables, fares "
        "files); EXTERNAL_TOOL_API: answered by calling a live service"))
    example_questions: list[str] = Field(default_factory=list, description="Two or three questions users ask")
    evidence_urls: list[str] = Field(default_factory=list, description="Fetched pages that show this area exists")

    @field_validator("knowledge_class", mode="before")
    @classmethod
    def _class(cls, value):
        value = str(value or "").strip().upper().replace(" ", "_")
        return value if value in KNOWLEDGE_CLASSES else "STATIC_KNOWLEDGE"

    @field_validator("key", mode="before")
    @classmethod
    def _key(cls, value):
        return "_".join(str(value or "area").lower().replace("-", " ").split())[:60]

    @field_validator("name", "description", mode="before")
    @classmethod
    def _text(cls, value):
        return _short(value)

    @field_validator("example_questions", "evidence_urls", mode="before")
    @classmethod
    def _list(cls, value):
        return _items(value)


class Organisation(BaseModel):
    name: str
    role: OrgRole
    website: str | None = None
    evidence_urls: list[str] = Field(default_factory=list)

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, value):
        value = str(value or "").strip().lower()
        return value if value in OrgRole.__args__ else "other"

    @field_validator("evidence_urls", mode="before")
    @classmethod
    def _list(cls, value):
        return _items(value)


class Entity(BaseModel):
    name: str = Field(description="snake_case entity type, e.g. train_operator, station, ticket_type")
    description: str = ""


class MetadataField(BaseModel):
    field: str = Field(description="snake_case chunk metadata field, e.g. organisation, topic, content_type")
    description: str = ""
    examples: list[str] = Field(default_factory=list)

    @field_validator("examples", mode="before")
    @classmethod
    def _list(cls, value):
        return _items(value)


class SourceRequirements(BaseModel):
    authoritative_only: bool = True
    government: bool = True
    operators: bool = True
    other: str = ""


class FlowSettings(BaseModel):
    brief: str = Field(description="The assistant's main prompt: what it is for and which region a question means "
                                   "when it names none (at most 1,000 characters)")
    search_country: str = Field("", description="One country's English name for web research, e.g. united kingdom; "
                                                "empty when several or worldwide")
    domain: str = Field(description="What the assistant is for, in a word or two (at most 60 characters)")
    scope: str = Field(description="What is in scope and what is not, in plain language, for the guardrails")
    supervisor_instructions: str = Field(description="Which questions go to the knowledge base and which to live "
                                                     "tools")


class DomainBlueprint(BaseModel):
    name: str
    category: str = Field(description="e.g. transportation, healthcare, finance")
    regions: list[str]
    audience: list[str]
    purpose: str
    language: str = "English"
    knowledge_areas: list[KnowledgeArea]
    entities: list[Entity] = Field(default_factory=list)
    organisations: list[Organisation] = Field(default_factory=list)
    source_requirements: SourceRequirements = Field(default_factory=SourceRequirements)
    metadata_fields: list[MetadataField] = Field(default_factory=list)
    flow: FlowSettings
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    @field_validator("regions", "audience", "assumptions", "unknowns", mode="before")
    @classmethod
    def _list(cls, value):
        return _items(value)


# ---------- 1. plan ----------

class ResearchPlan(BaseModel):
    queries: list[str] = Field(description=f"At most {MAX_QUERIES} web searches, each naming the region")
    search_country: str = Field("", description="One country's English name, lower case (e.g. united kingdom); "
                                                "empty when several or worldwide")


PLAN_PROMPT = (
    "You plan web research for a knowledge system before its sources are chosen. From the requirements, write "
    f"at most {MAX_QUERIES} web searches that find the domain's structure: the regulators and government bodies, "
    "the main organisations (operators, providers), the rules and rights that apply, and one search for each kind "
    "of question users ask. Every search names the region. Prefer searches that find official pages. Report "
    "through the record_research_plan tool only."
)


def plan_with_llm(requirements: SetupRequirements) -> tuple[ResearchPlan, dict]:
    prompt = "REQUIREMENTS:\n" + requirements.model_dump_json(indent=1)
    with observation(as_type="generation", name="blueprint_planner", model=llm.MODEL,
                     input=[{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=PLAN_PROMPT, name="record_research_plan",
                              description="Record the searches.", schema=ResearchPlan.model_json_schema(),
                              max_tokens=600, temperature=0)
        raw = structured.normalise(reply.tool_input, lists=("queries",), strings=("search_country",))
        plan = ResearchPlan.model_validate(raw)
        gen.update(output=plan.model_dump(), usage_details=reply.usage)
    return plan, reply.usage


# ---------- 2–3. search and fetch ----------

def _official(url: str) -> bool:
    return bool(research.OFFICIAL_DOMAINS.search((urlparse(url).hostname or "").lower()))


def gather(plan: ResearchPlan, search: Callable = research.web_search, fetch: Callable = research.fetch_source,
           max_pages: int = MAX_PAGES) -> dict:
    """{"searches": [{"query", "results"}], "pages": [fetched pages, text included], "skipped": [...]}"""
    searches, candidates = [], {}
    for query in [q for q in plan.queries if q.strip()][:MAX_QUERIES]:
        try:
            results = search(query, max_results=RESULTS_PER_QUERY, country=plan.search_country or None)
        except Exception as error:
            searches.append({"query": query, "results": [], "error": f"{type(error).__name__}: {error}"})
            continue
        searches.append({"query": query, "results": [{"url": r.get("url"), "title": r.get("title")} for r in results]})
        for rank, result in enumerate(results):
            url = result.get("url")
            if url and url not in candidates:
                candidates[url] = (not _official(url), rank, -(result.get("score") or 0))
    pages, skipped = [], []
    for url in sorted(candidates, key=candidates.get):     # official sites first, then search rank and score
        if len(pages) >= max_pages:
            break
        page = fetch(url)
        if page.get("error") or (page.get("chars") or 0) < research.MIN_TEXT_CHARS:
            skipped.append({"url": url, "reason": page.get("error") or "too little text"})
            continue
        pages.append(page)
    return {"searches": searches, "pages": pages, "skipped": skipped}


# ---------- 4. write ----------

WRITE_PROMPT = (
    "You are the Domain Analyst. From the requirements and the fetched pages, write the Domain Blueprint of the "
    "knowledge system: its knowledge areas (one per kind of question, plus the areas the domain needs that the "
    "user did not name), classed by how they are answered — STATIC_KNOWLEDGE for stable facts pages hold, "
    "DYNAMIC_KNOWLEDGE or EXTERNAL_TOOL_API for what changes constantly (live departures, delays, status, prices "
    "that change), STRUCTURED_DATA for datasets. A rule, scheme or policy ABOUT changing things is static (delay "
    "compensation, refund rules, cancellation policy): only their current state is dynamic. Then the entity types "
    "(kinds of thing, not named organisations); the organisations that matter (regulators, "
    "government, operators, industry and consumer bodies) with their websites; the source requirements; the "
    "chunk metadata fields worth keeping; and the flow settings: a brief that says what the assistant is for and "
    "which region a question means when it names none, a short domain, a scope that says what is in and out "
    "(live information goes to tools), supervisor instructions on which questions go to the knowledge base and "
    "which to live tools, and the search country. Ground it in the pages: every knowledge area and organisation "
    "lists the page URLs it came from (evidence_urls), never a URL that is not a fetched page; name no "
    "organisation the pages do not show. What you assume goes in assumptions, what you could not find in "
    "unknowns. Do not answer users' questions. Report through the record_domain_blueprint tool only."
)


def write_with_llm(requirements: SetupRequirements, pages: list[dict],
                   feedback: str | None = None, previous: dict | None = None) -> tuple[DomainBlueprint, dict]:
    blocks = [f"PAGE {p['url']} (title: {p.get('title') or '—'})\n{p['text'][:PAGE_CHARS]}" for p in pages]
    prompt = ("REQUIREMENTS:\n" + requirements.model_dump_json(indent=1) + "\n\nFETCHED PAGES:\n\n"
              + ("\n\n".join(blocks) or "(none)"))
    if previous is not None:
        prompt += ("\n\nPREVIOUS BLUEPRINT:\n" + json.dumps(previous, ensure_ascii=False, indent=1)
                   + f"\n\nTHE USER ASKS FOR THESE CHANGES:\n{feedback}\nKeep everything else as it was.")
    with observation(as_type="generation", name="blueprint_writer", model=llm.MODEL,
                     input=[{"role": "system", "content": WRITE_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=WRITE_PROMPT, name="record_domain_blueprint",
                              description="Record the Domain Blueprint.", schema=DomainBlueprint.model_json_schema(),
                              max_tokens=6000, temperature=0)
        blueprint = DomainBlueprint.model_validate(_normalise(reply.tool_input))
        gen.update(output=blueprint.model_dump(), usage_details=reply.usage)
    return blueprint, reply.usage


def _normalise(raw) -> dict:
    """Nested lists and objects sometimes arrive as JSON text (structured.py)."""
    raw = dict(raw) if isinstance(raw, dict) else {}
    for key in ("knowledge_areas", "entities", "organisations", "metadata_fields"):
        raw[key] = [i for i in structured.as_list(raw.get(key)) if isinstance(i, dict)]
    for key in ("flow", "source_requirements"):
        if isinstance(raw.get(key), str):
            try:
                raw[key] = json.loads(raw[key])
            except ValueError:
                raw.pop(key)
    return raw


# ---------- 5. check ----------

def check(blueprint: DomainBlueprint, pages: list[dict]) -> DomainBlueprint:
    """Keep only what the fetched pages support (see the module docstring)."""
    fetched = {p["url"] for p in pages}
    text = "\n".join(p.get("text") or "" for p in pages)
    hosts = {(urlparse(u).hostname or "").removeprefix("www.") for u in fetched}
    unknowns = list(blueprint.unknowns)
    areas = [a.model_copy(update={"evidence_urls": [u for u in a.evidence_urls if u in fetched]})
             for a in blueprint.knowledge_areas]
    organisations = []
    for org in blueprint.organisations:
        evidence = [u for u in org.evidence_urls if u in fetched]
        if not evidence and org.name.lower() not in text.lower():
            unknowns.append(f"{org.name}: named without a fetched page")
            continue
        website = org.website
        if website and (urlparse(website).hostname or "").removeprefix("www.") not in hosts and website not in text:
            website = None   # a website the pages do not show is not kept
        organisations.append(org.model_copy(update={"evidence_urls": evidence, "website": website}))
    flow = blueprint.flow.model_copy(update={
        k: (getattr(blueprint.flow, k) or "").strip()[:limit] for k, limit in FLOW_LIMITS.items()})
    flow = flow.model_copy(update={"search_country": flow.search_country.lower()})
    return blueprint.model_copy(update={"knowledge_areas": areas, "organisations": organisations, "flow": flow,
                                        "unknowns": list(dict.fromkeys(unknowns))})


# ---------- the whole run ----------

def generate(requirements: SetupRequirements, planner: Callable = plan_with_llm, writer: Callable = write_with_llm,
             search: Callable = research.web_search, fetch: Callable = research.fetch_source) -> tuple[DomainBlueprint, dict]:
    """(checked blueprint, research record)"""
    with observation(as_type="span", name="blueprint_research", input=requirements.model_dump()) as span:
        plan, _ = planner(requirements)
        gathered = gather(plan, search, fetch)
        blueprint, _ = writer(requirements, gathered["pages"])
        if not blueprint.flow.search_country and plan.search_country:   # the writer often leaves it blank
            blueprint = blueprint.model_copy(update={"flow": blueprint.flow.model_copy(
                update={"search_country": plan.search_country})})
        blueprint = check(blueprint, gathered["pages"])
        record = {"queries": plan.queries, "search_country": plan.search_country, "searches": gathered["searches"],
                  "pages": [{"url": p["url"], "title": p.get("title"), "chars": p.get("chars")} for p in gathered["pages"]],
                  "skipped": gathered["skipped"]}
        span.update(output={"areas": len(blueprint.knowledge_areas), "organisations": len(blueprint.organisations),
                            "pages": len(record["pages"])})
    return blueprint, record


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
                CREATE TABLE IF NOT EXISTS app_domain_blueprints (
                    version serial PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    status text NOT NULL,
                    blueprint jsonb,
                    research jsonb,
                    error text,
                    feedback text,
                    confirmed_at timestamptz,
                    confirmed_by text
                )""")
        _schema_ready = True


def _view(row: dict | None) -> dict | None:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()} if row else None


def start_version(feedback: str | None = None) -> int:
    """A new version, researching; refused while another one is."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616703)")
        if connection.execute("SELECT 1 FROM app_domain_blueprints WHERE status = 'researching'").fetchone():
            raise RuntimeError("a blueprint is already being researched")
        return connection.execute("INSERT INTO app_domain_blueprints (status, feedback) VALUES ('researching', %s) "
                                  "RETURNING version", (feedback,)).fetchone()["version"]


def expire_stale(minutes: int) -> int:
    """Research still running after `minutes` was cut off (the web app restarted): mark it failed."""
    ensure_schema()
    with _connect() as connection:
        return connection.execute(
            "UPDATE app_domain_blueprints SET status = 'failed', error = 'interrupted: please start again' "
            "WHERE status = 'researching' AND created_at < now() - make_interval(mins => %s)", (minutes,)).rowcount


def finish(version: int, blueprint: DomainBlueprint, record: dict) -> None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_domain_blueprints SET status = 'ready', blueprint = %s, research = %s "
                           "WHERE version = %s", (Jsonb(blueprint.model_dump()), Jsonb(record), version))


def fail(version: int, error: str) -> None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("UPDATE app_domain_blueprints SET status = 'failed', error = %s WHERE version = %s",
                           (error[:1000], version))


def latest() -> dict | None:
    ensure_schema()
    with _connect() as connection:
        return _view(connection.execute("SELECT * FROM app_domain_blueprints ORDER BY version DESC LIMIT 1").fetchone())


def get(version: int) -> dict | None:
    ensure_schema()
    with _connect() as connection:
        return _view(connection.execute("SELECT * FROM app_domain_blueprints WHERE version = %s", (version,)).fetchone())


def confirm(version: int, user_id: str | None) -> None:
    ensure_schema()
    with _connect() as connection:
        if connection.execute("UPDATE app_domain_blueprints SET confirmed_at = now(), confirmed_by = %s "
                              "WHERE version = %s AND status = 'ready'", (user_id, version)).rowcount != 1:
            raise ValueError("only a ready blueprint can be confirmed")

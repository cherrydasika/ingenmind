"""Knowledge metadata from the blueprint (epic #19, #15): what each stored page
is about, on every chunk of it (payload field `meta`), for retrieval to use.

    label_url(client, url, ...)  a stored page's labels, from its payload and
                                 stored text, merged onto all its chunks
    relabel(plan_version)        every page of the approved plan, in the
                                 background (the setup page's Relabel)
    view()                       the latest relabel run

Labels (`meta`):

    topic             knowledge-area keys the page answers: the labeller's choice;
                      the section's areas when it gives none
    organisation      the blueprint's organisation whose website is the page's
                      host; else the source's (or publisher's) name
    source_type       that organisation's role (regulator, government, operator…)
    authority         the source's authority: high 1.0, medium 0.6, low 0.3; for
                      a research page, the validator's authority score
    region            the blueprint's region, when it names exactly one
    effective_date    the page's own date (published or modified), when it says
    retrieved_at      when the page was read
    content_type      one of CONTENT_TYPES: the section's flags when they decide
                      it, else the labeller
    fields            the blueprint's metadata_fields: per field, the values the
                      page is about, each one of the field's examples
    labelled_by       "rules" or "rules+model"

The labeller is one forced-tool call per page (not per chunk: a page's chunks
share its labels), on the start of the page's text. Every value it gives is
checked in code against the allowed ones; anything else is dropped. If the
call fails, the page keeps the deterministic labels.
"""

import threading
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import knowledge_system
import llm
import structured
from common import config, storage
from tracing import observation

from . import content, plan, sources

CONTENT_TYPES = ("policy", "guide", "faq", "form", "news", "contact", "timetable", "reference", "other")
FLAG_TYPES = {"news": "news", "press": "news", "terms": "policy", "privacy": "policy", "cookies": "policy",
              "events": "news", "archive": "reference"}
AUTHORITY = {"high": 1.0, "medium": 0.6, "low": 0.3}
LABEL_CHARS = 6000          # of the page's text, in the labelling prompt
MAX_VALUES = 6              # per field


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _key(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def organisation_for(host: str, bp: dict) -> dict | None:
    """The blueprint's organisation whose website is this host (or a parent domain of it)."""
    for org in bp.get("organisations") or []:
        site = _host(org.get("website") or "")
        if site and (host == site or host.endswith("." + site)):
            return org
    return None


def deterministic(payload: dict, bp: dict, source: dict | None, section: dict | None) -> dict:
    """The labels setup already knows for a stored page."""
    host = _host(payload.get("source_url"))
    # The page's host, else its chosen source's (a section's pages may sit on another host of the site).
    org = organisation_for(host, bp) or organisation_for(_host("https://" + (source or {}).get("host", "")), bp)
    regions = bp.get("regions") or []
    flags = (section or {}).get("flags") or []
    research = payload.get("origin") == "research_agent"
    authority = None
    if source and source.get("authority") in AUTHORITY:
        authority = AUTHORITY[source["authority"]]
    elif research and isinstance(payload.get("research_scores"), dict):
        authority = payload["research_scores"].get("authority")
    meta = {
        "topic": [a for a in (payload.get("areas") or []) if isinstance(a, str)],
        "organisation": (org or {}).get("name") or (source or {}).get("name") or payload.get("research_publisher")
                        or host or None,
        "source_type": (org or {}).get("role"),
        "authority": authority,
        "region": regions[0] if len(regions) == 1 else None,
        "effective_date": payload.get("page_date") or payload.get("research_date"),
        "retrieved_at": payload.get("ingested_at"),
        "content_type": next((FLAG_TYPES[f] for f in flags if f in FLAG_TYPES), None),
    }
    return {k: v for k, v in meta.items() if v not in (None, "", [])}


# ---------- the page labeller ----------

LABEL_PROMPT = (
    "You label a web page for a knowledge base's search, using only the vocabulary given. For the content type "
    "pick the one that fits the page best. For each field, list the values from its allowed list that the page "
    "is specifically about (not values it merely mentions in passing); an empty list when none apply. When asked "
    "for topics, pick the knowledge areas the page answers. Use only allowed values, copied exactly. Judge only "
    "the text given. Report through the record_page_labels tool only."
)


def _schema(bp: dict, with_topic: bool) -> dict:
    fields = {}
    for f in bp.get("metadata_fields") or []:
        allowed = [str(e) for e in (f.get("examples") or []) if str(e).strip()]
        if f.get("field") and allowed:
            fields[f["field"]] = {"type": "array", "items": {"type": "string", "enum": allowed},
                                  "description": f.get("description") or f["field"]}
    properties = {"content_type": {"type": "string", "enum": list(CONTENT_TYPES)},
                  "fields": {"type": "object", "properties": fields}}
    if with_topic:
        properties["topic"] = {"type": "array", "items": {"type": "string",
                               "enum": [a["key"] for a in bp.get("knowledge_areas") or []]},
                               "description": "The knowledge areas the page answers"}
    return {"type": "object", "properties": properties, "required": ["content_type", "fields"]}


def label_with_llm(bp: dict, url: str, text: str, with_topic: bool) -> dict:
    areas = "\n".join(f"- {a['key']}: {a['name']} — {a.get('description') or ''}" for a in bp.get("knowledge_areas") or [])
    prompt = (f"KNOWLEDGE SYSTEM: {bp.get('name')} — {bp.get('purpose') or ''}\n"
              + (f"KNOWLEDGE AREAS:\n{areas}\n" if with_topic else "")
              + f"\nPAGE: {url}\n{text[:LABEL_CHARS]}")
    with observation(as_type="generation", name="page_labeller", model=llm.MODEL,
                     input=[{"role": "system", "content": LABEL_PROMPT}, {"role": "user", "content": prompt}],
                     metadata={"url": url}) as gen:
        reply = llm.call_tool(prompt, system=LABEL_PROMPT, name="record_page_labels",
                              description="Record the page's labels.", schema=_schema(bp, with_topic),
                              max_tokens=600, temperature=0)
        raw = reply.tool_input if isinstance(reply.tool_input, dict) else {}
        gen.update(output=raw, usage_details=reply.usage)
    return raw


def checked(raw: dict, bp: dict, with_topic: bool) -> dict:
    """Only allowed values, matched case- and underscore-insensitively to their canonical spelling."""
    out = {}
    content_type = _key(raw.get("content_type"))
    if content_type in CONTENT_TYPES:
        out["content_type"] = content_type
    fields_raw = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    fields = {}
    for f in bp.get("metadata_fields") or []:
        allowed = {_key(e): str(e) for e in (f.get("examples") or []) if str(e).strip()}
        values = [allowed[_key(v)] for v in structured.as_list(fields_raw.get(f.get("field"))) if _key(v) in allowed]
        if values:
            fields[f["field"]] = list(dict.fromkeys(values))[:MAX_VALUES]
    if fields:
        out["fields"] = fields
    if with_topic:
        keys = {a["key"] for a in bp.get("knowledge_areas") or []}
        topics = [t for t in structured.as_list(raw.get("topic")) if t in keys]
        if topics:
            out["topic"] = list(dict.fromkeys(topics))
    return out


# ---------- labelling a stored page ----------

def _stored(client: storage.PgStore, url: str) -> tuple[dict | None, str]:
    with client.connection() as connection:
        rows = connection.execute(f"SELECT payload, body FROM {client.table} WHERE source_url = %s "
                                  "ORDER BY chunk_index", (url,)).fetchall()
    if not rows:
        return None, ""
    return rows[0]["payload"], "\n".join(r["body"] for r in rows)


def label_url(client: storage.PgStore, url: str, bp: dict, sources_by_id: dict | None = None,
              sections_by_id: dict | None = None, labeller: Callable | None = None) -> dict | None:
    """Label a stored page and merge `meta` onto its chunks; the labels (None: not stored)."""
    payload, text = _stored(client, url)
    if payload is None:
        return None
    sources_by_id = sources_by_id if sources_by_id is not None else {s["source_id"]: s for s in sources.list_sources()}
    sections_by_id = sections_by_id if sections_by_id is not None else {}
    source = sources_by_id.get(int(payload["source_id"])) if payload.get("source_id") is not None else None
    if source is None:      # a research page, or the configured list: the chosen source on its host, if any
        source = next((s for s in sources_by_id.values() if s.get("host") and _host(url).endswith(s["host"])), None)
    section = sections_by_id.get(int(payload["content_id"])) if payload.get("content_id") is not None else None
    meta = deterministic(payload, bp, source, section)
    # The page's own topics: a section's areas are its whole section's (National Rail's "Help and
    # assistance" was refunds, delay compensation and passenger rights, so its page for autistic
    # passengers was not accessibility). They stay only when the labeller gives none.
    try:
        model = checked((labeller or label_with_llm)(bp, url, text, True), bp, True)
        meta = {**meta, **{k: v for k, v in model.items() if not (k == "content_type" and meta.get("content_type"))}}
        meta["labelled_by"] = "rules+model"
    except Exception:
        meta["labelled_by"] = "rules"
    meta["labelled_at"] = datetime.now(timezone.utc).isoformat()
    storage.merge_payload(client, url, {"meta": meta})
    return meta


def vocabulary() -> dict | None:
    """The filters the knowledge-base agent may use: the label values the stored chunks
    actually carry (topics with their names), or None when nothing is labelled."""
    bp = blueprint_or_none()
    names = {a["key"]: a["name"] for a in (bp or {}).get("knowledge_areas") or []}
    out = {"topic": {}, "organisation": set(), "content_type": set()}
    for client in (storage.get_client(), storage.get_client("research")):
        try:
            with client.connection() as connection:
                rows = connection.execute(f"""
                    SELECT DISTINCT payload->'meta'->'topic' AS topic, payload->'meta'->>'organisation' AS organisation,
                           payload->'meta'->>'content_type' AS content_type
                    FROM {client.table} WHERE payload ? 'meta' AND expires_at > now()""").fetchall()
        except Exception:
            continue
        for r in rows:
            for t in r["topic"] or []:
                out["topic"][t] = names.get(t, t.replace("_", " "))
            if r["organisation"]:
                out["organisation"].add(r["organisation"])
            if r["content_type"]:
                out["content_type"].add(r["content_type"])
    if not (out["topic"] or out["organisation"] or out["content_type"]):
        return None
    return {"topic": dict(sorted(out["topic"].items())), "organisation": sorted(out["organisation"]),
            "content_type": sorted(out["content_type"])}


def blueprint_or_none() -> dict | None:
    """The confirmed blueprint, or None on an install setup did not build."""
    try:
        return content._confirmed_blueprint()
    except Exception:
        return None


# ---------- relabelling the approved plan's pages ----------

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
                CREATE TABLE IF NOT EXISTS kb_labelling_runs (
                    id serial PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    finished_at timestamptz,
                    plan_version int NOT NULL,
                    status text NOT NULL CHECK (status IN ('labelling', 'done', 'failed')),
                    total int NOT NULL DEFAULT 0,
                    done int NOT NULL DEFAULT 0,
                    model int NOT NULL DEFAULT 0,
                    error text,
                    started_by text
                )""")
        _schema_ready = True


STALE_MINUTES = 30


def _start_thread(work) -> None:
    """Background work (tests replace this to run inline)."""
    threading.Thread(target=work, name="relabel", daemon=True).start()


def view() -> dict | None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("""
            UPDATE kb_labelling_runs SET status = 'failed', error = 'interrupted: please relabel again'
            WHERE status = 'labelling' AND created_at < now() - make_interval(mins => %s)""", (STALE_MINUTES,))
        row = connection.execute("SELECT * FROM kb_labelling_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()} if row else None


def relabel(user_id: str | None = None, labeller: Callable | None = None) -> int:
    """Label every stored page of the approved plan again, from its stored text; the run's id."""
    approved = plan.latest("approved")
    if not approved:
        raise ValueError("there is no approved plan to label")
    bp = content._confirmed_blueprint()
    urls = [p["url"] for p in plan.pages(approved["version"]) if p["status"] in ("ingested", "unchanged")]
    ensure_schema()
    view()                                       # a stale run no longer blocks
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616715)")
        if connection.execute("SELECT 1 FROM kb_labelling_runs WHERE status = 'labelling'").fetchone():
            raise RuntimeError("the pages are already being labelled")
        run_id = connection.execute("""
            INSERT INTO kb_labelling_runs (plan_version, status, total, started_by)
            VALUES (%s, 'labelling', %s, %s) RETURNING id""", (approved["version"], len(urls), user_id)).fetchone()["id"]

    def work():
        client = storage.get_client()
        sources_by_id = {s["source_id"]: s for s in sources.list_sources(include_removed=True)}
        sections_by_id = {c["content_id"]: c for c in content.list_sections()}
        done = model = 0
        try:
            with observation(as_type="span", name="relabel", input={"plan_version": approved["version"],
                                                                     "pages": len(urls)}):
                for url in urls:
                    meta = label_url(client, url, bp, sources_by_id, sections_by_id, labeller)
                    done += 1
                    model += bool(meta and meta.get("labelled_by") == "rules+model")
                    with _connect() as connection:
                        connection.execute("UPDATE kb_labelling_runs SET done = %s, model = %s WHERE id = %s",
                                           (done, model, run_id))
            status, error = "done", None
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        with _connect() as connection:
            connection.execute("UPDATE kb_labelling_runs SET status = %s, error = %s, finished_at = now() "
                               "WHERE id = %s", (status, error, run_id))
        knowledge_system.record_event("relabelled", user_id, {"plan_version": approved["version"], "pages": done,
                                                              "status": status})

    _start_thread(work)
    return run_id


def summary(plan_version: int) -> dict:
    """How the plan's stored chunks are labelled: pages with labels, and the values per field."""
    client = storage.get_client()
    with client.connection() as connection:
        rows = connection.execute(f"""
            SELECT DISTINCT ON (source_url) source_url, payload->'meta' AS meta FROM {client.table}
            WHERE payload->>'plan_version' = %s ORDER BY source_url, chunk_index""", (str(plan_version),)).fetchall()
    out = {"pages": len(rows), "labelled": 0, "by_model": 0, "content_type": {}, "organisation": {}, "fields": {}}
    for r in rows:
        meta = r["meta"] or {}
        if not meta:
            continue
        out["labelled"] += 1
        out["by_model"] += meta.get("labelled_by") == "rules+model"
        for key in ("content_type", "organisation"):
            if meta.get(key):
                out[key][meta[key]] = out[key].get(meta[key], 0) + 1
        for field, values in (meta.get("fields") or {}).items():
            counts = out["fields"].setdefault(field, {})
            for v in values:
                counts[v] = counts.get(v, 0) + 1
    return out

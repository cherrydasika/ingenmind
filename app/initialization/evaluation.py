"""The evaluation of a setup-built knowledge system (epic #19, #16): the flow
the blueprint describes, and an evaluation set written from the blueprint
and the pages the build stored.

    prepare()   in the background (one at a time): the candidate flow, then the set
    ensure_prepared()  from EVALUATING, once per approved plan: prepare() when
                the build finished and nothing was prepared for its plan
    run()       on the user's request: the set against the candidate flow, through
                the flow evaluations' runner (flows/evals.py), which scores each
                question by its kind and sums the results up per knowledge area
    view()      the latest preparation, its set and flow, and its latest run

The candidate flow: a copy of the live flow (else the built-in) with the
blueprint's brief, domain, supervisor instructions, search country and scope,
published as a new version of `setup_<domain>` but not made live (#17's Go
live promotes it).

The set (`setup_evaluation`, editable on the Evaluations page like any set):

    answer        each sourced area with stored pages: up to PAGES_PER_AREA of
                  its pages' text to one forced-tool call (question_writer) that
                  writes questions, each with its answer, the page it comes from
                  and a supporting quote. Kept only if, in code, the page is one
                  of those given and the quote's words, in order, are in that
                  page's stored text (case, whitespace and punctuation ignored);
                  else dropped and listed, as is a second question from the same
                  passage (areas share pages).
    not_covered   each sourced area with no stored page: its first example question
    live          each live-tool area: its first example question
    out_of_scope  two questions the blueprint's scope excludes (one model call)

    app_setup_evaluations  one row per preparation: status (preparing | ready |
                           failed), the plan and blueprint it is for, the
                           candidate flow, counts per area, dropped questions
"""

import re
import threading
import unicodedata
from datetime import datetime
from typing import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import flows
import knowledge_system
import llm
import structured
from common import config, storage
from flows import evals
from tracing import observation

from . import content, plan, sources, state

SET_ID = "setup_evaluation"
QUESTIONS_PER_AREA = 3
OUT_OF_SCOPE = 2
PAGES_PER_AREA = 4
PAGE_CHARS = 3000           # per page, in the writing prompt
MIN_QUOTE = 20              # characters: a shorter quote proves nothing
STALE_MINUTES = 20
SECONDS_PER_QUESTION = 30   # a full answer each (25 s on average locally), for the estimate shown before a run
LIVE_CLASSES = ("DYNAMIC_KNOWLEDGE", "EXTERNAL_TOOL_API")


def _start_thread(work) -> None:
    """Background work (tests replace this to run inline)."""
    threading.Thread(target=work, name="setup-evaluation", daemon=True).start()


# ---------- the candidate flow ----------

def flow_id_for(domain: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", domain).encode("ascii", "ignore")
                  .decode().lower()).strip("_")
    return f"setup_{slug or 'assistant'}"[:64].rstrip("_")


def apply_blueprint(spec: flows.FlowSpec, bp: dict, flow_id: str) -> flows.FlowSpec:
    """The flow with the blueprint's settings on its supervisor and input guardrail.
    A setting too short for the flow's limits keeps the flow's own; the guardrail's
    messages are emptied, so they name the blueprint's domain."""
    settings = bp["flow"]
    nodes = []
    for node in spec.nodes:
        config_ = dict(node.config)
        if node.type == "supervisor":
            for key, value in (("brief", settings.get("brief")), ("domain", settings.get("domain")),
                               ("instructions", settings.get("supervisor_instructions")),
                               ("search_country", settings.get("search_country"))):
                value = (value or "").strip()
                if value or key == "search_country":
                    config_[key] = value
            if len(config_.get("brief") or "") < 20:
                config_.pop("brief", None)
        elif node.type == "input_guardrail":
            scope = (settings.get("scope") or "").strip()
            config_["scope"] = scope if len(scope) >= 20 else ""      # empty: the brief
            # The live flow's messages name its own domain ("UK trains and the weather"):
            # left empty, they name the blueprint's.
            for key in ("block_message", "clarify_message", "output_message"):
                config_[key] = ""
        nodes.append(node.model_copy(update={"config": config_}))
    return flows.FlowSpec.model_validate({
        **spec.model_dump(mode="json"), "id": flow_id, "name": f"{bp['name']} (setup)"[:200],
        "description": f"Written by setup from the Domain Blueprint: {bp.get('purpose') or ''}"[:1000],
        "nodes": [n.model_dump(mode="json") for n in nodes]})


def _check_flow(spec: flows.FlowSpec) -> None:
    """It must compile here, as anything published must (web_api._run_check)."""
    import agent
    agent.graph_for(spec)


def candidate_flow(bp: dict, note: str, check: Callable | None = None) -> tuple[str, int]:
    """(flow id, version) of the newly published candidate; the live flow is unchanged."""
    builtin = flows.load_flow()
    base = flows.FlowSpec.model_validate(flows.store.live(builtin)["flow"])
    spec = apply_blueprint(base, bp, flow_id_for(bp["flow"].get("domain") or bp["name"]))
    issues = [i for i in flows.validate(spec) if i["level"] == "error"]
    if issues:
        raise flows.FlowError(issues)
    try:
        flows.store.create_flow(spec, builtin)
    except flows.store.FlowConflict:
        flows.store.save_draft(spec, builtin)
    return spec.id, flows.store.publish(spec.id, note, check or _check_flow)


# ---------- the pages behind each area ----------

def area_pages(plan_version: int, area: str, limit: int = PAGES_PER_AREA) -> list[dict]:
    """[{url, text}]: the area's stored pages from this plan (the most chunks first), each page's text whole."""
    client = storage.get_client()
    with client.connection() as connection:
        rows = connection.execute(f"""
            SELECT source_url, string_agg(body, E'\\n' ORDER BY chunk_index) AS text
            FROM {client.table}
            WHERE payload->>'plan_version' = %s AND payload->'areas' ? %s
            GROUP BY source_url ORDER BY count(*) DESC, source_url LIMIT %s""",
            (str(plan_version), area, limit)).fetchall()
    return [{"url": r["source_url"], "text": r["text"]} for r in rows]


# ---------- writing questions ----------

class DraftQuestion(BaseModel):
    question: str = Field(description="What a user would ask, in their words")
    expected_answer: str = Field(description="The answer, in one or two sentences, from the page alone")
    expected_source: str = Field(description="The URL of the page the answer comes from, exactly as given")
    supporting_quote: str = Field(description="A sentence copied word for word from that page that supports "
                                              "the answer")


class DraftQuestions(BaseModel):
    questions: list[DraftQuestion]


class ScopeQuestions(BaseModel):
    questions: list[str] = Field(description="Questions a user might ask that the scope excludes")


WRITE_PROMPT = (
    "You write an evaluation set for an assistant that answers from a knowledge base. From the pages given, write "
    "questions a real user of this assistant would ask about the knowledge area, each answered by one page alone. "
    "For each: the question in a user's words (do not copy the page's wording or name the page), the expected "
    "answer in one or two sentences, the page's URL exactly as given, and a supporting quote copied word for word "
    "from that page (one sentence, at least a few words). Use different pages where you can. Do not write a "
    "question the pages do not answer. Report through the record_questions tool only."
)

SCOPE_PROMPT = (
    "You test an assistant's scope guardrail. From its domain and scope, write questions a user might plausibly "
    "send it that the scope clearly excludes (near the domain, not absurd), one per excluded topic where you "
    "can. Only topics the assistant must refuse: never what the scope sends to live tools, operator websites or "
    "another part of the assistant (live times, departures, current status). Report through the record_questions "
    "tool only."
)


def write_with_llm(bp: dict, area: dict, pages: list[dict], count: int) -> DraftQuestions:
    blocks = [f"PAGE {p['url']}\n{p['text'][:PAGE_CHARS]}" for p in pages]
    prompt = (f"ASSISTANT: {bp['name']} — {bp.get('purpose') or ''}\nRegions: {', '.join(bp.get('regions') or [])}\n"
              f"KNOWLEDGE AREA: {area['name']} — {area['description']}\n\nWrite {count} questions.\n\n"
              + "\n\n".join(blocks))
    with observation(as_type="generation", name="question_writer", model=llm.MODEL,
                     input=[{"role": "system", "content": WRITE_PROMPT}, {"role": "user", "content": prompt}],
                     metadata={"area": area["key"]}) as gen:
        reply = llm.call_tool(prompt, system=WRITE_PROMPT, name="record_questions",
                              description="Record the questions.", schema=DraftQuestions.model_json_schema(),
                              max_tokens=2500, temperature=0)
        raw = reply.tool_input if isinstance(reply.tool_input, dict) else {}
        items = [structured.normalise(i, strings=tuple(DraftQuestion.model_fields))
                 for i in structured.as_list(raw.get("questions")) if isinstance(i, dict)]
        drafts = DraftQuestions(questions=[DraftQuestion.model_validate(i) for i in items])
        gen.update(output=drafts.model_dump(), usage_details=reply.usage)
    return drafts


def scope_with_llm(bp: dict, count: int) -> list[str]:
    settings = bp["flow"]
    prompt = f"DOMAIN: {settings.get('domain')}\nBRIEF: {settings.get('brief')}\nSCOPE: {settings.get('scope')}\n\n" \
             f"Write {count} questions."
    with observation(as_type="generation", name="question_writer", model=llm.MODEL,
                     input=[{"role": "system", "content": SCOPE_PROMPT}, {"role": "user", "content": prompt}],
                     metadata={"area": "out_of_scope"}) as gen:
        reply = llm.call_tool(prompt, system=SCOPE_PROMPT, name="record_questions",
                              description="Record the questions.", schema=ScopeQuestions.model_json_schema(),
                              max_tokens=600, temperature=0)
        questions = structured.normalise(reply.tool_input, lists=("questions",))["questions"]
        gen.update(output=questions, usage_details=reply.usage)
    return [q.strip() for q in questions if q.strip()]


# ---------- the grounding check ----------

def _normal(text: str) -> str:
    """Words only, in order: a quote of a list often joins its items with commas or
    semicolons the page does not have (items one per line), so punctuation is ignored."""
    return " ".join(re.sub(r"[^\w£$€%]+", " ", unicodedata.normalize("NFKC", text or "").lower()).split())


def grounded(draft: DraftQuestion, pages: list[dict]) -> str | None:
    """None when the question is grounded in a page given; else why it is not."""
    if not draft.question.strip() or not draft.expected_answer.strip():
        return "no question or no answer"
    page = next((p for p in pages if p["url"] == draft.expected_source.strip()), None)
    if page is None:
        return "its page is not one of the pages given"
    quote = _normal(draft.supporting_quote)
    if len(quote) < MIN_QUOTE:
        return "its supporting quote is too short"
    if f" {quote} " not in f" {_normal(page['text'])} ":     # whole words
        return "its supporting quote is not in the page"
    return None


# ---------- the set ----------

def build_set(bp: dict, plan_version: int, writer: Callable | None = None, scoper: Callable | None = None) -> dict:
    """{"questions": [...], "dropped": [...], "areas": {key: {kind, questions, pages}}}.
    Answer questions: QUESTIONS_PER_AREA each, fewer when the areas would not fit
    the set's limit (the room left shared among the areas left)."""
    writer, scoper = writer or write_with_llm, scoper or scope_with_llm
    questions, dropped, areas = [], [], {}
    quoted = set()      # (page, quote): areas share pages, and one passage makes one question
    sourced = [a for a in bp["knowledge_areas"] if a["knowledge_class"] in sources.SOURCED_CLASSES]
    live = [a for a in bp["knowledge_areas"] if a["knowledge_class"] in LIVE_CLASSES]
    pages = {a["key"]: area_pages(plan_version, a["key"]) for a in sourced}
    covered = [a for a in sourced if pages[a["key"]]]
    room = evals.MAX_QUESTIONS - (len(sourced) - len(covered)) - len(live) - OUT_OF_SCOPE
    areas_left = len(covered)

    for area in sourced:
        key = area["key"]
        if not pages[key]:
            example = next(iter(area.get("example_questions") or []), "")
            areas[key] = {"name": area["name"], "kind": "not_covered", "pages": 0, "questions": 0}
            if example:
                questions.append({"question": example, "expected": "", "area": key, "kind": "not_covered"})
                areas[key]["questions"] = 1
            else:
                dropped.append({"area": key, "question": "", "reason": "the area has no example question"})
            continue
        areas[key] = {"name": area["name"], "kind": "answer", "pages": len(pages[key]), "questions": 0}
        want = min(QUESTIONS_PER_AREA, -(-room // areas_left)) if room > 0 else 0
        areas_left -= 1
        if want <= 0:
            dropped.append({"area": key, "question": "", "reason": "the set is full"})
            continue
        try:
            drafts = writer(bp, area, pages[key], want).questions
        except Exception as error:
            dropped.append({"area": key, "question": "", "reason": f"no questions written: {type(error).__name__}"})
            continue
        for draft in drafts:
            reason = grounded(draft, pages[key]) if areas[key]["questions"] < want else "more than asked for"
            passage = (draft.expected_source.strip(), _normal(draft.supporting_quote))
            if not reason and passage in quoted:
                reason = "another question is from the same passage"
            if reason:
                dropped.append({"area": key, "question": draft.question[:300], "reason": reason,
                                "page": draft.expected_source[:300], "quote": draft.supporting_quote[:300]})
                continue
            questions.append({"question": draft.question, "expected": draft.expected_answer, "area": key,
                              "kind": "answer", "expected_source": draft.expected_source.strip()})
            quoted.add(passage)
            areas[key]["questions"] += 1
            room -= 1

    for area in live:
        key = area["key"]
        example = next(iter(area.get("example_questions") or []), "")
        areas[key] = {"name": area["name"], "kind": "live", "pages": 0, "questions": 1 if example else 0}
        if example:
            questions.append({"question": example, "expected": "", "area": key, "kind": "live"})
        else:
            dropped.append({"area": key, "question": "", "reason": "the area has no example question"})

    try:
        excluded = scoper(bp, OUT_OF_SCOPE)[:OUT_OF_SCOPE]
    except Exception as error:
        excluded = []
        dropped.append({"area": "out_of_scope", "question": "", "reason": f"none written: {type(error).__name__}"})
    areas["out_of_scope"] = {"name": "Out of scope", "kind": "out_of_scope", "pages": 0, "questions": len(excluded)}
    questions += [{"question": q, "expected": "", "expect_blocked": True, "area": "out_of_scope",
                   "kind": "out_of_scope"} for q in excluded]
    return {"questions": evals.clean_questions(questions), "dropped": dropped, "areas": areas}


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
                CREATE TABLE IF NOT EXISTS app_setup_evaluations (
                    id serial PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    status text NOT NULL CHECK (status IN ('preparing', 'ready', 'failed')),
                    plan_version int NOT NULL,
                    blueprint_version int,
                    flow_id text,
                    flow_version int,
                    record jsonb,
                    error text,
                    prepared_by text,
                    run_id text
                )""")
            connection.execute("ALTER TABLE app_setup_evaluations ADD COLUMN IF NOT EXISTS run_id text")
        _schema_ready = True


def _view(row: dict | None) -> dict | None:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()} if row else None


def latest() -> dict | None:
    ensure_schema()
    with _connect() as connection:
        connection.execute("""
            UPDATE app_setup_evaluations SET status = 'failed', error = 'interrupted: please prepare it again'
            WHERE status = 'preparing' AND created_at < now() - make_interval(mins => %s)""", (STALE_MINUTES,))
        return _view(connection.execute("SELECT * FROM app_setup_evaluations ORDER BY id DESC LIMIT 1").fetchone())


def _begin(plan_version: int, blueprint_version: int | None, user_id: str | None,
           only_if_none: bool = False) -> int | None:
    """A new preparation (None when one is going, or — only_if_none — this plan already has one)."""
    ensure_schema()
    latest()                                        # a stale one no longer blocks
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616724)")
        if connection.execute("SELECT 1 FROM app_setup_evaluations WHERE status = 'preparing'").fetchone():
            return None
        if only_if_none and connection.execute("SELECT 1 FROM app_setup_evaluations WHERE plan_version = %s",
                                               (plan_version,)).fetchone():
            return None
        return connection.execute("""
            INSERT INTO app_setup_evaluations (status, plan_version, blueprint_version, prepared_by)
            VALUES ('preparing', %s, %s, %s) RETURNING id""",
            (plan_version, blueprint_version, user_id)).fetchone()["id"]


def _set(evaluation_id: int, **fields) -> None:
    columns = ", ".join(f"{k} = %s" for k in fields)
    values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with _connect() as connection:
        connection.execute(f"UPDATE app_setup_evaluations SET {columns} WHERE id = %s", (*values, evaluation_id))


# ---------- preparing ----------

def _work(evaluation_id: int, bp: dict, plan_version: int, blueprint_version: int | None, check, writer,
          scoper) -> None:
    try:
        with observation(as_type="span", name="evaluation_set",
                         input={"plan_version": plan_version, "blueprint_version": blueprint_version}) as span:
            note = f"Setup: blueprint v{blueprint_version}, plan v{plan_version}"
            flow_id, flow_version = candidate_flow(bp, note, check)
            _set(evaluation_id, flow_id=flow_id, flow_version=flow_version)
            built = build_set(bp, plan_version, writer, scoper)
            evals.save_set(SET_ID, f"Setup: {bp['name']}"[:200],
                           f"Written by setup from blueprint v{blueprint_version} and plan v{plan_version}.",
                           built["questions"])
            record = {"questions": len(built["questions"]), "areas": built["areas"], "dropped": built["dropped"]}
            _set(evaluation_id, status="ready", record=record)
            span.update(output={k: record[k] for k in ("questions", "areas")} | {"dropped": len(built["dropped"])})
    except Exception as error:
        _set(evaluation_id, status="failed", error=f"{type(error).__name__}: {error}"[:1000])


def prepare(user_id: str | None = None, check: Callable | None = None, writer: Callable | None = None,
            scoper: Callable | None = None, only_if_none: bool = False) -> int | None:
    """Start preparing the evaluation for the approved plan; the preparation's id
    (None when one is going, or only_if_none and this plan has one)."""
    if knowledge_system.status()["state"] != state.EVALUATING:
        raise state.TransitionNotAllowed("the evaluation is prepared once the build has finished")
    approved = plan.latest("approved")
    if not approved:
        raise ValueError("there is no approved plan")
    bp = content._confirmed_blueprint()
    blueprint_version = knowledge_system.status()["blueprint_version"]
    evaluation_id = _begin(approved["version"], blueprint_version, user_id, only_if_none)
    if evaluation_id is None:
        if only_if_none:
            return None
        raise RuntimeError("the evaluation is already being prepared")
    _start_thread(lambda: _work(evaluation_id, bp, approved["version"], blueprint_version, check, writer, scoper))
    return evaluation_id


def ensure_prepared(check: Callable | None = None) -> None:
    """Prepare it by itself once the build has finished (once per approved plan)."""
    try:
        if knowledge_system.status()["state"] == state.EVALUATING:
            prepare(None, check, only_if_none=True)
    except (state.TransitionNotAllowed, ValueError):
        pass


def run(user_id: str | None, execute: Callable, check: Callable | None = None) -> str:
    """Queue the prepared set against the candidate flow; the run's id."""
    if knowledge_system.status()["state"] != state.EVALUATING:
        raise state.TransitionNotAllowed("the evaluation runs once the build has finished")
    record = latest()
    if not record or record["status"] != "ready":
        raise ValueError("prepare the evaluation first")
    if record["run_id"] and (current := evals.get_run(record["run_id"])) and current["status"] in ("queued", "running"):
        raise RuntimeError("the evaluation is already running")
    spec = flows.FlowSpec.model_validate(flows.store.get_version(record["flow_id"], record["flow_version"],
                                                                 flows.load_flow()))
    (check or _check_flow)(spec)
    run_id = evals.start(SET_ID, record["flow_id"], [(record["flow_version"], spec.model_dump(mode="json"))],
                         execute)[0]
    _set(record["id"], run_id=run_id)
    knowledge_system.record_event("evaluation_started", user_id, {"run_id": run_id, "flow_id": record["flow_id"],
                                                                  "flow_version": record["flow_version"]})
    return run_id


def view() -> dict | None:
    """The latest preparation, with its set's questions and its latest run (results included)."""
    record = latest()
    if record is None:
        return None
    question_set = evals.get_set(SET_ID) if record["status"] == "ready" else None
    questions = question_set["questions"] if question_set else []
    return {**record, "set_id": SET_ID, "questions": questions,
            "estimate": {"questions": len(questions), "minutes": round(len(questions) * SECONDS_PER_QUESTION / 60)},
            "run": evals.get_run(record["run_id"]) if record.get("run_id") else None}

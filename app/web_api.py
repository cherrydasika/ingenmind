"""JSON API + static host for the HTML/JS web UI (web/). Wraps the
pipeline modules — retrieval, generation, index info, embedding plots,
ingestion, ad-hoc documents, docs content.

    uvicorn web_api:app --app-dir /app --host 0.0.0.0 --port 8000
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/app/dags")

import numpy as np
import plotly
from psycopg.errors import DataError, UniqueViolation
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import adhoc_ingest
import agent
import agent_memory as memory_store   # web_api.agent_memory is a route handler
import answer_cache
import conversation
import durable
import a2a
import api_tools
import api_keys
import human
import knowledge_collections
import mcp_integration
import openapi_tools
import public_api
import secrets_box
import user_memory
import audit
import auth
import identity
import injection
import knowledge_system
import research
from initialization import (blueprint_run, build as setup_build, content as setup_content,
                            evaluation as setup_evaluation, labels as setup_labels, plan as setup_plan, readiness as setup_readiness, sources_run,
                            state as setup_state, supervisor)
import sessions
import source_profiles
import usage_limits
import web_security
import evaluation_archive as evaluation
import embedding_viz
import flows
from flows import golden as golden_sets
from flows import search_eval
import pii
import pipeline_flow
from common import config, storage
from ingestion_info import JOB_INFO, STATUS_LABELS
from pipeline import answer_question, retrieval_view
from qdrant_overview import fetch_qdrant_overview, fetch_research_overview
from retrieval import ignore_stage
from services import PROJECT, SERVICES, SHORTCUTS, statuses
import llm
from summarisation import list_models
from ttl_cache import ttl_cache

WEB_DIR = Path(os.environ.get("WEB_DIR", "/web"))
PLOTLY_JS = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
# Questions kept per session for the embedding-space trajectory.
MAX_HISTORY = 50

# Chat panes: which model plays which role, and the default model for each.
PANES = [
    {"key": "primary", "title": llm.label(), "default": [llm.PROVIDER, None]},
]

_history: dict[str, list[dict]] = {}
_history_lock = threading.Lock()


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _json(data, status: int = 200) -> Response:
    return Response(json.dumps(data, default=_json_default), status_code=status, media_type="application/json")


async def _over_limit(request: Request) -> Response | None:
    """429 when the signed-in user has reached a limit (usage_limits.py),
    else count this question and return None."""
    user = auth.current_user(request)
    refusal = user and await run_in_threadpool(usage_limits.admit, user)
    if not refusal:
        return None
    return Response(json.dumps({"error": refusal.message, "retry_after": refusal.retry_after}), status_code=429,
                    media_type="application/json", headers={"Retry-After": str(refusal.retry_after)})


def _scoped(request: Request, body):
    """The request body with the signed-in user's IDs, never the browser's
    claim: user_id is the user's, and the browser's session ID is kept apart
    per user (<user>:<session>), so no one reaches another user's agent
    memory or conversation context by sending their session ID."""
    user = auth.current_user(request)
    if isinstance(body, dict) and user:
        session = str(body.get("session_id") or "default")[:100]
        body = {**body, "user_id": user["user_id"], "session_id": f"{user['user_id']}:{session}"}
    return body


def _strip_vectors(retrieval: dict) -> dict:
    """Chunks carry their full dense vector (up to 1024 floats each) for the
    plots; the browser only needs the query vector, so drop the rest."""
    def lean(chunks):
        return [{k: v for k, v in c.items() if k != "vector"} for c in chunks]
    return {
        **retrieval,
        "chunks": lean(retrieval["chunks"]),
        "dense_ranking": lean(retrieval["dense_ranking"]),
        "sparse_ranking": lean(retrieval["sparse_ranking"]),
    }


def _by_pane(summaries: list[dict], selections: list) -> list[dict | None]:
    """Summaries only exist for panes with a model; line them up with the panes."""
    if not summaries:
        return [None] * len(selections)
    it = iter(summaries)
    return [next(it) if sel else None for sel in selections]


def _plot_json(fig) -> dict | None:
    return None if fig is None else json.loads(fig.to_json())


# The UI changes with every merge. Static URLs carry a version (a hash of
# web/static's file names, sizes and mtimes), so a changed file gets a URL the
# browser has never cached — relative module imports inherit it. no-cache on
# top makes browsers revalidate (cheap 304 via ETag) rather than reuse.
NO_CACHE = {"Cache-Control": "no-cache"}


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


def _static_version() -> str:
    digest = hashlib.sha1()
    for path in sorted((WEB_DIR / "static").rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:10]


def index(request: Request) -> Response:
    html = (WEB_DIR / "index.html").read_text().replace("/static/", f"/static/{_static_version()}/")
    return HTMLResponse(html, headers=NO_CACHE)


def plotly_js(request: Request) -> Response:
    # Served from the installed plotly package — no CDN, no vendored copy.
    return FileResponse(PLOTLY_JS, media_type="application/javascript")


def app_config(request: Request) -> Response:
    return _json({
        "panes": PANES,
        "user_id": (auth.current_user(request) or {}).get("user_id"),
        "collection": config.COLLECTION_NAME,
        "embeddings": {
            "A": {"model": config.EMBEDDING_MODEL.split("/")[-1], "vector_name": config.DENSE_VECTOR_NAME,
                  "dim": config.EMBEDDING_DIM},
            "B": {"model": "Not configured", "vector_name": "", "dim": 0, "enabled": False},
        },
        "sparse": {"model": config.SPARSE_MODEL_NAME, "vector_name": config.SPARSE_VECTOR_NAME},
    })


def answer_cache_view(request: Request) -> Response:
    return _json(answer_cache.stats())


async def answer_cache_clear(request: Request) -> Response:
    return _json({"cleared": await run_in_threadpool(answer_cache.clear)})


def model_catalogue(request: Request) -> Response:
    """The Models card (R16): which model does what, fallbacks, providers' state, list prices."""
    return _json(llm.catalogue())


def models(request: Request) -> Response:
    found, _ = list_models()
    return _json({"models": found})


def status(request: Request) -> Response:
    up = statuses()
    return _json([
        {"name": s["name"], "icon": s["icon"], "url": s["url"], "up": up.get(s["name"], True)}
        for s in SERVICES
    ])


def _parse_ask(body: dict) -> tuple[str, list, str] | Response:
    raw = body.get("question")
    if not isinstance(raw, str):
        return _json({"error": "question must be a string"}, 400)
    question = raw.strip()
    if not question:
        return _json({"error": "empty question"}, 400)
    # One entry per pane: [provider, model] or null for "no model".
    selections = [tuple(sel) if sel and sel[1] else None for sel in body.get("selections", [])]
    if len(selections) > len(PANES) or any(sel != (llm.PROVIDER, llm.MODEL) for sel in selections if sel):
        return _json({"error": "unsupported model selection"}, 400)
    return question, selections, body.get("session_id") or "anonymous"


NOT_SET_UP = "This knowledge system is being set up. Please try again once it is ready."


async def _not_set_up() -> Response | None:
    """Questions from users wait for setup; the playground and evaluations do
    not (setup evaluates its flow before going live)."""
    if await run_in_threadpool(knowledge_system.is_ready):
        return None
    return _json({"error": NOT_SET_UP, "setup": True}, 409)


async def ask(request: Request) -> Response:
    if blocked := await _not_set_up():
        return blocked
    body = _scoped(request, await request.json())
    parsed = _parse_ask(body)
    if isinstance(parsed, Response):
        return parsed
    if limited := await _over_limit(request):
        return limited
    return await run_in_threadpool(_answer, *parsed, body)


async def ask_stream(request: Request) -> Response:
    """Same answer as /api/ask, as server-sent events: one `stage` event as each
    stage (embedding, retrieval, generation) starts and finishes, then a final
    `result` (the /api/ask body) or `error` event."""
    if blocked := await _not_set_up():
        return blocked
    body = _scoped(request, await request.json())
    parsed = _parse_ask(body)
    if isinstance(parsed, Response):
        return parsed
    if limited := await _over_limit(request):
        return limited
    return _event_stream(lambda emit: _answer_payload(
        *parsed, body, lambda stage, state, **info: emit("stage", {"stage": stage, "state": state, **info})))


HEARTBEAT_SECONDS = 15


def _event_stream(work) -> StreamingResponse:
    """Run work(emit) in a thread and stream what it emits as server-sent
    events, ending with `result` (its return value) or `error`. A comment
    line every HEARTBEAT_SECONDS keeps tunnels and proxies from closing a
    quiet stream. When the browser goes away (Cancel, a closed tab), the
    run's agents are told to stop (agent.CANCELLED) and spend nothing more."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def emit(event: str, data: dict) -> None:
        loop.call_soon_threadsafe(events.put_nowait, (event, data))

    def run() -> None:
        token = agent.CANCELLED.set(stop)
        try:
            emit("result", work(emit))
        except Exception as exc:
            emit("error", {"error": "Unable to complete this request. Please try again."})
        finally:
            agent.CANCELLED.reset(token)

    async def stream():
        task = asyncio.ensure_future(run_in_threadpool(run))
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(events.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, default=_json_default)}\n\n"
                if event in ("result", "error"):
                    break
        finally:
            stop.set()     # finished, or the browser went away: either way nothing more to run
            await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _answer(question: str, selections: list, session_id: str, body: dict) -> Response:
    try:
        return _json(_answer_payload(question, selections, session_id, body))
    except Exception as exc:
        return _json({"error": "Unable to complete this travel request. Please try again."}, 500)


def _answer_payload(question: str, selections: list, session_id: str, body: dict,
                    on_stage=ignore_stage) -> dict:
    question = pii.mask(question)   # the model comparison has no flow: always masked
    result = answer_question(
        question,
        [sel for sel in selections if sel],
        session_id=session_id,
        user_id=body.get("user_id"),
        interaction_id=body.get("interaction_id"),
        on_stage=on_stage,
    )
    retrieval = result["retrieval"]
    alt = retrieval["alt"]
    if result.get("guardrails"):
        return {"question": question, "answer": result["message"], "guardrails": result["guardrails"],
                "retrieval": retrieval, "generations": {"A": _by_pane(result["summarisations"], selections), "B": None},
                "generation_seconds": None, "total": 0, "plots": {"A": None, "B": None}}
    with _history_lock:
        history = _history.setdefault(session_id, [])
        history.append({
            "question": question,
            "vector": retrieval["query_vector"],
            "alt_vector": None if alt["error"] else alt["query_vector"],
        })
        del history[:-MAX_HISTORY]
        snapshot = list(history)

    # One plot per model: 384-dim and 1024-dim vectors are different spaces.
    plot = embedding_viz.build_embedding_plot(snapshot, retrieval["chunks"])
    alt_plot = None if alt["error"] else embedding_viz.build_embedding_plot(
        snapshot, alt["chunks"], "", "alt_vector"
    )
    return {
        "question": question,
        "retrieval": {**_strip_vectors(retrieval), "alt": alt if alt["error"] else _strip_vectors(alt)},
        "generations": {
            "A": _by_pane(result["summarisations"], selections),
            "B": _by_pane(result["alt_summarisations"], selections) if result["alt_summarisations"] else None,
        },
        "generation_seconds": result["generation_seconds"],
        "total": result["total"],
        "plots": {"A": _plot_json(plot), "B": _plot_json(alt_plot)},
    }


async def reset_session(request: Request) -> Response:
    body = _scoped(request, await request.json())
    with _history_lock:
        _history.pop(body.get("session_id"), None)
    return _json({"ok": True})


# ---------- Agent (AgentCore harness) ----------

def agent_info(request: Request) -> Response:
    if request.query_params.get("fresh"):
        agent.describe.clear()
    info = agent.describe()
    try:
        live = agent.live_shape()
    except Exception:          # the chart then draws the built-in flow
        live = None
    return _json({**info, "live": live, "roles": {key: {**role, "prompt": "Restricted to administrator tooling."}
                                  for key, role in info.get("roles", {}).items()}})


def _question(body) -> tuple[tuple | None, Response | None]:
    """(question, session_id, max_iterations) from a run request, or an error."""
    if not isinstance(body, dict):
        return None, _json({"error": "expected a JSON object"}, 400)
    raw = body.get("question")
    if not isinstance(raw, str):
        return None, _json({"error": "question must be a string"}, 400)
    question = raw.strip()
    if not question:
        return None, _json({"error": "empty question"}, 400)
    max_iterations = body.get("max_iterations")
    if max_iterations is not None and (not isinstance(max_iterations, int)
                                       or not 1 <= max_iterations <= agent.MAX_ITERATIONS_LIMIT):
        return None, _json({"error": f"max_iterations must be 1–{agent.MAX_ITERATIONS_LIMIT}"}, 400)
    return (question, body.get("session_id") or "anonymous", max_iterations), None


async def agent_stream(request: Request) -> Response:
    """Agentic RAG, as server-sent events: `stage` events for each model turn,
    search (with its embedding and retrieval stages) and answer text, then a
    `result` with every search's full retrieval and the embedding plot. Runs
    the live flow."""
    if blocked := await _not_set_up():
        return blocked
    body = _scoped(request, await request.json())
    parsed, error = _question(body)
    if error:
        return error
    question, _, max_iterations = parsed
    if limited := await _over_limit(request):
        return limited
    # The server's session, not the browser's: the user's current one, or a
    # new one after sign-in or 30 minutes idle (sessions.py). Kept in the
    # cookie; every question here becomes a turn of its history.
    user_id = auth.current_user(request)["user_id"]
    session_id = await run_in_threadpool(sessions.current, user_id, request.session.get("sid"))
    request.session["sid"] = session_id
    return _event_stream(lambda emit: _ask_live(question, session_id, {**body, "session_id": session_id},
                                                max_iterations, emit))


def _ask_live(question: str, session_id: str, body: dict, max_iterations, emit) -> dict:
    """A user's question: their reply when the session waits for one (R32), a cached
    answer when one fits (answer_cache.py), else the live flow."""
    try:
        waiting = [w for w in human.waiting_for(session_id, body["user_id"]) if w["kind"] == "clarify"]
    except Exception as error:   # the question is still answered
        print(f"could not check for a waiting clarification: {error}", file=sys.stderr)
        waiting = []
    for request in waiting:
        if replied := _reply_live(request, question, session_id, body, emit):
            return replied
    follow_up = bool(conversation.recent_questions(session_id))
    spec, version = agent.live_flow()
    # A flow that asks a human never answers from the cache: every answer gets its approval.
    reusable = answer_cache.eligible_question(question, follow_up) and not any(n.type == "human" for n in spec.nodes)
    if reusable:
        if cached := answer_cache.lookup(question, spec.id, version):
            emit("stage", {"type": "cache", "state": "hit"})
            result = {**cached, "cached": True}
            try:
                sessions.record_turn(session_id, body["user_id"], question, result, None)
            except Exception as error:
                print(f"could not record the session turn: {error}", file=sys.stderr)
            return result
    result = _agent_payload(question, session_id, body, max_iterations, emit, record=True)
    if reusable and result.get("flow") and result.get("status") == "done":
        answer_cache.store(question, result["flow"]["id"], result["flow"]["version"], result)
    return result


def _reply_live(request: dict, reply: str, session_id: str, body: dict, emit) -> dict | None:
    """The user's message answers the clarifying question their run waits on (R32):
    the run goes on with it. None when the request was no longer open."""
    if not human.respond(request["request_id"], {"reply": reply}, body["user_id"]):
        return None
    sessions.settle_waiting(request["run_id"])
    result = agent.resume_run(request["run_id"], retrieval_view, lambda event: emit("stage", event),
                              answer={"reply": reply})
    trace_id = result.pop("trace_id", None)
    try:
        sessions.record_turn(session_id, body["user_id"], pii.mask(reply), result, trace_id)
        conversation.summarise_later(session_id)
    except Exception as error:
        print(f"could not record the session turn: {error}", file=sys.stderr)
    return _shape_agent_result(result)


# ---------- Flows (the agent graph as data) ----------
# Drafts and published versions live in Postgres (flows/store.py). The agent
# still runs the built-in flow; running saved versions comes later.

MAX_FLOW_BYTES = 512_000
MAX_FLOW_NODES = 200


def _live() -> dict:
    return flows.store.live_pointer() or {"flow_id": flows.load_flow().id, "version": 0, "updated_at": None}


def _flow_view(record: dict) -> dict:
    spec = flows.FlowSpec.model_validate(record["flow"])
    runtime = agent._runtime()
    # Where each kind can run ("main", "task"), so the builder can say so before it is dropped.
    components = [{**c, "runs_in": sorted(runtime.impl_states.get(flows.COMPONENTS[c["type"]].impl or "", ()))}
                  for c in flows.components()]
    try:
        red_team = flows.evals.red_team(spec.id)
    except Exception as error:   # the builder still opens
        print(f"red-team results unavailable: {error}", file=sys.stderr)
        red_team = {}
    return {**record, "flow": spec.model_dump(mode="json"), "components": components,
            "issues": flows.validate(spec) + flows.compiler.runtime_issues(spec, agent._runtime()),
            "live": _live(), "red_team": red_team, "red_team_min": RED_TEAM_MIN}


async def _flow_body(request: Request) -> tuple[flows.FlowSpec | None, Response | None]:
    raw = await request.body()
    if len(raw) > MAX_FLOW_BYTES:
        return None, _json({"error": "flow is too large"}, 413)
    try:
        body = json.loads(raw)
        spec = flows.FlowSpec.model_validate(body.get("flow", body) if isinstance(body, dict) else body)
    except ValueError as error:
        return None, _json({"error": "not a valid flow", "issues": [
            {"level": "error", "where": "flow", "message": str(error)[:2000]}]}, 422)
    total = len(spec.nodes) + sum(len(sub.nodes) for sub in spec.subflows.values())
    if total > MAX_FLOW_NODES:
        return None, _json({"error": f"a flow may have at most {MAX_FLOW_NODES} nodes"}, 413)
    return spec, None


def _run_check(spec: flows.FlowSpec) -> None:
    """Runnable: no validation errors, the guardrail policy holds, and it compiles here."""
    try:
        agent.graph_for(spec)
    except flows.FlowError:
        raise
    except Exception as error:
        raise flows.FlowError([{"level": "error", "where": spec.id,
                                "message": f"does not compile: {type(error).__name__}: {error}"}]) from error


def _publish_check(spec: flows.FlowSpec) -> None:
    """Publishable: the same as runnable."""
    _run_check(spec)


def flow_list(request: Request) -> Response:
    live = _live()
    return _json([{**f, "live_version": live["version"] if f["id"] == live["flow_id"] else None}
                  for f in flows.store.list_flows(flows.load_flow())])


async def flow_set_live(request: Request) -> Response:
    """Make a published version (0: the built-in flow) what users get."""
    body = await request.json()
    version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(version, int) or version < 0:
        return _json({"error": "version must be a published version number"}, 400)
    flow_id = request.path_params["flow_id"]
    # Measure before users get it (#62): a version with no finished evaluation goes live
    # only when the admin says so. The built-in flow (version 0) is exempt.
    try:                                 # an unknown version is a 404 before anything else
        await run_in_threadpool(flows.store.get_version, flow_id, version, flows.load_flow())
    except flows.store.FlowNotFound:
        return _json({"error": "unknown version"}, 404)
    red = (await run_in_threadpool(flows.evals.red_team, flow_id)).get(str(version)) if version else None
    if red and red["status"] == "done" and red["rate"] is not None and red["rate"] < RED_TEAM_MIN \
            and not body.get("red_team_ok"):
        return _json({"error": f"v{version} refused {red['met']} of {red['expectations']} red-team questions "
                               f"({round(red['rate'] * 100)}%; going live needs {round(RED_TEAM_MIN * 100)}%). "
                               "Look at the run on Evaluations, or make it live anyway.", "red_team": red}, 409)
    if version and not body.get("unevaluated_ok") and \
            not await run_in_threadpool(flows.evals.latest_evaluation, flow_id, version):
        return _json({"error": f"v{version} has not been evaluated: run a question set on it (Evaluations) "
                               "to see how it answers, or make it live anyway.", "unevaluated": True}, 409)
    try:
        flows.store.set_live(flow_id, version, flows.load_flow(), _run_check)
    except flows.store.FlowNotFound:
        return _json({"error": "unknown version"}, 404)
    except flows.FlowError as invalid:
        return _json({"error": "this version cannot run", "issues": invalid.issues}, 422)
    await run_in_threadpool(answer_cache.clear)   # cached answers came from the version users had
    return _json({"live": _live()})


def _run_target(flow_id: str, version) -> tuple[flows.FlowSpec, object]:
    """(spec, version label) for a playground run: a version number, "draft",
    or None for what the editor shows (draft, else latest published, else built-in)."""
    builtin = flows.load_flow()
    if isinstance(version, int):
        return flows.FlowSpec.model_validate(flows.store.get_version(flow_id, version, builtin)), version
    record = flows.store.get_flow(flow_id, builtin)
    if version == "draft" and record["source"] != "draft":
        raise flows.store.FlowNotFound(f"{flow_id} has no draft")
    label = "draft" if record["source"] == "draft" else record["published_version"]
    return flows.FlowSpec.model_validate(record["flow"]), label


async def flow_run(request: Request) -> Response:
    """The playground: run one flow version on a question, as server-sent
    events (the same public-safe events and result as the Retrieval page)."""
    body = _scoped(request, await request.json())
    parsed, error = _question(body)
    if error:
        return error
    question, session_id, max_iterations = parsed
    version = body.get("version")
    if version is not None and version != "draft" and not (isinstance(version, int) and version >= 0):
        return _json({"error": "version must be a number, \"draft\" or omitted"}, 400)
    try:
        spec, label = _run_target(request.path_params["flow_id"], version)
        _run_check(spec)
    except flows.store.FlowNotFound as missing:
        return _json({"error": f"unknown flow or version: {missing}"}, 404)
    except flows.FlowError as invalid:
        return _json({"error": "this flow cannot run", "issues": invalid.issues}, 422)
    if limited := await _over_limit(request):
        return limited
    return _event_stream(lambda emit: _agent_payload(question, session_id, body, max_iterations, emit,
                                                     (spec, label), "playground"))


RESERVED_FLOW_IDS = {"default", "diff", "templates", "validate"}   # API paths under /api/flows


async def flow_create(request: Request) -> Response:
    spec, error = await _flow_body(request)
    if error:
        return error
    if spec.id in RESERVED_FLOW_IDS:
        return _json({"error": f"{spec.id!r} is reserved; choose another id"}, 400)
    try:
        flows.store.create_flow(spec, flows.load_flow())
    except flows.store.FlowConflict as conflict:
        return _json({"error": str(conflict)}, 409)
    return _json(_flow_view(flows.store.get_flow(spec.id, flows.load_flow())), 201)


def flow_detail(request: Request) -> Response:
    """The flow to edit (draft, else latest published, else built-in), its
    history, the component catalogue the builder draws from, and its issues."""
    flow_id = request.path_params["flow_id"]
    builtin = flows.load_flow()
    try:
        record = flows.store.get_flow(builtin.id if flow_id == "default" else flow_id, builtin)
    except flows.store.FlowNotFound:
        return _json({"error": "unknown flow"}, 404)
    return _json(_flow_view(record))


def flow_version(request: Request) -> Response:
    try:
        spec = flows.store.get_version(request.path_params["flow_id"], request.path_params["version"],
                                       flows.load_flow())
    except flows.store.FlowNotFound:
        return _json({"error": "unknown version"}, 404)
    return _json({"flow": spec})


async def flow_save_draft(request: Request) -> Response:
    spec, error = await _flow_body(request)
    if error:
        return error
    if spec.id != request.path_params["flow_id"]:
        return _json({"error": "the flow's id does not match the URL"}, 400)
    try:
        flows.store.save_draft(spec, flows.load_flow())
    except flows.store.FlowNotFound:
        return _json({"error": "unknown flow"}, 404)
    return _json(_flow_view(flows.store.get_flow(spec.id, flows.load_flow())))


def flow_discard_draft(request: Request) -> Response:
    flow_id = request.path_params["flow_id"]
    flows.store.discard_draft(flow_id)
    try:
        return _json(_flow_view(flows.store.get_flow(flow_id, flows.load_flow())))
    except flows.store.FlowNotFound:
        return _json({"error": "unknown flow"}, 404)


async def flow_publish(request: Request) -> Response:
    flow_id = request.path_params["flow_id"]
    body = await request.json() if await request.body() else {}
    note = body.get("note") if isinstance(body, dict) and isinstance(body.get("note"), str) else ""
    try:
        version = flows.store.publish(flow_id, note, _publish_check)
    except flows.FlowError as invalid:
        return _json({"error": "fix the flow's errors before publishing", "issues": invalid.issues}, 422)
    except flows.store.FlowConflict as conflict:
        return _json({"error": f"cannot publish: {conflict}"}, 409)
    _red_team(flow_id, version)
    return _json({"version": version, **_flow_view(flows.store.get_flow(flow_id, flows.load_flow()))})


RED_TEAM_MIN = float(os.environ.get("RED_TEAM_MIN", "0.9") or 0.9)


def _red_team(flow_id: str, version: int) -> None:
    """Queue the red-team set on a newly published version (R14); off with
    RED_TEAM_ON_PUBLISH=false. A failure here never fails the publish."""
    if os.environ.get("RED_TEAM_ON_PUBLISH", "true").strip().lower() in ("0", "false", "no"):
        return
    try:
        spec, label = _run_target(flow_id, version)
        flows.evals.start(flows.evals.RED_TEAM, flow_id, [(label, spec.model_dump(mode="json"))], _eval_execute)
    except Exception as error:
        print(f"red team not started for {flow_id} v{version}: {error}", file=sys.stderr)


def flow_templates(request: Request) -> Response:
    return _json(flows.templates(flows.load_flow()))


def flow_architectures(request: Request) -> Response:
    """The Agent Builder's gallery of ready-made architectures (#69); the knowledge agents one (R35)
    from this deployment's collections."""
    try:
        collections = knowledge_collections.all_collections()
    except Exception as error:
        print(f"collections unavailable: {error}", file=sys.stderr)
        collections = None
    return _json(flows.architectures.architectures(collections))


async def flow_diff(request: Request) -> Response:
    """What changed from `old` to `new` (two flow JSONs)."""
    raw = await request.body()
    if len(raw) > 2 * MAX_FLOW_BYTES:
        return _json({"error": "flows are too large"}, 413)
    try:
        body = json.loads(raw)
        old, new = flows.FlowSpec.model_validate(body["old"]), flows.FlowSpec.model_validate(body["new"])
    except (ValueError, KeyError, TypeError) as error:
        return _json({"error": f"expected old and new flows: {str(error)[:300]}"}, 422)
    return _json({"changes": flows.diff(old, new)})


def flow_metrics(request: Request) -> Response:
    try:
        days = min(max(int(request.query_params.get("days", "30")), 1), 365)
    except ValueError:
        return _json({"error": "days must be a number"}, 400)
    return _json({"days": days, "versions": flows.store.metrics(request.path_params["flow_id"], days)})


# ---------- Flow evaluations: question sets × flow versions ----------

def _eval_execute(run: dict, index: int, question: dict) -> dict:
    """One question of an evaluation run (on the flow frozen when it was
    queued): the answer users would see, plus numbers."""
    spec = flows.FlowSpec.model_validate(run["spec"])
    version = int(run["version"]) if run["version"].isdigit() else run["version"]
    metrics = {}
    if "{domain}" in question["question"]:   # the red-team set names the flow's own domain
        domain = agent._flow_settings(spec).get("domain") or spec.name
        question = {**question, "question": question["question"].replace("{domain}", domain)}
    # Agent memory on, or off for a comparison run (R27): read and learned alike.
    with memory_store.using((run.get("options") or {}).get("memory", memory_store.USE)):
        result = agent.answer(question["question"], f"eval-{run['id']}-{index}", None, None, lambda event: None,
                              retrieval_view, (spec, version), "eval", metrics.update)
    tokens = (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0)
    return {**metrics, "answer": result.get("answer") or "", "tokens": tokens,
            "seconds": metrics.get("seconds") or result.get("total") or 0}


def eval_sets(request: Request) -> Response:
    return _json(flows.evals.list_sets())


async def eval_set_save(request: Request) -> Response:
    body = await request.json()
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    set_id = str(body.get("id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", set_id):
        return _json({"error": "id: lowercase letters, digits and _; starts with a letter"}, 400)
    try:
        questions = flows.evals.clean_questions(body.get("questions"))
        flows.evals.save_set(set_id, str(body.get("name") or set_id), str(body.get("description") or ""), questions)
    except ValueError as error:
        return _json({"error": str(error)}, 400)
    return _json(flows.evals.get_set(set_id))


async def golden_start(request: Request) -> Response:
    """Write a golden question set from the knowledge base, in the background."""
    body = await request.json()
    try:
        return _json(golden_sets.start(int(body.get("count") or 0)), 202)
    except (TypeError, ValueError) as error:
        return _json({"error": str(error)}, 409 if "already" in str(error) else 400)


def golden_status(request: Request) -> Response:
    return _json({"run": golden_sets.status(), "counts": list(golden_sets.COUNTS)})


def search_quality(request: Request) -> Response:
    """Search quality (R20): the test sets, the saved runs, the job in progress."""
    return _json({"job": search_eval.status(), "sets": search_eval.list_sets(), "runs": search_eval.list_runs(),
                  "counts": list(search_eval.COUNTS), "current": search_eval.current_settings(),
                  "reranker": {"model": search_eval.reranker.MODEL, "candidates": search_eval.reranker.CANDIDATES}})


async def search_quality_generate(request: Request) -> Response:
    """Write a search test set from the knowledge base, in the background."""
    body = await request.json()
    try:
        return _json(search_eval.generate(int(body.get("count") or 0)), 202)
    except (TypeError, ValueError) as error:
        return _json({"error": str(error)}, 409 if "already" in str(error) else 400)


def search_quality_set(request: Request) -> Response:
    found = search_eval.get_set(request.path_params["set_id"])
    return _json(found) if found else _json({"error": "no such set"}, 404)


async def search_quality_set_change(request: Request) -> Response:
    """Add a real question (with the page that answers it) or remove one by index."""
    body = await request.json()
    set_id = request.path_params["set_id"]
    try:
        if body.get("remove") is not None:
            return _json(search_eval.remove_question(set_id, int(body["remove"])))
        return _json(search_eval.add_question(set_id, body.get("question"), body.get("source")))
    except (TypeError, ValueError) as error:
        return _json({"error": str(error)}, 400)


def search_quality_set_delete(request: Request) -> Response:
    search_eval.delete_set(request.path_params["set_id"])
    return _json({"ok": True})


async def search_quality_run(request: Request) -> Response:
    """Score a set over every search setting (no model calls), in the background."""
    body = await request.json()
    try:
        return _json(search_eval.start_run(str(body.get("set_id") or ""), rerank=bool(body.get("rerank"))), 202)
    except LookupError as error:
        return _json({"error": str(error)}, 404)
    except ValueError as error:
        return _json({"error": str(error)}, 409 if "already" in str(error) else 400)


def search_quality_run_detail(request: Request) -> Response:
    found = search_eval.get_run(request.path_params["run_id"])
    return _json(found) if found else _json({"error": "no such run"}, 404)


def eval_archive_sets(request: Request) -> Response:
    """Question sets in the archived (pre-AWS) evaluation runs, to import."""
    out = []
    for run in evaluation.list_runs():
        full = evaluation.get(run["id"]) or {}
        gold = full.get("gold_answers") or []
        out.append({"id": run["id"], "name": run["name"], "created_at": run["created_at"],
                    "questions": [{"question": q["question"],
                                   "expected": (gold[i] or {}).get("answer") or "" if i < len(gold) else ""}
                                  for i, q in enumerate(full.get("questions") or [])]})
    return _json(out)


def eval_runs(request: Request) -> Response:
    return _json(flows.evals.list_runs(request.query_params.get("flow_id") or None))


async def eval_start(request: Request) -> Response:
    """Queue a question set against 1–3 versions of a flow (each checked to run first)."""
    body = await request.json()
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    versions = body.get("versions")
    if (not isinstance(versions, list) or not 1 <= len(versions) <= flows.evals.MAX_VERSIONS
            or not all(v == "draft" or (isinstance(v, int) and v >= 0) for v in versions)):
        return _json({"error": f"versions: 1–{flows.evals.MAX_VERSIONS} version numbers or \"draft\""}, 400)
    flow_id = str(body.get("flow_id") or "")
    try:
        targets = []
        for version in versions:
            spec, label = _run_target(flow_id, version)
            _run_check(spec)
            targets.append((label, spec.model_dump(mode="json")))
        memory = {"on": (True,), "off": (False,), "both": (True, False)}.get(body.get("memory") or "on")
        if memory is None:
            return _json({"error": "memory: on, off or both"}, 400)
        ids = flows.evals.start(str(body.get("set_id") or ""), flow_id, targets, _eval_execute, memory)
    except (flows.store.FlowNotFound, LookupError) as missing:
        return _json({"error": f"not found: {missing}"}, 404)
    except flows.FlowError as invalid:
        return _json({"error": "a version cannot run", "issues": invalid.issues}, 422)
    return _json({"runs": ids}, 201)


def eval_run_detail(request: Request) -> Response:
    run = flows.evals.get_run(request.path_params["run_id"])
    return _json(run) if run else _json({"error": "unknown evaluation run"}, 404)


def eval_cancel(request: Request) -> Response:
    flows.evals.cancel(request.path_params["run_id"])
    return _json({"ok": True})


async def flow_validate(request: Request) -> Response:
    spec, error = await _flow_body(request)
    if error:
        detail = json.loads(error.body)
        return _json({"issues": detail.get("issues") or [{"level": "error", "where": "flow", "message": detail["error"]}]})
    return _json({"issues": flows.validate(spec) + flows.compiler.runtime_issues(spec, agent._runtime())})


def agent_session(request: Request) -> Response:
    session_id = request.query_params.get("session_id", "")
    if not session_id:
        return _json({"error": "session_id is required"}, 400)
    return _json(agent.session_view(_scoped(request, {"session_id": session_id})["session_id"]))


def agent_memory(request: Request) -> Response:
    return _json({"error": "Raw agent memory is restricted to administrator tooling."}, 403)


async def agent_resume(request: Request) -> Response:
    """Approve or reject the sources a paused run proposed, and continue it,
    as server-sent events like /api/agent/stream."""
    body = await request.json()
    run_id, decisions = body.get("run_id"), body.get("decisions")
    if not isinstance(run_id, str) or not isinstance(decisions, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("approved"), bool) for v in decisions.values()):
        return _json({"error": "run_id and decisions {interrupt id: {approved, urls}} are required"}, 400)
    return _event_stream(lambda emit: _shape_agent_result(
        agent.resume(run_id, decisions, lambda event: emit("stage", event))))


def _agent_payload(question: str, session_id: str, body: dict, max_iterations: int | None, emit,
                   flow: tuple | None = None, source: str = "users", record: bool = False) -> dict:
    """record: keep the question and what the user saw in the session's
    history (the Retrieval page; the playground and evaluations are tests)."""
    metrics = {}
    try:
        result = agent.answer(question, session_id, body.get("user_id"), max_iterations,
                              lambda event: emit("stage", event), retrieval_view, flow, source, metrics.update,
                              **({"trace_metadata": body["trace_metadata"]} if body.get("trace_metadata") else {}))
    finally:   # a failed run still spent its tokens
        usage_limits.charge(body.get("user_id"),
                            (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0))
    trace_id = result.pop("trace_id", None)   # server-side only
    if record:
        try:
            # The question as the agents saw it: personal data masked (pii.py).
            asked = result.get("question") or question
            turn = sessions.record_turn(session_id, body["user_id"], asked, result, trace_id)
            conversation.summarise_later(session_id)   # a long session's older turns, folded (R26)
            if ((result.get("guardrails") or {}).get("input") or {}).get("decision") == "ALLOW":
                # Lasting facts the user told us, only if they opted in (R28).
                user_memory.learn_later(body["user_id"], asked, {"session_id": session_id, **turn})
        except Exception as error:   # the user still gets the answer
            print(f"could not record the session turn: {error}", file=sys.stderr)
    shaped = _shape_agent_result(result)
    if body.get("keep_trace"):   # for an A2A reply, which links the caller's trace to this one (R36)
        shaped["trace_id"] = trace_id
    return shaped


# ---------- Users (identity.py; manage_users) ----------

def user_list(request: Request) -> Response:
    users = identity.list_users()
    try:
        usage = usage_limits.overview(users)
    except Exception as error:   # the list still shows without the numbers
        print(f"usage overview unavailable: {error}", file=sys.stderr)
        usage = {}
    return _json({"users": [identity.public(u) | {"created_by": u["created_by"], "updated_by": u["updated_by"],
                                                  "updated_at": u["updated_at"], "usage": usage.get(u["user_id"])}
                            for u in users],
                  "permissions": identity.PERMISSIONS, "roles": identity.ROLES, "me": auth.current_user(request)["user_id"],
                  "limit_defaults": {"admin": usage_limits.defaults("admin"), "others": usage_limits.defaults("user"),
                                     "global_tokens_per_day": usage_limits.global_tokens_per_day()}})


async def user_create(request: Request) -> Response:
    body = await request.json()
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    try:
        user = await run_in_threadpool(identity.create_user, body.get("first_name", ""), body.get("email", ""),
                                       body.get("role", ""), body.get("permissions"), auth.current_user(request)["user_id"])
    except UniqueViolation:
        return _json({"error": "A user with this email already exists"}, 409)
    except ValueError as error:
        return _json({"error": str(error)}, 400)
    return _json({"user": identity.public(user)}, 201)


async def user_update(request: Request) -> Response:
    """Change first_name, role, permissions or status (deactivate rather than
    delete), or the user's usage limits."""
    body = await request.json()
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    actor = auth.current_user(request)
    user_id = request.path_params["user_id"]
    changes = {k: body[k] for k in ("first_name", "role", "permissions", "status") if k in body}
    try:
        if "limits" in body:   # {"per_minute", "tokens_per_day"}: null keeps the role's default
            if not isinstance(body["limits"], (dict, type(None))):
                raise ValueError("limits must be an object or null")
            if not await run_in_threadpool(identity.get_user, user_id):
                raise LookupError(user_id)
            await run_in_threadpool(usage_limits.set_override, user_id, body["limits"], actor["user_id"])
        if not changes:
            user = await run_in_threadpool(identity.get_user, user_id)
            if not user:
                raise LookupError(user_id)
            return _json({"user": identity.public(user)})
        await run_in_threadpool(identity.guard_change, actor, user_id, changes)
        user = await run_in_threadpool(identity.update_user, user_id, changes, actor["user_id"])
    except LookupError:
        return _json({"error": "No such user"}, 404)
    except ValueError as error:
        return _json({"error": str(error)}, 400)
    return _json({"user": identity.public(user)})


# ---------- Audit log (audit.py; manage_users or manage_settings) ----------

def _audit_filters(request: Request) -> dict:
    q = request.query_params
    return {"user_id": q.get("user") or None, "action": q.get("action") or None, "since": q.get("since") or None,
            "until": q.get("until") or None, "text": (q.get("q") or "").strip()[:100] or None}


def audit_list(request: Request) -> Response:
    try:
        filters = _audit_filters(request)
        rows, actions = audit.entries(**filters, limit=int(request.query_params.get("limit") or 500))
    except (ValueError, DataError) as error:
        return _json({"error": f"bad filter: {error}"}, 400)
    return _json({"entries": rows, "actions": actions, "users": [
        {"user_id": u["user_id"], "first_name": u["first_name"]} for u in identity.list_users()]})


def audit_csv(request: Request) -> Response:
    import csv
    import io
    try:
        rows, _ = audit.entries(**_audit_filters(request), limit=5000)
    except (ValueError, DataError) as error:
        return _json({"error": f"bad filter: {error}"}, 400)
    out = io.StringIO()
    columns = ["at", "user_name", "user_id", "action", "method", "path", "target", "status", "details", "ip", "request_id"]
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([json.dumps(row[c]) if c == "details" else ("" if row[c] is None else row[c]) for c in columns])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="audit-log-{time.strftime("%Y%m%d-%H%M")}.csv"'})


# ---------- Agent memory (agent_memory.py) ----------

def agent_memory_view(request: Request) -> Response:
    """What the agents have learned across runs: outcomes only, never anyone's conversation."""
    return _json({"agents": memory_store.AGENTS, "kinds": memory_store.KINDS, "entries": memory_store.entries(),
                  "in_use": memory_store.USE, "half_life_days": memory_store.HALF_LIFE_DAYS,
                  "can_manage": auth.allowed(auth.current_user(request), "manage_agents")})


async def agent_memory_entry(request: Request) -> Response:
    """Pin, unpin or forget one entry (R27)."""
    body = await request.json()
    if not isinstance(body, dict) or body.get("action") not in ("pin", "unpin", "forget"):
        return _json({"error": "action is pin, unpin or forget"}, 400)
    key = (str(body.get("agent") or ""), str(body.get("kind") or ""), str(body.get("key") or ""))
    done = (memory_store.forget(*key) if body["action"] == "forget"
            else memory_store.pin(*key, body["action"] == "pin"))
    return _json({"ok": True}) if done else _json({"error": "no such entry"}, 404)


# ---------- Sessions (sessions.py) ----------

def _may_see(user: dict, session: dict) -> bool:
    return session["user_id"] == user["user_id"] or identity.can(user, "view_sessions")


def session_list(request: Request) -> Response:
    """?user=me (default) | all | <user_id>; others' sessions need view_sessions."""
    user = auth.current_user(request)
    who = request.query_params.get("user", "me")
    see_all = identity.can(user, "view_sessions")
    if who != "me" and who != user["user_id"] and not see_all:
        return _json({"error": "You don't have permission to see other users' sessions"}, 403)
    target = user["user_id"] if who == "me" else (None if who == "all" else who)
    return _json({"sessions": sessions.list_sessions(target), "can_view_all": see_all,
                  "retention_days": sessions.RETENTION_DAYS})


# ---------- Durable runs (durable.py, R30) ----------

def _resume_and_record(run_id: str, answer: dict | None = None) -> dict:
    """Resume a run; an Ask-page one's answer joins its session, as if it had finished.
    answer: a person's decision for a run waiting on them (R32); its answer replaces the waiting turn."""
    result = agent.resume_run(run_id, retrieval_view, answer=answer)
    record = durable.get(run_id) or {}
    trace_id = result.pop("trace_id", None)
    if record.get("source") == "users" and record.get("session_id") and record.get("user_id"):
        try:
            if answer is None or not sessions.complete_waiting(run_id, result, trace_id):
                sessions.record_turn(record["session_id"], record["user_id"], record["asked"], result, trace_id)
            conversation.summarise_later(record["session_id"])
        except Exception as error:
            print(f"could not record the resumed turn: {error}", file=sys.stderr)
    return result


def runs_list(request: Request) -> Response:
    """Monitoring → Runs: every run's status, steps, time and tokens (view_sessions: questions show)."""
    return _json({"runs": durable.list_runs(source=request.query_params.get("source") or None),
                  "durable": durable.durable(), "retention_days": durable.RETENTION_DAYS,
                  "can_manage": auth.allowed(auth.current_user(request), "manage_agents")})


async def runs_resume(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    if not await run_in_threadpool(durable.resumable, run_id):
        return _json({"error": "this run can't be resumed (finished, cancelled, or nothing was saved)"}, 409)
    threading.Thread(target=_resume_and_record, args=(run_id,), name="durable-resume", daemon=True).start()
    return _json({"ok": True, "status": "resuming"}, 202)


async def runs_cancel(request: Request) -> Response:
    done = await run_in_threadpool(durable.cancel, request.path_params["run_id"])
    if done:
        await run_in_threadpool(human.cancel_for, request.path_params["run_id"])
    return _json({"ok": True}) if done else _json({"error": "nothing to cancel"}, 409)


# ---------- The versioned API and its keys (public_api.py, api_keys.py, R46) ----------

async def v1_ask(request: Request) -> Response:
    """Ask the live flow; the answer with its sources and answer check (public_api.answer)."""
    if blocked := await _not_set_up():
        return blocked
    body = await request.json()
    parsed, error = _question(body)
    if error:
        return error
    question, _, max_iterations = parsed
    if limited := await _over_limit(request):
        return limited
    user_id = auth.current_user(request)["user_id"]
    wanted = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    # A conversation of its own: the owner's browser session is left alone.
    session_id = await run_in_threadpool(sessions.current, user_id, wanted, False)
    result = await run_in_threadpool(_ask_live, question, session_id, {"user_id": user_id, "session_id": session_id},
                                     max_iterations, lambda *event: None)
    return _json(public_api.answer(result, session_id))


def v1_sessions(request: Request) -> Response:
    return _json({"sessions": sessions.list_sessions(auth.current_user(request)["user_id"])})


def v1_session(request: Request) -> Response:
    found = sessions.get_session(request.path_params["session_id"])
    if not found or found["user_id"] != auth.current_user(request)["user_id"]:
        return _json({"error": "No such session"}, 404)
    return _json({"session": found})


def v1_documents(request: Request) -> Response:
    return _json({"documents": adhoc_ingest.list_adhoc()})


def v1_openapi(request: Request) -> Response:
    return Response(public_api.spec_json(), media_type="application/json")


def v1_docs(request: Request) -> Response:
    return HTMLResponse(public_api.docs_page())


def a2a_card(request: Request) -> Response:
    """The A2A agent card (R36): the live flow as an agent other systems call."""
    spec, _ = agent.live_flow()
    settings = agent._flow_settings(spec)
    brief = knowledge_collections.expand(settings.get("brief") or flows.registry.UK_RAIL_BRIEF, everyone=True)
    return _json(a2a.card(str(request.base_url), f"IngenMind ({settings.get('domain') or 'assistant'})",
                          f"Answers questions from its knowledge base with cited sources. {brief}"[:800]))


async def a2a_rpc(request: Request) -> Response:
    """A2A JSON-RPC (R36): message/send runs the live flow (guardrails and all) as the key's owner and
    replies with a task holding the cited answer; tasks/get returns one of the caller's tasks."""
    try:
        body = await request.json()
    except ValueError:
        return _json(a2a.response(None, error=a2a.A2AError(a2a.PARSE_ERROR, "Parse error")))
    rpc_id = body.get("id") if isinstance(body, dict) else None
    try:
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            raise a2a.A2AError(a2a.INVALID_REQUEST, "Invalid request")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        user_id = auth.current_user(request)["user_id"]
        if body["method"] == "message/send":
            return _json(a2a.response(rpc_id, await _a2a_send(request, user_id, params.get("message"))))
        if body["method"] == "tasks/get":
            return _json(a2a.response(rpc_id, a2a.TASKS.get(user_id, str(params.get("id") or ""))))
        if body["method"] == "tasks/cancel":
            raise a2a.A2AError(-32002, "Task cannot be canceled: tasks here finish before the reply")
        raise a2a.A2AError(a2a.METHOD_NOT_FOUND, f"Method not found: {body['method']}")
    except a2a.A2AError as error:
        return _json(a2a.response(rpc_id, error=error))


async def _a2a_send(request: Request, user_id: str, message) -> dict:
    text = a2a.message_text(message)
    if await _not_set_up():
        raise a2a.A2AError(a2a.INTERNAL, NOT_SET_UP)
    if limited := await _over_limit(request):
        raise a2a.A2AError(-32000, json.loads(limited.body)["error"])
    context = message.get("contextId") if isinstance(message.get("contextId"), str) else None
    session_id = await run_in_threadpool(sessions.current, user_id, context, False)
    caller = (message.get("metadata") or {}).get(a2a.TRACE_KEY)
    body = {"user_id": user_id, "session_id": session_id, "keep_trace": True,
            "trace_metadata": {"a2a_caller_trace": str(caller)[:64]} if caller else None}
    result = await run_in_threadpool(_ask_live, text, session_id, body, None, lambda *event: None)
    item = a2a.task(public_api.answer(result, session_id), session_id, message, result.get("trace_id"))
    a2a.TASKS.put(user_id, item)
    return item


async def mcp_endpoint(request: Request) -> Response:
    """IngenMind as an MCP server (R38; Streamable HTTP, JSON replies, no server-sent stream): tools
    search_knowledge_base and ask, as the API key's owner (ask scope)."""
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    try:
        body = await request.json()
    except ValueError:
        return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    if not isinstance(body, dict):
        return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}, 400)
    if "id" not in body:                                    # a notification: accepted, no reply
        return Response(status_code=202)
    rpc_id, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    reply = lambda result: _json({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    if method == "initialize":
        return reply(mcp_integration.server_initialize(params))
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": mcp_integration.SERVER_TOOLS})
    if method != "tools/call":
        return _json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    name, args = params.get("name"), params.get("arguments") or {}
    user_id = auth.current_user(request)["user_id"]
    if name == "search_knowledge_base":
        query = str(args.get("query") or "").strip()[:500]
        if not query:
            return reply(mcp_integration.text_result("query is required", error=True))
        top_k = args.get("top_k") if isinstance(args.get("top_k"), int) and 1 <= args.get("top_k") <= 10 else 5
        return reply(await run_in_threadpool(_mcp_search, query, top_k, user_id))
    if name == "ask":
        question = str(args.get("question") or "").strip()
        if not question:
            return reply(mcp_integration.text_result("question is required", error=True))
        if await _not_set_up():
            return reply(mcp_integration.text_result(NOT_SET_UP, error=True))
        if limited := await _over_limit(request):
            return reply(mcp_integration.text_result(json.loads(limited.body)["error"], error=True))
        session_id = await run_in_threadpool(sessions.current, user_id, None, False)
        result = await run_in_threadpool(_ask_live, question, session_id, {"user_id": user_id, "session_id": session_id},
                                         None, lambda *event: None)
        sources = "\n".join(f"[{','.join(map(str, s['cited']))}] {s['url']}" for s in result.get("sources") or [])
        return reply(mcp_integration.text_result((result.get("answer") or "") + (f"\n\nSources:\n{sources}" if sources else "")))
    return _json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})


def _mcp_search(query: str, top_k: int, user_id: str) -> dict:
    """Passages for an MCP client, only from the collections this user may see (R35)."""
    within = knowledge_collections.scope(user_id)
    if within is False:
        return mcp_integration.text_result("No knowledge base is available to you.", error=True)
    from retrieval import hybrid_search
    found = hybrid_search(query, top_k=top_k, filters={"collection": within} if within else None)
    chunks = found["rankings"]["fused"]
    if not chunks:
        return mcp_integration.text_result("No passages matched.")
    lines = [f"[{i}] {c['source_url']}\n{' '.join(c['text'].split())[:1200]}" for i, c in enumerate(chunks, 1)
             if not injection.flags(c["text"])]
    return mcp_integration.text_result("\n\n".join(lines) or "No passages matched.")


def integrations(request: Request) -> Response:
    return _json({"servers": mcp_integration.servers(), "secrets": secrets_box.available(),
                  "api_tools": [openapi_tools.public(r) for r in openapi_tools.rows()]})


async def _integration_change(request: Request, change) -> Response:
    try:
        body = await request.json() if request.method in ("POST", "PUT") else {}
        await run_in_threadpool(change, body if isinstance(body, dict) else {})
    except (mcp_integration.McpError, openapi_tools.OpenApiImportError) as error:
        return _json({"error": str(error)}, 400)
    return integrations(request)


async def openapi_preview(request: Request) -> Response:
    """A spec's operations, to choose from (R39)."""
    body = await request.json()
    try:
        spec = await run_in_threadpool(openapi_tools.load, str(body.get("spec_url") or ""), str(body.get("text") or ""))
    except openapi_tools.OpenApiImportError as error:
        return _json({"error": str(error)}, 400)
    return _json({"title": (spec.get("info") or {}).get("title") or "", "base_url": openapi_tools.base_url(spec, str(body.get("spec_url") or "")),
                  "operations": openapi_tools.operations(spec)})


async def openapi_import(request: Request) -> Response:
    return await _integration_change(request, lambda b: openapi_tools.import_operations(
        str(b.get("spec_url") or ""), [o for o in b.get("operations") or [] if isinstance(o, str)],
        str(b.get("prefix") or ""), str(b.get("base_url") or ""),
        b.get("auth") if isinstance(b.get("auth"), dict) else None, str(b.get("text") or "")))


async def openapi_remove(request: Request) -> Response:
    return await _integration_change(request, lambda b: openapi_tools.remove(request.path_params["name"]))


async def openapi_test(request: Request) -> Response:
    """Try an imported tool (R39) with these arguments, through the same guardrails as an agent's call."""
    body = await request.json()
    name, args = request.path_params["name"], body.get("args") if isinstance(body.get("args"), dict) else {}
    tool = api_tools.get(name)
    if tool is None:
        return _json({"error": "No such tool"}, 404)
    if tool.side_effects:
        return _json({"error": "This tool changes things outside the app, so it isn't tried from here"}, 409)
    if reason := api_tools.check(name, args):
        return _json({"error": f"Refused by the tool guardrail: {reason}"}, 400)
    return _json(await run_in_threadpool(api_tools.run, name, args))


async def integration_add(request: Request) -> Response:
    return await _integration_change(request, lambda b: mcp_integration.add(
        str(b.get("server_id") or ""), b.get("name"), str(b.get("url") or ""),
        b.get("headers") if isinstance(b.get("headers"), dict) else None))


async def integration_refresh(request: Request) -> Response:
    return await _integration_change(request, lambda b: mcp_integration.refresh(request.path_params["server_id"]))


async def integration_enable(request: Request) -> Response:
    return await _integration_change(request, lambda b: mcp_integration.enable(
        request.path_params["server_id"], [t for t in b.get("tools") or [] if isinstance(t, str)]))


async def integration_remove(request: Request) -> Response:
    return await _integration_change(request, lambda b: mcp_integration.remove(request.path_params["server_id"]))


def my_keys(request: Request) -> Response:
    user = auth.current_user(request)
    return _json({"keys": api_keys.list_keys(user["user_id"]), "scopes": api_keys.allowed_scopes(user),
                  "max_days": api_keys.MAX_DAYS})


async def my_key_create(request: Request) -> Response:
    body = await request.json()
    try:
        key, record = await run_in_threadpool(api_keys.create, auth.current_user(request), body.get("name"),
                                              body.get("scopes"), body.get("days", 90))
    except api_keys.ApiKeyError as error:
        return _json({"error": str(error)}, 400)
    return _json({"key": key, "record": record}, 201)   # the key is shown this once


def my_key_revoke(request: Request) -> Response:
    done = api_keys.revoke(request.path_params["key_id"], auth.current_user(request)["user_id"])
    return _json({"ok": True}) if done else _json({"error": "No such active key"}, 404)


def keys_list(request: Request) -> Response:
    names = {u["user_id"]: u.get("first_name") or u["email"] for u in identity.list_users()}
    return _json({"keys": [{**k, "owner": names.get(k["user_id"], k["user_id"])} for k in api_keys.list_keys()]})


def key_revoke(request: Request) -> Response:
    done = api_keys.revoke(request.path_params["key_id"])
    return _json({"ok": True}) if done else _json({"error": "No such active key"}, 404)


# ---------- Collections (several knowledge bases, knowledge_collections.py, R35) ----------

def _collections_view(request: Request) -> dict:
    collections = knowledge_collections.all_collections()
    sources = []
    for dataset in ("curated", "research"):
        try:
            for row in storage.source_summary(storage.get_client(dataset)):
                payload = row["payload"] or {}
                sources.append({"url": payload.get("source_url"), "title": payload.get("title") or "",
                                "chunks": row["chunks"], "research": dataset == "research"})
        except Exception as error:   # no table yet: nothing ingested there
            print(f"sources unavailable ({dataset}): {error}", file=sys.stderr)
    of = knowledge_collections.of_sources(s["url"] for s in sources)
    for source in sources:
        source["collection"] = of.get(source["url"], knowledge_collections.MAIN)
    count = {}
    for source in sources:
        count[source["collection"]] = count.get(source["collection"], 0) + 1
    return {"collections": [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.items()},
                             "sources": count.get(c["collection_id"], 0)} for c in collections],
            "sources": sources, "roles": list(identity.ROLES),
            "can_manage": auth.allowed(auth.current_user(request), "manage_knowledge")}


async def collections_list(request: Request) -> Response:
    return _json(await run_in_threadpool(_collections_view, request))


async def _collection_change(request: Request, change) -> Response:
    try:
        body = await request.json() if request.method != "DELETE" else {}
        await run_in_threadpool(change, body)
    except knowledge_collections.CollectionError as error:
        return _json({"error": str(error)}, 400)
    return _json(await run_in_threadpool(_collections_view, request))


async def collections_create(request: Request) -> Response:
    return await _collection_change(request, lambda b: knowledge_collections.create(
        str(b.get("collection_id") or ""), b.get("name"), b.get("description"), b.get("roles") or []))


async def collections_update(request: Request) -> Response:
    return await _collection_change(request, lambda b: knowledge_collections.update(
        request.path_params["collection_id"], b.get("name"), b.get("description"), b.get("roles") or []))


async def collections_delete(request: Request) -> Response:
    return await _collection_change(request, lambda b: knowledge_collections.delete(request.path_params["collection_id"]))


async def collections_assign(request: Request) -> Response:
    """Move sources into this collection ("main": back to the main one)."""
    def change(body):
        urls = body.get("urls")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise knowledge_collections.CollectionError("urls must be a list of source URLs")
        knowledge_collections.assign(urls, request.path_params["collection_id"])
    return await _collection_change(request, change)


# ---------- Approvals (Ask a human, human.py, R32) ----------

def approvals_list(request: Request) -> Response:
    """Monitoring → Approvals: answers and actions waiting for a person, then the latest decided."""
    return _json({"requests": [human.public(r) for r in human.inbox()]})


async def approvals_decide(request: Request) -> Response:
    """Approve or reject one request; the run goes on in the background with the decision."""
    body = await request.json()
    if not isinstance(body.get("approved"), bool):
        return _json({"error": "approved (true or false) is required"}, 400)
    actions = body.get("actions")
    if actions is not None and not (isinstance(actions, list) and all(isinstance(a, int) for a in actions)):
        return _json({"error": "actions must be a list of action numbers"}, 400)
    decision = {"approved": body["approved"], "note": str(body.get("note") or "")[:human.NOTE_CHARS],
                **({"actions": actions} if actions is not None else {})}
    request_id = request.path_params["request_id"]
    found = await run_in_threadpool(human.get, request_id)
    if not found or found["kind"] != "approve":
        return _json({"error": "No such approval"}, 404)
    done = await run_in_threadpool(human.respond, request_id, decision, auth.current_user(request)["user_id"])
    if not done:
        return _json({"error": "This was already decided, timed out or cancelled"}, 409)
    threading.Thread(target=_resume_and_record, args=(done["run_id"], decision), name="human-resume",
                     daemon=True).start()
    return _json({"ok": True, "status": "resuming"}, 202)


def _expire_human_requests() -> None:
    """Every minute: requests nobody answered in time go on with the safe default."""
    while True:
        time.sleep(60)
        try:
            for request in human.expire_due():
                try:
                    _resume_and_record(request["run_id"], {"expired": True})
                except Exception as error:
                    print(f"could not resume expired run {request['run_id']}: {error}", file=sys.stderr)
        except Exception as error:   # the database may be restarting: try again next minute
            print(f"human request expiry skipped: {error}", file=sys.stderr)


# ---------- The user's own long-term memory (user_memory.py, R28) ----------

def _fact_view(f: dict) -> dict:
    return {"id": f["fact_id"], "text": f["text"], "source": f["source"], "created_at": f["created_at"],
            "updated_at": f["updated_at"]}


def my_memory(request: Request) -> Response:
    user = auth.current_user(request)
    return _json({"opted_in": user_memory.opted_in(user["user_id"]), "max": user_memory.MAX_FACTS,
                  "facts": [_fact_view(f) for f in user_memory.facts(user["user_id"])]})


async def my_memory_consent(request: Request) -> Response:
    """Opt in or out; out deletes every fact. Recorded in the audit log."""
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("opted_in"), bool):
        return _json({"error": "opted_in: true or false"}, 400)
    user = auth.current_user(request)
    await run_in_threadpool(user_memory.set_opted_in, user["user_id"], body["opted_in"])
    await run_in_threadpool(audit.record, "memory_consent", user, method="PUT", path="/api/me/memory",
                            details={"opted_in": body["opted_in"]})
    return my_memory(request)


async def my_memory_fact(request: Request) -> Response:
    """PUT {text}: edit one fact; DELETE: delete it."""
    user = auth.current_user(request)
    fact_id = request.path_params["fact_id"]
    if request.method == "DELETE":
        found = await run_in_threadpool(user_memory.delete, user["user_id"], fact_id)
    else:
        body = await request.json()
        try:
            found = await run_in_threadpool(user_memory.edit, user["user_id"], fact_id, (body or {}).get("text"))
        except ValueError as error:
            return _json({"error": str(error)}, 400)
    return my_memory(request) if found else _json({"error": "no such fact"}, 404)


async def my_memory_clear(request: Request) -> Response:
    await run_in_threadpool(user_memory.delete, auth.current_user(request)["user_id"])
    return my_memory(request)


async def session_new(request: Request) -> Response:
    """Start a new conversation (R26): the current session ends, so the next
    question starts a fresh one, with no earlier turns as context."""
    user = auth.current_user(request)
    await run_in_threadpool(sessions.end, user["user_id"], request.session.get("sid"))
    request.session.pop("sid", None)
    return _json({"ok": True})


def session_current(request: Request) -> Response:
    """This user's current session (to show its conversation), or none when
    the next question would start a new one."""
    user = auth.current_user(request)
    sid = request.session.get("sid")
    found = sessions.get_session(sid) if sid else None
    if not found or found["user_id"] != user["user_id"] or found["status"] != "active" \
            or (datetime.now(found["last_activity"].tzinfo) - found["last_activity"]).total_seconds() \
            > sessions.IDLE_MINUTES * 60:
        return _json({"session": None})
    return _json({"session": found})


def session_detail(request: Request) -> Response:
    found = sessions.get_session(request.path_params["session_id"])
    if not found or not _may_see(auth.current_user(request), found):
        return _json({"error": "No such session"}, 404)   # others' sessions are not acknowledged
    return _json({"session": found})


def _shape_agent_result(result: dict) -> dict:
    """The browser needs no chunk vectors (no page plots them any more)."""
    return {**result, "searches": [{**s, "retrieval": _strip_vectors(s["retrieval"])} for s in result["searches"]]}


# ---------- Home ----------

def home(request: Request) -> Response:
    up = statuses()
    links = lambda items: [{"icon": i, "name": n, "url": u, "description": d} for i, n, u, d in items]
    return _json({
        "services": [{**{k: v for k, v in s.items() if k != "health"}, "up": up.get(s["name"], True)}
                     for s in SERVICES],
        "shortcuts": links(SHORTCUTS),
        "project": links(PROJECT),
    })


# ---------- Knowledge system ----------

def knowledge_system_status(request: Request) -> Response:
    """Whether the knowledge system is set up (everyone); its counts, recent
    events and what a reset deletes (admins with manage_settings)."""
    status = knowledge_system.status()
    out = {k: status[k] for k in ("state", "origin", "set_up_at")}
    can_manage = auth.allowed(auth.current_user(request), "manage_settings")
    if can_manage:
        counts = knowledge_system.counts()
        out.update(status, counts=counts, events=knowledge_system.events(),
                   emptied=[{"table": t, "label": label, "rows": counts[t]} for t, label in knowledge_system.EMPTIED],
                   kept=list(knowledge_system.KEPT),
                   master={"emptied": [label for _, label in knowledge_system.MASTER_EMPTIED],
                           "kept": list(knowledge_system.MASTER_KEPT)},
                   web_search=research.web_search_configured())
    return _json({**out, "can_manage": can_manage})


def _knowledge_changed() -> None:
    fetch_qdrant_overview.clear()
    fetch_research_overview.clear()
    agent.describe.clear()


def _reset(request: Request, everything: bool) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    confirmation = knowledge_system.MASTER_CONFIRMATION if everything else knowledge_system.RESET_CONFIRMATION
    try:
        deleted = knowledge_system.reset(confirmation, user_id, everything)
    except knowledge_system.Busy as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:
        return _json({"error": f"Reset failed, nothing was deleted: {exc}"}, 502)
    finally:
        _knowledge_changed()
    return _json({"ok": True, "deleted": deleted})


def _set_aside(request: Request, everything: bool) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    answer_cache.clear()   # the domain's answers go with it
    try:
        result = knowledge_system.set_aside(user_id, everything)
    except (knowledge_system.Busy, ValueError) as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:
        return _json({"error": f"Starting over failed, nothing changed: {exc}"}, 502)
    finally:
        _knowledge_changed()
    return _json({"ok": True, **result})


async def knowledge_system_leave_demo(request: Request) -> Response:
    """Replace the demo with your own domain: the knowledge reset, while the
    knowledge system is the demo (nothing of yours to lose, so no sign-in again)."""
    if knowledge_system.status().get("origin") != "demo":
        return _json({"error": "this is not the demo: start over from Setup"}, 409)
    return await run_in_threadpool(_reset, request, False)


CONFIRM_FIRST = "Confirm it's you first: sign in again."


async def setup_start_over(request: Request) -> Response:
    """Start over, e.g. for another domain: the current domain is set aside
    (kept until the new one goes live, knowledge_system.set_aside), with
    everything its flows and sessions too (admins only). Needs the person to
    have signed in again just before (auth.confirmed), once per start."""
    body = await request.json()
    everything = body.get("everything") is True
    if everything and (auth.current_user(request) or {}).get("role") != "admin":
        return _json({"error": "only an admin can reset everything"}, 403)
    if not auth.confirmed(request):
        return _json({"error": CONFIRM_FIRST, "confirm": True}, 403)
    response = await run_in_threadpool(_set_aside, request, everything)
    if response.status_code == 200:
        auth.use_confirmation(request)
    return response


async def setup_restore(request: Request) -> Response:
    """Go back to the domain set aside: the unfinished setup is discarded."""
    user_id = (auth.current_user(request) or {}).get("user_id")
    await run_in_threadpool(answer_cache.clear)
    try:
        info = await run_in_threadpool(knowledge_system.restore, user_id)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    except knowledge_system.Busy as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:
        return _json({"error": f"Going back failed, nothing changed: {exc}"}, 502)
    finally:
        _knowledge_changed()
    return _json({"ok": True, "restored": info["name"]})


# ---------- Setup (the Initialization Agent, epic #19) ----------

def setup_view(request: Request) -> Response:
    # Once the build has finished, the evaluation is prepared by itself (once per plan).
    setup_evaluation.ensure_prepared(_publish_check)
    return _json(supervisor.view())


async def setup_message(request: Request) -> Response:
    """One turn of the setup conversation: the agent's reply and the new view."""
    text = (await request.json()).get("text")
    if not isinstance(text, str) or not text.strip():
        return _json({"error": "say something"}, 400)
    if len(text) > 4000:
        return _json({"error": "keep it under 4,000 characters"}, 400)
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        return _json(await run_in_threadpool(supervisor.respond, text, user_id))
    except supervisor.NotConversing as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:   # the model or the database: the user's turn is kept, they can send again
        return _json({"error": f"The setup agent couldn't answer ({type(exc).__name__}). Please send it again."}, 502)


async def setup_confirm(request: Request) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        return _json(await run_in_threadpool(supervisor.confirm, user_id))
    except supervisor.NotConversing as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def setup_blueprint(request: Request) -> Response:
    """Start (or restart) the blueprint research."""
    try:
        await run_in_threadpool(blueprint_run.start)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_blueprint_revise(request: Request) -> Response:
    feedback = (await request.json()).get("feedback")
    if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 2000:
        return _json({"error": "say what to change (at most 2,000 characters)"}, 400)
    try:
        await run_in_threadpool(blueprint_run.revise, feedback)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_blueprint_confirm(request: Request) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(blueprint_run.confirm, user_id)
    except (setup_state.TransitionNotAllowed, ValueError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_back(request: Request) -> Response:
    """Back to the conversation to change the requirements."""
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(blueprint_run.back_to_conversation, user_id)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_sources_discover(request: Request) -> Response:
    try:
        await run_in_threadpool(sources_run.start)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_source_choose(request: Request) -> Response:
    status = (await request.json()).get("status")
    try:
        await run_in_threadpool(sources_run.choose, request.path_params["source_id"], status)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_source_add(request: Request) -> Response:
    url = (await request.json()).get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 2000:
        return _json({"error": "give a web address"}, 400)
    try:
        await run_in_threadpool(sources_run.add_site, url)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_sources_continue(request: Request) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(sources_run.continue_, user_id)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_content_analyse(request: Request) -> Response:
    """Analyse the chosen sites again (or one: {"source_id"})."""
    only = (await request.json()).get("source_id")
    try:
        await run_in_threadpool(setup_content.start, only=only if isinstance(only, int) else None)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


def setup_content_section(request: Request) -> Response:
    """One section with all its pages."""
    try:
        return _json(setup_content.get_section(request.path_params["content_id"]))
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)


async def setup_content_choose(request: Request) -> Response:
    status = (await request.json()).get("status")
    try:
        await run_in_threadpool(setup_content.choose, request.path_params["content_id"], status)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_content_exclude(request: Request) -> Response:
    excluded = (await request.json()).get("excluded")
    if not isinstance(excluded, list) or not all(isinstance(u, str) for u in excluded) or len(excluded) > 50000:
        return _json({"error": "excluded must be a list of page addresses"}, 400)
    try:
        section = await run_in_threadpool(setup_content.exclude, request.path_params["content_id"], excluded)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    return _json({"section": section, "view": await run_in_threadpool(supervisor.view)})


async def setup_back_to_sources(request: Request) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(setup_content.back_to_sources, user_id)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


def setup_plan_review(request: Request) -> Response:
    """The draft ingestion plan for the current selection, for review."""
    try:
        return _json(setup_plan.review())
    except ValueError as exc:
        return _json({"error": str(exc)}, 409)


async def setup_plan_approve(request: Request) -> Response:
    """Build RAG: approve the plan version that was reviewed."""
    version = (await request.json()).get("version")
    if not isinstance(version, int):
        return _json({"error": "version must be the reviewed plan's version"}, 400)
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(setup_build.build_rag, version, user_id)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_build_start(request: Request) -> Response:
    """Queue the approved plan's job again (if queuing it failed)."""
    version = (await request.json()).get("version")
    if not isinstance(version, int):
        return _json({"error": "version must be the approved plan's version"}, 400)
    try:
        await run_in_threadpool(setup_build.start, version, (auth.current_user(request) or {}).get("user_id"))
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_build_retry(request: Request) -> Response:
    try:
        await run_in_threadpool(setup_build.retry, (auth.current_user(request) or {}).get("user_id"))
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


def setup_evaluation_view(request: Request) -> Response:
    return _json(setup_evaluation.view())


async def setup_evaluation_prepare(request: Request) -> Response:
    """Write the candidate flow and the evaluation set again (in the background)."""
    try:
        await run_in_threadpool(setup_evaluation.prepare, (auth.current_user(request) or {}).get("user_id"),
                                _publish_check)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(setup_evaluation.view))


async def setup_evaluation_run(request: Request) -> Response:
    """Run the prepared set against the candidate flow (in the background)."""
    try:
        await run_in_threadpool(setup_evaluation.run, (auth.current_user(request) or {}).get("user_id"),
                                _eval_execute, _run_check)
    except (setup_state.TransitionNotAllowed, ValueError, RuntimeError, flows.FlowError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(setup_evaluation.view))


def setup_readiness_view(request: Request) -> Response:
    """The readiness report: scores, their definitions and numbers, the gaps."""
    try:
        return _json(setup_readiness.report())
    except ValueError as exc:          # no confirmed blueprint yet
        return _json({"error": str(exc)}, 409)


async def setup_go_live(request: Request) -> Response:
    """Make the evaluated candidate flow live: setup is done (READY)."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    await run_in_threadpool(answer_cache.clear)
    user_id = (auth.current_user(request) or {}).get("user_id")
    body = body if isinstance(body, dict) else {}
    confirm = body.get("confirm_gaps") is True
    # Going live deletes the domain set aside when this setup started over: say so first.
    previous = await run_in_threadpool(knowledge_system.aside)
    if previous and body.get("replace_aside") is not True:
        return _json({"error": f"Going live deletes {previous['name']}.", "aside": previous}, 409)
    try:
        await run_in_threadpool(setup_readiness.go_live, user_id, confirm, _run_check)
    except setup_readiness.GapsNotConfirmed as exc:
        return _json({"error": str(exc), "confirm": True}, 409)
    except (setup_state.TransitionNotAllowed, ValueError, flows.FlowError) as exc:
        return _json({"error": str(exc)}, 409)
    if previous:
        await run_in_threadpool(knowledge_system.discard_aside, user_id)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_relabel(request: Request) -> Response:
    """Label the approved plan's stored pages again, from their stored text (in the background)."""
    try:
        await run_in_threadpool(setup_labels.relabel, (auth.current_user(request) or {}).get("user_id"))
    except (ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


def knowledge_flagged(request: Request) -> Response:
    """Passages that read like instructions to an AI (injection.py): withheld from the agents."""
    return _json({"passages": injection.flagged_passages()})


def knowledge_provenance(request: Request) -> Response:
    """Why is this page in the knowledge base? ?url=…"""
    url = (request.query_params.get("url") or "").strip()
    if not url or len(url) > 2000:
        return _json({"error": "give the page's url"}, 400)
    try:
        return _json(setup_build.provenance(url))
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)


async def setup_content_ttl(request: Request) -> Response:
    ttl = (await request.json()).get("ttl_days")
    try:
        await run_in_threadpool(setup_plan.set_ttl, request.path_params["content_id"], ttl)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    return _json(await run_in_threadpool(setup_plan.review))


async def setup_back_to_content(request: Request) -> Response:
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        await run_in_threadpool(setup_plan.back_to_content, user_id)
    except setup_state.TransitionNotAllowed as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


# ---------- Sources ----------

def overview(request: Request) -> Response:
    if request.query_params.get("fresh"):
        fetch_qdrant_overview.clear()
    count, rows = fetch_qdrant_overview()
    return _json({"collection": config.COLLECTION_NAME, "count": count, "rows": rows,
                  "can_manage": auth.allowed(auth.current_user(request), "manage_knowledge")})


def source_chunks(request: Request) -> Response:
    """One page's chunks in order: heading path (R21), length and the start of the text."""
    url = request.query_params.get("url") or ""
    client = storage.get_client("research" if request.query_params.get("dataset") == "research" else "curated")
    with client.connection() as connection:
        rows = connection.execute(
            f"SELECT chunk_index, body, payload->'headings' AS headings, payload->>'chunking' AS chunking "
            f"FROM {client.table} WHERE source_url = %s ORDER BY chunk_index", (url,)).fetchall()
    return _json({"url": url, "chunks": [{"index": r["chunk_index"], "headings": r["headings"] or [],
                                          "chunking": r["chunking"] or "fixed", "chars": len(r["body"]),
                                          "text": r["body"][:400]} for r in rows]})


# ---------- Ingestion ----------

def _job_state(kind: str) -> dict:
    info = JOB_INFO[kind]
    state = {"id": kind, "icon": info["icon"], "label": info["label"], "description": info["description"],
             "runs": [], "error": None, "summary": None}
    try:
        jobs = storage.list_jobs(storage.get_client(), kind, limit=5)
        state["runs"] = [{"job_id": job["id"], "state": job["status"],
                          "start_date": (job["started_at"] or job["created_at"]).isoformat(),
                          "end_date": job["finished_at"].isoformat() if job["finished_at"] else None,
                          "next_index": job["next_index"], "total": job["total"]} for job in jobs]
        state["summary"] = next((job["summary"] for job in jobs if job["summary"]), None)
    except Exception as exc:
        state["error"] = f"Could not fetch jobs for {kind}: {exc}"
        return state
    return state


def ingestion(request: Request) -> Response:
    if request.query_params.get("fresh"):
        fetch_qdrant_overview.clear()
        fetch_research_overview.clear()
    count, rows = fetch_qdrant_overview()
    return _json({
        "collection": config.COLLECTION_NAME,
        "count": count,
        "urls": len(rows),
        "status_labels": STATUS_LABELS,
        "jobs": [_job_state(kind) for kind in JOB_INFO],
        "research": fetch_research_overview(),
        "can_manage": auth.allowed(auth.current_user(request), "manage_knowledge"),
    })


MAX_REMOVE = 200


def source_reports(request: Request) -> Response:
    """Data-source reports from research, newest first (source_profiles.py)."""
    try:
        reports = source_profiles.list_reports(int(request.query_params.get("limit") or 50))
    except Exception as exc:
        return _json({"error": f"Couldn't load data source reports: {exc}"}, 502)
    return _json({"reports": reports, "can_manage": auth.allowed(auth.current_user(request), "manage_knowledge")})


async def source_reports_remove(request: Request) -> Response:
    ids = (await request.json()).get("report_ids")
    if not isinstance(ids, list) or not ids or len(ids) > MAX_REMOVE or not all(isinstance(i, str) and i for i in ids):
        return _json({"error": f"report_ids must be a list of 1 to {MAX_REMOVE} ids"}, 400)
    try:
        removed = await run_in_threadpool(source_profiles.delete_reports, ids)
    except Exception as exc:
        return _json({"error": f"Failed to remove reports: {exc}"}, 502)
    return _json({"ok": True, "removed": removed})


async def remove_sources(request: Request) -> Response:
    """Delete every chunk of the chosen pages: {"dataset": "curated" | "research", "urls": [...]}."""
    body = await request.json()
    dataset, urls = body.get("dataset"), body.get("urls")
    if dataset not in ("curated", "research"):
        return _json({"error": "dataset must be curated or research"}, 400)
    if not isinstance(urls, list) or not urls or len(urls) > MAX_REMOVE \
            or not all(isinstance(u, str) and u.strip() for u in urls):
        return _json({"error": f"urls must be a list of 1 to {MAX_REMOVE} URLs"}, 400)
    client = storage.get_client(dataset)
    try:
        for url in dict.fromkeys(urls):
            await run_in_threadpool(storage.delete_url_points, client, url)
    except Exception as exc:
        return _json({"error": f"Failed to remove sources: {exc}"}, 502)
    finally:
        fetch_qdrant_overview.clear()
        fetch_research_overview.clear()
    return _json({"ok": True, "removed": len(dict.fromkeys(urls))})


async def trigger(request: Request) -> Response:
    kind = (await request.json()).get("kind")
    if kind not in JOB_INFO:
        return _json({"error": f"unknown job {kind!r}"}, 400)
    try:
        entries = []
        if kind == "ingest_urls":
            entries = await run_in_threadpool(knowledge_system.urls, config.MAX_URLS_PER_JOB)
            if not entries:
                return _json({"error": "no URLs to ingest yet"}, 400)
        job = await run_in_threadpool(storage.enqueue_job, storage.get_client(), kind, entries)
    except (ValueError, FileNotFoundError) as exc:
        return _json({"error": str(exc)}, 400)
    except UniqueViolation:
        return _json({"error": "an ingestion job is already queued or running"}, 409)
    except Exception as exc:
        return _json({"error": f"Failed to queue {kind}: {exc}"}, 502)
    return _json({"ok": True, "job_id": job["id"]})


# ---------- Ad-hoc ----------

def adhoc_settings(request: Request) -> Response:
    return _json({
        "default_ttl_days": config.DEFAULT_TTL_DAYS,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "documents": adhoc_ingest.list_adhoc(),
    })


async def adhoc_preview(request: Request) -> Response:
    body = await request.json()
    return _json(await run_in_threadpool(adhoc_ingest.preview, body.get("title", ""), body.get("text", "")))


async def adhoc_submit(request: Request) -> Response:
    body = await request.json()
    title, text = body.get("title", ""), body.get("text", "")
    try:
        ttl_days = float(body.get("ttl_days"))
    except (TypeError, ValueError):
        return _json({"error": "ttl_days must be a number"}, 400)
    if not title.strip() or not text.strip() or ttl_days <= 0:
        return _json({"error": "a title, some content and a positive lifetime are required"}, 400)
    try:
        result = await run_in_threadpool(adhoc_ingest.ingest, title, text, ttl_days)
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)}, 500)
    fetch_qdrant_overview.clear()  # show it in Sources right away
    return _json(result)


# ---------- Docs ----------

def docs(request: Request) -> Response:
    return _json({
        "steps": [pipeline_flow.step_detail(key) for key in pipeline_flow.step_keys()],
        "schema": pipeline_flow.schema_fields(),
    })


def docs_sample(request: Request) -> Response:
    sample = pipeline_flow.sample_record()
    return _json({"message": sample} if isinstance(sample, str) else {"record": sample})


# ---------- Evaluations ----------

def evaluations(request: Request) -> Response:
    return _json({
        "runs": evaluation.list_runs(),
        "active": None,
    })


async def evaluation_start(request: Request) -> Response:
    return _json({"error": "Local-model evaluations are unavailable during the AWS migration"}, 410)


def evaluation_detail(request: Request) -> Response:
    run = evaluation.get(request.path_params["run_id"])
    return _json(run) if run else _json({"error": "no such evaluation"}, 404)


def evaluation_cancel(request: Request) -> Response:
    return _json({"error": "Historical evaluation runs cannot be changed"}, 410)


def evaluation_resume(request: Request) -> Response:
    return _json({"error": "Local-model evaluations are unavailable during the AWS migration"}, 410)


if auth.config_error():
    raise SystemExit(f"Refusing to start: {auth.config_error()}")

@contextlib.asynccontextmanager
async def _lifespan(app):
    # The knowledge system's first run (record, URL import) before the first request.
    try:
        await run_in_threadpool(knowledge_system.ensure_schema)
    except Exception as exc:   # the database may still be starting; the first use retries
        print(f"knowledge system first run deferred: {exc}", file=sys.stderr)
    # Runs a restart cut short (R30): marked interrupted; recent Ask-page ones resume in the background.
    try:
        await run_in_threadpool(durable.ensure_setup)
        resumed = await run_in_threadpool(durable.recover, _resume_and_record)
        if resumed:
            print(f"resuming {len(resumed)} interrupted run(s)", file=sys.stderr)
        await run_in_threadpool(durable.prune)
    except Exception as exc:
        print(f"durable run recovery skipped: {exc}", file=sys.stderr)
    threading.Thread(target=_expire_human_requests, name="human-expiry", daemon=True).start()
    yield


app = Starlette(lifespan=_lifespan, middleware=[
    # Outer to inner: security headers on every response, cross-site
    # state-changing requests refused (web_security.py), the signed cookie,
    # then the permission check per /api request.
    Middleware(web_security.SecurityHeaders, https=auth.COOKIE_SECURE),
    Middleware(web_security.SameOrigin),
    Middleware(SessionMiddleware, secret_key=auth.session_secret(), session_cookie="rag_session",
               max_age=auth.SESSION_MAX_AGE, same_site="lax", https_only=auth.COOKIE_SECURE),
    # Outside the permission check, so refused attempts are recorded too.
    Middleware(audit.AuditMiddleware),
    Middleware(auth.AuthMiddleware),
], routes=[
    *auth.ROUTES,
    Route("/", index),
    Route("/vendor/plotly.min.js", plotly_js),
    Route("/api/config", app_config),
    Route("/api/models", models),
    Route("/api/models/catalogue", model_catalogue),
    Route("/api/answer-cache", answer_cache_view),
    Route("/api/answer-cache/clear", answer_cache_clear, methods=["POST"]),
    Route("/api/status", status),
    Route("/api/ask", ask, methods=["POST"]),
    Route("/api/ask/stream", ask_stream, methods=["POST"]),
    Route("/api/agent", agent_info),
    Route("/api/agent/stream", agent_stream, methods=["POST"]),
    Route("/api/agent/resume", agent_resume, methods=["POST"]),
    Route("/api/agent/memory", agent_memory),
    Route("/api/agent-memory", agent_memory_view),
    Route("/api/agent-memory/entry", agent_memory_entry, methods=["POST"]),
    Route("/api/users", user_list),
    Route("/api/users", user_create, methods=["POST"]),
    Route("/api/users/{user_id}", user_update, methods=["PUT"]),
    Route("/api/audit", audit_list),
    Route("/api/audit.csv", audit_csv),
    Route("/api/agent/session", agent_session),
    Route("/api/flows", flow_list),
    Route("/api/flows", flow_create, methods=["POST"]),
    Route("/api/flows/validate", flow_validate, methods=["POST"]),
    Route("/api/flows/templates", flow_templates),
    Route("/api/flows/architectures", flow_architectures),
    Route("/api/flows/diff", flow_diff, methods=["POST"]),
    Route("/api/flows/{flow_id}", flow_detail),
    Route("/api/flows/{flow_id}/draft", flow_save_draft, methods=["PUT"]),
    Route("/api/flows/{flow_id}/draft", flow_discard_draft, methods=["DELETE"]),
    Route("/api/flows/{flow_id}/publish", flow_publish, methods=["POST"]),
    Route("/api/flows/{flow_id}/live", flow_set_live, methods=["POST"]),
    Route("/api/flows/{flow_id}/run", flow_run, methods=["POST"]),
    Route("/api/flows/{flow_id}/metrics", flow_metrics),
    Route("/api/flows/{flow_id}/versions/{version:int}", flow_version),
    Route("/api/flow-evals/sets", eval_sets),
    Route("/api/flow-evals/sets", eval_set_save, methods=["POST"]),
    Route("/api/flow-evals/archive", eval_archive_sets),
    Route("/api/flow-evals/golden", golden_status),
    Route("/api/flow-evals/golden", golden_start, methods=["POST"]),
    Route("/api/flow-evals/search", search_quality),
    Route("/api/flow-evals/search/sets", search_quality_generate, methods=["POST"]),
    Route("/api/flow-evals/search/sets/{set_id}", search_quality_set),
    Route("/api/flow-evals/search/sets/{set_id}", search_quality_set_change, methods=["POST"]),
    Route("/api/flow-evals/search/sets/{set_id}", search_quality_set_delete, methods=["DELETE"]),
    Route("/api/flow-evals/search/runs", search_quality_run, methods=["POST"]),
    Route("/api/flow-evals/search/runs/{run_id}", search_quality_run_detail),
    Route("/api/flow-evals/runs", eval_runs),
    Route("/api/flow-evals/runs", eval_start, methods=["POST"]),
    Route("/api/flow-evals/runs/{run_id}", eval_run_detail),
    Route("/api/flow-evals/runs/{run_id}/cancel", eval_cancel, methods=["POST"]),
    Route("/api/session/reset", reset_session, methods=["POST"]),
    Route("/api/sessions", session_list),
    Route("/api/sessions/current", session_current),
    Route("/api/sessions/new", session_new, methods=["POST"]),
    Route("/api/runs", runs_list),
    Route("/api/runs/{run_id}/resume", runs_resume, methods=["POST"]),
    Route("/api/runs/{run_id}/cancel", runs_cancel, methods=["POST"]),
    Route("/.well-known/agent-card.json", a2a_card),
    Route("/api/v1/a2a", a2a_rpc, methods=["POST"]),
    Route("/api/v1/mcp", mcp_endpoint, methods=["GET", "POST"]),
    Route("/api/integrations", integrations),
    Route("/api/integrations/openapi/preview", openapi_preview, methods=["POST"]),
    Route("/api/integrations/openapi", openapi_import, methods=["POST"]),
    Route("/api/integrations/openapi/{name}", openapi_remove, methods=["DELETE"]),
    Route("/api/integrations/openapi/{name}/test", openapi_test, methods=["POST"]),
    Route("/api/integrations", integration_add, methods=["POST"]),
    Route("/api/integrations/{server_id}/refresh", integration_refresh, methods=["POST"]),
    Route("/api/integrations/{server_id}/tools", integration_enable, methods=["PUT"]),
    Route("/api/integrations/{server_id}", integration_remove, methods=["DELETE"]),
    Route("/api/v1/openapi.json", v1_openapi),
    Route("/api/v1/docs", v1_docs),
    Route("/api/v1/ask", v1_ask, methods=["POST"]),
    Route("/api/v1/sessions", v1_sessions),
    Route("/api/v1/sessions/{session_id}", v1_session),
    Route("/api/v1/documents", v1_documents),
    Route("/api/v1/documents", adhoc_submit, methods=["POST"]),
    Route("/api/v1/flows", flow_list),
    Route("/api/v1/evaluations", eval_runs),
    Route("/api/v1/evaluations", eval_start, methods=["POST"]),
    Route("/api/v1/evaluations/{run_id}", eval_run_detail),
    Route("/api/me/keys", my_keys),
    Route("/api/me/keys", my_key_create, methods=["POST"]),
    Route("/api/me/keys/{key_id}", my_key_revoke, methods=["DELETE"]),
    Route("/api/keys", keys_list),
    Route("/api/keys/{key_id}", key_revoke, methods=["DELETE"]),
    Route("/api/collections", collections_list),
    Route("/api/collections", collections_create, methods=["POST"]),
    Route("/api/collections/{collection_id}", collections_update, methods=["PUT"]),
    Route("/api/collections/{collection_id}", collections_delete, methods=["DELETE"]),
    Route("/api/collections/{collection_id}/sources", collections_assign, methods=["POST"]),
    Route("/api/approvals", approvals_list),
    Route("/api/approvals/{request_id}", approvals_decide, methods=["POST"]),
    Route("/api/me/memory", my_memory),
    Route("/api/me/memory", my_memory_consent, methods=["PUT"]),
    Route("/api/me/memory", my_memory_clear, methods=["DELETE"]),
    Route("/api/me/memory/{fact_id:int}", my_memory_fact, methods=["PUT", "DELETE"]),
    Route("/api/sessions/{session_id}", session_detail),
    Route("/api/home", home),
    Route("/api/knowledge-system", knowledge_system_status),
    Route("/api/knowledge-system/leave-demo", knowledge_system_leave_demo, methods=["POST"]),
    Route("/api/setup/start-over", setup_start_over, methods=["POST"]),
    Route("/api/setup/restore", setup_restore, methods=["POST"]),
    Route("/api/setup", setup_view),
    Route("/api/setup/message", setup_message, methods=["POST"]),
    Route("/api/setup/confirm", setup_confirm, methods=["POST"]),
    Route("/api/setup/blueprint", setup_blueprint, methods=["POST"]),
    Route("/api/setup/blueprint/revise", setup_blueprint_revise, methods=["POST"]),
    Route("/api/setup/blueprint/confirm", setup_blueprint_confirm, methods=["POST"]),
    Route("/api/setup/back", setup_back, methods=["POST"]),
    Route("/api/setup/sources/discover", setup_sources_discover, methods=["POST"]),
    Route("/api/setup/sources/add", setup_source_add, methods=["POST"]),
    Route("/api/setup/sources/continue", setup_sources_continue, methods=["POST"]),
    Route("/api/setup/sources/{source_id:int}", setup_source_choose, methods=["POST"]),
    Route("/api/setup/content/analyse", setup_content_analyse, methods=["POST"]),
    Route("/api/setup/content/{content_id:int}", setup_content_section),
    Route("/api/setup/content/{content_id:int}", setup_content_choose, methods=["POST"]),
    Route("/api/setup/content/{content_id:int}/urls", setup_content_exclude, methods=["POST"]),
    Route("/api/setup/back-to-sources", setup_back_to_sources, methods=["POST"]),
    Route("/api/setup/content/{content_id:int}/ttl", setup_content_ttl, methods=["POST"]),
    Route("/api/setup/plan", setup_plan_review),
    Route("/api/setup/plan/approve", setup_plan_approve, methods=["POST"]),
    Route("/api/setup/build/start", setup_build_start, methods=["POST"]),
    Route("/api/setup/build/retry", setup_build_retry, methods=["POST"]),
    Route("/api/setup/evaluation", setup_evaluation_view),
    Route("/api/setup/evaluation/prepare", setup_evaluation_prepare, methods=["POST"]),
    Route("/api/setup/evaluation/run", setup_evaluation_run, methods=["POST"]),
    Route("/api/setup/readiness", setup_readiness_view),
    Route("/api/setup/relabel", setup_relabel, methods=["POST"]),
    Route("/api/setup/go-live", setup_go_live, methods=["POST"]),
    Route("/api/knowledge/provenance", knowledge_provenance),
    Route("/api/knowledge/flagged", knowledge_flagged),
    Route("/api/setup/back-to-content", setup_back_to_content, methods=["POST"]),
    Route("/api/overview", overview),
    Route("/api/ingestion", ingestion),
    Route("/api/ingestion/trigger", trigger, methods=["POST"]),
    Route("/api/sources/remove", remove_sources, methods=["POST"]),
    Route("/api/sources/chunks", source_chunks),
    Route("/api/source-reports", source_reports),
    Route("/api/source-reports/remove", source_reports_remove, methods=["POST"]),
    Route("/api/adhoc", adhoc_settings),
    Route("/api/adhoc/preview", adhoc_preview, methods=["POST"]),
    Route("/api/adhoc/submit", adhoc_submit, methods=["POST"]),
    Route("/api/docs", docs),
    Route("/api/docs/sample", docs_sample),
    Route("/api/evaluations", evaluations),
    Route("/api/evaluations/start", evaluation_start, methods=["POST"]),
    Route("/api/evaluations/{run_id}", evaluation_detail),
    Route("/api/evaluations/{run_id}/cancel", evaluation_cancel, methods=["POST"]),
    Route("/api/evaluations/{run_id}/resume", evaluation_resume, methods=["POST"]),
    Mount("/static/{version}", NoCacheStaticFiles(directory=WEB_DIR / "static"), name="static"),
])

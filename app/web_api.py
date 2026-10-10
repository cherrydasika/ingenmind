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
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/app/dags")

import numpy as np
import plotly
from psycopg.errors import UniqueViolation
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
import auth
import identity
import knowledge_system
from initialization import (blueprint_run, build as setup_build, content as setup_content,
                            evaluation as setup_evaluation, labels as setup_labels, plan as setup_plan, readiness as setup_readiness, sources_run,
                            state as setup_state, supervisor)
import sessions
import source_profiles
import evaluation_archive as evaluation
import embedding_viz
import flows
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
    return _event_stream(lambda emit: _answer_payload(
        *parsed, body, lambda stage, state, **info: emit("stage", {"stage": stage, "state": state, **info})))


def _event_stream(work) -> StreamingResponse:
    """Run work(emit) in a thread and stream what it emits as server-sent
    events, ending with `result` (its return value) or `error`."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data: dict) -> None:
        loop.call_soon_threadsafe(events.put_nowait, (event, data))

    def run() -> None:
        try:
            emit("result", work(emit))
        except Exception as exc:
            emit("error", {"error": "Unable to complete this request. Please try again."})

    async def stream():
        task = asyncio.ensure_future(run_in_threadpool(run))
        try:
            while True:
                event, data = await events.get()
                yield f"event: {event}\ndata: {json.dumps(data, default=_json_default)}\n\n"
                if event in ("result", "error"):
                    break
        finally:
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
    return _json({**info, "roles": {key: {**role, "prompt": "Restricted to administrator tooling."}
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
    # The server's session, not the browser's: the user's current one, or a
    # new one after sign-in or 30 minutes idle (sessions.py). Kept in the
    # cookie; every question here becomes a turn of its history.
    user_id = auth.current_user(request)["user_id"]
    session_id = await run_in_threadpool(sessions.current, user_id, request.session.get("sid"))
    request.session["sid"] = session_id
    return _event_stream(lambda emit: _agent_payload(question, session_id, {**body, "session_id": session_id},
                                                     max_iterations, emit, record=True))


# ---------- Flows (the agent graph as data) ----------
# Drafts and published versions live in Postgres (flows/store.py). The agent
# still runs the built-in flow; running saved versions comes later.

MAX_FLOW_BYTES = 512_000
MAX_FLOW_NODES = 200


def _live() -> dict:
    return flows.store.live_pointer() or {"flow_id": flows.load_flow().id, "version": 0, "updated_at": None}


def _flow_view(record: dict) -> dict:
    spec = flows.FlowSpec.model_validate(record["flow"])
    return {**record, "flow": spec.model_dump(mode="json"), "components": flows.components(),
            "issues": flows.validate(spec) + flows.compiler.runtime_issues(spec, agent._runtime()),
            "live": _live()}


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
    try:
        flows.store.set_live(flow_id, version, flows.load_flow(), _run_check)
    except flows.store.FlowNotFound:
        return _json({"error": "unknown version"}, 404)
    except flows.FlowError as invalid:
        return _json({"error": "this version cannot run", "issues": invalid.issues}, 422)
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
    return _json({"version": version, **_flow_view(flows.store.get_flow(flow_id, flows.load_flow()))})


def flow_templates(request: Request) -> Response:
    return _json(flows.templates(flows.load_flow()))


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
        ids = flows.evals.start(str(body.get("set_id") or ""), flow_id, targets, _eval_execute)
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
    result = agent.answer(question, session_id, body.get("user_id"), max_iterations,
                          lambda event: emit("stage", event), retrieval_view, flow, source)
    trace_id = result.pop("trace_id", None)   # server-side only
    if record:
        try:
            sessions.record_turn(session_id, body["user_id"], question, result, trace_id)
        except Exception as error:   # the user still gets the answer
            print(f"could not record the session turn: {error}", file=sys.stderr)
    return _shape_agent_result(result)


# ---------- Users (identity.py; manage_users) ----------

def user_list(request: Request) -> Response:
    return _json({"users": [identity.public(u) | {"created_by": u["created_by"], "updated_by": u["updated_by"],
                                                  "updated_at": u["updated_at"]} for u in identity.list_users()],
                  "permissions": identity.PERMISSIONS, "roles": identity.ROLES, "me": auth.current_user(request)["user_id"]})


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
    """Change first_name, role, permissions or status (deactivate rather than delete)."""
    body = await request.json()
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    actor = auth.current_user(request)
    user_id = request.path_params["user_id"]
    changes = {k: body[k] for k in ("first_name", "role", "permissions", "status") if k in body}
    try:
        await run_in_threadpool(identity.guard_change, actor, user_id, changes)
        user = await run_in_threadpool(identity.update_user, user_id, changes, actor["user_id"])
    except LookupError:
        return _json({"error": "No such user"}, 404)
    except ValueError as error:
        return _json({"error": str(error)}, 400)
    return _json({"user": identity.public(user)})


# ---------- Agent memory (agent_memory.py) ----------

def agent_memory_view(request: Request) -> Response:
    """What the agents have learned across runs: outcomes only, never anyone's conversation."""
    return _json({"agents": memory_store.AGENTS, "kinds": memory_store.KINDS, "entries": memory_store.entries()})


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
                   kept=list(knowledge_system.KEPT), confirmation=knowledge_system.RESET_CONFIRMATION)
    return _json({**out, "can_manage": can_manage})


async def knowledge_system_reset(request: Request) -> Response:
    """Empty the knowledge base and what was learnt from it, so setup can start again."""
    body = await request.json()
    user_id = (auth.current_user(request) or {}).get("user_id")
    try:
        deleted = await run_in_threadpool(knowledge_system.reset, str(body.get("confirm") or ""), user_id)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except knowledge_system.Busy as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:
        return _json({"error": f"Reset failed, nothing was deleted: {exc}"}, 502)
    finally:
        fetch_qdrant_overview.clear()
        fetch_research_overview.clear()
    return _json({"ok": True, "deleted": deleted})


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
    if not isinstance(excluded, list) or not all(isinstance(u, str) for u in excluded) or len(excluded) > 5000:
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
    user_id = (auth.current_user(request) or {}).get("user_id")
    confirm = isinstance(body, dict) and body.get("confirm_gaps") is True
    try:
        await run_in_threadpool(setup_readiness.go_live, user_id, confirm, _run_check)
    except setup_readiness.GapsNotConfirmed as exc:
        return _json({"error": str(exc), "confirm": True}, 409)
    except (setup_state.TransitionNotAllowed, ValueError, flows.FlowError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


async def setup_relabel(request: Request) -> Response:
    """Label the approved plan's stored pages again, from their stored text (in the background)."""
    try:
        await run_in_threadpool(setup_labels.relabel, (auth.current_user(request) or {}).get("user_id"))
    except (ValueError, RuntimeError) as exc:
        return _json({"error": str(exc)}, 409)
    return _json(await run_in_threadpool(supervisor.view))


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
    yield


app = Starlette(lifespan=_lifespan, middleware=[
    # Outer to inner: the signed cookie, then the permission check per /api request.
    Middleware(SessionMiddleware, secret_key=auth.session_secret(), session_cookie="rag_session",
               max_age=auth.SESSION_MAX_AGE, same_site="lax", https_only=auth.COOKIE_SECURE),
    Middleware(auth.AuthMiddleware),
], routes=[
    *auth.ROUTES,
    Route("/", index),
    Route("/vendor/plotly.min.js", plotly_js),
    Route("/api/config", app_config),
    Route("/api/models", models),
    Route("/api/status", status),
    Route("/api/ask", ask, methods=["POST"]),
    Route("/api/ask/stream", ask_stream, methods=["POST"]),
    Route("/api/agent", agent_info),
    Route("/api/agent/stream", agent_stream, methods=["POST"]),
    Route("/api/agent/resume", agent_resume, methods=["POST"]),
    Route("/api/agent/memory", agent_memory),
    Route("/api/agent-memory", agent_memory_view),
    Route("/api/users", user_list),
    Route("/api/users", user_create, methods=["POST"]),
    Route("/api/users/{user_id}", user_update, methods=["PUT"]),
    Route("/api/agent/session", agent_session),
    Route("/api/flows", flow_list),
    Route("/api/flows", flow_create, methods=["POST"]),
    Route("/api/flows/validate", flow_validate, methods=["POST"]),
    Route("/api/flows/templates", flow_templates),
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
    Route("/api/flow-evals/runs", eval_runs),
    Route("/api/flow-evals/runs", eval_start, methods=["POST"]),
    Route("/api/flow-evals/runs/{run_id}", eval_run_detail),
    Route("/api/flow-evals/runs/{run_id}/cancel", eval_cancel, methods=["POST"]),
    Route("/api/session/reset", reset_session, methods=["POST"]),
    Route("/api/sessions", session_list),
    Route("/api/sessions/current", session_current),
    Route("/api/sessions/{session_id}", session_detail),
    Route("/api/home", home),
    Route("/api/knowledge-system", knowledge_system_status),
    Route("/api/knowledge-system/reset", knowledge_system_reset, methods=["POST"]),
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
    Route("/api/setup/back-to-content", setup_back_to_content, methods=["POST"]),
    Route("/api/overview", overview),
    Route("/api/ingestion", ingestion),
    Route("/api/ingestion/trigger", trigger, methods=["POST"]),
    Route("/api/sources/remove", remove_sources, methods=["POST"]),
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

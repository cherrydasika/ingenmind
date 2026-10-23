"""Agent-to-agent (R36): IngenMind's live flow as an A2A agent other systems
call, and other A2A agents as sub-agents in a flow (the Remote agent
component). A2A is JSON-RPC 2.0 over HTTP (protocol 0.3):

    server   /.well-known/agent-card.json   the agent card (public)
             POST /api/v1/a2a               message/send, tasks/get (web_api.a2a_rpc),
                                            with an API key (R46) whose scopes include ask;
                                            the question runs through the live flow, guardrails
                                            and all; the reply is a completed task whose
                                            artifact holds the cited answer (text) and its
                                            sources and answer check (data)
    client   send(url, text, ...)           message/send to a remote agent; its reply as text,
                                            data, state and its trace id

Traces link both sides: a request carries the caller's trace id in the
message metadata (ingenmind_trace_id) and the reply carries this side's.
"""

import json
import threading
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

import outbound

PROTOCOL = "0.3.0"
MAX_TASKS = 500
MAX_TEXT = 4000
TRACE_KEY = "ingenmind_trace_id"

# JSON-RPC and A2A error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND = -32001


class A2AError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def card(base_url: str, name: str, description: str) -> dict:
    """The agent card: who this agent is, where to call it, how to authenticate."""
    base = base_url.rstrip("/")
    return {
        "protocolVersion": PROTOCOL, "name": name, "description": description, "version": "1.0.0",
        "url": f"{base}/api/v1/a2a", "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer",
                                       "description": "An IngenMind API key with the ask scope (Profile → API keys)"}},
        "security": [{"apiKey": []}],
        "skills": [{"id": "ask", "name": "Answer a question", "description": description,
                    "tags": ["question answering", "retrieval"], "inputModes": ["text/plain"],
                    "outputModes": ["text/plain", "application/json"]}],
        "documentationUrl": f"{base}/api/v1/docs",
    }


def message_text(message: dict) -> str:
    """The text of a user message (its text parts, joined)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        raise A2AError(INVALID_PARAMS, "params.message must be a user message")
    texts = [p.get("text") for p in message.get("parts") or [] if isinstance(p, dict) and p.get("kind") == "text"]
    text = "\n".join(t for t in texts if isinstance(t, str)).strip()
    if not text:
        raise A2AError(INVALID_PARAMS, "the message has no text part")
    return text


def task(result: dict, context_id: str, request: dict, trace_id: str | None = None) -> dict:
    """An /ask result (public_api.answer) as an A2A task: completed with the cited answer and its
    sources; input-required when a person must clarify or approve first (Ask a human)."""
    waiting = result.get("status") == "waiting"
    reply = {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "contextId": context_id,
             "parts": [{"kind": "text", "text": result.get("answer") or ""}]}
    out = {"kind": "task", "id": result.get("run_id") or uuid.uuid4().hex, "contextId": context_id,
           "status": {"state": "input-required" if waiting else "completed", "message": reply,
                      "timestamp": datetime.now(timezone.utc).isoformat()},
           "history": [request],
           "metadata": {**({TRACE_KEY: trace_id} if trace_id else {})}}
    if not waiting:
        out["artifacts"] = [{"artifactId": uuid.uuid4().hex, "name": "answer", "parts": [
            {"kind": "text", "text": result.get("answer") or ""},
            {"kind": "data", "data": {"sources": result.get("sources") or [],
                                      "answer_check": result.get("answer_check"), "flow": result.get("flow")}}]}]
    return out


class Tasks:
    """The tasks this process answered, for tasks/get (the latest MAX_TASKS, each its owner's)."""

    def __init__(self):
        self.items: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.lock = threading.Lock()

    def put(self, owner: str, item: dict) -> None:
        with self.lock:
            self.items[item["id"]] = (owner, item)
            while len(self.items) > MAX_TASKS:
                self.items.popitem(last=False)

    def get(self, owner: str, task_id: str) -> dict:
        with self.lock:
            found = self.items.get(task_id)
        if not found or found[0] != owner:
            raise A2AError(TASK_NOT_FOUND, "Task not found")
        return found[1]


TASKS = Tasks()


def response(rpc_id, result=None, error: A2AError | None = None) -> dict:
    if error is not None:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": error.code, "message": str(error)}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


# ---------- client: a remote agent as a sub-agent ----------

def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with outbound.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as error:
        raise A2AError(error.code, f"HTTP {error.code}: {error.read()[:300].decode(errors='replace')}") from None


def send(url: str, text: str, *, token: str = "", timeout: float = 60, context_id: str | None = None,
         trace_id: str | None = None, post=_post) -> dict:
    """message/send to a remote A2A agent: {"text", "data" (its data parts), "state", "trace_id",
    "context_id"}. Raises A2AError when it can't be reached or says no."""
    message = {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
               "parts": [{"kind": "text", "text": text[:MAX_TEXT]}],
               **({"contextId": context_id} if context_id else {}),
               **({"metadata": {TRACE_KEY: trace_id}} if trace_id else {})}
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send", "params": {"message": message}}
    reply = post(url, body, {"Authorization": f"Bearer {token}"} if token else {}, timeout)
    if reply.get("error"):
        raise A2AError(reply["error"].get("code", INTERNAL), reply["error"].get("message") or "remote agent error")
    result = reply.get("result") or {}
    parts = []
    if result.get("kind") == "task":
        for artifact in result.get("artifacts") or []:
            parts += artifact.get("parts") or []
        if not parts:
            parts = ((result.get("status") or {}).get("message") or {}).get("parts") or []
        state = (result.get("status") or {}).get("state") or "unknown"
    else:                                   # a plain message reply
        parts, state = result.get("parts") or [], "completed"
    texts = [p.get("text") for p in parts if p.get("kind") == "text" and isinstance(p.get("text"), str)]
    data = [p.get("data") for p in parts if p.get("kind") == "data" and isinstance(p.get("data"), dict)]
    return {"text": "\n".join(texts).strip(), "data": data, "state": state,
            "trace_id": (result.get("metadata") or {}).get(TRACE_KEY), "context_id": result.get("contextId")}

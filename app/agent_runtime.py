"""Where the agents' model turns run: a Bedrock AgentCore harness, or this
local stand-in for it.

    AGENT_RUNTIME  agentcore | local. Default: agentcore when AGENT_HARNESS_ARN
                   is set, local otherwise.

LocalHarness implements the part of the AgentCore API agent.py uses,
invoke_harness(), so the graph, tool loop and stream reading are the same on
both. Like a harness runtime session, it keeps each session's conversation:
an invocation sends only the new messages (a task, or tool results), and the
history lives in PostgreSQL (agent_sessions), so it survives restarts. Every
tool is an inline function: one invocation is one model turn through llm.py,
ending at the model's answer or at its tool calls.

Not emulated: AgentCore Memory's long-term facts and summaries, and the
runtime's microVM lifecycle."""

import json
import os
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import llm
from common import config

HARNESS_ARN = os.environ.get("AGENT_HARNESS_ARN", "").strip()
RUNTIME = (os.environ.get("AGENT_RUNTIME", "").strip().lower()
           or ("agentcore" if HARNESS_ARN else "local"))
# The harness's model defaults, which the flow builder's LLM node starts from.
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_ITERATIONS = 6
# Messages kept per session; older turns are dropped, whole exchanges at a time.
MAX_HISTORY_MESSAGES = 40
INTERRUPTED = "Interrupted: this tool call was not run."


def config_error() -> str | None:
    """Why the configured agent runtime cannot run, or None."""
    if RUNTIME == "agentcore":
        return None if HARNESS_ARN else "AGENT_RUNTIME=agentcore needs AGENT_HARNESS_ARN"
    if RUNTIME != "local":
        return f"AGENT_RUNTIME={RUNTIME!r} is not agentcore or local"
    error = llm.config_error()
    return f"the local agent runtime uses the chat model, which is not configured: {error}" if error else None


# ---------- session history ----------

_schema_ready = False
_schema_lock = threading.Lock()


def _connect():
    # A plain connection: this table needs no pgvector (which the worker sets up).
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def _connection():
    global _schema_ready
    if not _schema_ready:
        with _schema_lock, _connect() as setup:
            setup.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    runtime_session text PRIMARY KEY,
                    actor text NOT NULL,
                    messages jsonb NOT NULL DEFAULT '[]',
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            setup.execute("CREATE INDEX IF NOT EXISTS agent_sessions_actor ON agent_sessions (actor)")
            _schema_ready = True
    return _connect()


def load_history(runtime_session: str) -> list[dict]:
    with _connection() as connection:
        row = connection.execute("SELECT messages FROM agent_sessions WHERE runtime_session = %s",
                                 (runtime_session,)).fetchone()
    return row["messages"] if row else []


def save_history(runtime_session: str, actor: str, messages: list[dict]) -> None:
    with _connection() as connection:
        connection.execute("""
            INSERT INTO agent_sessions (runtime_session, actor, messages) VALUES (%s, %s, %s)
            ON CONFLICT (runtime_session) DO UPDATE SET messages = EXCLUDED.messages, actor = EXCLUDED.actor,
                updated_at = now()
        """, (runtime_session, actor, Jsonb(messages)))


def actor_sessions(actor: str) -> list[dict]:
    """Every runtime session of one actor (browser session): its id and messages."""
    with _connection() as connection:
        return connection.execute("""
            SELECT runtime_session, messages, updated_at FROM agent_sessions WHERE actor = %s ORDER BY runtime_session
        """, (actor,)).fetchall()


def _merge(history: list[dict], new: list[dict]) -> list[dict]:
    """Append new messages. A turn left waiting for tool results (an
    interrupted run) gets error results first, as every provider requires
    each tool call to be answered in the next message."""
    messages = list(history)
    for message in new:
        pending = _pending_uses(messages)
        if pending:
            answered = {b["toolResult"]["toolUseId"] for b in message.get("content", []) if "toolResult" in b}
            missing = [{"toolResult": {"toolUseId": u, "status": "error", "content": [{"text": INTERRUPTED}]}}
                       for u in pending if u not in answered]
            if message["role"] == "user":
                message = {**message, "content": missing + list(message["content"])}
            elif missing:
                messages.append({"role": "user", "content": missing})
        if messages and messages[-1]["role"] == message["role"] == "user":
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] + list(message["content"])}
        else:
            messages.append(message)
    return messages


def _pending_uses(messages: list[dict]) -> list[str]:
    if not messages or messages[-1]["role"] != "assistant":
        return []
    return [b["toolUse"]["toolUseId"] for b in messages[-1]["content"] if "toolUse" in b]


def _trim(messages: list[dict]) -> list[dict]:
    """Keep the last MAX_HISTORY_MESSAGES, starting at a user message that
    asks something (not one carrying tool results for a dropped turn)."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    for start in range(len(messages) - MAX_HISTORY_MESSAGES, len(messages)):
        message = messages[start]
        if message["role"] == "user" and not any("toolResult" in b for b in message["content"]):
            return messages[start:]
    return messages[-1:]


# ---------- the harness API ----------

def _tool_spec(tool: dict) -> dict:
    """An AgentCore inline_function tool as llm.chat() takes it."""
    spec = tool["config"]["inlineFunction"]
    return {"name": tool["name"], "description": spec["description"], "schema": spec["inputSchema"]}


class LocalHarness:
    """invoke_harness(**params) for the parameters agent._Run sends."""

    def __init__(self, chat=None):
        self.chat = chat or llm.chat
        # One runtime session runs one turn at a time, as on AgentCore.
        self.locks: dict[str, threading.Lock] = {}
        self.locks_lock = threading.Lock()

    def _lock(self, runtime_session: str) -> threading.Lock:
        with self.locks_lock:
            return self.locks.setdefault(runtime_session, threading.Lock())

    def invoke_harness(self, *, runtimeSessionId: str, messages: list[dict], systemPrompt: list[dict],
                       tools: list[dict], actorId: str = "", model: dict | None = None, **_ignored) -> dict:
        config = (model or {}).get("bedrockModelConfig") or {}

        def turn() -> llm.Turn:
            with self._lock(runtimeSessionId):
                history = _merge(load_history(runtimeSessionId), _plain(messages))
                reply = self.chat("\n\n".join(block["text"] for block in systemPrompt), history,
                                  [_tool_spec(t) for t in tools] or None,
                                  max_tokens=config.get("maxTokens") or DEFAULT_MAX_TOKENS,
                                  temperature=config.get("temperature", DEFAULT_TEMPERATURE))
                content = reply.content or [{"text": ""}]
                save_history(runtimeSessionId, actorId, _trim(history + [{"role": "assistant", "content": content}]))
            return reply
        return {"stream": _events(turn)}


def _plain(messages: list[dict]) -> list[dict]:
    """agent.py's messages: tool results with status and text, as stored."""
    return [{"role": m["role"], "content": [dict(b) for b in m["content"]]} for m in messages]


def _events(run_turn):
    """The turn as AgentCore's harness stream, which agent._Run._read parses.
    The model call runs after messageStart, as on AgentCore, so _read times
    the turn from messageStart to messageStop."""
    yield {"messageStart": {"role": "assistant"}}
    turn = run_turn()
    for index, block in enumerate(turn.content):
        if "text" in block and block["text"]:
            yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": block["text"]}}}
        elif "toolUse" in block:
            use = block["toolUse"]
            yield {"contentBlockStart": {"contentBlockIndex": index,
                                         "start": {"toolUse": {"toolUseId": use["toolUseId"], "name": use["name"]}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": index, "delta": {
                "toolUse": {"input": json.dumps(use["input"], ensure_ascii=False)}}}}
    yield {"messageStop": {"stopReason": turn.stop_reason}}
    yield {"metadata": {"usage": {"inputTokens": turn.input_tokens, "outputTokens": turn.output_tokens}}}

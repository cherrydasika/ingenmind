"""MCP (R38): tools from any MCP server in a flow, and IngenMind as an MCP
server. MCP is JSON-RPC 2.0; both sides here use the Streamable HTTP
transport (one POST per message; the reply is JSON, or a short SSE stream).

    mcp_servers   server_id, name, url, headers (encrypted: a bearer token, an
                  API key), tools (the server's tools/list, kept on refresh),
                  enabled (the tool names agents may call), created_at,
                  refreshed_at

Client: Admin → Integrations adds a server, lists its tools and enables
them one by one; an MCP tool node plugged into an agent offers that server's
enabled tools (optionally fewer) as `mcp__<server>__<tool>`. Every call goes
through the tool guardrails (R13: the arguments against the tool's schema,
no personal data, links or instruction-like text) and a calls limit per run,
and its result reaches the agent fenced as untrusted data.

Secrets (headers) are encrypted with INTEGRATIONS_KEY (a Fernet key); without
it, servers that need none still work and adding headers is refused.

Server (web_api.mcp_endpoint, POST /api/v1/mcp, an API key with the ask
scope): tools search_knowledge_base (passages with their sources, only the
collections the key's owner may see) and ask (the live flow's cited answer).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import secrets_box
from common import config

PROTOCOL = "2025-06-18"
CLIENT = {"name": "IngenMind", "version": "1.0.0"}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
TOOL_PREFIX = "mcp__"
MAX_RESULT = 6000
TIMEOUT = 30

_schema_ready = False
_schema_lock = threading.Lock()


class McpError(Exception):
    """A server that can't be reached, or says no."""


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616745)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    server_id text PRIMARY KEY,
                    name text NOT NULL,
                    url text NOT NULL,
                    headers text,
                    tools jsonb NOT NULL DEFAULT '[]',
                    enabled text[] NOT NULL DEFAULT '{}',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    refreshed_at timestamptz
                )""")
        _schema_ready = True


# ---------- secrets (secrets_box.py) ----------

def _seal(headers: dict) -> str | None:
    try:
        return secrets_box.seal(headers)
    except secrets_box.SecretError as error:
        raise McpError(f"{error} for this server") from None


def _open(sealed: str | None) -> dict:
    try:
        return secrets_box.open_(sealed)
    except secrets_box.SecretError as error:
        raise McpError(str(error)) from None


# ---------- the protocol (client side) ----------

def _parse(raw: bytes, content_type: str, want_id) -> dict:
    """The JSON-RPC reply with this id, from a JSON body or an SSE stream."""
    if "text/event-stream" in (content_type or ""):
        for line in raw.decode(errors="replace").splitlines():
            if line.startswith("data:"):
                message = json.loads(line[5:].strip() or "null")
                if isinstance(message, dict) and message.get("id") == want_id:
                    return message
        raise McpError("the server's stream ended without a reply")
    return json.loads(raw or b"null") or {}


class Session:
    """One conversation with an MCP server: initialize, then requests."""

    def __init__(self, url: str, headers: dict | None = None, timeout: float = TIMEOUT):
        if not (url.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?/", url)):
            raise McpError("an MCP server URL must be https:// (or http:// to localhost)")
        self.url, self.headers, self.timeout = url, dict(headers or {}), timeout
        self.session_id = None
        self.next_id = 0
        result = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT})
        self.server = result.get("serverInfo") or {}
        self.notify("notifications/initialized")

    def _post(self, body: dict):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL, **self.headers,
                   **({"Mcp-Session-Id": self.session_id} if self.session_id else {})}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            reply = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            raise McpError(f"HTTP {error.code}: {error.read()[:200].decode(errors='replace')}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise McpError(f"can't reach the server: {error}") from None
        with reply:
            self.session_id = reply.headers.get("Mcp-Session-Id") or self.session_id
            return reply.read(), reply.headers.get("Content-Type", "")

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        raw, content_type = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                        **({"params": params} if params is not None else {})})
        reply = _parse(raw, content_type, self.next_id)
        if reply.get("error"):
            raise McpError(reply["error"].get("message") or "the server refused")
        return reply.get("result") or {}

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})


def list_tools(url: str, headers: dict | None = None) -> list[dict]:
    session = Session(url, headers)
    tools, cursor = [], None
    for _ in range(10):   # pages
        result = session.request("tools/list", {"cursor": cursor} if cursor else {})
        tools += [{"name": t["name"], "description": (t.get("description") or "")[:1000],
                   "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}}
                  for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return tools


def call(server: dict, tool: str, args: dict, timeout: float = TIMEOUT) -> dict:
    """{"ok", "text", "error", "seconds"}: one tools/call on a registered server."""
    started = time.monotonic()
    try:
        session = Session(server["url"], _open(server.get("headers")), timeout)
        result = session.request("tools/call", {"name": tool, "arguments": args})
    except McpError as error:
        return {"ok": False, "text": "", "error": str(error), "seconds": round(time.monotonic() - started, 3)}
    texts = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            texts.append(item.get("text") or "")
        elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            texts.append(item["resource"].get("text") or "")
    if result.get("structuredContent") and not texts:
        texts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(t for t in texts if t)[:MAX_RESULT]
    failed = bool(result.get("isError"))
    return {"ok": not failed, "text": text, "error": text[:300] if failed else None,
            "seconds": round(time.monotonic() - started, 3)}


# ---------- the registry (Admin → Integrations) ----------

def add(server_id: str, name: str, url: str, headers: dict | None = None) -> dict:
    if not ID_PATTERN.match(server_id or ""):
        raise McpError("the id must be 2–24 lower-case letters, digits or _, starting with a letter")
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise McpError("a server needs a name")
    headers = {str(k): str(v) for k, v in (headers or {}).items() if str(k).strip()}
    tools = list_tools(url, headers)                     # reachable, and what it offers
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO mcp_servers (server_id, name, url, headers, tools, refreshed_at)
            VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT DO NOTHING RETURNING *""",
                                 (server_id, name, url.strip(), _seal(headers), Jsonb(tools))).fetchone()
    if row is None:
        raise McpError(f"there is already a server {server_id!r}")
    return public(row)


def refresh(server_id: str) -> dict:
    server = get(server_id)
    tools = list_tools(server["url"], _open(server.get("headers")))
    names = {t["name"] for t in tools}
    with _connect() as connection:
        return public(connection.execute("""
            UPDATE mcp_servers SET tools = %s, enabled = %s, refreshed_at = now() WHERE server_id = %s RETURNING *""",
                                         (Jsonb(tools), [n for n in server["enabled"] if n in names], server_id)).fetchone())


def enable(server_id: str, tools: list[str]) -> dict:
    server = get(server_id)
    names = {t["name"] for t in server["tools"]}
    unknown = [t for t in tools if t not in names]
    if unknown:
        raise McpError(f"the server has no tool(s): {', '.join(unknown)}")
    with _connect() as connection:
        return public(connection.execute("UPDATE mcp_servers SET enabled = %s WHERE server_id = %s RETURNING *",
                                         (sorted(set(tools)), server_id)).fetchone())


def remove(server_id: str) -> bool:
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute("DELETE FROM mcp_servers WHERE server_id = %s", (server_id,)).rowcount)


def get(server_id: str) -> dict:
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM mcp_servers WHERE server_id = %s", (server_id,)).fetchone()
    if row is None:
        raise McpError(f"no MCP server {server_id!r}")
    return row


def servers() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return [public(r) for r in connection.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()]


def public(row: dict) -> dict:
    """Never the headers: only whether there are any."""
    return {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k != "headers"},
            "has_headers": bool(row.get("headers"))}


# ---------- what an agent is offered ----------

def tool_name(server_id: str, tool: str) -> str:
    """mcp__<server>__<tool>, within a model's 64-character tool-name limit."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", tool)
    return f"{TOOL_PREFIX}{server_id}__{safe}"[:64]


def offered(plugs: list[dict]) -> dict[str, dict]:
    """The MCP tools an agent may call, by their tool name: {name: {"server", "tool", "spec", "schema",
    "max_calls"}}, from its MCP tool nodes ({"server", "tools", "max_calls"}): enabled ones only."""
    out = {}
    for plug in plugs:
        try:
            server = get(plug["server"])
        except McpError:
            continue
        chosen = set(plug.get("tools") or []) or set(server["enabled"])
        for tool in server["tools"]:
            if tool["name"] in server["enabled"] and tool["name"] in chosen:
                name = tool_name(server["server_id"], tool["name"])
                schema = tool["inputSchema"] if isinstance(tool["inputSchema"], dict) else {}
                out[name] = {"server": server, "tool": tool["name"], "schema": schema,
                             "max_calls": plug.get("max_calls", 5),
                             "spec": {"name": name, "config": {"inlineFunction": {
                                 "description": f"{tool['description'] or tool['name']} (MCP server {server['name']})"[:1000],
                                 "inputSchema": {"type": "object", "properties": schema.get("properties") or {},
                                                 "required": schema.get("required") or []}}}}}
    return out


def check(entry: dict, args, calls_so_far: int) -> str | None:
    """The tool guardrails (R13) for an MCP tool call: its limit per run, then the arguments."""
    import api_tools
    if calls_so_far >= entry["max_calls"]:
        return f"{entry['tool']} was already called {calls_so_far} times for this question (the most is {entry['max_calls']})"
    schema = entry["schema"]
    return api_tools.check_arguments(schema.get("properties") or {}, schema.get("required") or [], args,
                                     allow_urls=False)


# ---------- IngenMind as an MCP server ----------

SERVER_INFO = {"name": "IngenMind", "version": "1.0.0"}
SERVER_TOOLS = [
    {"name": "search_knowledge_base",
     "description": "Search IngenMind's knowledge base: the passages that match, each with its number and source "
                    "URL. Only the collections your key's owner may see.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "What to look for"},
         "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Passages to return (default 5)"}}}},
    {"name": "ask",
     "description": "Ask IngenMind's live flow a question: a short answer citing its sources, checked against "
                    "them before it is returned.",
     "inputSchema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"}}}},
]


def server_initialize(params: dict) -> dict:
    asked = (params or {}).get("protocolVersion")
    return {"protocolVersion": asked if asked in (PROTOCOL, "2025-03-26") else PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
            "instructions": "Use search_knowledge_base for passages and their sources, ask for a cited answer."}


def text_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}

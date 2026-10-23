"""API tools imported from an OpenAPI spec (R39): no Python file needed.

    openapi_tools  name, title, description, base_url, method, path,
                   parameters (name → {in, schema, description, required, explode}),
                   body (JSON body properties and required), auth (type and
                   where; its secret sealed with INTEGRATIONS_KEY), side_effects,
                   spec_url, created_at

Admin → Integrations: paste a spec URL (JSON or YAML; OpenAPI 3), pick the
operations, set auth (none, an API key in a header or the query, or OAuth
client credentials), import. Each operation becomes an API tool beside the
built-in ones (api_tools.all_tools): an agent with an API tools resource that
lists it may call it, through the tool guardrails (R13). GET operations are
read-only; any other method is flagged side-effecting, so it is refused
unless the flow has an approval step (R32), as for built-in tools.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import secrets_box
from common import config

MAX_SPEC = 3_000_000
MAX_SUMMARY = 3000
TIMEOUT = 15
NAME = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
METHODS = ("get", "post", "put", "patch", "delete")
AUTH_TYPES = ("none", "api_key", "oauth_client_credentials")

_schema_ready = False
_schema_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}       # tool → (OAuth access token, expiry)


class OpenApiImportError(ValueError):
    """A spec or an import that can't be used."""


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
            connection.execute("SELECT pg_advisory_xact_lock(72616746)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS openapi_tools (
                    name text PRIMARY KEY,
                    title text NOT NULL,
                    description text NOT NULL DEFAULT '',
                    base_url text NOT NULL,
                    method text NOT NULL,
                    path text NOT NULL,
                    parameters jsonb NOT NULL DEFAULT '{}',
                    body jsonb NOT NULL DEFAULT '{}',
                    auth jsonb NOT NULL DEFAULT '{"type": "none"}',
                    secret text,
                    side_effects boolean NOT NULL DEFAULT false,
                    spec_url text NOT NULL DEFAULT '',
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def _https(url: str) -> str:
    url = (url or "").strip()
    if not (url.startswith("https://") or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?/", url)):
        raise OpenApiImportError("the URL must be https:// (or http:// to localhost)")
    return url


# ---------- reading a spec ----------

def load(spec_url: str = "", text: str = "") -> dict:
    """The spec, from its URL or pasted text (JSON or YAML)."""
    if spec_url:
        request = urllib.request.Request(_https(spec_url), headers={"User-Agent": "IngenMind"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
                text = reply.read(MAX_SPEC + 1).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as error:
            raise OpenApiImportError(f"can't fetch the spec: {error}") from None
    if len(text) > MAX_SPEC:
        raise OpenApiImportError("the spec is too large")
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise OpenApiImportError(f"not JSON or YAML: {error}") from None
    if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3") or not isinstance(spec.get("paths"), dict):
        raise OpenApiImportError("not an OpenAPI 3 spec (it needs openapi: 3.x and paths)")
    return spec


def _resolve(spec: dict, node, depth: int = 0):
    """Follow local $refs (#/components/…), a few levels deep."""
    if depth > 6 or not isinstance(node, dict):
        return node
    if "$ref" in node and str(node["$ref"]).startswith("#/"):
        target = spec
        for part in node["$ref"][2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(spec, target, depth + 1)
    return {k: (_resolve(spec, v, depth + 1) if isinstance(v, dict) else
                [_resolve(spec, i, depth + 1) for i in v] if isinstance(v, list) else v) for k, v in node.items()}


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def operations(spec: dict) -> list[dict]:
    """Every operation: id, method, path, summary, parameters and JSON body (for the UI to pick from)."""
    out = []
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = {}
            for raw in [*shared, *(op.get("parameters") or [])]:
                p = _resolve(spec, raw)
                if not isinstance(p, dict) or p.get("in") not in ("query", "path", "header") or not p.get("name"):
                    continue
                params[p["name"]] = {"in": p["in"], "schema": p.get("schema") or {"type": "string"},
                                     "description": (p.get("description") or "")[:300],
                                     "required": bool(p.get("required")) or p["in"] == "path",
                                     "explode": p.get("explode", True)}
            body = {"properties": {}, "required": []}
            content = (_resolve(spec, op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
            schema = _resolve(spec, content.get("schema") or {})
            if schema.get("type") == "object" or schema.get("properties"):
                body = {"properties": schema.get("properties") or {}, "required": schema.get("required") or []}
            out.append({"id": _slug(op.get("operationId") or f"{method}_{path}")[:40], "method": method.upper(),
                        "path": path, "summary": (op.get("summary") or op.get("description") or f"{method.upper()} {path}")[:200],
                        "description": (op.get("description") or "")[:600], "parameters": params, "body": body,
                        "side_effects": method != "get"})
    return out


def base_url(spec: dict, spec_url: str = "") -> str:
    servers = spec.get("servers") or []
    url = (servers[0] or {}).get("url", "") if servers and isinstance(servers[0], dict) else ""
    return urllib.parse.urljoin(spec_url, url) if url and spec_url else url


# ---------- importing ----------

def import_operations(spec_url: str, chosen: list[str], prefix: str, base: str = "", auth: dict | None = None,
                      text: str = "") -> list[dict]:
    """Make API tools from the chosen operations. auth: {"type": "none"} | {"type": "api_key", "in": "header"|
    "query", "name": "X-Api-Key", "value": "…"} | {"type": "oauth_client_credentials", "token_url", "client_id",
    "client_secret", "scope"}; the secret is sealed, never stored plainly."""
    spec = load(spec_url, text)
    base = _https(base or base_url(spec, spec_url))
    ops = {op["id"]: op for op in operations(spec)}
    missing = [c for c in chosen if c not in ops]
    if not chosen or missing:
        raise OpenApiImportError(f"choose operations from the spec{': unknown ' + ', '.join(missing) if missing else ''}")
    prefix = _slug(prefix or "")[:20]
    auth = dict(auth or {"type": "none"})
    if auth.get("type") not in AUTH_TYPES:
        raise OpenApiImportError(f"auth: one of {', '.join(AUTH_TYPES)}")
    secret = None
    if auth["type"] == "api_key":
        if auth.get("in") not in ("header", "query") or not auth.get("name") or not auth.get("value"):
            raise OpenApiImportError("an API key needs where it goes (header or query), its name and its value")
        secret = {"value": auth.pop("value")}
    elif auth["type"] == "oauth_client_credentials":
        if not auth.get("token_url") or not auth.get("client_id") or not auth.get("client_secret"):
            raise OpenApiImportError("OAuth client credentials need a token URL, a client id and a client secret")
        _https(auth["token_url"])
        secret = {"client_secret": auth.pop("client_secret")}
    try:
        sealed = secrets_box.seal(secret) if secret else None
    except secrets_box.SecretError as error:
        raise OpenApiImportError(str(error)) from None
    ensure_schema()
    made = []
    with _connect() as connection:
        for op_id in chosen:
            op = ops[op_id]
            name = "_".join(p for p in (prefix, op["id"]) if p)[:48]
            if not NAME.match(name):
                raise OpenApiImportError(f"{name!r} can't be a tool name: give a short prefix of letters")
            row = connection.execute("""
                INSERT INTO openapi_tools (name, title, description, base_url, method, path, parameters, body, auth,
                                           secret, side_effects, spec_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description,
                    base_url = EXCLUDED.base_url, method = EXCLUDED.method, path = EXCLUDED.path,
                    parameters = EXCLUDED.parameters, body = EXCLUDED.body, auth = EXCLUDED.auth,
                    secret = EXCLUDED.secret, side_effects = EXCLUDED.side_effects, spec_url = EXCLUDED.spec_url
                RETURNING *""", (name, op["summary"], op["description"], base, op["method"], op["path"],
                                 Jsonb(op["parameters"]), Jsonb(op["body"]), Jsonb(auth), sealed,
                                 op["side_effects"], spec_url)).fetchone()
            made.append(public(row))
    _forget()
    return made


def remove(name: str) -> bool:
    ensure_schema()
    with _connect() as connection:
        done = bool(connection.execute("DELETE FROM openapi_tools WHERE name = %s", (name,)).rowcount)
    _forget()
    return done


def rows() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("SELECT * FROM openapi_tools ORDER BY name").fetchall()


def public(row: dict) -> dict:
    return {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k != "secret"},
            "has_secret": bool(row.get("secret"))}


# ---------- as API tools (api_tools.all_tools) ----------

_cache = {"at": 0.0, "tools": {}}
CACHE_SECONDS = 30


def _forget() -> None:
    _cache["at"] = 0.0


def tools() -> dict:
    """name → api_tools.ApiTool for every imported operation (cached briefly)."""
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["tools"]
    from api_tools import ApiTool
    out = {}
    for row in rows():
        params = row["parameters"] or {}
        body = row["body"] or {}
        properties = {n: {**(p.get("schema") or {}), "description": p.get("description") or ""} for n, p in params.items()}
        properties.update({n: s for n, s in (body.get("properties") or {}).items() if n not in properties})
        required = [n for n, p in params.items() if p.get("required")] + [
            n for n in body.get("required") or [] if n not in params]
        out[row["name"]] = ApiTool(
            name=row["name"], title=row["title"],
            description=f"{row['title']}. {row['description']}".strip()[:900] + f" (imported: {row['method']} {row['path']})",
            properties=properties, required=required, run=_runner(row), side_effects=row["side_effects"],
            attribution=urllib.parse.urlsplit(row["base_url"]).netloc)
    _cache.update(at=time.monotonic(), tools=out)
    return out


def _token(row: dict) -> str:
    cached = _tokens.get(row["name"])
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    auth, secret = row["auth"], secrets_box.open_(row["secret"])
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": auth["client_id"],
                                   "client_secret": secret["client_secret"],
                                   **({"scope": auth["scope"]} if auth.get("scope") else {})}).encode()
    with urllib.request.urlopen(urllib.request.Request(auth["token_url"], data=data, method="POST"),
                                timeout=TIMEOUT) as reply:
        token = json.loads(reply.read())
    _tokens[row["name"]] = (token["access_token"], time.time() + float(token.get("expires_in") or 300))
    return token["access_token"]


def _value(schema: dict, value, explode: bool):
    if isinstance(value, list) and not explode:
        return ",".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def request_for(row: dict, args: dict) -> urllib.request.Request:
    """The HTTP request one call makes (path, query, header and JSON body arguments; auth)."""
    params, body_props = row["parameters"] or {}, (row["body"] or {}).get("properties") or {}
    path, query, headers, body = row["path"], [], {"Accept": "application/json", "User-Agent": "IngenMind"}, {}
    for name, value in args.items():
        spec = params.get(name)
        if spec is None:
            if name in body_props:
                body[name] = value
            continue
        value = _value(spec.get("schema") or {}, value, spec.get("explode", True))
        if spec["in"] == "path":
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        elif spec["in"] == "query":
            query += [(name, v) for v in value] if isinstance(value, list) else [(name, value)]
        elif spec["in"] == "header":
            headers[name] = str(value)
    auth = row["auth"] or {"type": "none"}
    if auth["type"] == "api_key":
        key = secrets_box.open_(row["secret"])["value"]
        if auth["in"] == "header":
            headers[auth["name"]] = key
        else:
            query.append((auth["name"], key))
    elif auth["type"] == "oauth_client_credentials":
        headers["Authorization"] = f"Bearer {_token(row)}"
    url = row["base_url"].rstrip("/") + path + (f"?{urllib.parse.urlencode(query, doseq=True)}" if query else "")
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, method=row["method"], headers=headers)


def _runner(row: dict):
    def run(args: dict) -> dict:
        from api_tools import failure
        started = time.monotonic()
        try:
            request = request_for(row, args)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
                raw = reply.read(2_000_000)
        except urllib.error.HTTPError as error:
            detail = error.read(300).decode(errors="replace")
            return failure(f"{row['title']}: HTTP {error.code} {detail}".strip(), time.monotonic() - started,
                           service=error.code >= 500)
        except (urllib.error.URLError, OSError, secrets_box.SecretError, KeyError) as error:
            return failure(f"{row['title']}: {error}", time.monotonic() - started, service=True)
        try:
            summary = json.loads(raw)
        except ValueError:
            summary = raw.decode(errors="replace")
        text = json.dumps(summary, ensure_ascii=False)
        if len(text) > MAX_SUMMARY:      # the model reads a bounded part of a long reply
            summary = {"truncated": True, "start": text[:MAX_SUMMARY]}
        host = urllib.parse.urlsplit(row["base_url"]).netloc
        return {"ok": True, "cached": False, "error": None, "seconds": round(time.monotonic() - started, 3),
                "summary": summary, "display": {"headline": row["title"], "facts": [], "links": [],
                                                "warning": None, "attribution": host}}
    return run

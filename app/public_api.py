"""The versioned API (R46): what /api/v1/ promises other systems, as an
OpenAPI document (/api/v1/openapi.json) and a docs page (/api/v1/docs). The
routes are in web_api.py; callers use an API key (api_keys.py) as
`Authorization: Bearer im_…`, or a browser session. Rate limits and the audit
log apply as for the key's owner.

v1 is stable: fields may be added, never renamed or removed.
"""

import html
import json

VERSION = "1.0.0"
ANSWER_FIELDS = ("answer", "status", "sources", "citations", "answer_check", "flow", "session_id", "run_id",
                 "waiting", "cached", "question", "read_as")


def answer(result: dict, session_id: str) -> dict:
    """An /ask result in the v1 shape: the answer, what it cites, whether it passed the answer check."""
    out = {k: result.get(k) for k in ANSWER_FIELDS if k in result}
    out.update(session_id=session_id, status=result.get("status") or "done", cached=bool(result.get("cached")))
    return out


_ERROR = {"description": "Refused or failed", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
_JSON = lambda ref: {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}
_ANY = {"content": {"application/json": {"schema": {"type": "object"}}}}

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "IngenMind API", "version": VERSION,
             "description": "Ask the live flow, read sessions, add documents and run evaluations. Authenticate with "
                            "an API key (Profile → API keys) as the header Authorization: Bearer im_…; a key only reaches "
                            "what its scopes and its owner's permissions cover. Fields may be added within v1, "
                            "never renamed or removed."},
    "servers": [{"url": "/api/v1"}],
    "security": [{"apiKey": []}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer", "description": "im_<id>_<secret>"}},
        "schemas": {
            "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
            "AskRequest": {"type": "object", "required": ["question"], "properties": {
                "question": {"type": "string"},
                "session_id": {"type": "string", "description": "Continue a conversation (from an earlier answer)"}}},
            "Source": {"type": "object", "properties": {
                "url": {"type": "string"}, "cited": {"type": "array", "items": {"type": "integer"}},
                "organisation": {"type": "string"}, "effective_date": {"type": "string"},
                "collection": {"type": "string"}}},
            "Answer": {"type": "object", "properties": {
                "answer": {"type": "string", "description": "With [n] citations"},
                "status": {"type": "string", "enum": ["done", "waiting"],
                           "description": "waiting: a person must clarify or approve first (Ask a human)"},
                "sources": {"type": "array", "items": {"$ref": "#/components/schemas/Source"}},
                "citations": {"type": "object", "description": "[n] → the passage it cites"},
                "answer_check": {"type": "object", "description": "passed, overall, scores, failed_on"},
                "flow": {"type": "object"}, "session_id": {"type": "string"}, "run_id": {"type": "string"},
                "cached": {"type": "boolean"}}},
            "Document": {"type": "object", "required": ["title", "text", "ttl_days"], "properties": {
                "title": {"type": "string"}, "text": {"type": "string"},
                "ttl_days": {"type": "number", "description": "How long it stays in the knowledge base"}}},
            "EvaluationRequest": {"type": "object", "required": ["flow_id", "set_id", "versions"], "properties": {
                "flow_id": {"type": "string"}, "set_id": {"type": "string"},
                "versions": {"type": "array", "items": {"oneOf": [{"type": "integer"}, {"const": "draft"}]}},
                "memory": {"type": "string", "enum": ["on", "off", "both"]}}},
        }},
    "paths": {
        "/ask": {"post": {"summary": "Ask the live flow a question (scope: ask)", "operationId": "ask",
                          "requestBody": {"required": True, **_JSON("AskRequest")},
                          "responses": {"200": {"description": "The answer", **_JSON("Answer")}, "401": _ERROR,
                                        "403": _ERROR, "429": _ERROR}}},
        "/sessions": {"get": {"summary": "Your sessions, newest first (scope: ask)", "operationId": "listSessions",
                              "responses": {"200": {"description": "Sessions", **_ANY}}}},
        "/sessions/{session_id}": {"get": {"summary": "One of your sessions, with its turns (scope: ask)",
                                           "operationId": "getSession",
                                           "parameters": [{"name": "session_id", "in": "path", "required": True,
                                                           "schema": {"type": "string"}}],
                                           "responses": {"200": {"description": "The session", **_ANY}, "404": _ERROR}}},
        "/documents": {
            "get": {"summary": "The ad-hoc documents in the knowledge base (scope: ingest)", "operationId": "listDocuments",
                    "responses": {"200": {"description": "Documents", **_ANY}}},
            "post": {"summary": "Add a document to the knowledge base (scope: ingest)", "operationId": "addDocument",
                     "requestBody": {"required": True, **_JSON("Document")},
                     "responses": {"200": {"description": "Ingested", **_ANY}, "400": _ERROR}}},
        "/flows": {"get": {"summary": "Flows and their versions, to evaluate (scope: evaluate)", "operationId": "listFlows",
                           "responses": {"200": {"description": "Flows", **_ANY}}}},
        "/evaluations": {
            "get": {"summary": "Evaluation runs (scope: evaluate)", "operationId": "listEvaluations",
                    "responses": {"200": {"description": "Runs", **_ANY}}},
            "post": {"summary": "Run a question set against flow versions (scope: evaluate)",
                     "operationId": "startEvaluation", "requestBody": {"required": True, **_JSON("EvaluationRequest")},
                     "responses": {"201": {"description": "Queued run ids", **_ANY}, "422": _ERROR}}},
        "/evaluations/{run_id}": {"get": {"summary": "One evaluation run and its results (scope: evaluate)",
                                          "operationId": "getEvaluation",
                                          "parameters": [{"name": "run_id", "in": "path", "required": True,
                                                          "schema": {"type": "string"}}],
                                          "responses": {"200": {"description": "The run", **_ANY}, "404": _ERROR}}},
    },
}


def docs_page() -> str:
    """A plain page from SPEC: every route with its scope, and a curl example."""
    rows = []
    for path, methods in SPEC["paths"].items():
        for method, op in methods.items():
            rows.append(f"<tr><td><code>{method.upper()}</code></td><td><code>/api/v1{html.escape(path)}</code></td>"
                        f"<td>{html.escape(op['summary'])}</td></tr>")
    example = ('curl -s https://YOUR-HOST/api/v1/ask \\\n  -H "Authorization: Bearer $INGENMIND_KEY" \\\n'
               '  -H "Content-Type: application/json" \\\n  -d \'{"question": "Can I take my bike on the train?"}\'')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>IngenMind API v1</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1f2937;background:#fff}}
table{{border-collapse:collapse;width:100%}}td{{border-top:1px solid #e5e7eb;padding:6px 8px;vertical-align:top}}
pre{{background:#f3f4f6;padding:12px;border-radius:8px;overflow:auto}}
@media (prefers-color-scheme: dark){{body{{background:#111827;color:#e5e7eb}}pre{{background:#1f2937}}td{{border-color:#374151}}}}</style>
</head><body><h1>IngenMind API v1</h1><p>{html.escape(SPEC['info']['description'])}</p>
<p>Make a key in the app under <strong>Profile → API keys</strong>. Scopes: <code>ask</code>, <code>ingest</code>,
<code>evaluate</code>, <code>admin</code>. The machine-readable description is
<a href="/api/v1/openapi.json">/api/v1/openapi.json</a> (OpenAPI {SPEC['openapi']}).</p>
<h2>Example</h2><pre>{html.escape(example)}</pre>
<h2>Routes</h2><table>{''.join(rows)}</table></body></html>"""


def spec_json() -> str:
    return json.dumps(SPEC)

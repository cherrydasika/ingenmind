"""A draft API tool from a data-source report (R40): the recommended source's
integration sketch (research.Integration) and profile become an `app/api_tools`
module in the shape of weather.py, for an engineer to finish and review in a
pull request. Nothing is written into the app and nothing is registered: the
admin downloads the file. When the source publishes an OpenAPI spec, the
report also points to importing it without code (openapi_tools.py, R39).
"""

import keyword
import re

TYPES = {"int": "integer", "integer": "integer", "number": "number", "float": "number", "decimal": "number",
         "bool": "boolean", "boolean": "boolean", "list": "array", "array": "array"}


class StubError(ValueError):
    """A report a tool can't be drafted from."""


def _name(text: str) -> str:
    name = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").lower())).strip("_")[:40]
    if not name or not name[0].isalpha() or keyword.iskeyword(name):
        name = f"tool_{name}".strip("_")
    return name


def parameters(inputs: str) -> list[dict]:
    """'station (CRS code), rows (int), arrivals (bool)' → [{name, type, description}]."""
    out = []
    for part in re.split(r",(?![^()]*\))", inputs or ""):
        match = re.match(r"\s*([A-Za-z_][\w ]*?)\s*(?:\(([^)]*)\))?\s*$", part)
        if not match or not match.group(1).strip():
            continue
        note = (match.group(2) or "").strip()
        kind = TYPES.get(note.lower().split()[0] if note else "", "string")
        name = _name(match.group(1))
        if name not in {p["name"] for p in out}:
            out.append({"name": name, "type": kind, "description": note if kind == "string" else ""})
    return out


def _literal(text) -> str:
    return repr(" ".join(str(text or "").split()))


def draft(report: dict) -> dict:
    """{"filename", "code", "tool_name", "registry_line", "openapi_url", "notes"} for a report recommending an
    api_tool; raises StubError otherwise."""
    recommendation = report.get("recommendation") or {}
    if recommendation.get("method") != "api_tool":
        raise StubError("this report doesn't recommend an API tool (its method is "
                        f"{recommendation.get('method') or 'none'})")
    sketch = recommendation.get("integration") or {}
    recommended = recommendation.get("recommended") or ""
    profile = next((p for p in report.get("profiles") or [] if p.get("name") == recommended), {})
    name = _name(sketch.get("tool_name") or recommended)
    params = parameters(sketch.get("inputs") or "")
    said = (sketch.get("endpoint") or "").strip()
    endpoint = said if re.match(r"^https?://\S+$", said) else ""   # the sketch may describe it in words
    needs_key = profile.get("auth") not in (None, "none", "unknown")
    env = f"{name.upper()}_API_KEY"
    properties = ",\n".join(
        f"        {p['name']!r}: {{\"type\": {p['type']!r}, \"description\": {_literal(p['description'] or p['name'])}}}"
        for p in params) or "        # no inputs in the sketch: add them"
    header = [
        f"{name}: {sketch.get('description') or recommended}",
        "",
        "DRAFT generated from a data-source report (R40): finish, test and review",
        "it in a pull request, then add it to TOOLS in app/api_tools/__init__.py.",
        "",
        f"Source: {recommended} ({profile.get('provider') or 'provider unknown'})",
        f"Endpoint: {said or 'unknown: see the docs'}",
        f"Docs: {profile.get('docs_url') or 'unknown'}",
        f"Auth: {profile.get('auth') or 'unknown'}" + (f" (key in the {env} environment variable)" if needs_key else ""),
        f"Format: {profile.get('format') or 'unknown'}",
        f"Pricing: {profile.get('pricing') or 'unknown'}; rate limits: {profile.get('rate_limits') or 'unknown'}",
        f"Licence and terms: {profile.get('licence_and_terms') or 'unknown'}",
        f"Notes: {sketch.get('notes') or '-'}",
        f"Report: {report.get('report_id', '')}",
    ]
    docstring = "\n".join(line.replace('"""', "'''") for line in header)
    key_lines = (f"\n    key = os.environ.get({env!r}, \"\")\n    if not key:\n"
                 f"        return failure(\"{name} needs {env} to be set\")\n"
                 "    params[\"key\"] = key   # TODO: where the key goes (a header or a parameter): see the docs\n"
                 if needs_key else "")
    code = f'''"""{docstring}
"""

import json
{"import os" + chr(10) if needs_key else ""}import time
import urllib.error
import urllib.parse
import urllib.request

from . import ApiTool, failure

ENDPOINT = {endpoint!r}   # TODO: {"confirm the URL and the parameters it takes" if endpoint else "the URL: see the docs"}
ATTRIBUTION = {_literal(f"Data from {recommended}")}


def _run(args: dict) -> dict:
    started = time.monotonic()
    params = {{k: v for k, v in args.items() if v not in (None, "")}}{key_lines}
    request = urllib.request.Request(f"{{ENDPOINT}}?{{urllib.parse.urlencode(params)}}",
                                     headers={{"Accept": "application/json", "User-Agent": "IngenMind"}})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        return failure(f"{name}: HTTP {{error.code}}", time.monotonic() - started, service=error.code >= 500)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return failure(f"{name}: {{error}}", time.monotonic() - started, service=True)
    # TODO: keep only what answers the question, so the agent reads a short summary
    return {{"ok": True, "cached": False, "error": None, "seconds": round(time.monotonic() - started, 3),
            "summary": data, "display": {{"headline": {_literal(recommended)}, "facts": [], "links": [],
                                         "warning": None, "attribution": ATTRIBUTION}}}}


TOOL = ApiTool(
    name={name!r},
    title={_literal(recommended or name)},
    description={_literal(sketch.get("description") or recommended)},
    properties={{
{properties}
    }},
    required={[p["name"] for p in params]!r},
    run=_run,
    attribution=ATTRIBUTION,
)
'''
    compile(code, f"{name}.py", "exec")         # a draft, but valid Python
    return {"filename": f"app/api_tools/{name}.py", "code": code, "tool_name": name,
            "registry_line": f"from . import {name}  # then add {name}.TOOL to TOOLS",
            "openapi_url": profile.get("spec_url"),
            "notes": [n for n in (
                f"Set {env} on the server." if needs_key else None,
                "The source publishes an OpenAPI spec: Admin → Integrations can import it without code."
                if profile.get("spec_url") else None,
                "The endpoint isn't known yet: find it in the docs." if not endpoint else None,
                "Its authentication isn't known: check the docs before integrating."
                if profile.get("auth") in (None, "unknown") else None) if n]}

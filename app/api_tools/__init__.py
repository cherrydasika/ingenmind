"""Registry of external-API tools for the External-APIs agent (app/agent.py).

Each module defines TOOL, an ApiTool: a name, a description and an input
schema for the model, and run(args) that calls the API. Every run returns
the same shape, so the agent, the graph and the UI handle any tool alike:

    {"ok", "cached", "error", "seconds",
     "summary": {...},          # what the model reads (JSON)
     "display": {"headline", "facts": [[label, value]], "links": [[label, url]],
                 "warning", "attribution"}}

To add an API: write a module defining its ApiTool(s) and add them to TOOLS
below (one module may define several, like swiss_transport).

Before a call runs, check() applies the tool guardrails (R13): the
arguments must match the tool's schema (no unknown ones, the right types,
strings at most MAX_STRING characters), carry no personal data, no links
and nothing that reads like instructions to an AI; each tool may be called
at most max_calls times in a run; and a tool with side_effects (one that
changes something outside, such as a booking) is refused unless the flow
has an approval step (Ask a human, R32): then the call is only proposed,
and runs once a person approves it.
"""

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import injection
import pii

MAX_STRING = 500
URL = re.compile(r"\bhttps?://|\bwww\.", re.IGNORECASE)
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


@dataclass
class ApiTool:
    name: str
    title: str
    description: str
    properties: dict
    required: list[str]
    run: Callable[[dict], dict]
    steps: list[list[str]] = field(default_factory=list)
    attribution: str = ""
    status: Callable[[], dict | None] = lambda: None
    side_effects: bool = False   # it changes something outside (books, sends, pays): needs a person's approval
    max_calls: int = 6           # in one run (one question)
    allow_urls: bool = False     # whether a string argument may be a link

    def spec(self) -> dict:
        """The harness's inline_function tool definition."""
        return {"type": "inline_function", "name": self.name, "config": {"inlineFunction": {
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "required": self.required},
        }}}

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "params": [{"name": n, "type": p.get("type", "any"), "description": p.get("description", ""),
                            "required": n in self.required} for n, p in self.properties.items()],
                "steps": self.steps, "attribution": self.attribution, "status": self.status()}


def failure(error: str, seconds: float = 0.0, service: bool = False) -> dict:
    """service: the API itself failed (unreachable, 5xx, quota), not the request: it counts towards the breaker."""
    return {"ok": False, "cached": False, "error": error, "seconds": round(seconds, 3), "summary": None, "display": None,
            "service_error": service}


from . import entry_requirements, swiss_transport, weather  # noqa: E402  (the modules use ApiTool)

TOOLS: dict[str, ApiTool] = {tool.name: tool for tool in (
    entry_requirements.TOOL,
    weather.TOOL,
    swiss_transport.CONNECTIONS,
    swiss_transport.DEPARTURES,
)}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def check(name: str, args, calls_so_far: int = 0, approval: bool = False) -> str | None:
    """Why this call must not run (or, with side effects, be proposed), or None (the tool guardrails).
    approval: the flow has an approval step, so a side-effecting call may be proposed."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"there is no tool {name!r}"
    if tool.side_effects and not approval:
        return f"{name} changes things outside this app and needs a person's approval, which this flow doesn't have"
    if calls_so_far >= tool.max_calls:
        return f"{name} was already called {calls_so_far} times for this question (the most is {tool.max_calls})"
    if not isinstance(args, dict):
        return "the arguments must be an object"
    unknown = sorted(set(args) - set(tool.properties))
    if unknown:
        return f"unknown argument(s): {', '.join(unknown)}"
    missing = [r for r in tool.required if args.get(r) in (None, "")]
    if missing:
        return f"missing argument(s): {', '.join(missing)}"
    for key, value in args.items():
        schema = tool.properties[key]
        expected = TYPES.get(schema.get("type"))
        if value is not None and expected and (not isinstance(value, expected)
                                               or (isinstance(value, bool) and schema.get("type") != "boolean")):
            return f"{key} must be a {schema.get('type')}"
        if "enum" in schema and value not in schema["enum"]:
            return f"{key} must be one of {', '.join(map(str, schema['enum']))}"
    for text in _strings(args):
        if len(text) > MAX_STRING:
            return f"an argument is longer than {MAX_STRING} characters"
        if found := pii.kinds(text):
            return f"an argument contains {pii.describe(found)}, which is never sent to outside services"
        if not tool.allow_urls and URL.search(text):
            return "an argument contains a link, which this tool doesn't take"
        if found := injection.flags(text):
            return f"an argument {injection.describe(found)}"
    return None


# Circuit breaker (R15): a tool that failed BREAK_AFTER times in a row is
# skipped for COOL_DOWN seconds, so a broken API doesn't slow every question.
BREAK_AFTER = 3
COOL_DOWN = 120.0
_failures: dict[str, list] = {}   # name → [consecutive failures, skipped until (monotonic)]
_breaker_lock = threading.Lock()


def breaker(name: str) -> dict | None:
    """The tool's breaker while it is open: {"failures", "seconds_left"}; else None."""
    with _breaker_lock:
        count, until = _failures.get(name, (0, 0.0))
    left = until - time.monotonic()
    return {"failures": count, "seconds_left": round(left)} if left > 0 else None


def _note(name: str, ok: bool) -> None:
    with _breaker_lock:
        if ok:
            _failures.pop(name, None)
            return
        count = _failures.get(name, [0, 0.0])[0] + 1
        _failures[name] = [count, time.monotonic() + COOL_DOWN if count >= BREAK_AFTER else 0.0]


def run(name: str, args: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return failure(f"Unknown tool {name!r}")
    if open_ := breaker(name):
        return failure(f"{name} is skipped for {open_['seconds_left']} s after {open_['failures']} failures in a row; "
                       "answer without it")
    try:
        result = tool.run(args)
    except Exception as error:
        result = failure(f"{name} failed: {type(error).__name__}: {error}", service=True)
    # A failure the request caused (a place that doesn't exist) isn't the API failing.
    _note(name, not result.get("service_error"))
    return result

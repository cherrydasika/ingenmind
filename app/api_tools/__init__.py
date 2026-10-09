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
"""

from dataclasses import dataclass, field
from typing import Callable


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


def failure(error: str, seconds: float = 0.0) -> dict:
    return {"ok": False, "cached": False, "error": error, "seconds": round(seconds, 3), "summary": None, "display": None}


from . import entry_requirements, swiss_transport, weather  # noqa: E402  (the modules use ApiTool)

TOOLS: dict[str, ApiTool] = {tool.name: tool for tool in (
    entry_requirements.TOOL,
    weather.TOOL,
    swiss_transport.CONNECTIONS,
    swiss_transport.DEPARTURES,
)}


def run(name: str, args: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return failure(f"Unknown tool {name!r}")
    try:
        return tool.run(args)
    except Exception as error:
        return failure(f"{name} failed: {type(error).__name__}: {error}")

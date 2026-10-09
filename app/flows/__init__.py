"""Flows: the agent graph as data (flow JSON), a registry of component
types, and a compiler to LangGraph. The visual builder edits the JSON; the
runtime (agent.py) compiles app/flows/default_flow.json into the graph it runs.
"""

import json
from pathlib import Path

from .compiler import FlowError, Runtime, compile_flow, validate
from .registry import COMPONENTS
from .spec import FlowSpec
from . import compiler, evals, store
from .diff import diff
from .templates import templates

DEFAULT_FLOW_PATH = Path(__file__).with_name("default_flow.json")


def load_flow(path: Path = DEFAULT_FLOW_PATH) -> FlowSpec:
    return FlowSpec.model_validate(json.loads(path.read_text()))


def components() -> list[dict]:
    return [c.describe() for c in COMPONENTS.values()]


__all__ = ["diff", "templates", "COMPONENTS", "DEFAULT_FLOW_PATH", "compiler", "evals", "store", "FlowError", "FlowSpec", "Runtime", "compile_flow", "components", "load_flow", "validate"]

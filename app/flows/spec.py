"""The flow JSON: a graph of components that the visual builder edits and the
compiler turns into a LangGraph StateGraph.

    {"id", "name", "version", "state": "main",
     "nodes": [{"id", "type", "label", "config", "position"}],
     "edges": [{"source", "target", "kind": "flow" | "resource", "outcome", "port"}],
     "subflows": {"knowledge_base": {...a nested flow...}}}

Two kinds of edge: "flow" edges are the order steps run in (an "outcome"
names the branch of a routed component), "resource" edges plug a resource
(LLM, retriever, memory, tools…) into a component's input "port"; they
configure components and never become graph edges.
"""

from typing import Literal

from pydantic import BaseModel, Field

NODE_ID = r"^[a-z][a-z0-9_]{0,63}$"


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeSpec(BaseModel):
    id: str = Field(pattern=NODE_ID)
    type: str
    label: str | None = None
    config: dict = Field(default_factory=dict)
    position: Position | None = None


class EdgeSpec(BaseModel):
    source: str
    target: str
    kind: Literal["flow", "resource"] = "flow"
    outcome: str | None = None   # flow edges out of a routed component: which branch
    port: str | None = None      # resource edges: the target's input it plugs into


class FlowSpec(BaseModel):
    id: str = Field(pattern=NODE_ID)
    name: str
    version: int = 1
    description: str = ""
    state: str = "main"          # which state schema the graph runs on
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    subflows: dict[str, "FlowSpec"] = Field(default_factory=dict)

    def node(self, node_id: str) -> NodeSpec | None:
        return next((n for n in self.nodes if n.id == node_id), None)

"""Validate a flow spec and compile it into a LangGraph StateGraph.

The runtime (agent.py) supplies a Runtime: the state schema per flow
"state", a factory per component "impl" (config → node function), a router
per routed impl (state → outcome), and a fan-out router factory for
components that delegate in parallel. Gates (the input guardrail) run before
the graph, so they are skipped when wiring START.
"""

from dataclasses import dataclass, field
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .registry import COMPONENTS, Component
from .spec import EdgeSpec, FlowSpec


class FlowError(ValueError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("; ".join(f"{i['where']}: {i['message']}" for i in issues if i["level"] == "error"))


@dataclass
class Runtime:
    states: dict[str, type]
    factories: dict[str, Callable]                  # impl → (node spec) → node function
    routers: dict[str, Callable] = field(default_factory=dict)   # impl → (state) → outcome
    fan_out: dict[str, Callable] = field(default_factory=dict)   # impl → (targets) → router returning str | [Send]
    delegates: set[str] | None = None               # names a fan-out may delegate to (None: any)
    impl_states: dict[str, set[str]] = field(default_factory=dict)  # impl → the flow states it can run in
    subflow_state: str | None = None                # the state a delegated subflow must run on


# ---------- validation ----------

def _issue(level: str, where: str, message: str) -> dict:
    return {"level": level, "where": where, "message": message}


def validate(spec: FlowSpec, path: str = "") -> list[dict]:
    """Every problem with the flow, as [{"level": "error"|"warning", "where", "message"}]."""
    issues: list[dict] = []
    where = path or spec.id
    nodes = {}
    for node in spec.nodes:
        if node.id in nodes:
            issues.append(_issue("error", f"{where}.{node.id}", "duplicate node id"))
        nodes[node.id] = node
        component = COMPONENTS.get(node.type)
        if component is None:
            issues.append(_issue("error", f"{where}.{node.id}", f"unknown component type {node.type!r}"))
            continue
        try:
            component.config.model_validate(node.config)
        except ValueError as error:
            issues.append(_issue("error", f"{where}.{node.id}", f"invalid config: {error}"))
        if node.type == "subflow":
            name = node.config.get("subflow")
            if name not in spec.subflows:
                issues.append(_issue("error", f"{where}.{node.id}", f"unknown subflow {name!r}"))
        if component.kind == "resource" and not component.runnable:
            issues.append(_issue("warning", f"{where}.{node.id}", f"{component.title} is not supported by the runtime yet"))
    starts = [n for n in spec.nodes if n.type == "start"]
    ends = [n for n in spec.nodes if n.type == "end"]
    if len(starts) != 1:
        issues.append(_issue("error", where, f"needs exactly one start node (has {len(starts)})"))
    if not ends:
        issues.append(_issue("error", where, "needs an end node"))

    flow_edges = [e for e in spec.edges if e.kind == "flow"]
    for edge in spec.edges:
        for end in (edge.source, edge.target):
            if end not in nodes:
                issues.append(_issue("error", f"{where}.{edge.source}→{edge.target}", f"unknown node {end!r}"))
    if any(i["level"] == "error" for i in issues):
        return issues

    kind = {n.id: COMPONENTS[n.type].kind for n in spec.nodes}
    for edge in spec.edges:
        label = f"{where}.{edge.source}→{edge.target}"
        if edge.kind == "resource":
            source, target = COMPONENTS[nodes[edge.source].type], COMPONENTS[nodes[edge.target].type]
            port = edge.port or source.provides
            if source.kind != "resource":
                issues.append(_issue("error", label, "a resource edge must start at a resource"))
            elif port != source.provides:
                issues.append(_issue("error", label, f"{source.title} provides {source.provides!r}, not {port!r}"))
            elif port not in target.inputs:
                issues.append(_issue("error", label, f"{target.title} has no {port!r} input"))
        elif kind[edge.source] == "resource" or kind[edge.target] == "resource":
            issues.append(_issue("error", label, "flow edges connect steps, not resources"))

    for node in spec.nodes:
        component = COMPONENTS[node.type]
        if component.kind == "resource" or node.type == "end":
            continue
        out = [e for e in flow_edges if e.source == node.id]
        label = f"{where}.{node.id}"
        if component.outcomes or component.fan_out:
            allowed = set(component.outcomes)
            seen = [e.outcome for e in out]
            for edge in out:
                ok = edge.outcome in allowed or (component.fan_out and (edge.outcome or "").startswith("delegate:"))
                if not ok:
                    issues.append(_issue("error", label, f"unknown outcome {edge.outcome!r}"))
                elif component.fan_out and edge.outcome.startswith("delegate:"):
                    expected = _delegate_name(nodes.get(edge.target))
                    if expected is None:
                        issues.append(_issue("error", label, f"can only delegate to an agent or a subflow, not {edge.target!r}"))
                    elif edge.outcome != f"delegate:{expected}":
                        issues.append(_issue("error", label, f"delegation to {edge.target!r} must be 'delegate:{expected}'"))
            for outcome in component.outcomes:
                if seen.count(outcome) != 1:
                    issues.append(_issue("error", label, f"outcome {outcome!r} needs exactly one edge (has {seen.count(outcome)})"))
            if component.fan_out and not any((o or "").startswith("delegate:") for o in seen):
                issues.append(_issue("warning", label, "delegates to no specialist"))
        elif len(out) != 1 or out[0].outcome:
            issues.append(_issue("error", label, f"needs exactly one outgoing flow edge without an outcome (has {len(out)})"))

    steps = [n.id for n in spec.nodes if kind[n.id] != "resource"]
    graph = {n: [e.target for e in flow_edges if e.source == n] for n in steps}
    if starts and ends:
        reached = _reach(graph, starts[0].id)
        for n in steps:
            if n not in reached:
                issues.append(_issue("error", f"{where}.{n}", "not reachable from start"))
        reverse = {n: [s for s in steps if n in graph[s]] for n in steps}
        end_ids = {e.id for e in ends}
        can_end = set().union(*(_reach(reverse, e) for e in end_ids))
        for n in steps:
            if n not in can_end:
                issues.append(_issue("error", f"{where}.{n}", "cannot reach an end"))
    # A cycle that avoids every loop-bounding component can run forever.
    unbounded = {n: [t for t in out if not COMPONENTS[nodes[t].type].bounds_loops]
                 for n, out in graph.items() if not COMPONENTS[nodes[n].type].bounds_loops}
    for cycle in _cycles(unbounded):
        issues.append(_issue("error", f"{where}", f"unbounded loop through {', '.join(sorted(cycle))}: "
                                                   "route it through a component that limits it"))
    if not path and spec.state == "main":   # a subflow's End returns to its parent
        issues += _guardrail_policy(spec, graph, starts, kind)
    used = [n.config.get("subflow") for n in spec.nodes if n.type == "subflow"]
    for name, sub in spec.subflows.items():
        if used.count(name) > 1:
            issues.append(_issue("error", f"{where}/{name}", "a subflow can be used by one node only"))
        elif name not in used:
            issues.append(_issue("warning", f"{where}/{name}", "subflow is not used by any node"))
        issues += validate(sub, f"{where}/{name}")
    return issues


def _guardrail_policy(spec: FlowSpec, graph: dict, starts: list, kind: dict) -> list[dict]:
    """Travel-only guardrails are not optional: the input check runs before
    every flow, and every answer passes an output guardrail before End."""
    issues = []
    if not any(n.type == "input_guardrail" for n in spec.nodes):
        issues.append(_issue("warning", spec.id, "the input guardrail runs before every flow, even when not drawn"))
    if not starts:
        return issues
    # Paths that skip the output guardrail: drop its outgoing edges and a
    # gate's BLOCK edge (a blocked question ends with the policy message).
    guarded = {n.id for n in spec.nodes if n.type == "output_guardrail"}
    gates = {n.id for n in spec.nodes if kind.get(n.id) == "gate"}
    skipping = {}
    for n, targets in graph.items():
        if n in guarded:
            skipping[n] = []
        elif n in gates:
            skipping[n] = [e.target for e in spec.edges
                           if e.kind == "flow" and e.source == n and e.outcome != "BLOCK"]
        else:
            skipping[n] = targets
    ends = {n.id for n in spec.nodes if n.type == "end"}
    if _reach(skipping, starts[0].id) & ends:
        issues.append(_issue("error", spec.id, "every answer must pass an output guardrail before End"))
    return issues


def _delegate_name(node) -> str | None:
    """What the supervisor calls a delegation target: its role, or its subflow."""
    if node is None or node.type not in ("specialist_agent", "subflow"):
        return None
    try:
        config = COMPONENTS[node.type].config.model_validate(node.config)
    except ValueError:
        return None
    return getattr(config, "role", None) or getattr(config, "subflow", None)


def _reach(graph: dict, start: str) -> set:
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += graph.get(n, [])
    return seen


def _cycles(graph: dict) -> list[set]:
    """Strongly connected components with a cycle (Tarjan)."""
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, []):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = set()
            while True:
                w = stack.pop()
                on.discard(w)
                group.add(w)
                if w == v:
                    break
            if len(group) > 1 or v in graph.get(v, []):
                out.append(group)

    for v in graph:
        if v not in index:
            visit(v)
    return out


# ---------- compile ----------

def compile_flow(spec: FlowSpec, runtime: Runtime, checkpointer=None):
    """A compiled LangGraph graph for the flow (subflows compiled into it)."""
    errors = runnable_issues(spec, runtime)
    if errors:
        raise FlowError(errors)
    return _build(spec, runtime).compile(checkpointer=checkpointer)


def runtime_issues(spec: FlowSpec, runtime: Runtime, path: str = "") -> list[dict]:
    """What the runtime cannot run: an unknown state schema or implementation."""
    where = path or spec.id
    issues = []
    if spec.state not in runtime.states:
        issues.append(_issue("error", where, f"the runtime has no state {spec.state!r} "
                                             f"(known: {', '.join(sorted(runtime.states))})"))
    for node in spec.nodes:
        component = COMPONENTS[node.type]
        if component.kind == "flow" and component.impl and component.impl not in runtime.factories:
            issues.append(_issue("error", f"{where}.{node.id}", f"the runtime cannot run {component.title} yet"))
        allowed = runtime.impl_states.get(component.impl or "")
        if allowed and spec.state not in allowed:
            issues.append(_issue("error", f"{where}.{node.id}", f"{component.title} cannot run in a "
                                                                f"{spec.state!r} flow (only {', '.join(sorted(allowed))})"))
        if node.type == "subflow" and runtime.subflow_state:
            sub = spec.subflows.get(node.config.get("subflow"))
            if sub is not None and sub.state != runtime.subflow_state:
                issues.append(_issue("error", f"{where}.{node.id}",
                                     f"a delegated subflow must use state {runtime.subflow_state!r}"))
    if runtime.delegates is not None:
        for edge in spec.edges:
            name = (edge.outcome or "").removeprefix("delegate:")
            if edge.kind == "flow" and (edge.outcome or "").startswith("delegate:") and name not in runtime.delegates:
                issues.append(_issue("error", f"{where}.{edge.source}", f"cannot delegate to {name!r}: the supervisor "
                                                                        f"knows {', '.join(sorted(runtime.delegates))}"))
    for name, sub in spec.subflows.items():
        issues += runtime_issues(sub, runtime, f"{where}/{name}")
    return issues


def runnable_issues(spec: FlowSpec, runtime: Runtime) -> list[dict]:
    """Errors that stop the flow from running: validation plus the runtime."""
    return [i for i in validate(spec) if i["level"] == "error"] + runtime_issues(spec, runtime)


def _target(spec: FlowSpec, node_id: str) -> str:
    """Graph name of a flow edge's target: END for end nodes, through gates."""
    node = spec.node(node_id)
    if node.type == "end":
        return END
    if COMPONENTS[node.type].kind == "gate":
        # Gates run before the graph; what passes them continues on ALLOW.
        allow = next(e for e in spec.edges if e.kind == "flow" and e.source == node_id and e.outcome == "ALLOW")
        return _target(spec, allow.target)
    return node_id


def _single_next(spec: FlowSpec, node_id: str) -> EdgeSpec:
    return next(e for e in spec.edges if e.kind == "flow" and e.source == node_id)


def _build(spec: FlowSpec, runtime: Runtime) -> StateGraph:
    graph = StateGraph(runtime.states[spec.state])
    steps = [n for n in spec.nodes if COMPONENTS[n.type].kind == "flow"]
    for node in steps:
        component = COMPONENTS[node.type]
        if node.type == "subflow":
            graph.add_node(node.id, _build(spec.subflows[node.config["subflow"]], runtime).compile())
        else:
            # The runtime sees every setting, defaults included.
            settled = node.model_copy(update={"config": component.config.model_validate(node.config).model_dump()})
            graph.add_node(node.id, runtime.factories[component.impl](settled))
    start = next(n for n in spec.nodes if n.type == "start")
    graph.add_edge(START, _target(spec, _single_next(spec, start.id).target))
    for node in steps:
        component = COMPONENTS[node.type]
        out = [e for e in spec.edges if e.kind == "flow" and e.source == node.id]
        if component.fan_out:
            targets = {e.outcome: _target(spec, e.target) for e in out}
            payload_kind = {t: COMPONENTS[spec.node(t).type].accepts_subflow_payload for t in targets.values() if t != END}
            router = runtime.fan_out[component.impl](targets, payload_kind)
            graph.add_conditional_edges(node.id, router, sorted(set(targets.values())))
        elif component.outcomes:
            path = {e.outcome: _target(spec, e.target) for e in out}
            route = runtime.routers[component.impl]
            graph.add_conditional_edges(node.id, lambda state, route=route: route(state), path)
        else:
            graph.add_edge(node.id, _target(spec, out[0].target))
    return graph


def subflow_payload(task: dict, wrap: bool):
    """What a delegated task looks like to its target (a Send's argument)."""
    return {"task": task} if wrap else task


__all__ = ["FlowError", "Runtime", "Send", "compile_flow", "runnable_issues", "runtime_issues", "subflow_payload", "validate", "Component"]

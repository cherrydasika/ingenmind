"""What changed between two versions of a flow: nodes added, removed or
changed (label, settings), edges added or removed, and flow details. Node
positions are ignored."""

from .spec import FlowSpec


def _edge_key(edge) -> tuple:
    return (edge.source, edge.target, edge.kind, edge.outcome or "", edge.port or "")


def _edge_text(edge) -> str:
    branch = f" ({edge.outcome})" if edge.outcome else ""
    return f"{edge.source} {'⇢' if edge.kind == 'resource' else '→'} {edge.target}{branch}"


def diff(old: FlowSpec, new: FlowSpec, path: str = "") -> list[dict]:
    """[{"flow", "change": added|removed|changed, "kind": node|edge|flow|subflow, "id", "details"}]"""
    where = path or new.id
    changes = []
    for key in ("name", "description", "state"):
        if getattr(old, key) != getattr(new, key):
            changes.append({"flow": where, "change": "changed", "kind": "flow", "id": key,
                            "details": {key: [getattr(old, key), getattr(new, key)]}})
    before = {n.id: n for n in old.nodes}
    after = {n.id: n for n in new.nodes}
    for node_id in after.keys() - before.keys():
        changes.append({"flow": where, "change": "added", "kind": "node", "id": node_id,
                        "details": {"type": after[node_id].type}})
    for node_id in before.keys() - after.keys():
        changes.append({"flow": where, "change": "removed", "kind": "node", "id": node_id,
                        "details": {"type": before[node_id].type}})
    for node_id in before.keys() & after.keys():
        a, b = before[node_id], after[node_id]
        details = {}
        if a.type != b.type:
            details["type"] = [a.type, b.type]
        if (a.label or None) != (b.label or None):
            details["label"] = [a.label, b.label]
        for key in sorted(a.config.keys() | b.config.keys()):
            if a.config.get(key) != b.config.get(key):
                details[f"config.{key}"] = [a.config.get(key), b.config.get(key)]
        if details:
            changes.append({"flow": where, "change": "changed", "kind": "node", "id": node_id, "details": details})
    old_edges = {_edge_key(e): e for e in old.edges}
    new_edges = {_edge_key(e): e for e in new.edges}
    for key in new_edges.keys() - old_edges.keys():
        changes.append({"flow": where, "change": "added", "kind": "edge", "id": _edge_text(new_edges[key]), "details": {}})
    for key in old_edges.keys() - new_edges.keys():
        changes.append({"flow": where, "change": "removed", "kind": "edge", "id": _edge_text(old_edges[key]), "details": {}})
    for name in new.subflows.keys() - old.subflows.keys():
        changes.append({"flow": where, "change": "added", "kind": "subflow", "id": name, "details": {}})
    for name in old.subflows.keys() - new.subflows.keys():
        changes.append({"flow": where, "change": "removed", "kind": "subflow", "id": name, "details": {}})
    for name in old.subflows.keys() & new.subflows.keys():
        changes += diff(old.subflows[name], new.subflows[name], f"{where}/{name}")
    order = {"flow": 0, "subflow": 1, "node": 2, "edge": 3}
    return sorted(changes, key=lambda c: (c["flow"].count("/"), c["flow"], order[c["kind"]], c["change"], c["id"]))

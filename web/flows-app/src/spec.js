// Canvas → flow JSON, ids for new nodes, and what may connect to what.

export const rawId = (canvasId) => canvasId.split("/").pop();

/** The flow JSON the canvas shows (positions rounded; groups become subflows). */
export function toSpec(nodes, edges, meta) {
  const build = (parentId, path) => {
    const members = nodes.filter((n) => (n.parentId || null) === parentId);
    const ids = new Set(members.map((n) => n.id));
    const flow = {
      ...(meta.flows[path] || { id: path.split("/").pop(), name: path.split("/").pop(), state: "task" }),
      nodes: members.map((n) => ({
        ...n.data.node,
        position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      })),
      edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => e.data.spec),
      subflows: {},
    };
    for (const group of members.filter((n) => n.type === "subflow")) {
      const name = group.data.node.config.subflow;
      flow.subflows[name] = build(group.id, `${path}/${name}`);
    }
    return flow;
  };
  return build(null, meta.root);
}

/** The same flow without positions: what validation and "changed?" care about less. */
export function withoutPositions(spec) {
  return {
    ...spec,
    nodes: spec.nodes.map(({ position: _p, ...n }) => n),
    subflows: Object.fromEntries(Object.entries(spec.subflows || {}).map(([k, v]) => [k, withoutPositions(v)])),
  };
}

export function uniqueId(base, taken) {
  const clean = base.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^[^a-z]+/, "") || "node";
  if (!taken.has(clean)) return clean;
  for (let i = 2; ; i++) if (!taken.has(`${clean}_${i}`)) return `${clean}_${i}`;
}

/** Delegation outcome for an edge from a fan-out node to `target`. */
export function delegateOutcome(target) {
  // The supervisor routes a task by agent role (or subflow name): match it.
  const config = target.data.node.config || {};
  const roleDefault = target.data.component?.config_schema?.properties?.role?.default;
  return `delegate:${config.role || config.subflow || roleDefault || rawId(target.id)}`;
}

/** Why a connection is not allowed, or null when it is. */
export function connectionProblem(conn, nodes, edges) {
  const source = nodes.find((n) => n.id === conn.source);
  const target = nodes.find((n) => n.id === conn.target);
  if (!source || !target || source.id === target.id) return "pick two different nodes";
  if ((source.parentId || null) !== (target.parentId || null)) return "connect nodes inside the same flow";
  const sc = source.data.component, tc = target.data.component;
  if (conn.sourceHandle === "provides") {
    if (conn.targetHandle !== "inputs") return "plug a resource into a port (left side)";
    const port = sc.provides || sc.plugs;
    if (!tc?.inputs?.includes(port)) return `${tc?.title || "this"} has no ${port} input`;
    if (edges.some((e) => e.source === source.id && e.target === target.id)) return "already plugged in";
    if (sc.plugs) {   // an agent plugged into another agent: a sub-agent (#66)
      if (edges.some((e) => e.source === source.id && e.data?.resource)) return "a sub-agent plugs into one agent only";
      const below = new Set([source.id]);
      for (let grew = true; grew;) {
        grew = false;
        for (const e of edges) if (e.data?.resource && below.has(e.target) && !below.has(e.source)
          && nodes.find((n) => n.id === e.source)?.data.component?.plugs) { below.add(e.source); grew = true; }
      }
      if (below.has(target.id)) return "that would put the agents in a loop";
    }
    return null;
  }
  if (conn.targetHandle !== "in") return "flow edges go into the top of a step";
  if (tc?.kind === "resource" || target.data.node.type === "start") return "a flow edge cannot end here";
  if (conn.sourceHandle === "delegate") {
    if (!["specialist_agent", "subflow"].includes(target.data.node.type)) return "delegate to an agent or a subflow";
    if (edges.some((e) => e.source === source.id && e.target === target.id)) return "already delegated";
    return null;
  }
  if (edges.some((e) => e.source === source.id && e.sourceHandle === conn.sourceHandle)) {
    return "that branch is already connected; delete its edge first";
  }
  return null;
}

/** The name an agent or evaluator goes by: its label, else its id (unique, case-insensitive). */
export function nodeName(node) {
  return (node.label || node.id || "").trim();
}
export const nameKey = (name) => name.toLowerCase().split(/\s+/).filter(Boolean).join(" ");

/** Default settings a new node needs (required fields without defaults). */
export function initialConfig(type, extra = {}) {
  if (type === "placeholder") return { message: "Not implemented yet" };
  return extra;
}

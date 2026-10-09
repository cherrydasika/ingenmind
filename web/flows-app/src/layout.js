// Flow JSON → React Flow nodes and edges. A subflow node becomes a group that
// holds its nested flow; nested canvas ids are "<group id>/<node id>". Saved
// positions are kept; a flow without them (or "auto layout") is laid out with
// dagre: steps by their flow edges, resources in a column on the left.
import dagre from "@dagrejs/dagre";

export const GROUP_PAD = { top: 48, side: 28, bottom: 28 };
const RESOURCE_GAP = { x: 90, y: 14 };

/** Handles leaving a node: one per outcome for routed components. */
export function sourceHandles(component) {
  if (!component || component.kind === "resource") return [];
  const outcomes = component.outcomes || [];
  if (!outcomes.length && !component.fan_out) return [{ id: "out", label: null }];
  const handles = outcomes.map((o) => ({ id: `out:${o}`, label: o, outcome: o }));
  if (component.fan_out) handles.unshift({ id: "delegate", label: "delegate", delegate: true });
  return handles;
}

export function nodeSize(component) {
  if (!component) return [232, 72];
  if (component.kind === "control") return [104, 38];
  if (component.kind === "resource") return [196, 54];
  const handles = sourceHandles(component);
  if (handles.length > 1 || handles[0]?.label) return [Math.max(232, handles.length * 46), 100];
  return [232, 72];
}

export function handleFor(spec, component) {
  if (spec.kind === "resource") return "provides";
  if (component?.fan_out && (spec.outcome || "").startsWith("delegate:")) return "delegate";
  if (spec.outcome) return `out:${spec.outcome}`;
  return "out";
}

function dagreLayout(items, links) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 78, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const item of items) g.setNode(item.id, { width: item.width, height: item.height });
  for (const link of links) g.setEdge(link.source, link.target, { weight: link.weight ?? 1, minlen: link.minlen ?? 1 });
  dagre.layout(g);
  const positions = {};
  for (const item of items) {
    const n = g.node(item.id);
    positions[item.id] = { x: n.x - item.width / 2, y: n.y - item.height / 2 };
  }
  return positions;
}

function placeResources(resources, edges, positions, steps) {
  if (!resources.length) return;
  const height = Object.fromEntries([...steps, ...resources].map((item) => [item.raw, item.height]));
  const centreY = (raw) => positions[raw].y + height[raw] / 2;
  const left = steps.length ? Math.min(...steps.map((item) => positions[item.raw].x)) : 0;
  const width = Math.max(...resources.map((item) => item.width));
  const wanted = {};
  // Resources feeding other resources (vector DB → retriever) follow their consumer: two passes.
  for (let pass = 0; pass < 2; pass++) {
    for (const item of resources) {
      const ys = edges.filter((e) => e.source === item.raw && (positions[e.target] || wanted[e.target] !== undefined))
        .map((e) => (positions[e.target] ? centreY(e.target) : wanted[e.target]));
      wanted[item.raw] = ys.length ? Math.min(...ys) : 0;
    }
  }
  let next = -Infinity;
  for (const item of [...resources].sort((a, b) => wanted[a.raw] - wanted[b.raw])) {
    const y = Math.max(wanted[item.raw] - item.height / 2, next);
    positions[item.raw] = { x: left - RESOURCE_GAP.x - width, y };
    next = y + item.height + RESOURCE_GAP.y;
  }
}

function autoPositions(flow, items) {
  const steps = items.filter((item) => item.component?.kind !== "resource");
  const resources = items.filter((item) => item.component?.kind === "resource");
  // A fan-out's own branch (the supervisor's "answer") goes below the agents it delegates to.
  const fanOut = new Set(items.filter((item) => item.component?.fan_out).map((item) => item.raw));
  const known = new Set(items.map((item) => item.raw));
  const links = flow.edges.filter((e) => e.kind !== "resource" && known.has(e.source) && known.has(e.target)).map((e) => ({
    source: e.source, target: e.target, weight: e.outcome === "error" ? 1 : 3,
    minlen: fanOut.has(e.source) && !(e.outcome || "").startsWith("delegate:") ? 2 : 1,
  }));
  const positions = dagreLayout(steps.map((s) => ({ ...s, id: s.raw })), links);
  placeResources(resources, flow.edges.filter((e) => e.kind === "resource"), positions, steps);
  const xs = Object.values(positions);
  const minX = Math.min(...xs.map((p) => p.x)), minY = Math.min(...xs.map((p) => p.y));
  for (const p of xs) {
    p.x -= minX;
    p.y -= minY;
  }
  return positions;
}

function convert(flow, catalogue, parent, out, meta, path, auto) {
  const prefix = parent ? `${parent}/` : "";
  const items = [];
  for (const node of flow.nodes) {
    const component = catalogue[node.type];
    let [width, height] = nodeSize(component);
    let inner = null;
    if (node.type === "subflow" && flow.subflows?.[node.config?.subflow]) {
      const name = node.config.subflow;
      inner = { nodes: [], edges: [] };
      const box = convert(flow.subflows[name], catalogue, prefix + node.id, inner, meta, `${path}/${name}`, auto);
      width = Math.max(box.width + GROUP_PAD.side * 2, 320);
      height = Math.max(box.height + GROUP_PAD.top + GROUP_PAD.bottom, 160);
    }
    items.push({ raw: node.id, node, component, width, height, inner });
  }
  const positioned = !auto && items.length && items.every((item) => item.node.position);
  const positions = positioned
    ? Object.fromEntries(items.map((item) => [item.raw, { ...item.node.position }]))
    : autoPositions(flow, items);
  let maxX = 0, maxY = 0;
  for (const item of items) {
    maxX = Math.max(maxX, positions[item.raw].x + item.width);
    maxY = Math.max(maxY, positions[item.raw].y + item.height);
  }
  const { nodes: _n, edges: _e, subflows: _s, ...flowMeta } = flow;
  meta[path] = flowMeta;

  for (const item of items) {
    const { node, component } = item;
    const rfNode = {
      id: prefix + item.raw,
      type: item.inner ? "subflow" : component?.kind === "resource" ? "resource" : component?.kind === "control" ? "control" : "step",
      position: positions[item.raw],
      data: { node: stripPosition(node), component, status: null, issues: [], path },
      style: { width: item.width, height: item.height },
      // A subflow box moves by its title bar only: its body lets clicks and
      // drags through, so dragging inside it pans the canvas (flows.css).
      ...(item.inner ? { dragHandle: ".fl-group-head" } : {}),
    };
    if (parent) {
      rfNode.parentId = parent;
      rfNode.position = positioned ? rfNode.position
        : { x: rfNode.position.x + GROUP_PAD.side, y: rfNode.position.y + GROUP_PAD.top };
    }
    out.nodes.push(rfNode);
    if (item.inner) {
      out.nodes.push(...item.inner.nodes);
      out.edges.push(...item.inner.edges);
    }
  }
  for (const e of flow.edges) out.edges.push(makeEdge(e, prefix, catalogue[flow.nodes.find((n) => n.id === e.source)?.type]));
  return { width: maxX, height: maxY };
}

function stripPosition(node) {
  const { position: _p, ...rest } = node;
  return { config: {}, ...rest };
}

let edgeSeq = 0;

export function makeEdge(spec, prefix, sourceComponent) {
  const resource = spec.kind === "resource";
  const clean = { source: spec.source, target: spec.target, kind: spec.kind || "flow",
                  ...(spec.outcome ? { outcome: spec.outcome } : {}), ...(spec.port ? { port: spec.port } : {}) };
  return {
    id: `e${++edgeSeq}:${prefix}${spec.source}->${spec.target}`,
    source: prefix + spec.source, target: prefix + spec.target,
    sourceHandle: handleFor(clean, sourceComponent),
    targetHandle: resource ? "inputs" : "in",
    type: resource ? "default" : "smoothstep",
    label: (spec.outcome || "").startsWith("delegate:") ? spec.outcome.slice(9).replace(/_/g, " ") : undefined,
    className: resource ? "edge-resource" : spec.outcome === "error" ? "edge-error" : "edge-flow",
    data: { spec: clean, resource },
    markerEnd: resource ? undefined : { type: "arrowclosed", width: 16, height: 16 },
    zIndex: prefix ? 1 : 0,
  };
}

/** {nodes, edges, meta}: meta maps a flow path ("travel_assistant",
 *  "travel_assistant/knowledge_base") to its id, name, state, description… */
export function layoutFlow(flow, components, { auto = false } = {}) {
  const catalogue = Object.fromEntries(components.map((c) => [c.type, c]));
  const out = { nodes: [], edges: [] };
  const meta = {};
  convert(flow, catalogue, "", out, meta, flow.id, auto);
  return { ...out, meta: { root: flow.id, flows: meta } };
}

/** Grow or shrink each group around its children (and keep them inside it). */
export function fitGroups(nodes) {
  const groups = nodes.filter((n) => n.type === "subflow");
  if (!groups.length) return nodes;
  const byId = new Map(nodes.map((n) => [n.id, { ...n }]));
  // Innermost groups first, so an outer group fits the inner one's new size.
  const depth = (n) => (n.parentId ? 1 + depth(byId.get(n.parentId)) : 0);
  for (const group of [...groups].sort((a, b) => depth(b) - depth(a))) {
    const g = byId.get(group.id);
    const kids = [...byId.values()].filter((n) => n.parentId === g.id);
    if (!kids.length) continue;
    const w = (n) => n.style?.width ?? n.measured?.width ?? 200;
    const h = (n) => n.style?.height ?? n.measured?.height ?? 60;
    const minX = Math.min(...kids.map((k) => k.position.x)), minY = Math.min(...kids.map((k) => k.position.y));
    const maxX = Math.max(...kids.map((k) => k.position.x + w(k))), maxY = Math.max(...kids.map((k) => k.position.y + h(k)));
    const dx = GROUP_PAD.side - minX, dy = GROUP_PAD.top - minY;
    for (const k of kids) byId.set(k.id, { ...byId.get(k.id), position: { x: k.position.x + dx, y: k.position.y + dy } });
    byId.set(g.id, {
      ...g,
      position: { x: g.position.x - dx, y: g.position.y - dy },
      style: { ...g.style, width: Math.max(maxX - minX + GROUP_PAD.side * 2, 320),
               height: Math.max(maxY - minY + GROUP_PAD.top + GROUP_PAD.bottom, 160) },
    });
  }
  return nodes.map((n) => byId.get(n.id));
}

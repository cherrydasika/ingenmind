// Copy and paste of canvas nodes: their settings and the edges between them.
// Pasted nodes get fresh ids in the scope they land in (the subflow they
// came from when it is on the canvas, else the main flow).
import { makeEdge } from "./layout.js";
import { rawId, uniqueId } from "./spec.js";

const PASTE_OFFSET = 40;

/** {clip} or {error}: the selected nodes (one scope, no subflow boxes). */
export function copySelection(nodes, edges) {
  const selected = nodes.filter((n) => n.selected);
  if (!selected.length) return { error: "Select nodes to copy (click, or shift-drag a box)." };
  if (selected.some((n) => n.type === "subflow")) return { error: "Subflow boxes cannot be copied yet; copy the nodes inside them." };
  const scope = selected[0].parentId || null;
  if (selected.some((n) => (n.parentId || null) !== scope)) return { error: "Copy nodes from one flow at a time." };
  const ids = new Set(selected.map((n) => n.id));
  return {
    clip: {
      scope,
      nodes: selected.map((n) => ({
        raw: rawId(n.id), type: n.type, spec: n.data.node, component: n.data.component,
        position: { ...n.position }, style: { ...n.style },
      })),
      edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => e.data.spec),
    },
  };
}

/** {nodes, edges} to add for a paste, numbered `times` (1, 2, …) to cascade repeated pastes. */
export function pasteClip(clip, nodes, meta, times = 1) {
  const group = clip.scope ? nodes.find((n) => n.id === clip.scope) : null;
  const parentId = group ? group.id : null;
  const prefix = parentId ? `${parentId}/` : "";
  const path = group ? `${group.data.path}/${group.data.node.config.subflow}` : meta.root;
  const taken = new Set(nodes.filter((n) => (n.parentId || null) === parentId).map((n) => rawId(n.id)));
  const rename = {};
  for (const item of clip.nodes) {
    rename[item.raw] = uniqueId(item.raw, taken);
    taken.add(rename[item.raw]);
  }
  const shift = PASTE_OFFSET * times;
  const added = clip.nodes.map((item) => ({
    id: prefix + rename[item.raw],
    type: item.type,
    position: { x: item.position.x + shift, y: item.position.y + shift },
    style: item.style,
    selected: true,
    data: { node: { ...item.spec, id: rename[item.raw], config: structuredClone(item.spec.config || {}) },
            component: item.component, status: null, issues: [], path },
    ...(parentId ? { parentId } : {}),
  }));
  const components = Object.fromEntries(clip.nodes.map((item) => [item.raw, item.component]));
  const addedEdges = clip.edges.map((spec) =>
    makeEdge({ ...spec, source: rename[spec.source], target: rename[spec.target] }, prefix, components[spec.source]));
  return { nodes: added, edges: addedEdges };
}

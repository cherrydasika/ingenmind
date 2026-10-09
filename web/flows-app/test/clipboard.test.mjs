// node --test: copy/paste and the canvas ⇄ flow JSON round trip.
// fixture.json is the built-in flow and component catalogue; regenerate it when either changes:
//   PYTHONPATH=app:dags python -c "import json, flows; print(json.dumps({'flow':
//     flows.load_flow().model_dump(mode='json', exclude_none=True), 'components': flows.components()}))"
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { copySelection, pasteClip } from "../src/clipboard.js";
import { fitGroups, layoutFlow } from "../src/layout.js";
import { toSpec } from "../src/spec.js";

const fixture = JSON.parse(readFileSync(new URL("./fixture.json", import.meta.url)));
const canvas = () => {
  const laid = layoutFlow(fixture.flow, fixture.components);
  return { ...laid, nodes: fitGroups(laid.nodes) };
};

test("round trip keeps the flow", () => {
  const { nodes, edges, meta } = canvas();
  const spec = toSpec(nodes, edges, meta);
  const strip = (f) => ({ ...f, nodes: f.nodes.map(({ position, ...n }) => n),
    subflows: Object.fromEntries(Object.entries(f.subflows).map(([k, v]) => [k, strip(v)])) });
  const again = toSpec(...(() => { const l = layoutFlow(spec, fixture.components); return [fitGroups(l.nodes), l.edges, l.meta]; })());
  assert.deepEqual(strip(again), strip(spec));
});

test("copy needs a selection in one scope, without subflow boxes", () => {
  const { nodes, edges } = canvas();
  assert.match(copySelection(nodes, edges).error, /Select nodes/);
  const pick = (ids) => nodes.map((n) => ({ ...n, selected: ids.includes(n.id) }));
  assert.match(copySelection(pick(["knowledge_base_agent"]), edges).error, /Subflow boxes/);
  assert.match(copySelection(pick(["supervisor", "knowledge_base_agent/retrieval_agent"]), edges).error, /one flow/);
});

test("paste renames, keeps settings and inner edges, stays in its subflow", () => {
  const { nodes, edges, meta } = canvas();
  const ids = ["knowledge_base_agent/retriever", "knowledge_base_agent/vector_db"];
  const picked = nodes.map((n) => ({ ...n, selected: ids.includes(n.id) }));
  const { clip } = copySelection(picked, edges);
  assert.equal(clip.edges.length, 1);                     // vector_db ⇢ retriever, not ⇢ ingest (not copied)
  const pasted = pasteClip(clip, nodes, meta);
  assert.deepEqual(pasted.nodes.map((n) => n.id).sort(),
    ["knowledge_base_agent/retriever_2", "knowledge_base_agent/vector_db_2"]);
  assert.ok(pasted.nodes.every((n) => n.parentId === "knowledge_base_agent"));
  assert.equal(pasted.edges[0].source, "knowledge_base_agent/vector_db_2");
  assert.equal(pasted.edges[0].target, "knowledge_base_agent/retriever_2");
  // the pasted flow serialises into the subflow
  const spec = toSpec([...nodes, ...pasted.nodes], [...edges, ...pasted.edges], meta);
  const kb = spec.subflows.knowledge_base;
  assert.ok(kb.nodes.some((n) => n.id === "retriever_2"));
  assert.ok(kb.edges.some((e) => e.source === "vector_db_2" && e.target === "retriever_2" && e.kind === "resource"));
  // settings are copies, not shared
  pasted.nodes[0].data.node.config.top_k = 9;
  assert.equal(clip.nodes[0].spec.config.top_k, undefined);
});

test("paste into the main flow when the subflow is gone", () => {
  const { nodes, edges, meta } = canvas();
  const picked = nodes.map((n) => ({ ...n, selected: n.id === "knowledge_base_agent/retriever" }));
  const { clip } = copySelection(picked, edges);
  const main = nodes.filter((n) => !n.parentId && n.id !== "knowledge_base_agent");
  const pasted = pasteClip(clip, main, meta, 2);
  assert.equal(pasted.nodes[0].id, "retriever");
  assert.equal(pasted.nodes[0].parentId, undefined);
  assert.equal(pasted.nodes[0].position.x, clip.nodes[0].position.x + 80);
});

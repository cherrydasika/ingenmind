// Agent stream events → which canvas node they belong to and its status.
// Main-graph nodes report by id ("node" events); inside the knowledge-base
// subflow only public activity events exist, matched by component type.

const ACTIVITY_TYPE = {
  rewriter: "query_rewriter",
  embedding: "retrieval_agent",
  retrieval: "retrieval_agent",
  evaluator: "evidence_evaluator",
  research: "research_agent",
  r_search: "research_agent",
  r_extract: "research_agent",
  r_validate: "source_validator",
  r_ingest: "ingest",
};

function byType(nodes, type) {
  return nodes.filter((n) => n.data.node.type === type).map((n) => n.id);
}

function delegateTarget(nodes, edges, agent) {
  const edge = edges.find((e) => e.data.spec.outcome === `delegate:${agent}`);
  return edge ? [edge.target] : [];
}

function status(state, extra) {
  if (state === "start") return "running";
  if (state === "error") return "error";
  return extra || "done";
}

/** What a trace keeps from an event: everything the server made public but the routing keys. */
export function traceEntry(event) {
  const { type: _t, node: _n, ...rest } = event;
  return { kind: event.type, at: Date.now(), ...rest };
}

/** [{id, status}] for one stream event. */
export function eventTargets(event, nodes, edges) {
  switch (event.type) {
    case "node":
      return nodes.some((n) => n.id === event.node) ? [{ id: event.node, status: status(event.state) }] : [];
    case "guardrail": {
      const blocked = event.state === "done" && event.decision && event.decision !== "ALLOW" ? "blocked" : null;
      return byType(nodes, `${event.stage}_guardrail`).map((id) => ({ id, status: status(event.state, blocked) }));
    }
    case "answer_eval": {
      const failed = event.state === "done" && event.passed === false ? "failed" : null;
      return byType(nodes, "answer_evaluator").map((id) => ({ id, status: status(event.state, failed) }));
    }
    case "activity": {
      if (event.node === "knowledge_base" || event.node === "external_apis") {
        return delegateTarget(nodes, edges, event.node).map((id) => ({ id, status: status(event.state) }));
      }
      if (event.node === "call") {
        return delegateTarget(nodes, edges, "external_apis").map((id) => ({ id, status: "running" }));
      }
      const type = ACTIVITY_TYPE[event.node];
      return type ? byType(nodes, type).map((id) => ({ id, status: status(event.state) })) : [];
    }
    default:
      return [];
  }
}

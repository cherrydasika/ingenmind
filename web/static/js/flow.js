// Multi-agent workflow graph for the Retrieval page: every step of the
// LangGraph state machine (app/agent.py) as a small box coloured by its
// state, left to right, with the evidence evaluator's branches drawn out —
// in the spirit of a workflow scheduler's graph view:
//
//                                                  ┌ retry → Rewrite & retry ┐ (back to Search)
//   Question → Input guard → Supervisor → Knowledge base → Search → Evidence check ─ good ─┐
//                                  │                               ├ gap → Research ┘ (back to Search, or its report to the Summarizer)
//                                  │                               └ other → Not handled ──┤
//                                  └→ External APIs → API call ────────────────────────────┤
//                                       Summarizer → Output guard → Answer check → Answer ◀┘
//
// Driven by /api/agent/stream's public events: guardrail, node, model and
// answer_eval events, and "activity" events that carry only safe details
// (timings, decisions, routes, scores and counts; agent.py _public_progress).

import { fmtTime, h } from "./ui.js";

const SVG = "http://www.w3.org/2000/svg";
const NODE_W = 116, NODE_H = 46, END_W = 92, COL = 140, ROW = 62, PAD = 16;

// key: [label, column, row, width?]
const NODES = {
  question: ["Question", 0, 1, END_W],
  input_guardrail: ["Input guard", 1, 1],
  supervisor: ["Supervisor", 2, 1],
  knowledge_base: ["Knowledge base", 3, 1],
  search: ["Search", 4, 1],
  evaluator: ["Evidence check", 5, 1],
  rewrite: ["Rewrite & retry", 6, 0],
  research: ["Research", 6, 2],
  other: ["Not handled", 6, 3],
  external_apis: ["External APIs", 3, 4],
  call: ["API call", 4, 4],
  writer: ["Summarizer", 7, 1],
  output_guardrail: ["Output guard", 8, 1],
  answer_eval: ["Answer check", 9, 1],
  answer: ["Answer", 10, 1, END_W],
};
// [from, to, branch label?]. A branch is lit only by the evaluator's decision.
const EDGES = [
  ["question", "input_guardrail"], ["input_guardrail", "supervisor"],
  ["supervisor", "knowledge_base"], ["supervisor", "external_apis"],
  ["knowledge_base", "search"], ["search", "evaluator"],
  ["evaluator", "writer", "good"], ["evaluator", "rewrite", "retry"],
  ["evaluator", "research", "gap"], ["evaluator", "other", "other"],
  ["rewrite", "search"], ["research", "search"], ["research", "writer"], ["other", "writer"],
  ["external_apis", "call"], ["call", "writer"],
  ["writer", "output_guardrail"], ["output_guardrail", "answer_eval"], ["answer_eval", "answer"],
];
const BRANCH = { GOOD_EVIDENCE: "good", RETRIEVAL_FAILURE: "retry", KNOWLEDGE_GAP: "gap" };
const BRANCH_NODE = { good: "writer", retry: "rewrite", gap: "research", other: "other" };
// Activity nodes (agent.py) → the box that shows them.
const ACTIVITY = {
  knowledge_base: "knowledge_base", external_apis: "external_apis", embedding: "search", retrieval: "search",
  evaluator: "evaluator", rewriter: "rewrite", call: "call",
  research: "research", r_search: "research", r_extract: "research", r_validate: "research", r_ingest: "research",
  r_classify: "research", r_known: "research", r_profile: "research", r_probe: "research", r_judge: "research",
};
const METHOD_TEXT = {
  api_tool: "a new API tool", feed_consumer: "a feed consumer", bulk_ingest: "a scheduled download",
  page_ingest: "ingesting pages", none: "no source",
};
const STATE_TEXT = { idle: "waiting", active: "running", done: "done", error: "failed", skipped: "skipped" };

export function shortModel(model) {
  return String(model || "").replace(/^[a-z]+\.anthropic\./, "").replace(/-\d{8}-v\d+:\d+$/, "");
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  el.append(...children);
  return el;
}

const box = (key) => {
  const [, col, row, w = NODE_W] = NODES[key];
  const x = PAD + col * COL + (NODE_W - w) / 2;
  return { x, y: PAD + row * ROW, w, h: NODE_H };
};

function edgePath(from, to) {
  const a = box(from), b = box(to);
  if (b.x < a.x) {   // back to Search: leave from the left, arrive on its top or bottom
    const sx = a.x, sy = a.y + a.h / 2, tx = b.x + b.w / 2;
    const ty = a.y < b.y ? b.y : b.y + b.h;
    return `M${sx},${sy} C${sx - 40},${sy} ${tx},${sy} ${tx},${ty}`;
  }
  const sx = a.x + a.w, sy = a.y + a.h / 2, tx = b.x, ty = b.y + b.h / 2;
  if (sy === ty) return `M${sx},${sy} L${tx},${ty}`;
  const mid = (sx + tx) / 2;
  return `M${sx},${sy} C${mid},${sy} ${mid},${ty} ${tx},${ty}`;
}

export class AgenticFlow {
  constructor(cfg, info) {
    this.model = info?.harness ? shortModel(info.harness.model) : "the chat model";
    this.hints = {
      question: "The user's question",
      input_guardrail: "Scope and safety check on the question",
      supervisor: `${this.model} · reads the question and assigns tasks`,
      knowledge_base: `${this.model} · searches the ingested pages`,
      search: `${cfg.embeddings.A.model} · pgvector + full text, fused`,
      evaluator: "Judges the knowledge-base evidence and picks a branch",
      rewrite: "Retrieval missed: rewrite the queries and search again (once)",
      research: "Knowledge gap: web search, validate and ingest sources, then search again; for live data, profile data sources for an admin report",
      other: "Conflicting or insufficient evidence: no agent for this yet",
      external_apis: `${this.model} · ${(info?.roles?.external_apis?.tools || []).map((t) => t.title.split(" · ")[0]).join(", ") || "live APIs"}`,
      call: "A live API call from the registry",
      writer: `${this.model} · the supervisor writes the answer from the findings`,
      output_guardrail: "Scope and safety check on the answer",
      answer_eval: "Correctness · faithfulness · completeness · citations",
      answer: "What the user sees",
    };
    this.el = this.build();
    this.reset();
  }

  get element() {
    return this.el;
  }

  build() {
    const width = PAD * 2 + 10 * COL + NODE_W, height = PAD * 2 + 4 * ROW + NODE_H;
    const marker = (id) => svg("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svg("path", { d: "M0,0 L10,5 L0,10 z" }));
    this.edges = {};
    const edgeLayer = svg("g", { class: "dag-edges" });
    for (const [from, to, branch] of EDGES) {
      const d = edgePath(from, to);
      const path = svg("path", { d, class: "dag-edge", "marker-end": "url(#dag-arrow)" });
      const g = svg("g", { class: `dag-edge-g${box(to).x < box(from).x ? " is-back" : ""}`, "data-state": "idle" }, path);
      if (branch) {   // the label sits just before the branch's target, clear of the fan
        const t = box(to);
        g.append(svg("text", { class: "dag-edge-label", x: t.x - 6, y: t.y + NODE_H / 2 - 6, "text-anchor": "end" }, branch));
      }
      edgeLayer.append(g);
      this.edges[`${from}>${to}`] = { g, branch, from, to };
    }
    this.boxes = {};
    const nodeLayer = svg("g", { class: "dag-nodes" });
    for (const [key, [label]] of Object.entries(NODES)) {
      const { x, y, w } = box(key);
      const end = key === "question" || key === "answer";
      const title = svg("title");
      const meta = svg("text", { class: "dag-meta", x: x + w / 2, y: y + 33, "text-anchor": "middle" });
      const g = svg("g", { class: `dag-node${end ? " is-end" : ""}`, "data-key": key, "data-state": "idle" },
        title,
        svg("rect", { x, y, width: w, height: NODE_H, rx: end ? NODE_H / 2 : 7 }),
        svg("text", { class: "dag-label", x: x + w / 2, y: y + 19, "text-anchor": "middle" }, label),
        meta);
      nodeLayer.append(g);
      this.boxes[key] = { g, title, meta };
    }
    this.now = h("span", { class: "dag-now-text" });
    this.steps = h("ol", { class: "dag-steps" });
    this.stepsCount = h("span", { class: "faint" });
    this.live = h("div", { class: "sr-only", "aria-live": "polite" });
    const legend = h("div", { class: "dag-legend" }, ["idle", "active", "done", "error", "skipped"].map((state) =>
      h("span", { class: "dag-key", "data-state": state }, h("i"), STATE_TEXT[state])));
    return h("div", { class: "dag" },
      h("div", { class: "dag-bar" }, h("div", { class: "dag-now" }, h("strong", {}, "Now "), this.now), legend),
      h("div", { class: "dag-scroll" },
        svg("svg", { class: "dag-svg", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Workflow graph",
          style: `--dag-w:${width}px` }, svg("defs", {}, marker("dag-arrow")), edgeLayer, nodeLayer)),
      h("details", { class: "dag-log" }, h("summary", {}, "Steps ", this.stepsCount), this.steps),
      this.live);
  }

  // ---------- state ----------

  // time: seconds, when the event says; else wall-clock time from the step's
  // first start (a step can run more than once: two searches, a retry).
  setNode(key, state, { detail, time } = {}) {
    const n = this.state[key];
    n.state = state;
    if (detail !== undefined) n.detail = detail;
    if (state === "active") {
      n.since ??= performance.now();
      n.used = true;
      this.reach(key);
    } else if (state === "done" || state === "error") {
      n.used = true;
      n.time = time ?? (n.since == null ? n.time : (performance.now() - n.since) / 1000);
    }
    this.draw(key);
  }

  // A step started: the edges into it from steps that ran are the path taken.
  reach(key) {
    for (const edge of Object.values(this.edges)) {
      if (edge.to !== key || edge.branch || this.isIdleReport(edge)) continue;
      if (this.state[edge.from].used) this.setEdge(edge, "done");
    }
  }

  // Research reports to the Summarizer only when the gap remained.
  isIdleReport(edge) {
    return edge.from === "research" && edge.to === "writer" && this.lastBranch !== "gap";
  }

  setEdge(edge, state) {
    edge.state = state;
    edge.g.dataset.state = state;
  }

  branch(name, state = "done") {
    const edge = Object.values(this.edges).find((e) => e.branch === name);
    if (edge) this.setEdge(edge, state);
  }

  draw(key) {
    const n = this.state[key], b = this.boxes[key];
    b.g.dataset.state = n.state;
    const time = n.state === "active" && n.since != null ? (performance.now() - n.since) / 1000 : n.time;
    b.meta.textContent = key === "question" ? (n.state === "done" ? "asked" : STATE_TEXT[n.state])
      : [time != null && n.state !== "skipped" && (!this.restored || key === "answer") ? fmtTime(time) : STATE_TEXT[n.state], n.short]
        .filter(Boolean).join(" · ");
    b.title.textContent = [NODES[key][0], this.hints[key], n.detail].filter(Boolean).join("\n");
  }

  // Elapsed time on running steps, and the "Now" line.
  tick() {
    const running = Object.keys(NODES).filter((key) => this.state[key].state === "active");
    for (const key of running) this.draw(key);
    this.now.textContent = this.el.dataset.run !== "running" ? (this.el.dataset.run === "done" ? "Finished" : this.el.dataset.run === "error" ? "Failed" : "Waiting for a question")
      : running.length ? running.map((key) => NODES[key][0]).join(" · ") : "…";
  }

  step(text, kind = "") {
    this.steps.append(h("li", { class: kind }, text));
    this.stepsCount.textContent = `(${this.steps.children.length})`;
  }

  reset() {
    clearInterval(this.timer);
    this.timer = null;
    this.state = Object.fromEntries(Object.keys(NODES).map((key) => [key, { state: "idle", detail: "", time: null, short: "", used: false, since: null }]));
    this.counts = {};
    this.round = 0;
    this.restored = false;
    this.lastBranch = null;
    for (const edge of Object.values(this.edges)) this.setEdge(edge, "idle");
    for (const key of Object.keys(NODES)) this.draw(key);
    this.steps.replaceChildren();
    this.stepsCount.textContent = "";
    this.el.dataset.run = "idle";
    this.tick();
  }

  // ---------- live run ----------

  start(question) {
    this.reset();
    this.el.dataset.run = "running";
    this.setNode("question", "done", { detail: question });
    this.timer = setInterval(() => this.tick(), 200);
    this.tick();
  }

  // One /api/agent/stream event.
  event(e) {
    if (e.type === "guardrail") return this.guardrail(e);
    if (e.type === "node") {
      if (e.node === "supervisor" && e.state === "start") this.round = e.round || 0;
      return undefined;
    }
    if (e.type === "model" && e.agent === "supervisor") return this.supervisorTurn(e);
    if (e.type === "model" && (e.agent === "knowledge_base" || e.agent === "external_apis")) {
      this.activity({ node: e.agent, state: e.state === "start" ? "start" : "done" }, false);
      return undefined;
    }
    if (e.type === "answer_eval") return this.answerCheck(e);
    if (e.type === "activity") return this.activity(e, true);
    return undefined;
  }

  guardrail(e) {
    const key = `${e.stage}_guardrail`;
    if (!this.state[key]) return;
    if (e.state === "start") return this.setNode(key, "active");
    const allowed = e.decision === "ALLOW";
    this.setNode(key, allowed ? "done" : "error", { detail: `Decision: ${e.decision}`, time: e.seconds });
    this.state[key].short = allowed ? "" : e.decision.toLowerCase();
    this.draw(key);
    this.step(`${NODES[key][0]}: ${e.decision}`, allowed ? "ok" : "err");
  }

  // Activities can nest and repeat (two searches, two tasks): a box stays
  // running until every one of its activities has finished.
  activity(e, counted) {
    const key = ACTIVITY[e.node];
    if (!key) return;
    const id = counted ? `${key}` : `${key}:model`;
    if (e.state === "start") {
      this.counts[id] = (this.counts[id] || 0) + 1;
      if (this.state[key].state !== "active") this.setNode(key, "active");
      return;
    }
    this.counts[id] = Math.max(0, (this.counts[id] || 0) - 1);
    if (e.state === "error" || e.failed) this.state[key].failed = true;
    if (key === "evaluator" && e.decision) this.evaluated(e);
    if (key === "search" && e.node === "retrieval" && e.chunks != null) {
      this.state.search.searches = (this.state.search.searches || 0) + 1;
      this.state.search.detail = `${plural(this.state.search.searches, "search")} · last: ${plural(e.chunks, "chunk")}${e.sources != null ? ` from ${plural(e.sources, "source")}` : ""}`;
    }
    if (key === "call" && e.tool) this.state.call.detail = `${e.tool}${e.cached ? " (cached)" : ""}`;
    if (key === "research" && e.node === "r_classify" && e.status === "data_source") {
      this.state.research.detail = "Live data: looking for data sources";
      this.step("Research: the gap needs live data, so it looks for data sources instead of pages");
    }
    if (key === "research" && e.node === "r_profile" && e.state === "done" && e.profiles != null) {
      this.state.research.detail = `${plural(e.profiles + (e.reused || 0), "data source")} profiled${e.reused ? ` (${e.reused} known)` : ""}`;
    }
    if (key === "research" && e.node === "r_judge" && e.state === "done") {
      this.state.research.detail = e.recommended ? `Recommends ${METHOD_TEXT[e.status] || e.status}` : "No source recommended";
      this.step(`Research: ${this.state.research.detail}${e.saved ? " · report saved" : ""}`, e.recommended ? "ok" : "err");
    }
    if (key === "research" && e.node === "r_validate" && e.accepted != null) {
      this.state.research.detail = `${e.accepted} of ${plural(e.sources, "source")} passed validation`;
      this.step(`Research: ${this.state.research.detail}`, e.accepted ? "ok" : "err");
    }
    const busy = Object.entries(this.counts).some(([k, n]) => k.split(":")[0] === key && n > 0);
    if (!busy) {
      this.setNode(key, this.state[key].failed ? "error" : "done");
      if (key === "knowledge_base" || key === "external_apis") this.step(`${NODES[key][0]} finished${e.seconds != null ? ` in ${fmtTime(e.seconds)}` : ""}`);
    } else {
      this.draw(key);
    }
  }

  // The evidence evaluator's decision lights its branch.
  evaluated(e) {
    const name = BRANCH[e.decision] || "other";
    this.lastBranch = name;
    const score = e.confidence ?? e.overall_confidence;
    this.state.evaluator.short = name;
    this.state.evaluator.detail = `${e.decision}${score != null ? ` · score ${score.toFixed(2)}` : ""}${e.attempt > 1 ? " · attempt 2" : ""}`;
    this.branch(name);
    if (name === "other") this.setNode("other", "done", { detail: `${e.decision}: no agent handles this yet` });
    this.step(`Evidence check: ${this.state.evaluator.detail} → ${name === "good" ? "summarizer" : NODES[BRANCH_NODE[name]][0]}`,
      name === "good" ? "ok" : name === "other" ? "err" : "");
  }

  answerCheck(e) {
    if (e.state === "start") return this.setNode("answer_eval", "active");
    this.setNode("answer_eval", e.passed ? "done" : "error", { time: e.seconds,
      detail: e.passed ? `Passed · ${e.overall.toFixed(2)}` : `Failed on ${(e.failed_on || []).join(", ")}: a standard message is sent` });
    this.state.answer_eval.short = e.passed ? e.overall.toFixed(2) : "fail";
    this.draw("answer_eval");
    this.step(`Answer check: ${e.passed ? "passed" : "failed"} · ${e.overall.toFixed(2)}`, e.passed ? "ok" : "err");
  }

  // The supervisor's first turns plan (Supervisor); the turn after the
  // findings come back writes the answer (Summarizer): same agent.
  supervisorTurn(e) {
    const key = this.round > 0 ? "writer" : "supervisor";
    if (e.state === "start") {
      if (key === "writer" && this.state.supervisor.state === "active") this.setNode("supervisor", "done");
      return this.setNode(key, "active");
    }
    if (key === "supervisor" && e.stop_reason === "tool_use") {
      this.setNode("supervisor", "done", { detail: "Assigned tasks to the specialists" });
      this.step("Supervisor assigned tasks");
    } else if (key === "writer") {
      this.setNode("writer", "done", { detail: "Wrote the answer from the findings" });
    } else {
      this.setNode("supervisor", "done", { detail: "Answered without the specialists" });
      this.setNode("writer", "done", { detail: "Answered without the specialists" });
    }
    return undefined;
  }

  finish(result) {
    for (const [stage, verdict] of Object.entries(result.guardrails || {})) {
      if (this.state[`${stage}_guardrail`].state !== "done" && this.state[`${stage}_guardrail`].state !== "error") {
        this.guardrail({ stage, state: "done", decision: verdict.decision });
      }
    }
    const check = result.answer_check || result.answer_evaluation;
    if (check && this.state.answer_eval.state !== "done" && this.state.answer_eval.state !== "error") {
      this.answerCheck({ state: "done", ...check });
    }
    const failed = check && !check.passed;
    const blocked = Object.values(result.guardrails || {}).some((v) => v.decision !== "ALLOW");
    for (const key of Object.keys(NODES)) {
      const n = this.state[key];
      if (n.state === "active") this.setNode(key, "done");
      else if (n.state === "idle" && key !== "answer") this.setNode(key, "skipped");
    }
    this.state.answer.short = blocked ? "blocked" : failed ? "withheld" : "";
    this.setNode("answer", failed || blocked ? "error" : "done", { time: result.total,
      detail: blocked ? "Blocked by a guardrail" : failed ? "The answer check failed: a standard message was sent" : "Ready" });
    // The path taken: every edge between two steps that ran (branches only
    // by the evaluator's decision); the rest fade.
    for (const edge of Object.values(this.edges)) {
      if (edge.state === "done") continue;
      if (!edge.branch && !this.isIdleReport(edge) && this.state[edge.from].used && this.state[edge.to].used) this.setEdge(edge, "done");
      else if (!(this.restored && edge.branch)) this.setEdge(edge, "skipped");
    }
    this.end("done", "Answer ready");
  }

  fail(message) {
    const running = Object.keys(NODES).filter((key) => this.state[key].state === "active");
    for (const key of running.length ? running : ["supervisor"]) this.setNode(key, "error", { detail: message });
    this.state.answer.short = "none";
    this.setNode("answer", "error", { detail: "No answer" });
    this.step(`Failed: ${message}`, "err");
    this.end("error", `Failed: ${message}`);
  }

  end(run, announce) {
    clearInterval(this.timer);
    this.timer = null;
    this.el.dataset.run = run;
    this.live.textContent = announce;
    this.tick();
  }

  // Redraw a finished run from a saved result (after a reload): which steps
  // ran, from its activities; the branch taken is not kept.
  show(result) {
    this.start(result.question);
    this.restored = true;
    for (const [stage, verdict] of Object.entries(result.guardrails || {})) {
      if (stage === "input") this.guardrail({ stage, state: "done", decision: verdict.decision });
    }
    if (this.state.input_guardrail.state === "done") {
      this.setNode("supervisor", "done");
    }
    for (const [node, state] of Object.entries(result.activities || {})) {
      this.activity({ node, state: "start" }, true);
      this.activity({ node, state }, true);
    }
    if (result.rounds > 0) this.setNode("writer", "done");
    for (const [stage, verdict] of Object.entries(result.guardrails || {})) {
      if (stage === "output") this.guardrail({ stage, state: "done", decision: verdict.decision });
    }
    this.finish(result);
  }
}

// The architecture gallery (#69): ready-made agent flows, one card at a time
// (arrows, the keyboard, swipe or the dots), each with a diagram drawn from the
// flow itself; Apply puts its components on the canvas.
import { useEffect, useMemo, useRef, useState } from "react";

const TOOL_TEXT = { retriever: "knowledge base", web_search: "web search", scraper: "page reader", api_tools: "live APIs",
  mcp_tool: "MCP" };
const BOX_W = 150, BOX_H = 58, GAP_X = 18, GAP_Y = 46, PAD = 12;
// The components that run as an agent with sub-agents (app/agent_tree.py AGENT_TYPES): what the top one does with them.
const AGENT_TYPES = { agent: null, planner: "plans their tasks", blackboard: "runs a shared board" };

// The agent tree of a flow: {id: {name, tools, checks, subs}}, and its top agent.
function agentTree(flow) {
  const nodes = Object.fromEntries(flow.nodes.map((n) => [n.id, n]));
  const agents = {};
  for (const n of flow.nodes) {
    if (n.type in AGENT_TYPES) agents[n.id] = { id: n.id, name: n.label || n.id, role: AGENT_TYPES[n.type], tools: [], checks: [], subs: [], parent: null };
  }
  for (const e of flow.edges) {
    if (e.kind !== "resource" || !agents[e.target]) continue;
    const source = nodes[e.source];
    if (source?.type in AGENT_TYPES) { agents[e.target].subs.push(e.source); agents[e.source].parent = e.target; }
    else if (source?.type === "check") agents[e.target].checks.push(source.label || source.id);
    else if (TOOL_TEXT[source?.type]) agents[e.target].tools.push(TOOL_TEXT[source.type]);
  }
  const top = Object.values(agents).find((a) => !a.parent);
  return { agents, top };
}

// Leaves get columns; a parent sits over the middle of its children.
function layout(tree) {
  const pos = {};
  let column = 0;
  const place = (id, depth) => {
    const a = tree.agents[id];
    if (!a.subs.length) { pos[id] = { x: column++, depth }; return; }
    a.subs.forEach((s) => place(s, depth + 1));
    const xs = a.subs.map((s) => pos[s].x);
    pos[id] = { x: (Math.min(...xs) + Math.max(...xs)) / 2, depth };
  };
  if (tree.top) place(tree.top.id, 1);
  return { pos, columns: Math.max(column, 1), depth: Math.max(1, ...Object.values(pos).map((p) => p.depth)) };
}

export function ArchDiagram({ arch }) {
  const tree = useMemo(() => (arch.flow ? agentTree(arch.flow) : null), [arch]);
  if (!tree?.top) {
    return (
      <div className="fl-arch-steps is-soon">
        {arch.steps.map((s, i) => <span key={i}>{i > 0 && <b>→</b>}<span className="fl-arch-step">{s}</span></span>)}
      </div>
    );
  }
  const { pos, columns, depth } = layout(tree);
  const width = Math.max(columns * (BOX_W + GAP_X) - GAP_X + PAD * 2, 3 * (BOX_W + GAP_X));
  const height = (depth + 1) * (BOX_H + GAP_Y) + PAD;
  const xOf = (id) => PAD + pos[id].x * (BOX_W + GAP_X) + (width - PAD * 2 - (columns * (BOX_W + GAP_X) - GAP_X)) / 2;
  const yOf = (id) => PAD + pos[id].depth * (BOX_H + GAP_Y);
  const top = tree.top.id;
  const topY = PAD;   // the question → … → answer row
  const lines = [];
  const boxes = [];
  for (const a of Object.values(tree.agents)) {
    const x = xOf(a.id), y = yOf(a.id);
    for (const s of a.subs) {
      lines.push(<path key={`${a.id}-${s}`} className="fl-arch-line"
        d={`M${x + BOX_W / 2},${y + BOX_H} C${x + BOX_W / 2},${y + BOX_H + GAP_Y / 2} ${xOf(s) + BOX_W / 2},${yOf(s) - GAP_Y / 2} ${xOf(s) + BOX_W / 2},${yOf(s)}`} />);
    }
    boxes.push(
      <g key={a.id} className={`fl-arch-agent${a.id === top ? " is-top" : ""}`}>
        <rect x={x} y={y} width={BOX_W} height={BOX_H} rx={9} />
        <text x={x + 10} y={y + 19} className="fl-arch-name">◎ {a.name.length > 19 ? `${a.name.slice(0, 18)}…` : a.name}</text>
        <text x={x + 10} y={y + 35} className="fl-arch-meta">{a.tools.length ? a.tools.join(" · ") : a.role || (a.subs.length ? `${a.subs.length} sub-agents` : "reasons only")}</text>
        {a.checks.length > 0 && <text x={x + 10} y={y + 50} className="fl-arch-check">✓ {a.checks.join(", ")}</text>}
      </g>);
  }
  const tx = xOf(top);
  return (
    <svg className="fl-arch-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${arch.name} diagram`}>
      <g className="fl-arch-path">
        <rect x={tx - 108} y={topY} width={96} height={30} rx={15} /><text x={tx - 60} y={topY + 19} textAnchor="middle">Question</text>
        <rect x={tx + BOX_W + 12} y={topY} width={96} height={30} rx={15} /><text x={tx + BOX_W + 60} y={topY + 19} textAnchor="middle">Answer</text>
        <path className="fl-arch-line" d={`M${tx - 12},${topY + 15} L${tx + BOX_W / 2},${topY + 15} L${tx + BOX_W / 2},${yOf(top)}`} />
        <path className="fl-arch-line" d={`M${tx + BOX_W / 2},${topY + 15} L${tx + BOX_W + 12},${topY + 15}`} />
      </g>
      {lines}
      {boxes}
    </svg>
  );
}

export function ArchitectureGallery({ architectures, canApply, onApply, onClose }) {
  const [index, setIndex] = useState(0);
  const start = useRef(null);
  const count = architectures.length;
  const go = (step) => setIndex((i) => (i + step + count) % count);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  if (!count) return null;
  const arch = architectures[index];
  return (
    <div className="fl-picker-backdrop fl-gallery-backdrop" onClick={onClose}>
      <div className="fl-gallery" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Architectures"
        onPointerDown={(e) => { start.current = e.clientX; }}
        onPointerUp={(e) => { if (start.current !== null && Math.abs(e.clientX - start.current) > 60) go(e.clientX < start.current ? 1 : -1); start.current = null; }}>
        <div className="fl-gallery-head">
          <div className="fl-panel-title">Start from an architecture</div>
          <span className="fl-faint">{index + 1} of {count} · swipe or use ← →</span>
          <button type="button" className="fl-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="fl-gallery-body">
          <button type="button" className="fl-gallery-nav" onClick={() => go(-1)} aria-label="Previous">‹</button>
          <div className="fl-gallery-card" key={arch.id}>
            <div className="fl-gallery-title">
              <h3>{arch.name}</h3>
              <span className={`fl-chip${arch.ready ? "" : " is-soon"}`}>{arch.group}</span>
            </div>
            <div className="fl-arch-steps">
              {arch.steps.map((s, i) => <span key={i}>{i > 0 && <b>→</b>}<span className="fl-arch-step">{s}</span></span>)}
            </div>
            <div className="fl-arch-diagram"><ArchDiagram arch={arch} /></div>
            <p>{arch.summary}</p>
            <p className="fl-faint"><strong>When to use it:</strong> {arch.when}</p>
          </div>
          <button type="button" className="fl-gallery-nav" onClick={() => go(1)} aria-label="Next">›</button>
        </div>
        <div className="fl-gallery-foot">
          <div className="fl-gallery-dots">
            {architectures.map((a, i) => (
              <button key={a.id} type="button" className={`fl-dot${i === index ? " is-active" : ""}${a.ready ? "" : " is-soon"}`}
                onClick={() => setIndex(i)} aria-label={a.name} title={a.name} />
            ))}
          </div>
          <button type="button" className="fl-btn fl-btn-primary" disabled={!arch.ready || !canApply}
            title={!arch.ready ? "Coming soon: the runtime cannot run it yet" : canApply ? "Replace the canvas with it; undo brings yours back" : "Open a draft to apply it"}
            onClick={() => onApply(arch)}>{arch.ready ? "Apply to this flow" : "Coming soon"}</button>
        </div>
      </div>
    </div>
  );
}

// The architecture as this flow: its name and id, its guardrail settings, and its
// brief and domain on the new top agent.
export function adapt(arch, current) {
  const flow = JSON.parse(JSON.stringify(arch.flow));
  const old = Object.fromEntries((current?.nodes || []).map((n) => [n.type, n]));
  const guard = old.input_guardrail;
  const brain = old.agent || old.supervisor;
  const plugged = new Set(flow.edges.filter((e) => e.port === "agents").map((e) => e.source));
  for (const n of flow.nodes) {
    // The flow's guardrail settings, unless the architecture sets its own (the knowledge agents one, R35).
    if (n.type === "input_guardrail" && guard) n.config = { ...guard.config, ...n.config, stage: "input" };
    if (n.type === "agent" && !plugged.has(n.id) && brain) {
      // An architecture that sets its own scope (the knowledge agents one, R35) keeps it.
      for (const key of ["brief", "domain"]) if (brain.config?.[key] && !n.config?.[key]) n.config = { ...n.config, [key]: brain.config[key] };
    }
  }
  return { ...flow, id: current.id, name: current.name, version: current.version,
    description: `${arch.name}: ${arch.summary}`.slice(0, 1000), subflows: {} };
}

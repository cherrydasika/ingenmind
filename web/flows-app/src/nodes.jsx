// Node renderers: a step card (agents, evaluators, guardrails, steps) with a
// handle per outcome, a resource pill (LLM, memory, retriever, tools…),
// start/end, and a subflow group.
import { Handle, Position } from "@xyflow/react";
import { sourceHandles } from "./layout.js";

export const CATEGORY = {
  agent: { icon: "◎", label: "Agent" },
  evaluator: { icon: "✓", label: "Evaluator" },
  guardrail: { icon: "⛨", label: "Guardrail" },
  step: { icon: "▸", label: "Step" },
  model: { icon: "✦", label: "Model" },
  memory: { icon: "◷", label: "Memory" },
  data: { icon: "▤", label: "Data" },
  tool: { icon: "⚙", label: "Tool" },
  control: { icon: "●", label: "Control" },
};

export const STATUS_TEXT = { running: "running", done: "done", error: "error", blocked: "blocked", failed: "failed" };

function title(data) {
  return data.node.label || data.component?.title || data.node.type;
}

function Badges({ data }) {
  const errors = data.issues.filter((i) => i.level === "error");
  const warnings = data.issues.filter((i) => i.level === "warning");
  return (
    <>
      {errors.length > 0 && <span className="fl-badge fl-badge-error" title={errors.map((i) => i.message).join("\n")}>{errors.length} !</span>}
      {!errors.length && warnings.length > 0 && <span className="fl-badge fl-badge-blocked" title={warnings.map((i) => i.message).join("\n")}>!</span>}
      {data.status && <span className={`fl-badge fl-badge-${data.status}`}>{STATUS_TEXT[data.status]}</span>}
    </>
  );
}

function classes(base, data, selected) {
  const errors = data.issues.some((i) => i.level === "error");
  return [base, `fl-cat-${data.component?.category || "step"}`, data.status && `is-${data.status}`,
          errors && "has-error", data.diff && `diff-${data.diff}`, selected && "is-selected"].filter(Boolean).join(" ");
}

function Outcomes({ handles }) {
  if (handles.length === 1 && !handles[0].label) {
    return <Handle type="source" position={Position.Bottom} id="out" />;
  }
  return (
    <div className="fl-outcomes">
      {handles.map((h) => (
        <div key={h.id} className={`fl-outcome ${h.outcome === "error" ? "is-error" : ""} ${h.delegate ? "is-delegate" : ""}`} title={h.label}>
          <span>{h.label.replace(/_/g, " ").toLowerCase()}</span>
          <Handle type="source" position={Position.Bottom} id={h.id} />
        </div>
      ))}
    </div>
  );
}

export function StepNode({ data, selected }) {
  const { component, node } = data;
  const cat = CATEGORY[component?.category] || CATEGORY.step;
  return (
    <div className={classes("fl-node", data, selected)}>
      <Handle type="target" position={Position.Top} id="in" />
      {component?.inputs?.length > 0 && <Handle type="target" position={Position.Left} id="inputs" className="fl-port" />}
      <div className="fl-node-main">
        <div className="fl-node-head">
          <span className="fl-icon">{cat.icon}</span>
          <span className="fl-title">{title(data)}</span>
          <Badges data={data} />
        </div>
        <div className="fl-node-sub">
          {component?.title || node.type}
          {component?.kind === "gate" && <span className="fl-chip">runs before graph</span>}
          {component?.bounds_loops && <span className="fl-chip">bounds loops</span>}
        </div>
      </div>
      <Outcomes handles={sourceHandles(component)} />
    </div>
  );
}

export function ResourceNode({ data, selected }) {
  const { component } = data;
  const cat = CATEGORY[component?.category] || CATEGORY.tool;
  return (
    <div className={`${classes("fl-resource", data, selected)} ${component?.runnable === false ? "is-unsupported" : ""}`}>
      {component?.inputs?.length > 0 && <Handle type="target" position={Position.Left} id="inputs" className="fl-port" />}
      <span className="fl-icon">{cat.icon}</span>
      <div className="fl-resource-text">
        <span className="fl-title">{title(data)}</span>
        <span className="fl-node-sub">{component?.title} → {component?.provides}</span>
      </div>
      <Badges data={data} />
      <Handle type="source" position={Position.Right} id="provides" className="fl-port" />
    </div>
  );
}

export function ControlNode({ data, selected }) {
  const isStart = data.node.type === "start";
  return (
    <div className={`${classes("fl-control", data, selected)} ${isStart ? "is-start" : "is-end"}`}>
      {!isStart && <Handle type="target" position={Position.Top} id="in" />}
      {isStart ? "Start" : "End"}
      {data.issues.some((i) => i.level === "error") && <span className="fl-badge fl-badge-error">!</span>}
      {isStart && <Handle type="source" position={Position.Bottom} id="out" />}
    </div>
  );
}

export function SubflowNode({ data, selected }) {
  return (
    <div className={classes("fl-group", data, selected)}>
      <Handle type="target" position={Position.Top} id="in" />
      <div className="fl-group-head">
        <span className="fl-icon">◎</span>
        <span className="fl-title">{title(data)}</span>
        <span className="fl-chip">subflow · {data.node.config.subflow}</span>
        <Badges data={data} />
      </div>
      <Handle type="source" position={Position.Bottom} id="out" />
    </div>
  );
}

export const nodeTypes = { step: StepNode, resource: ResourceNode, control: ControlNode, subflow: SubflowNode };

// The flow builder: palette → drag components onto the canvas, connect
// handles (checked as you drag), edit settings in forms generated from each
// component's schema, validate as you edit, save drafts, publish versions,
// make one live for users, import/export the JSON. The playground runs any
// saved version: its nodes light up, and a click on a node shows its trace
// (public-safe: timings, decisions, scores, counts — never text).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyNodeChanges, Background, Controls, MiniMap, ReactFlow, useEdgesState, useNodesState, useReactFlow } from "@xyflow/react";
import { fitGroups, GROUP_PAD, layoutFlow, makeEdge, nodeSize } from "./layout.js";
import { CATEGORY, nodeTypes } from "./nodes.jsx";
import { eventTargets, traceEntry } from "./events.js";
import { ConfigForm } from "./ConfigForm.jsx";
import { connectionProblem, delegateOutcome, initialConfig, rawId, toSpec, uniqueId, withoutPositions } from "./spec.js";
import { flowsApi } from "./api.js";
import { copySelection, pasteClip } from "./clipboard.js";

const DRAG_TYPE = "application/x-flow-component";
const HISTORY_LIMIT = 100;
const SETTLE_MS = 350;          // a drag or a burst of typing is one undo step
const SNAP_KEY = "flows.snap";
const READABLE_ZOOM = 0.8;      // opening a flow never zooms out further than this
const FRAME_PAD = 24;
const MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl+";
let clipboard = null;           // shared across flows in this tab

function readSnap() {
  try { return localStorage.getItem(SNAP_KEY) === "1"; } catch { return false; }
}

/** What undo restores: the canvas without run status, issues or selection. */
function snapshot(nodes, edges, meta, key) {
  return {
    key, meta,
    nodes: nodes.map((n) => ({ ...n, selected: false, data: { ...n.data, status: null, issues: [], diff: null } })),
    edges: edges.map((e) => ({ ...e, selected: false, animated: false })),
  };
}

const SHORTCUTS = [
  [`${MOD}Z`, "Undo"], [`${MOD}⇧Z`, "Redo"], [`${MOD}C`, "Copy the selected nodes"], [`${MOD}V`, "Paste"],
  [`${MOD}D`, "Duplicate the selected nodes"], [`${MOD}S`, "Save the draft"], ["Delete / ⌫", "Delete the selection"],
  ["⇧ + drag", "Select a box of nodes"], [`${MOD.replace("+", "")} + click`, "Add to the selection"],
  ["Esc", "Clear the selection; leave a version view"], ["?", "Show or hide these shortcuts"],
];

function Shortcuts({ onClose }) {
  return (
    <div className="fl-shortcuts" role="dialog" aria-label="Keyboard shortcuts">
      <div className="fl-panel-title">Keyboard shortcuts</div>
      <table className="fl-config"><tbody>
        {SHORTCUTS.map(([keys, what]) => <tr key={keys}><td><kbd>{keys}</kbd></td><td>{what}</td></tr>)}
      </tbody></table>
      <button type="button" className="fl-btn" onClick={onClose}>Close</button>
    </div>
  );
}
const CATEGORY_ORDER = ["agent", "evaluator", "guardrail", "step", "model", "memory", "data", "tool", "control"];
const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

const rfType = (component) => (component?.kind === "resource" ? "resource" : component?.kind === "control" ? "control"
  : component?.type === "subflow" ? "subflow" : "step");

// ---------- issues → nodes ----------

function issuesByNode(issues, nodes, root) {
  const parentOf = { [root]: null };
  for (const n of nodes) if (n.type === "subflow") parentOf[`${n.data.path}/${n.data.node.config.subflow}`] = n.id;
  const map = {};
  const general = [];
  for (const issue of issues) {
    const dot = issue.where.indexOf(".");
    const path = dot < 0 ? issue.where : issue.where.slice(0, dot);
    const rest = dot < 0 ? null : issue.where.slice(dot + 1);
    const parent = parentOf[path];
    let target = null;
    if (rest && !rest.includes("→") && parent !== undefined) target = parent ? `${parent}/${rest}` : rest;
    else if (!rest && parent) target = parent;
    if (target && nodes.some((n) => n.id === target)) (map[target] ||= []).push(issue);
    else general.push(issue);
  }
  return { map, general };
}

// ---------- side panels ----------

function Palette({ components }) {
  const groups = CATEGORY_ORDER.map((cat) => [cat, components.filter((c) => c.category === cat)]).filter(([, l]) => l.length);
  return (
    <aside className="fl-palette">
      <div className="fl-panel-title" title={"Drag onto the canvas, or into a subflow box. Connect a bottom handle to a top "
        + "handle for the order steps run in; a resource's right handle to a step's left port."}>Components</div>
      {groups.map(([cat, list]) => (
        <div key={cat} className="fl-palette-group">
          <div className="fl-palette-label">{CATEGORY[cat]?.label || cat}</div>
          {list.map((c) => (
            <div key={c.type} className={`fl-palette-item fl-cat-${c.category}`} title={c.description} draggable
              onDragStart={(e) => { e.dataTransfer.setData(DRAG_TYPE, c.type); e.dataTransfer.effectAllowed = "move"; }}>
              <span className="fl-icon">{CATEGORY[c.category]?.icon}</span>
              <span>{c.title}</span>
              {!c.runnable && <span className="fl-chip">soon</span>}
            </div>
          ))}
        </div>
      ))}
    </aside>
  );
}

const TRACE_HIDE = new Set(["kind", "at", "state"]);

function TraceValue({ name, value }) {
  if (name === "scores" && value && typeof value === "object") {
    return (
      <div className="fl-scores">
        {Object.entries(value).map(([k, v]) => (
          <div key={k} className="fl-score">
            <span>{k.replace(/_/g, " ")}</span>
            <span className="fl-score-bar"><span style={{ width: `${Math.round(Math.max(0, Math.min(1, v)) * 100)}%` }} /></span>
            <span>{Number(v).toFixed(2)}</span>
          </div>
        ))}
      </div>
    );
  }
  if (Array.isArray(value)) return <span>{value.length ? value.join(", ") : "—"}</span>;
  if (typeof value === "boolean") return <span>{value ? "yes" : "no"}</span>;
  if (typeof value === "number") return <span>{name === "seconds" ? `${value.toFixed(2)} s` : value}</span>;
  return <span>{String(value)}</span>;
}

function Trace({ entries }) {
  return (
    <>
      <div className="fl-section">Last run</div>
      <ol className="fl-trace">
        {entries.map((entry, i) => (
          <li key={i}>
            <div className="fl-trace-head">
              <span className={`fl-badge fl-badge-${entry.state === "start" ? "running" : entry.failed || entry.state === "error" ? "error" : "done"}`}>
                {entry.kind === "activity" ? entry.state : `${entry.kind.replace("_", " ")} · ${entry.state}`}
              </span>
              {typeof entry.seconds === "number" && <span className="fl-faint">{entry.seconds.toFixed(2)} s</span>}
            </div>
            {Object.entries(entry).filter(([k]) => !TRACE_HIDE.has(k) && k !== "seconds").length > 0 && (
              <table className="fl-config">
                <tbody>
                  {Object.entries(entry).filter(([k]) => !TRACE_HIDE.has(k) && k !== "seconds").map(([k, v]) => (
                    <tr key={k}><td>{k.replace(/_/g, " ")}</td><td><TraceValue name={k} value={v} /></td></tr>
                  ))}
                </tbody>
              </table>
            )}
          </li>
        ))}
      </ol>
      <p className="fl-hint">Public-safe trace: timings, decisions, scores and counts. Prompts, tasks, queries and
        answers are never shown.</p>
    </>
  );
}

function NodeDetails({ node, edges, trace, onSpec, onDelete, readOnly }) {
  const { node: spec, component, issues } = node.data;
  const outgoing = edges.filter((e) => e.source === node.id && !e.data.resource);
  const plugged = edges.filter((e) => e.target === node.id && e.data.resource);
  const configComponent = spec.type === "subflow"
    ? { ...component, config_schema: { ...component.config_schema, properties: {} } } : component;
  return (
    <>
      <div className="fl-col">
      <div className="fl-panel-title">{spec.label || component?.title}</div>
      <div className="fl-details-type"><code>{spec.type}</code> · {component?.kind} · id <code>{spec.id}</code></div>
      <p>{component?.description}</p>
      {issues.map((i, n) => <div key={n} className={`fl-issue is-${i.level}`}>{i.message}</div>)}
      <label className="fl-field">
        <span className="fl-field-name">label</span>
        <input value={spec.label || ""} placeholder={component?.title} disabled={readOnly}
          onChange={(e) => onSpec((s) => ({ ...s, label: e.target.value || undefined }))} />
      </label>
      {spec.type === "subflow" && <p className="fl-hint">Runs the nested flow <code>{spec.config.subflow}</code> once per delegated task.</p>}
      {outgoing.length > 0 && (
        <>
          <div className="fl-section">Next</div>
          <ul className="fl-list">
            {outgoing.map((e) => <li key={e.id}><span className="fl-chip">{e.data.spec.outcome || "then"}</span> → {e.data.spec.target}</li>)}
          </ul>
        </>
      )}
      {component?.inputs?.length > 0 && (
        <>
          <div className="fl-section">Ports</div>
          <ul className="fl-list">
            {component.inputs.map((port) => {
              const names = plugged.filter((e) => (e.data.spec.port || port) === port).map((e) => rawId(e.source));
              return <li key={port}><span className="fl-chip">{port}</span> {names.length ? names.join(", ") : <span className="fl-faint">not connected</span>}</li>;
            })}
          </ul>
        </>
      )}
      {component?.provides && <><div className="fl-section">Provides</div><p><span className="fl-chip">{component.provides}</span></p></>}
      {!readOnly && <button type="button" className="fl-btn fl-btn-danger" onClick={onDelete}>Delete node</button>}
      </div>
      <div className="fl-col">
        <div className="fl-section">Settings</div>
        <ConfigForm component={configComponent} config={spec.config} readOnly={readOnly}
          onChange={(config) => onSpec((s) => ({ ...s, config }))} />
      </div>
      {trace?.length > 0 && <div className="fl-col"><Trace entries={trace} /></div>}
    </>
  );
}

function EdgeDetails({ edge, onDelete, readOnly }) {
  const s = edge.data.spec;
  return (
    <div className="fl-col">
      <div className="fl-panel-title">{s.kind === "resource" ? "Resource link" : "Flow edge"}</div>
      <p><code>{s.source}</code> → <code>{s.target}</code></p>
      {s.outcome && <p>Branch <span className="fl-chip">{s.outcome}</span></p>}
      {s.kind === "resource" && <p className="fl-hint">Plugs <code>{s.source}</code> into <code>{s.target}</code>'s port.</p>}
      {!readOnly && <button type="button" className="fl-btn fl-btn-danger" onClick={onDelete}>Delete edge</button>}
    </div>
  );
}

function FlowDetails({ meta, setMeta, record, onLoadVersion, onViewVersion, confirm, onMakeLive, onDiff, onDuplicate,
                      metrics }) {
  const flowId = meta.flows[meta.root].id;
  const root = meta.flows[meta.root];
  const set = (key) => (e) => setMeta((m) => ({ ...m, flows: { ...m.flows, [m.root]: { ...m.flows[m.root], [key]: e.target.value } } }));
  return (
    <>
      <div className="fl-col">
      <div className="fl-panel-title">Flow</div>
      <label className="fl-field"><span className="fl-field-name">name</span><input value={root.name} onChange={set("name")} /></label>
      <label className="fl-field"><span className="fl-field-name">description</span>
        <textarea rows={3} value={root.description || ""} onChange={set("description")} /></label>
      <p className="fl-hint">Click a node or edge to edit it. Delete or Backspace removes the selection.</p>
      <div className="fl-legend">
        <div><span className="fl-legend-line" /> flow: the order steps run in</div>
        <div><span className="fl-legend-line is-error" /> error branch</div>
        <div><span className="fl-legend-line is-resource" /> resource plugged into a port</div>
      </div>
      </div>
      <div className="fl-col">
      <div className="fl-panel-title">Versions</div>
      <div className="fl-section">Users get</div>
      <p className="fl-live-line"><span className="fl-chip is-live">live</span> <code>{record.live.flow_id}</code> v{record.live.version}
        {record.live.flow_id === flowId ? " (this flow)" : ""}</p>
      <div className="fl-section">History</div>
      <ul className="fl-list fl-history">
        {record.versions.map((v) => {
          const live = record.live.flow_id === flowId && record.live.version === v.version;
          const asking = confirm === `live:${v.version}`;
          return (
            <li key={v.version}>
              <span className="fl-chip">v{v.version}</span>
              {live && <span className="fl-chip is-live">live</span>}
              <span className="fl-history-note">{v.note || (v.published_at ? new Date(v.published_at).toLocaleString() : "")}</span>
              <button type="button" className="fl-link" onClick={() => onViewVersion(v.version)}
                title="Look at this version read-only; the editor keeps your changes">view</button>
              <button type="button" className="fl-link" onClick={() => onLoadVersion(v.version)}
                title="Replace the editor's contents with this version (undo brings yours back)">load</button>
              <button type="button" className="fl-link" onClick={() => onDiff(v.version)} title="Compare with what the editor shows">diff</button>
              <button type="button" className="fl-link" onClick={() => onDuplicate(v.version)} title="Start a new flow from this version">duplicate</button>
              {!live && (
                <button type="button" className={`fl-link ${asking ? "is-confirm" : ""}`} onClick={() => onMakeLive(v.version)}
                  title="Users on the Retrieval page get this version">{asking ? "confirm: users get this" : "make live"}</button>
              )}
            </li>
          );
        })}
        {!record.versions.length && <li className="fl-faint">Not published yet.</li>}
      </ul>
      </div>
      {metrics && <div className="fl-col"><Metrics metrics={metrics} /></div>}
    </>
  );
}

function Verdict({ issues, general }) {
  const errors = issues.filter((i) => i.level === "error");
  const warnings = issues.filter((i) => i.level === "warning");
  const [open, setOpen] = useState(false);
  const cls = errors.length ? "is-error" : warnings.length ? "is-warning" : "is-ok";
  const text = errors.length ? `${errors.length} error${errors.length > 1 ? "s" : ""}: cannot publish`
    : warnings.length ? `Valid, ${warnings.length} warning${warnings.length > 1 ? "s" : ""}` : "Valid";
  return (
    <div className={`fl-verdict ${cls}`}>
      <button type="button" onClick={() => setOpen(!open)} disabled={!issues.length}>{text}</button>
      {open && <ul>{issues.map((i, n) => <li key={n} className={general.includes(i) ? "" : "fl-faint"}><code>{i.where}</code> {i.message}</li>)}</ul>}
    </div>
  );
}

function Playground({ record, flowId, dirty, session, onEvent, onStart, onDone }) {
  const [question, setQuestion] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState([]);
  useEffect(() => { setTurns([]); setTarget(""); }, [flowId]);
  const current = record.source === "draft" ? "the draft" : `v${record.published_version}`;
  const options = [{ value: "", label: `What the editor shows (${current})` },
    ...record.versions.map((v) => ({ value: String(v.version), label: `v${v.version}${v.version === 0 ? " · built-in" : ""}` }))];
  const blocked = dirty && target === "";
  const submit = async (event) => {
    event.preventDefault();
    const q = question.trim();
    if (!q || busy || blocked) return;
    onStart();
    setBusy(true);
    setQuestion("");
    const id = `${Date.now()}-${Math.random()}`;
    setTurns((list) => [...list, { id, question: q, answer: null, error: null }]);
    const update = (patch) => setTurns((list) => list.map((t) => (t.id === id ? { ...t, ...patch } : t)));
    try {
      const { id: sessionId, userId } = session();
      const result = await flowsApi.run(flowId, {
        question: q, session_id: sessionId, user_id: userId, version: target === "" ? undefined : Number(target),
      }, onEvent);
      update({ answer: result.answer ?? "", flow: result.flow, total: result.total });
    } catch (err) {
      update({ error: err.message });
    } finally {
      setBusy(false);
      onDone?.();
    }
  };
  return (
    <div className="fl-run">
      {turns.length > 0 && (
        <div className="fl-chat">
          {turns.map((t) => (
            <div key={t.id} className="fl-turn">
              <div className="fl-q">{t.question}</div>
              {t.answer !== null && <div className="fl-answer">{t.answer ? <AnswerText text={t.answer} /> : <span className="fl-faint">No answer.</span>}
                {t.flow && <div className="fl-faint fl-turn-meta">{t.flow.id} {typeof t.flow.version === "number" ? `v${t.flow.version}` : t.flow.version}{t.total ? ` · ${t.total.toFixed(1)} s` : ""}</div>}</div>}
              {t.error && <div className="fl-issue is-error">{t.error}</div>}
              {t.answer === null && !t.error && <div className="fl-faint">Running… click a node to follow its trace.</div>}
            </div>
          ))}
        </div>
      )}
      <form onSubmit={submit}>
        <select className="fl-select" value={target} onChange={(e) => setTarget(e.target.value)} disabled={busy}
          title="Which version of this flow to run">
          {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <input value={question} onChange={(e) => setQuestion(e.target.value)} disabled={busy}
          placeholder="Playground: ask a travel question and watch this flow run…" />
        <button type="submit" disabled={busy || !question.trim() || blocked}>{busy ? "Running…" : "Run"}</button>
      </form>
      {blocked && <div className="fl-field-help">Save the draft to run your changes, or pick a published version.</div>}
    </div>
  );
}

function NewFlow({ templates, initialFrom, versionLabel, onCreate, onCancel }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [from, setFrom] = useState(initialFrom || "blank");
  useEffect(() => { if (initialFrom) setFrom(initialFrom); }, [initialFrom]);
  const ok = ID_PATTERN.test(id) && name.trim();
  const chosen = templates.find((t) => `template:${t.id}` === from);
  return (
    <form className="fl-newflow" onSubmit={(e) => { e.preventDefault(); if (ok) onCreate(id, name.trim(), from); }}>
      <input value={id} onChange={(e) => setId(e.target.value)} placeholder="id, e.g. visa_only" autoFocus />
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" />
      <select value={from} onChange={(e) => setFrom(e.target.value)}>
        <option value="blank">Blank (guardrails only)</option>
        <option value="copy">Copy of what the editor shows</option>
        {versionLabel && <option value={initialFrom}>Duplicate {versionLabel}</option>}
        <optgroup label="Templates">
          {templates.map((t) => <option key={t.id} value={`template:${t.id}`}>{t.name}</option>)}
        </optgroup>
      </select>
      <button type="submit" className="fl-btn fl-btn-primary" disabled={!ok}>Create</button>
      <button type="button" className="fl-btn" onClick={onCancel}>Cancel</button>
      {chosen && <span className="fl-field-help">{chosen.description}</span>}
      {id && !ID_PATTERN.test(id) && <span className="fl-field-help">lowercase letters, digits and _; starts with a letter</span>}
    </form>
  );
}

const BLANK_FLOW = {
  nodes: [{ id: "start", type: "start" }, { id: "input_guardrail", type: "input_guardrail", config: { stage: "input" } },
          { id: "output_guardrail", type: "output_guardrail" }, { id: "end", type: "end" }],
  edges: [{ source: "start", target: "input_guardrail" },
          { source: "input_guardrail", target: "output_guardrail", outcome: "ALLOW" },
          { source: "input_guardrail", target: "end", outcome: "BLOCK" },
          { source: "output_guardrail", target: "end", outcome: "ALLOW" },
          { source: "output_guardrail", target: "end", outcome: "BLOCK" }],
};

// ---------- diff ----------

function DiffPanel({ diff, onSelect, onClose }) {
  const groups = {};
  for (const c of diff.changes) (groups[c.flow] ||= []).push(c);
  return (
    <div className="fl-col fl-col-wide">
      <div className="fl-panel-title">Changes since {diff.label}</div>
      <p className="fl-hint">Compared with what the editor shows now. Added and changed nodes are outlined on the canvas;
        positions are ignored.</p>
      {!diff.changes.length && <p>No changes.</p>}
      {Object.entries(groups).map(([flow, changes]) => (
        <div key={flow}>
          <div className="fl-section">{flow}</div>
          <ul className="fl-list fl-diff">
            {changes.map((c, i) => (
              <li key={i} className={`is-${c.change}`}>
                <span className={`fl-chip fl-diff-${c.change}`}>{c.change}</span>
                <span className="fl-faint">{c.kind}</span>
                {c.kind === "node" && c.change !== "removed"
                  ? <button type="button" className="fl-link fl-diff-id" onClick={() => onSelect(c)}>{c.id}</button>
                  : <code>{c.id}</code>}
                {Object.entries(c.details).filter(([, v]) => Array.isArray(v)).map(([k, [a, b]]) => (
                  <div key={k} className="fl-diff-detail"><code>{k}</code> {JSON.stringify(a) ?? "—"} → {JSON.stringify(b) ?? "—"}</div>
                ))}
              </li>
            ))}
          </ul>
        </div>
      ))}
      <button type="button" className="fl-btn" onClick={onClose}>Close comparison</button>
    </div>
  );
}

// ---------- answers ----------

/** The answer's text with **bold** spans (the summarizer's only Markdown), safely as React nodes. */
function AnswerText({ text }) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : part);
}

// ---------- metrics ----------

function Metrics({ metrics }) {
  if (!metrics) return null;
  const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
  const compact = (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v));
  const secs = (v) => `${v < 10 ? v.toFixed(1) : Math.round(v)}s`;
  return (
    <>
      <div className="fl-panel-title">Metrics <span className="fl-faint">· last {metrics.days} days</span></div>
      {!metrics.versions.length ? <p className="fl-hint">No runs yet.</p> : (
        <table className="fl-config fl-metrics">
          <thead><tr><th>version</th><th>runs</th><th>pass</th><th>blocked</th><th>time</th><th>tokens</th></tr></thead>
          <tbody>
            {metrics.versions.map((m) => (
              <tr key={`${m.version}-${m.source}`} title={`${m.source}; last run ${new Date(m.last_run).toLocaleString()}; `
                + `${m.failed} failed; avg overall ${m.overall ?? "—"}; avg ${m.rounds} rounds`}>
                <td>{m.version === "draft" ? "draft" : `v${m.version}`}<div className="fl-faint">{m.source === "users" ? "users" : "playground"}</div></td>
                <td>{m.runs}{m.failed ? <span className="fl-err"> ({m.failed}✗)</span> : ""}</td>
                <td title={`${m.passed} of ${m.evaluated} evaluated runs passed`}>{pct(m.pass_rate)}</td>
                <td title="input / output guardrail">{m.input_blocked}/{m.output_blocked}</td>
                <td title="median / 90th percentile">{secs(m.p50_seconds)}<div className="fl-faint">p90 {secs(m.p90_seconds)}</div></td>
                <td title="average tokens per run">{compact(m.tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="fl-hint">Pass = answer evaluator passes among evaluated runs; blocked = input / output guardrail.
        Numbers only: questions and answers are not stored.</p>
    </>
  );
}

// ---------- the builder ----------

export function App({ initialFlowId = "travel_assistant", session, colorMode = "dark" }) {
  const { screenToFlowPosition, getNodes, getNodesBounds, setViewport } = useReactFlow();
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [meta, setMeta] = useState(null);
  const [components, setComponents] = useState([]);
  const [record, setRecord] = useState(null);
  const [flowList, setFlowList] = useState([]);
  const [saved, setSaved] = useState(null);
  const [issues, setIssues] = useState([]);
  const [selection, setSelection] = useState(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [newFlow, setNewFlow] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [note, setNote] = useState("");
  const [traces, setTraces] = useState({});
  const [templates, setTemplates] = useState([]);
  const [duplicateOf, setDuplicateOf] = useState(null);
  const [diff, setDiff] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [snap, setSnap] = useState(readSnap);
  const [showKeys, setShowKeys] = useState(false);
  const [flowPanel, setFlowPanel] = useState(false);   // the flow's own details, opened from the toolbar
  const [viewing, setViewing] = useState(null);     // a version shown read-only: {label, nodes, edges}
  const [, setHistTick] = useState(0);
  const hist = useRef({ past: [], future: [], stable: null });
  const pastes = useRef(0);
  const fileInput = useRef(null);
  const canvasRef = useRef(null);
  const latest = useRef({ nodes, edges });
  latest.current = { nodes, edges };

  const catalogue = useMemo(() => Object.fromEntries(components.map((c) => [c.type, c])), [components]);
  const spec = useMemo(() => (meta ? toSpec(nodes, edges, meta) : null), [nodes, edges, meta]);
  const fullKey = useMemo(() => (spec ? JSON.stringify(spec) : null), [spec]);
  const shapeKey = useMemo(() => (spec ? JSON.stringify(withoutPositions(spec)) : null), [spec]);
  const dirty = saved !== null && fullKey !== saved;
  const errors = issues.filter((i) => i.level === "error");

  const say = useCallback((text, kind = "info") => {
    setToast({ text, kind, at: Date.now() });
  }, []);
  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast(null), toast.kind === "error" ? 7000 : 3500);
    return () => clearTimeout(t);
  }, [toast]);

  /** Fit the flow to the canvas, but never below a readable zoom: a flow too
   *  big for that is shown from its top (centred if it fits across). */
  const frame = useCallback(() => {
    const el = canvasRef.current;
    const ns = getNodes();
    if (!el || !ns.length) return;
    const { width: w, height: h } = el.getBoundingClientRect();
    const b = getNodesBounds(ns);
    const zoom = Math.max(READABLE_ZOOM, Math.min(1, (w - 2 * FRAME_PAD) / b.width, (h - 2 * FRAME_PAD) / b.height));
    const along = (size, start, span) => (span * zoom <= size - 2 * FRAME_PAD
      ? (size - span * zoom) / 2 - start * zoom : FRAME_PAD - start * zoom);
    setViewport({ x: along(w, b.x, b.width), y: along(h, b.y, b.height), zoom });
  }, [getNodes, getNodesBounds, setViewport]);

  /** Put a flow on the canvas. `baseline`: it is what is saved (not a change). */
  const show = useCallback((flow, comps, { baseline = false, auto = false } = {}) => {
    const laid = layoutFlow(flow, comps, { auto });
    const placed = fitGroups(laid.nodes);
    setNodes(placed);
    setEdges(laid.edges);
    setMeta(laid.meta);
    setSelection(null);
    setViewing(null);
    if (baseline) {
      setSaved(JSON.stringify(toSpec(placed, laid.edges, laid.meta)));
      hist.current = { past: [], future: [], stable: null };   // a different flow: nothing to undo
      setHistTick((t) => t + 1);
    }
    setTimeout(frame, 30);
  }, [setNodes, setEdges, frame]);

  const applyRecord = useCallback((rec) => {
    setRecord(rec);
    setComponents(rec.components);
    setIssues(rec.issues);
    show(rec.flow, rec.components, { baseline: true });
  }, [show]);

  const refreshList = useCallback(() => flowsApi.list().then(setFlowList).catch(() => {}), []);

  const open = useCallback(async (id) => {
    setBusy(true);
    try {
      applyRecord(await flowsApi.get(id));
      setLoadError(null);
    } catch (err) {
      setLoadError(err.message);
    } finally {
      setBusy(false);
    }
  }, [applyRecord]);

  useEffect(() => {
    open(initialFlowId);
    refreshList();
    flowsApi.templates().then(setTemplates).catch(() => {});
  }, [open, refreshList, initialFlowId]);

  const flowId = record?.flow?.id;
  const refreshMetrics = useCallback(() => {
    if (flowId) flowsApi.metrics(flowId).then(setMetrics).catch(() => setMetrics(null));
  }, [flowId]);
  useEffect(() => { setMetrics(null); setDiff(null); refreshMetrics(); }, [refreshMetrics]);

  // Validate as you edit (positions do not matter).
  useEffect(() => {
    if (!spec) return undefined;
    const t = setTimeout(() => {
      flowsApi.validate(spec).then((r) => setIssues(r.issues)).catch(() => {});
    }, 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapeKey]);

  // Show issues on their nodes.
  const { map: issueMap, general } = useMemo(
    () => (meta ? issuesByNode(issues, nodes, meta.root) : { map: {}, general: issues }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [issues, meta, nodes.length]);
  useEffect(() => {
    setNodes((list) => {
      let changed = false;
      const next = list.map((n) => {
        const mine = issueMap[n.id] || [];
        if (JSON.stringify(mine) === JSON.stringify(n.data.issues)) return n;
        changed = true;
        return { ...n, data: { ...n.data, issues: mine } };
      });
      return changed ? next : list;
    });
  }, [issueMap, setNodes]);

  // Outline what a comparison found added or changed.
  useEffect(() => {
    const marks = {};
    if (diff && meta) {
      const parentOf = { [meta.root]: null };
      for (const n of latest.current.nodes) if (n.type === "subflow") parentOf[`${n.data.path}/${n.data.node.config.subflow}`] = n.id;
      for (const c of diff.changes) {
        if (c.kind !== "node" || c.change === "removed" || parentOf[c.flow] === undefined) continue;
        marks[parentOf[c.flow] ? `${parentOf[c.flow]}/${c.id}` : c.id] = c.change;
      }
    }
    setNodes((list) => {
      let changed = false;
      const next = list.map((n) => {
        const mark = marks[n.id] || null;
        if ((n.data.diff || null) === mark) return n;
        changed = true;
        return { ...n, data: { ...n.data, diff: mark } };
      });
      return changed ? next : list;
    });
  }, [diff, meta, setNodes]);

  // ---------- undo / redo ----------
  // When the flow JSON settles, the previous settled state becomes an undo step.
  const metaRef = useRef(meta);
  metaRef.current = meta;
  const keyRef = useRef(fullKey);
  keyRef.current = fullKey;
  const settle = useCallback(() => {
    const h = hist.current;
    const key = keyRef.current;
    if (!key || !metaRef.current) return;
    if (!h.stable) {
      h.stable = snapshot(latest.current.nodes, latest.current.edges, metaRef.current, key);
      return;
    }
    if (h.stable.key === key) return;
    h.past.push(h.stable);
    if (h.past.length > HISTORY_LIMIT) h.past.shift();
    h.future = [];
    h.stable = snapshot(latest.current.nodes, latest.current.edges, metaRef.current, key);
    setHistTick((t) => t + 1);
  }, []);
  useEffect(() => {
    if (!fullKey) return undefined;
    const t = setTimeout(settle, SETTLE_MS);
    return () => clearTimeout(t);
  }, [fullKey, settle]);

  const restore = useCallback((state) => {
    hist.current.stable = state;
    setNodes(state.nodes);
    setEdges(state.edges);
    setMeta(state.meta);
    setSelection(null);
    setHistTick((t) => t + 1);
  }, [setNodes, setEdges]);
  const undo = useCallback(() => {
    settle();                                   // a change still settling is the step to undo
    const h = hist.current;
    if (!h.past.length) return;
    h.future.push(h.stable);
    restore(h.past.pop());
  }, [settle, restore]);
  const redo = useCallback(() => {
    settle();
    const h = hist.current;
    if (!h.future.length) return;
    h.past.push(h.stable);
    restore(h.future.pop());
  }, [settle, restore]);

  // ---------- copy / paste ----------
  const copy = useCallback(() => {
    const { clip, error } = copySelection(latest.current.nodes, latest.current.edges);
    if (error) return say(error, "error");
    clipboard = clip;
    pastes.current = 0;
    return say(`Copied ${clip.nodes.length} node${clip.nodes.length > 1 ? "s" : ""}`);
  }, [say]);
  const paste = useCallback(() => {
    if (!clipboard || !metaRef.current) return say("Nothing to paste: copy nodes first.", "error");
    pastes.current += 1;
    const { nodes: added, edges: addedEdges } = pasteClip(clipboard, latest.current.nodes, metaRef.current, pastes.current);
    setNodes((list) => fitGroups([...list.map((n) => (n.selected ? { ...n, selected: false } : n)), ...added]));
    setEdges((list) => [...list, ...addedEdges]);
    setSelection(added.length === 1 ? { kind: "node", id: added[0].id } : null);
    return say(`Pasted ${added.length} node${added.length > 1 ? "s" : ""}`);
  }, [setNodes, setEdges, say]);
  const duplicateSelection = useCallback(() => {
    const { clip, error } = copySelection(latest.current.nodes, latest.current.edges);
    if (error) return say(error, "error");
    clipboard = clip;
    pastes.current = 0;
    return paste();
  }, [paste, say]);

  const canvasIdOf = (change) => {
    if (!meta) return null;
    if (change.flow === meta.root) return change.id;
    const group = latest.current.nodes.find((n) => n.type === "subflow"
      && `${n.data.path}/${n.data.node.config.subflow}` === change.flow);
    return group ? `${group.id}/${change.id}` : null;
  };

  // ---------- editing ----------

  const updateSpec = useCallback((id, fn) => {
    setNodes((list) => list.map((n) => (n.id === id ? { ...n, data: { ...n.data, node: fn(n.data.node) } } : n)));
  }, [setNodes]);

  const removeNodes = useCallback((ids) => {
    const doomed = new Set(ids);
    let grew = true;
    const all = latest.current.nodes;
    while (grew) {
      grew = false;
      for (const n of all) if (n.parentId && doomed.has(n.parentId) && !doomed.has(n.id)) { doomed.add(n.id); grew = true; }
    }
    setNodes((list) => list.filter((n) => !doomed.has(n.id)));
    setEdges((list) => list.filter((e) => !doomed.has(e.source) && !doomed.has(e.target)));
    setSelection(null);
  }, [setNodes, setEdges]);

  const onNodesDelete = useCallback((deleted) => removeNodes(deleted.map((n) => n.id)), [removeNodes]);

  const isValidConnection = useCallback(
    (conn) => !connectionProblem(conn, latest.current.nodes, latest.current.edges), []);

  const onConnect = useCallback((conn) => {
    const { nodes: ns, edges: es } = latest.current;
    const problem = connectionProblem(conn, ns, es);
    if (problem) return say(problem, "error");
    const source = ns.find((n) => n.id === conn.source), target = ns.find((n) => n.id === conn.target);
    const base = { source: rawId(source.id), target: rawId(target.id) };
    let edgeSpec;
    if (conn.sourceHandle === "provides") edgeSpec = { ...base, kind: "resource" };
    else if (conn.sourceHandle === "delegate") edgeSpec = { ...base, kind: "flow", outcome: delegateOutcome(target) };
    else if (conn.sourceHandle?.startsWith("out:")) edgeSpec = { ...base, kind: "flow", outcome: conn.sourceHandle.slice(4) };
    else edgeSpec = { ...base, kind: "flow" };
    const prefix = source.parentId ? `${source.parentId}/` : "";
    setEdges((list) => [...list, makeEdge(edgeSpec, prefix, source.data.component)]);
    return undefined;
  }, [setEdges, say]);

  const onConnectEnd = useCallback((_, state) => {
    if (state?.isValid === false && state.toNode && state.fromNode) {
      const problem = connectionProblem({
        source: state.fromHandle?.type === "source" ? state.fromNode.id : state.toNode.id,
        target: state.fromHandle?.type === "source" ? state.toNode.id : state.fromNode.id,
        sourceHandle: state.fromHandle?.type === "source" ? state.fromHandle.id : state.toHandle?.id,
        targetHandle: state.fromHandle?.type === "source" ? state.toHandle?.id : state.fromHandle?.id,
      }, latest.current.nodes, latest.current.edges);
      if (problem) say(problem, "error");
    }
  }, [say]);

  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback((event) => {
    event.preventDefault();
    const type = event.dataTransfer.getData(DRAG_TYPE);
    const component = catalogue[type];
    if (!component || !meta || viewing) return;
    const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    const ns = latest.current.nodes;
    const byId = new Map(ns.map((n) => [n.id, n]));
    const abs = (n) => (n.parentId ? { x: abs(byId.get(n.parentId)).x + n.position.x, y: abs(byId.get(n.parentId)).y + n.position.y } : n.position);
    const inside = ns.filter((n) => n.type === "subflow").filter((g) => {
      const p = abs(g);
      return point.x >= p.x && point.y >= p.y && point.x <= p.x + (g.style?.width || 0) && point.y <= p.y + (g.style?.height || 0);
    });
    const group = inside.sort((a, b) => b.id.split("/").length - a.id.split("/").length)[0];
    if (group && type === "subflow") return say("Subflows cannot be nested in the editor: drop it outside the box.", "error");
    const scope = ns.filter((n) => (n.parentId || null) === (group?.id || null));
    const id = uniqueId(type, new Set(scope.map((n) => rawId(n.id))));
    const [width, height] = nodeSize(component);
    const origin = group ? abs(group) : { x: 0, y: 0 };
    const path = group ? `${group.data.path}/${group.data.node.config.subflow}` : meta.root;
    const canvasId = group ? `${group.id}/${id}` : id;
    const added = [];
    const addedEdges = [];
    let config = initialConfig(type);
    if (type === "subflow") {
      const taken = new Set(scope.filter((n) => n.type === "subflow").map((n) => n.data.node.config.subflow));
      const name = uniqueId(id, taken);
      config = { subflow: name };
      setMeta((m) => ({ ...m, flows: { ...m.flows, [`${path}/${name}`]: { id: name, name: "New subflow", version: 1, description: "", state: "task" } } }));
      for (const [child, y] of [["start", 14], ["end", 110]]) {
        added.push({ id: `${canvasId}/${child}`, type: "control", parentId: canvasId,
          position: { x: GROUP_PAD.side + 112, y: GROUP_PAD.top + y },
          data: { node: { id: child, type: child, config: {} }, component: catalogue[child], status: null, issues: [], path: `${path}/${name}` },
          style: { width: nodeSize(catalogue[child])[0], height: nodeSize(catalogue[child])[1] } });
      }
      addedEdges.push(makeEdge({ source: "start", target: "end", kind: "flow" }, `${canvasId}/`, catalogue.start));
    }
    const node = {
      id: canvasId, type: rfType(component),
      position: { x: point.x - origin.x - width / 2, y: point.y - origin.y - height / 2 },
      data: { node: { id, type, config }, component, status: null, issues: [], path },
      style: type === "subflow" ? { width: 320, height: 200 } : { width, height },
      ...(type === "subflow" ? { dragHandle: ".fl-group-head" } : {}),
      ...(group ? { parentId: group.id } : {}),
    };
    setNodes((list) => fitGroups([...list, node, ...added]));
    setEdges((list) => [...list, ...addedEdges]);
    setSelection({ kind: "node", id: canvasId });
    return undefined;
  }, [catalogue, meta, viewing, screenToFlowPosition, setNodes, setEdges, say]);

  // ---------- saving ----------

  const guard = async (fn) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      say(err.message, "error");
      if (err.issues?.length) setIssues(err.issues);
    } finally {
      setBusy(false);
    }
  };

  const saveDraft = () => guard(async () => {
    const rec = await flowsApi.saveDraft(spec);
    setRecord(rec);
    setIssues(rec.issues);
    setSaved(fullKey);
    refreshList();
    say("Draft saved");
  });

  const publish = () => guard(async () => {
    if (dirty || record.source !== "draft") await flowsApi.saveDraft(spec);
    const rec = await flowsApi.publish(spec.id, note);
    applyRecord(rec);
    setNote("");
    setConfirm(null);
    refreshList();
    say(`Published v${rec.version}`);
  });

  const discard = () => guard(async () => {
    if (record.source === "draft") applyRecord(await flowsApi.discardDraft(spec.id));
    else show(record.flow, components, { baseline: true });
    setConfirm(null);
    refreshList();
    say("Changes discarded");
  });

  const createFlow = (id, name, from) => guard(async () => {
    let base;
    if (from === "copy") base = spec;
    else if (from.startsWith("template:")) base = templates.find((t) => `template:${t.id}` === from)?.flow;
    else if (from.startsWith("version:")) base = (await flowsApi.version(spec.id, Number(from.slice(8)))).flow;
    else base = BLANK_FLOW;
    if (!base) throw new Error("unknown starting point");
    const flow = { ...base, id, name, version: 1 };
    const rec = await flowsApi.create(flow);
    applyRecord(rec);
    setNewFlow(false);
    setDuplicateOf(null);
    refreshList();
    say(`Created ${id}`);
  });

  const showDiff = (version) => guard(async () => {
    const { flow } = await flowsApi.version(spec.id, version);
    const { changes } = await flowsApi.diff(flow, spec);
    setSelection(null);
    setDiff({ label: `v${version}`, changes });
  });

  const duplicate = (version) => {
    setDuplicateOf(version);
    setNewFlow(true);
  };

  const makeLive = (version) => {
    if (confirm !== `live:${version}`) {
      setConfirm(`live:${version}`);
      return;
    }
    guard(async () => {
      const { live } = await flowsApi.setLive(spec.id, version);
      setRecord((r) => ({ ...r, live }));
      setConfirm(null);
      refreshList();
      say(`Users now get ${live.flow_id} v${live.version}`);
    });
  };

  const switchFlow = (id) => {
    if (dirty && confirm !== `switch:${id}`) {
      setConfirm(`switch:${id}`);
      say("Unsaved changes. Choose the flow again to discard them.", "error");
      return;
    }
    setConfirm(null);
    open(id);
  };

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"), { href: url, download: `${spec.id}.flow.json` });
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const importJson = async (file) => {
    try {
      const parsed = JSON.parse(await file.text());
      const flow = { ...(parsed.flow || parsed), id: spec.id };
      if (!Array.isArray(flow.nodes) || !Array.isArray(flow.edges)) throw new Error("not a flow file (needs nodes and edges)");
      show(flow, components);
      say(`Imported ${file.name}: save the draft to keep it`);
    } catch (err) {
      say(`Import failed: ${err.message}`, "error");
    }
  };

  const loadVersion = (v) => guard(async () => {
    const { flow } = await flowsApi.version(spec.id, v);
    show(flow, components);
    say(`Loaded v${v} into the editor: save the draft to keep it, or undo`);
  });

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      if (mod && key === "s") {
        e.preventDefault();
        if (dirty && !busy && !viewing) saveDraft();
        return;
      }
      // Text fields keep their own undo, copy and paste.
      const field = e.target.closest?.("input, textarea, select, [contenteditable='true']");
      if (field) return;
      if (e.key === "Escape") {
        if (viewing) setViewing(null);
        setShowKeys(false);
        setFlowPanel(false);
        setSelection(null);
        setNodes((list) => list.map((n) => (n.selected ? { ...n, selected: false } : n)));
        return;
      }
      if (e.key === "?") { setShowKeys((v) => !v); return; }
      if (viewing || !mod) return;
      if (key === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if ((key === "z" && e.shiftKey) || key === "y") { e.preventDefault(); redo(); }
      else if (key === "c") { e.preventDefault(); copy(); }
      else if (key === "v") { e.preventDefault(); paste(); }
      else if (key === "d") { e.preventDefault(); duplicateSelection(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // ---------- view a version read-only ----------
  const viewVersion = (v) => guard(async () => {
    const { flow } = await flowsApi.version(spec.id, v);
    const laid = layoutFlow(flow, components);
    setSelection(null);
    setViewing({ version: v, label: `${spec.id} v${v}`, nodes: fitGroups(laid.nodes), edges: laid.edges });
    setTimeout(frame, 30);
  });
  // A version on view takes only measurements and selection, never edits.
  const onViewChange = (changes) => setViewing((v) => v && ({
    ...v, nodes: applyNodeChanges(changes.filter((c) => c.type === "dimensions" || c.type === "select"), v.nodes),
  }));
  const leaveView = () => {
    setViewing(null);
    setSelection(null);
    setTimeout(frame, 30);
  };
  const toggleSnap = () => setSnap((on) => {
    try { localStorage.setItem(SNAP_KEY, on ? "0" : "1"); } catch { /* per-browser nicety */ }
    return !on;
  });

  // ---------- live run ----------

  const applyStatus = useCallback((updates) => {
    if (!updates.length) return;
    const next = Object.fromEntries(updates.map((u) => [u.id, u.status]));
    setNodes((list) => list.map((n) => (n.id in next ? { ...n, data: { ...n.data, status: next[n.id] } } : n)));
    setEdges((list) => list.map((e) => {
      const running = next[e.target] === "running" && !e.data.resource;
      return e.animated === running ? e : { ...e, animated: running };
    }));
  }, [setNodes, setEdges]);
  const onEvent = useCallback((event) => {
    const targets = eventTargets(event, latest.current.nodes, latest.current.edges);
    applyStatus(targets);
    if (targets.length) {
      const entry = traceEntry(event);
      setTraces((all) => {
        const next = { ...all };
        for (const t of targets) next[t.id] = [...(next[t.id] || []), entry];
        return next;
      });
    }
  }, [applyStatus]);
  const onReset = useCallback(() => {
    setTraces({});
    setNodes((list) => list.map((n) => (n.data.status ? { ...n, data: { ...n.data, status: null } } : n)));
    setEdges((list) => list.map((e) => (e.animated ? { ...e, animated: false } : e)));
  }, [setNodes, setEdges]);

  // ---------- render ----------

  if (loadError && !record) return <div className="fl-issue is-error">Couldn't load the flow: {loadError}</div>;
  if (!record || !meta) return <div className="fl-hint">Loading the flow…</div>;

  const shownNodes = viewing ? viewing.nodes : nodes;
  const shownEdges = viewing ? viewing.edges : edges;
  const selectedNode = selection?.kind === "node" ? shownNodes.find((n) => n.id === selection.id) : null;
  const selectedEdge = selection?.kind === "edge" ? shownEdges.find((e) => e.id === selection.id) : null;
  const panelOpen = flowPanel || !!selectedNode || !!selectedEdge || !!diff;
  const deselect = () => {
    setSelection(null);
    setNodes((list) => list.map((n) => (n.selected ? { ...n, selected: false } : n)));
    setEdges((list) => list.map((e) => (e.selected ? { ...e, selected: false } : e)));
  };
  const closePanel = () => {
    setFlowPanel(false);
    setDiff(null);
    deselect();
  };
  const toggleFlowPanel = () => {
    const showingOther = !!selectedNode || !!selectedEdge || !!diff;
    deselect();
    setDiff(null);
    setFlowPanel((v) => !v || showingOther);
  };
  const canUndo = hist.current.past.length > 0 || (hist.current.stable && hist.current.stable.key !== fullKey);
  const canRedo = hist.current.future.length > 0;
  const status = record.source === "draft" ? `Draft · published v${record.published_version}`
    : record.source === "published" ? `Published v${record.published_version}` : "Built-in flow (version 0)";

  return (
    <div className="fl-app">
      <div className="fl-toolbar">
        <div className="fl-toolbar-left">
          <select className="fl-select" value={spec.id} onChange={(e) => switchFlow(e.target.value)} disabled={busy}>
            {flowList.map((f) => <option key={f.id} value={f.id}>{f.name} ({f.id}){f.has_draft ? " · draft" : ""}{f.live_version !== null && f.live_version !== undefined ? ` · live v${f.live_version}` : ""}</option>)}
            {!flowList.some((f) => f.id === spec.id) && <option value={spec.id}>{spec.name}</option>}
          </select>
          <button type="button" className="fl-btn" onClick={() => { setDuplicateOf(null); setNewFlow(!newFlow); }}>New flow</button>
          <span className="fl-status">{status}{dirty && <strong> · unsaved changes</strong>}</span>
        </div>
        <div className="fl-toolbar-right">
          <button type="button" className="fl-btn fl-icon-btn" disabled={!canUndo || !!viewing} onClick={undo}
            title={`Undo (${MOD}Z)`} aria-label="Undo">↶</button>
          <button type="button" className="fl-btn fl-icon-btn" disabled={!canRedo || !!viewing} onClick={redo}
            title={`Redo (${MOD}⇧Z)`} aria-label="Redo">↷</button>
          <span className="fl-sep" />
          <button type="button" className="fl-btn fl-btn-primary" disabled={!dirty || busy || !!viewing} onClick={saveDraft}
            title={`${MOD}S`}>Save draft</button>
          <button type="button" className="fl-btn" disabled={busy || (!dirty && record.source !== "draft")}
            onClick={() => (confirm === "discard" ? discard() : setConfirm("discard"))}>
            {confirm === "discard" ? "Click again to discard" : record.source === "draft" && !dirty ? "Discard draft" : "Revert"}
          </button>
          <button type="button" className="fl-btn" disabled={busy || errors.length > 0 || (!dirty && record.source !== "draft")}
            title={errors.length ? "Fix the errors first" : "Freeze the draft as the next version"}
            onClick={() => setConfirm(confirm === "publish" ? null : "publish")}>Publish…</button>
          <span className="fl-sep" />
          <button type="button" className="fl-btn" disabled={!!viewing} onClick={() => show(spec, components, { auto: true })}>Auto-layout</button>
          <button type="button" className={`fl-btn ${snap ? "is-on" : ""}`} onClick={toggleSnap} aria-pressed={snap}
            title="Snap nodes to a 20px grid while dragging">Snap</button>
          <button type="button" className="fl-btn" onClick={exportJson}>Export</button>
          <button type="button" className="fl-btn" onClick={() => fileInput.current?.click()}>Import</button>
          <input ref={fileInput} type="file" accept="application/json,.json" hidden
            onChange={(e) => { if (e.target.files[0]) importJson(e.target.files[0]); e.target.value = ""; }} />
          <button type="button" className="fl-btn fl-icon-btn" onClick={() => setShowKeys((v) => !v)}
            title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">?</button>
          <button type="button" className={`fl-btn ${flowPanel ? "is-on" : ""}`} aria-pressed={flowPanel}
            onClick={toggleFlowPanel}
            title="Name, description, versions, history and metrics">Flow details</button>
          <Verdict issues={issues} general={general} />
        </div>
      </div>
      {showKeys && <Shortcuts onClose={() => setShowKeys(false)} />}
      {viewing && (
        <div className="fl-viewing">
          <span>Viewing <strong>{viewing.label}</strong> read-only. Your editor contents are unchanged.</span>
          <button type="button" className="fl-btn" onClick={() => { const v = viewing.version; leaveView(); loadVersion(v); }}>
            Load into editor</button>
          <button type="button" className="fl-btn fl-btn-primary" onClick={leaveView}>Back to editor</button>
        </div>
      )}
      {newFlow && <NewFlow templates={templates} initialFrom={duplicateOf !== null ? `version:${duplicateOf}` : null}
        versionLabel={duplicateOf !== null ? `${spec.id} v${duplicateOf}` : null} onCreate={createFlow}
        onCancel={() => { setNewFlow(false); setDuplicateOf(null); }} />}
      {confirm === "publish" && (
        <form className="fl-newflow" onSubmit={(e) => { e.preventDefault(); publish(); }}>
          <span>Publish <strong>{spec.name}</strong> as v{record.published_version + 1}</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed? (optional)" autoFocus />
          <button type="submit" className="fl-btn fl-btn-primary" disabled={busy}>Publish</button>
          <button type="button" className="fl-btn" onClick={() => setConfirm(null)}>Cancel</button>
          <span className="fl-field-help">Publishing does not change what users get: make a version live from the History list.</span>
        </form>
      )}
      {session && <Playground record={record} flowId={spec.id} dirty={dirty} session={session} onEvent={onEvent}
        onStart={onReset} onDone={refreshMetrics} />}
      {toast && <div className={`fl-toast is-${toast.kind}`}>{toast.text}</div>}
      <Palette components={components} />
      <div className="fl-body">
        <div className="fl-canvas" ref={canvasRef} onDragOver={onDragOver} onDrop={onDrop}>
          <ReactFlow
            nodes={shownNodes}
            edges={shownEdges}
            nodeTypes={nodeTypes}
            onNodesChange={viewing ? onViewChange : onNodesChange}
            onEdgesChange={viewing ? undefined : onEdgesChange}
            onNodesDelete={onNodesDelete}
            onConnect={onConnect}
            onConnectEnd={onConnectEnd}
            isValidConnection={isValidConnection}
            nodesDraggable={!viewing}
            nodesConnectable={!viewing}
            snapToGrid={snap}
            snapGrid={[20, 20]}
            multiSelectionKeyCode={["Meta", "Control"]}
            onNodeClick={(_, node) => setSelection({ kind: "node", id: node.id })}
            onEdgeClick={(_, edge) => setSelection({ kind: "edge", id: edge.id })}
            onPaneClick={() => setSelection(null)}
            onNodeDragStop={() => setNodes((list) => fitGroups(list))}
            deleteKeyCode={viewing ? null : ["Backspace", "Delete"]}
            colorMode={colorMode}
            minZoom={0.15}
            panOnScroll
            zoomOnPinch
            zoomActivationKeyCode={["Meta", "Control"]}
          >
            <Background gap={20} size={1} />
            <MiniMap pannable zoomable nodeStrokeWidth={2} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        {panelOpen && <aside className="fl-details" aria-label="Details">
          <button type="button" className="fl-close" onClick={closePanel} title="Close (Esc)" aria-label="Close">×</button>
          {viewing && !selectedNode && !selectedEdge ? (
            <div className="fl-col">
              <div className="fl-panel-title">{viewing.label}</div>
              <p className="fl-hint">Read-only. Click a node to see its settings in this version.</p>
            </div>
          ) : diff && !selectedNode && !selectedEdge ? (
            <DiffPanel diff={diff} onClose={() => setDiff(null)} onSelect={(c) => {
              const id = canvasIdOf(c);
              if (id) setSelection({ kind: "node", id });
            }} />
          ) : selectedNode ? (
            <NodeDetails node={selectedNode} edges={shownEdges} trace={viewing ? null : traces[selectedNode.id]}
              readOnly={!!viewing} onSpec={(fn) => updateSpec(selectedNode.id, fn)}
              onDelete={() => removeNodes([selectedNode.id])} />
          ) : selectedEdge ? (
            <EdgeDetails edge={selectedEdge} readOnly={!!viewing}
              onDelete={() => { setEdges((l) => l.filter((e) => e.id !== selectedEdge.id)); setSelection(null); }} />
          ) : (
            <FlowDetails meta={meta} setMeta={setMeta} record={record} onLoadVersion={loadVersion} onViewVersion={viewVersion}
              confirm={confirm} onMakeLive={makeLive} onDiff={showDiff} onDuplicate={duplicate} metrics={metrics} />
          )}
        </aside>}
      </div>
    </div>
  );
}

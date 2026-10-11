// Monitoring → Overview: is it up, what is live, how it has been answering.
// Deliberately short; the details are a click away (Agent Builder, Knowledge
// base, Evaluations, Langfuse).

import { api } from "./api.js";
import { card, errorBox, fmtInt, h, loading, section } from "./ui.js";

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const secs = (v) => (v === null || v === undefined ? "—" : `${v.toFixed(1)} s`);

function kpi(label, value, note, href) {
  const body = [h("div", { class: "kpi-label" }, label), h("div", { class: "kpi-value" }, value),
    note ? h("div", { class: "kpi-note" }, note) : null];
  return href ? h("a", { class: "kpi", href, style: "text-decoration:none;color:inherit" }, body) : h("div", { class: "kpi" }, body);
}

// The live flow's last 7 days, from what real users asked ("users" runs).
function usage(metrics) {
  const rows = (metrics?.versions || []).filter((r) => r.source === "users");
  if (!rows.length) return null;
  const runs = rows.reduce((n, r) => n + r.runs, 0);
  const evaluated = rows.reduce((n, r) => n + r.evaluated, 0);
  const passed = rows.reduce((n, r) => n + r.passed, 0);
  const blocked = rows.reduce((n, r) => n + r.input_blocked + r.output_blocked, 0);
  const p50 = rows.reduce((t, r) => t + r.p50_seconds * r.runs, 0) / runs;
  return { runs, passRate: evaluated ? passed / evaluated : null, blocked, p50 };
}

export class MonitoringPage {
  title = "Overview";

  constructor(root, { session } = {}) {
    this.root = root;
    this.session = session;
  }

  async mount() {
    this.root.replaceChildren(loading());
    const can = (p) => Boolean(this.session?.can(p));
    let services;
    try {
      services = await api.status();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the status: ${err.message}`));
      return;
    }
    const [system, agentInfo, overview] = await Promise.all([
      api.knowledgeSystem().catch(() => null),
      api.agent().catch(() => null),
      can("view_knowledge") ? api.overview().catch(() => null) : null,
    ]);
    const live = agentInfo?.live;
    const metrics = live && can("manage_agents") ? await api.flowMetrics(live.flow_id).catch(() => null) : null;
    const used = usage(metrics);

    const ready = system?.state === "READY";
    const kpis = h("div", { class: "kpis" },
      kpi("Knowledge system", ready ? (system.origin === "demo" ? "Demo" : "Ready") : system ? "Being set up" : "—",
        ready ? null : "Questions wait until setup goes live", can("manage_settings") && !ready ? "#/setup" : null),
      kpi("Live flow", live ? live.name : "—", live ? `version ${live.version} · ${live.kind === "agent" ? "one agent" : live.kind === "supervisor" ? "multi-agent" : "custom"}` : null,
        can("manage_agents") ? "#/flows" : null),
      overview ? kpi("Knowledge base", `${fmtInt(overview.rows?.length || 0)} pages`, overview.count != null ? `${fmtInt(overview.count)} chunks` : null,
        "#/knowledge-base") : null,
      used ? kpi("Questions, last 7 days", fmtInt(used.runs), `${pct(used.passRate)} passed the answer check · ${secs(used.p50)} typical`) : null,
      used ? kpi("Blocked, last 7 days", fmtInt(used.blocked), "by the input or output guardrail") : null);

    const down = services.filter((s) => !s.up);
    const list = h("div", {}, services.map((s) => h("div", { class: "service-row" },
      h("span", { class: `dot ${s.up ? "up" : "down"}` }),
      s.url ? h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.name) : h("span", {}, s.name),
      h("span", { class: "faint", style: "margin-left:auto" }, s.up ? "up" : "down"))));

    this.root.replaceChildren(h("div", { class: "stack" },
      kpis,
      section("services", "Services", down.length
        ? `${down.map((s) => s.name).join(", ")} ${down.length === 1 ? "is" : "are"} down. Langfuse and MinIO are optional: they run only with the tracing overlay.`
        : "Everything is up.", card({ children: list })),
    ));
  }
}

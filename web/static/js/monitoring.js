// Monitoring → Overview: is it up, what is live, how it has been answering.
// Deliberately short; the details are a click away (Agent Builder, Knowledge
// base, Evaluations, Langfuse).

import { api } from "./api.js";
import { card, errorBox, fmtInt, h, loading, section } from "./ui.js";
import { modelChoiceCard } from "./model_choice.js";

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

// Monitoring → Models (llm.catalogue, R16).
const USES = { default: "default", fallback: "fallback", guardrails: "guardrails", evaluators: "evaluators", rewriting: "query rewriting", setup: "setup research" };
function models(c) {
  const price = (v) => (v === undefined ? "—" : `$${v}`);
  const rows = c.models.map((m) => h("tr", {},
    h("td", {}, h("code", {}, m.model || "—")),
    h("td", {}, m.provider),
    h("td", {}, m.uses.map((u) => USES[u] || u).join(", ")),
    h("td", { class: "num" }, price(m.input)), h("td", { class: "num" }, price(m.output)),
    h("td", { class: "num" }, m.context ? `${Math.round(m.context / 1000)}k` : "—"),
    h("td", {}, m.error ? h("span", { class: "badge badge-err", title: m.error }, "not set up")
      : m.breaker ? h("span", { class: "badge badge-warn" }, `skipped ${m.breaker.seconds_left} s`)
        : h("span", { class: "badge badge-ok" }, "ready"))));
  return [h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Model", "Provider", "Used for", "In / M", "Out / M", "Context", "State"].map((t) => h("th", {}, t)))),
    h("tbody", {}, rows))),
  h("p", { class: "note", style: "margin-top:8px" }, c.price_note)];
}

// Monitoring → Answer cache (answer_cache.py, R19).
function cacheCard(c, reload) {
  const clear = h("button", { class: "btn btn-sm btn-ghost", type: "button", disabled: !c.entries }, "Clear");
  clear.addEventListener("click", async () => { clear.disabled = true; await api.answerCacheClear().catch(() => null); reload(); });
  return h("div", { class: "toolbar", style: "justify-content:flex-start;gap:18px;flex-wrap:wrap" },
    kpi("Hit rate, last 7 days", c.hit_rate === null ? "—" : pct(c.hit_rate), `${fmtInt(c.hits)} reused of ${fmtInt(c.hits + c.misses)}`),
    kpi("Cached answers", fmtInt(c.entries), c.enabled ? `kept ${c.hours} h · similarity ≥ ${c.similarity}` : "off (ANSWER_CACHE=false)"),
    clear);
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
    const catalogue = can("manage_agents") || can("manage_settings") ? await api.modelCatalogue().catch(() => null) : null;
    const cache = can("manage_settings") ? await api.answerCache().catch(() => null) : null;
    const choice = can("manage_settings") ? await api.modelChoice().catch(() => null) : null;
    const rated = can("view_sessions") || can("manage_agents") || can("manage_settings")
      ? await api.feedbackSummary().catch(() => null) : null;   // R51
    const via = rated ? await api.channelUsage().catch(() => null) : null;   // R50
    const others = via ? Object.entries(via.channels).filter(([k]) => k !== "app") : [];

    const ready = system?.state === "READY";
    const kpis = h("div", { class: "kpis" },
      kpi("Knowledge system", ready ? (system.origin === "demo" ? "Demo" : "Ready") : system ? "Being set up" : "—",
        ready ? null : "Questions wait until setup goes live", can("manage_settings") && !ready ? "#/setup" : null),
      kpi("Live flow", live ? live.name : "—", live ? `version ${live.version} · ${live.kind === "agent" ? "one agent" : live.kind === "supervisor" ? "multi-agent" : "custom"}` : null,
        can("manage_agents") ? "#/flows" : null),
      overview ? kpi("Knowledge base", `${fmtInt(overview.rows?.length || 0)} pages`, overview.count != null ? `${fmtInt(overview.count)} chunks` : null,
        "#/knowledge-base") : null,
      used ? kpi("Questions, last 7 days", fmtInt(used.runs), `${pct(used.passRate)} passed the answer check · ${secs(used.p50)} typical`) : null,
      used ? kpi("Blocked, last 7 days", fmtInt(used.blocked), "by the input or output guardrail") : null,
      rated && rated.up + rated.down ? kpi("Feedback, last 7 days", `👍 ${fmtInt(rated.up)} · 👎 ${fmtInt(rated.down)}`,
        `${pct(rated.up / (rated.up + rated.down))} helpful`, can("run_evaluations") && can("view_sessions") ? "#/evaluations" : null) : null,
      others.length ? kpi("By channel, last 7 days", others.map(([k, n]) => `${{ widget: "Widget", slack: "Slack", teams: "Teams" }[k] || k} ${fmtInt(n)}`).join(" · "),
        `and ${fmtInt(via.channels.app || 0)} on the Ask page`, can("manage_settings") ? "#/integrations" : null) : null);

    const down = services.filter((s) => !s.up);
    const list = h("div", {}, services.map((s) => h("div", { class: "service-row" },
      h("span", { class: `dot ${s.up ? "up" : "down"}` }),
      s.url ? h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.name) : h("span", {}, s.name),
      h("span", { class: "faint", style: "margin-left:auto" }, s.up ? "up" : "down"),
      !s.up && s.fix ? h("div", { class: "note", style: "flex-basis:100%" }, s.fix) : null)));

    this.root.replaceChildren(h("div", { class: "stack" },
      kpis,
      section("services", "Services", down.length
        ? `${down.map((s) => s.name).join(", ")} ${down.length === 1 ? "is" : "are"} down. Langfuse and MinIO are optional: they run only with the tracing overlay.`
        : "Everything is up.", card({ children: list })),
      choice ? section("chat-model", "Chat model", "The model behind the agents, guardrails and answer checks: "
        + "local models on this machine, or a hosted frontier model on your own API key.",
      card({ children: modelChoiceCard(choice, () => this.mount()) })) : null,
      cache ? section("answer-cache", "Answer cache", "Checked answers reused for questions that mean the same, "
        + "on the same live version and knowledge base: no model calls. Cleared when another version goes live.",
      card({ children: cacheCard(cache, () => this.mount()) })) : null,
      catalogue ? section("models", "Models", "Which model does what, what takes over when one fails (LLM_FALLBACK), "
        + "and the list price per million tokens.", card({ children: models(catalogue) })) : null,
    ));
  }
}

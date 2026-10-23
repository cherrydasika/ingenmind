// Monitoring → Operations (R53, app/operations.py): is the system healthy and
// what does it cost — latency, errors, timeouts, guardrail blocks, budget
// breaches, tokens and cost per day, flow, agent, model, user and channel,
// the answer cache and the ingestion queue. From the run records in
// PostgreSQL, not Langfuse. Admins only.

import { api } from "./api.js";
import { chartBox, colours, plot } from "./analytics.js";
import { card, dataTable, errorBox, fmtInt, h, loading, metric, note, section } from "./ui.js";

const DAY = 86400000;
const iso = (d) => d.toISOString().slice(0, 10);
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(v && v < 0.1 ? 1 : 0)}%`);
const secs = (v) => (v === null || v === undefined ? "—" : `${v < 10 ? v.toFixed(1) : Math.round(v)} s`);
const money = (v) => (v === null || v === undefined ? "—" : `$${v < 1 ? v.toFixed(4) : v.toFixed(2)}`);
const tokens = (r) => fmtInt(r.input_tokens + r.output_tokens);
const FILTERS = [["flow", "Flow"], ["model", "Model"], ["channel", "Channel"], ["user", "User"], ["source", "Source"]];
const DAYS = { type: "date", tickformat: "%d %b", dtick: DAY };   // one tick a day, never hours
const SOURCES = { users: "Users", playground: "Playground", eval: "Evaluations" };

// Each group's health row: runs, latency, failures and blocks, tokens, cost.
const healthColumns = (first) => [first, { key: "runs", label: "Runs", num: true }, { key: "p50", label: "p50", num: true },
  { key: "p95", label: "p95", num: true }, { key: "errors", label: "Errors", num: true }, { key: "timeouts", label: "Timeouts", num: true },
  { key: "blocks", label: "Blocked", num: true }, { key: "budget", label: "Over budget", num: true },
  { key: "tokens", label: "Tokens", num: true }, { key: "cost", label: "Cost", num: true }];
const healthRow = (r, name) => ({ name, runs: fmtInt(r.runs), p50: secs(r.p50), p95: secs(r.p95),
  errors: r.failed ? `${fmtInt(r.failed)} (${pct(r.error_rate)})` : "0", timeouts: fmtInt(r.timed_out),
  blocks: fmtInt(r.input_blocked + r.output_blocked), budget: fmtInt(r.budget_hit), tokens: tokens(r), cost: money(r.cost) });

export class OperationsPage {
  title = "Operations";

  constructor(root) {
    this.root = root;
    const today = new Date();
    this.query = { from: iso(new Date(today - 6 * DAY)), to: iso(today) };
    this.observer = new MutationObserver(() => this.data && this.draw());   // the theme changed: redraw
  }

  unmount() {
    this.observer.disconnect();
  }

  async mount() {
    this.observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    await this.load();
  }

  async load() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.operations(this.query);
    } catch (err) {
      this.root.replaceChildren(this.filter(), errorBox(`Couldn't load operations: ${err.message}`));
      return;
    }
    this.render();
  }

  filter() {
    const o = this.data?.options || {};
    const from = h("input", { type: "date", value: this.query.from, "aria-label": "From", style: "width:auto" });
    const to = h("input", { type: "date", value: this.query.to, "aria-label": "To", style: "width:auto" });
    const selects = FILTERS.map(([key, label]) => {
      const values = (o[key] || []).map((v) => (typeof v === "object" ? v : { id: v, name: key === "source" ? SOURCES[v] || v : v }));
      const chosen = this.query[key] || "";
      if (chosen && !values.some((v) => v.id === chosen)) values.push({ id: chosen, name: chosen });
      return h("select", { "aria-label": label, style: "width:auto", "data-filter": key },
        h("option", { value: "" }, `Any ${label.toLowerCase()}`),
        values.map((v) => h("option", { value: v.id, selected: v.id === chosen ? "" : null }, v.name)));
    });
    const apply = () => {
      this.query = { from: from.value, to: to.value };
      for (const s of selects) if (s.value) this.query[s.dataset.filter] = s.value;
      this.load();
    };
    const preset = (label, first, last) => h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
      this.query = { ...this.query, from: iso(first), to: iso(last) };
      this.load();
    } }, label);
    const today = new Date();
    return h("div", { class: "toolbar analytics-filter" },
      h("div", { class: "analytics-dates" }, from, h("span", { class: "faint" }, "to"), to, ...selects,
        h("button", { class: "btn btn-sm", type: "button", onclick: apply }, "Apply")),
      h("div", {}, preset("Yesterday", new Date(today - DAY), new Date(today - DAY)),
        preset("7 days", new Date(today - 6 * DAY), today), preset("30 days", new Date(today - 29 * DAY), today)));
  }

  tiles(s, cache) {
    return h("div", { class: "metrics ops-metrics" },
      metric("Cost (approx.)", money(s.cost)), metric("Runs", fmtInt(s.runs)),
      metric("Time p50 / p95", `${secs(s.p50)} / ${secs(s.p95)}`), metric("Errors", `${fmtInt(s.failed)} · ${pct(s.error_rate)}`),
      metric("Timeouts", `${fmtInt(s.timed_out)} · ${pct(s.timeout_rate)}`),
      metric("Guardrail blocks", `${fmtInt(s.input_blocked)} in · ${fmtInt(s.output_blocked)} out`),
      metric("Budget breaches", fmtInt(s.budget_hit)), metric("Tokens", tokens(s)),
      metric("Cache hit rate", cache.hit_rate === null ? "—" : `${pct(cache.hit_rate)} of ${fmtInt(cache.hits + cache.misses)}`));
  }

  render() {
    const d = this.data;
    const y = d.yesterday;
    this.charts = { cost: chartBox(260), speed: chartBox(240), problems: chartBox(240) };
    const slow = y.slowest;
    const slowest = [slow.flow && `flow ${slow.flow.name} (p95 ${secs(slow.flow.p95)})`,
      slow.agent && `agent ${slow.agent.name} (p95 ${secs(slow.agent.p95)} per run)`,
      slow.model && `model ${slow.model.name} (p95 ${secs(slow.model.p95)} per agent run)`].filter(Boolean);
    const filtered = Object.keys(d.filters).length;
    const priced = d.unpriced.length ? ` No price is known for ${d.unpriced.join(", ")}: their tokens are not in the cost.` : "";
    const agents = d.agents.map((a) => ({ ...a, name: a.role, model: a.models.join(", ") || "—", runs: fmtInt(a.runs),
      p50: secs(a.p50), p95: secs(a.p95), tokens: tokens(a), cost: money(a.cost) }));
    const models = d.models.map((m) => ({ name: m.model, p50: secs(m.p50), p95: secs(m.p95), input: fmtInt(m.input_tokens),
      output: fmtInt(m.output_tokens), cost: m.priced ? money(m.cost) : "no price" }));
    const users = d.users.map((u) => healthRow(u, u.key ? (u.name ? `${u.name}${u.email ? ` (${u.email})` : ""}` : u.key) : "Not recorded (before this version) or not signed in"));
    const ing = d.ingestion;
    const sinceQueued = ing.oldest_queued_seconds === null ? "" : ` · oldest waiting ${Math.round(ing.oldest_queued_seconds / 60)} min`;
    const nameCol = (label) => ({ key: "name", label });
    this.root.replaceChildren(h("div", { class: "stack" },
      this.filter(),
      section("yesterday", `Yesterday (${y.day})`, `What yesterday cost and what was slow${filtered ? ", for the filters chosen" : ""}. Times are a run's whole answer; an agent's and a model's are its model time.`,
        card({ children: y.runs ? [this.tiles(y, y.cache),
          h("p", { class: "note" }, slowest.length ? `Slowest: ${slowest.join(" · ")}.` : "No agent times were recorded yesterday.")]
          : note("No runs yesterday.") })),
      section("period", `${d.from} to ${d.to}`, `All runs: users, the playground and evaluations (filter by source). Cost is approximate: tokens × each model's list price.${priced}`,
        card({ children: d.summary.runs ? [this.tiles(d.summary, d.cache), h("div", { class: "analytics-grid" },
          h("div", {}, h("div", { class: "faint" }, "Cost per day, by model"), this.charts.cost),
          h("div", {}, h("div", { class: "faint" }, "Response time: typical and slowest (95%)"), this.charts.speed),
          h("div", {}, h("div", { class: "faint" }, "Errors, timeouts, blocks and budget breaches"), this.charts.problems))]
          : note("No runs in this period.") })),
      d.summary.runs ? section("breakdown", "Where the time and money go", "Slowest first. Errors include timeouts; blocked counts both guardrails.",
        card({ title: "Per flow", children: dataTable(healthColumns(nameCol("Flow")), d.flows.map((r) => healthRow(r, r.key))) }),
        card({ title: "Per agent", children: agents.length ? dataTable([nameCol("Agent"), { key: "model", label: "Model" },
          { key: "runs", label: "Runs", num: true }, { key: "p50", label: "p50", num: true }, { key: "p95", label: "p95", num: true },
          { key: "tokens", label: "Tokens", num: true }, { key: "cost", label: "Cost", num: true }], agents)
          : note("Per-agent times are recorded from this version on.") }),
        card({ title: "Per model", children: dataTable([nameCol("Model"), { key: "p50", label: "p50", num: true },
          { key: "p95", label: "p95", num: true }, { key: "input", label: "Input tokens", num: true },
          { key: "output", label: "Output tokens", num: true }, { key: "cost", label: "Cost", num: true }], models) }),
        card({ title: "Per channel", children: dataTable(healthColumns(nameCol("Channel")), d.channels.map((r) => healthRow(r, r.key === "unknown" ? "Not recorded (before this version)" : r.key))) }),
        card({ title: "Per source", children: dataTable(healthColumns(nameCol("Source")), d.sources.map((r) => healthRow(r, SOURCES[r.key] || r.key))) }),
        card({ title: "Top users by cost", children: dataTable(healthColumns(nameCol("User")), users) })) : null,
      section("ingestion", "Ingestion", "The worker's queue now, and the jobs that ended in this period.",
        card({ children: [h("div", { class: "metrics ops-metrics" }, metric("Queued now", `${fmtInt(ing.queued)}${sinceQueued}`),
          metric("Running now", fmtInt(ing.running)), metric("Succeeded", fmtInt(ing.succeeded)), metric("Failed", fmtInt(ing.failed))),
          ing.failures.length ? dataTable([{ key: "at", label: "When" }, { key: "kind", label: "Job" }, { key: "error", label: "Error" }],
            ing.failures.map((f) => ({ ...f, at: f.at.slice(0, 16).replace("T", " "), error: f.error || "—" }))) : null] })),
    ));
    this.draw();
  }

  draw() {
    if (!window.Plotly) { setTimeout(() => this.draw(), 200); return; }   // loaded with defer
    const days = this.data.days;
    if (!this.data.summary.runs || !days.length) return;
    const c = colours();
    const x = days.map((r) => r.day);
    const models = [...new Set(days.flatMap((r) => Object.keys(r.cost_by_model)))];
    const tones = [c.primary, c.ok, c.warn, c.faint, c.err];
    plot(this.charts.cost, models.map((m, i) => ({ type: "bar", x, y: days.map((r) => r.cost_by_model[m] || 0), name: m,
      marker: { color: tones[i % tones.length] } })),
    { barmode: "stack", showlegend: models.length > 1, legend: { orientation: "h", y: -0.2 }, margin: { l: 48, r: 16, t: 8, b: 72 }, xaxis: DAYS,
      yaxis: { type: "linear", tickprefix: "$", rangemode: "tozero" } });
    plot(this.charts.speed, [{ type: "scatter", mode: "lines+markers", x, y: days.map((r) => r.p50), line: { color: c.primary }, name: "Typical" },
      { type: "scatter", mode: "lines+markers", x, y: days.map((r) => r.p95), line: { color: c.warn, dash: "dot" }, name: "Slowest 5%" }],
    { xaxis: DAYS, yaxis: { type: "linear", ticksuffix: " s", rangemode: "tozero" }, showlegend: true, legend: { orientation: "h", y: -0.2 }, margin: { l: 48, r: 16, t: 8, b: 72 } });
    const bar = (name, key, color) => ({ type: "bar", x, y: days.map(key), name, marker: { color } });
    plot(this.charts.problems, [bar("Errors", (r) => r.failed - r.timed_out, c.err), bar("Timeouts", (r) => r.timed_out, c.warn),
      bar("Blocked", (r) => r.input_blocked + r.output_blocked, c.faint), bar("Over budget", (r) => r.budget_hit, c.primary)],
    { barmode: "stack", xaxis: DAYS, yaxis: { type: "linear", rangemode: "tozero" }, showlegend: true, legend: { orientation: "h", y: -0.2 }, margin: { l: 48, r: 16, t: 8, b: 72 } });
  }
}

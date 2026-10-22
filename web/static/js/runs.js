// Monitoring → Runs (R30, app/durable.py): every agent run with its status,
// steps, time and tokens. A run a restart cut short is "interrupted": recent
// Ask-page ones resume by themselves; any interrupted or failed run can be
// resumed from its last step, or cancelled.

import { api } from "./api.js";
import { badge, card, errorBox, fmtTime, h, loading, note } from "./ui.js";

const POLL_MS = 4000;
const KIND = { done: "ok", running: "info", interrupted: "warn", failed: "err", cancelled: "neutral" };
const SOURCE = { users: "Ask page", playground: "Playground", eval: "Evaluation" };
const when = (iso) => (iso ? String(iso).slice(0, 19).replace("T", " ") : "—");
const tokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));

export class RunsPage {
  title = "Runs";

  constructor(root) {
    this.root = root;
    this.timer = null;
    this.flash = null;
  }

  unmount() {
    clearTimeout(this.timer);
  }

  async mount() {
    this.root.replaceChildren(loading());
    await this.load();
  }

  async load() {
    clearTimeout(this.timer);
    try {
      this.data = await api.runs();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load runs: ${err.message}`));
      return;
    }
    this.render();
    if (this.data.runs.some((r) => r.status === "running")) this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  async act(fn, id) {
    try {
      await fn(id);
      this.flash = null;
    } catch (err) {
      this.flash = err.message;
    }
    setTimeout(() => this.load(), 600);
  }

  render() {
    const d = this.data;
    const rows = d.runs.map((r) => {
      const canResume = d.can_manage && ["interrupted", "failed"].includes(r.status);
      const canCancel = d.can_manage && ["running", "interrupted", "failed"].includes(r.status);
      return h("tr", {},
        h("td", { class: "wrap" }, h("div", {}, r.asked), r.read_as ? h("div", { class: "faint" }, `Read as: ${r.read_as}`) : null,
          r.error ? h("div", { class: "faint" }, r.error) : null),
        h("td", {}, badge(r.status, KIND[r.status] || "neutral"), r.resumes ? h("div", { class: "faint" }, `resumed ×${r.resumes}`) : null),
        h("td", {}, SOURCE[r.source] || r.source, h("div", { class: "faint" }, `${r.flow_id} v${r.version}`)),
        h("td", { class: "num" }, String(r.steps), r.last_node ? h("div", { class: "faint" }, r.last_node) : null),
        h("td", { class: "num" }, fmtTime(r.seconds || 0)),
        h("td", { class: "num" }, `${tokens(r.input_tokens)} / ${tokens(r.output_tokens)}`),
        h("td", {}, when(r.started_at)),
        h("td", {}, canResume ? h("button", { class: "btn btn-sm", type: "button", onclick: () => this.act(api.resumeRun, r.run_id) }, "Resume") : null,
          canCancel ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.act(api.cancelRun, r.run_id) }, "Cancel") : null));
    });
    this.root.replaceChildren(h("div", { class: "stack" },
      note(`Every agent run, newest first: its status, how many steps it finished, time and tokens (in / out). `
        + (d.durable ? "Each step is checkpointed in PostgreSQL: a run a restart cut short shows as interrupted; "
          + "recent Ask-page ones resume by themselves and their answer joins the conversation. "
          : "Checkpoints are kept in memory on this install (DURABLE_RUNS=false or no database), so runs can't resume. ")
        + `Kept ${d.retention_days} days.`),
      this.flash ? errorBox(this.flash) : null,
      card({ children: d.runs.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Question", "Status", "From", "Steps", "Time", "Tokens", "Started", ""].map((t) =>
          h("th", { class: ["Steps", "Time", "Tokens"].includes(t) ? "num" : null }, t)))),
        h("tbody", {}, rows))) : h("div", { class: "empty-state" }, "No runs yet.") })));
  }
}

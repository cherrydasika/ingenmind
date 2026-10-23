// Evaluations → Nightly runs and the gate (R56, app/flows/eval_gate.py):
// which question sets run every night on the live flow, which sets a version
// must match or beat the live one on before it goes live, and every set's
// pass rate over time. A nightly drop raises the evaluation-regression alert.

import { api } from "./api.js";
import { chartBox, colours, plot } from "./analytics.js";
import { card, errorBox, h, note, section } from "./ui.js";

const TONES = (c) => [c.primary, c.ok, c.warn, c.err, c.faint];

export class EvalSchedule {
  constructor() {
    this.root = h("div");
    this.message = null;
  }

  async load() {
    try {
      this.data = await api.evalSchedule();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the nightly runs: ${err.message}`));
      return;
    }
    this.render();
  }

  async save(body) {
    try {
      await api.evalScheduleSave(body);
      this.message = { kind: "ok", text: "Saved." };
    } catch (err) {
      this.message = { kind: "err", text: err.message };
    }
    await this.load();
  }

  render() {
    const { schedule: s, sets, trends } = this.data;
    const picks = (chosen, name) => h("div", { class: "eval-sched-sets" }, sets.map((set) => h("label", { class: "sched-check" },
      h("input", { type: "checkbox", value: set.id, name, checked: chosen.includes(set.id) ? "" : null }), set.name)));
    const nightly = picks(s.sets, "nightly");
    const gate = picks(s.gate_sets, "gate");
    const enabled = h("input", { type: "checkbox", checked: s.enabled ? "" : null });
    const hour = h("input", { type: "number", min: "0", max: "23", value: s.hour, style: "width:5em", "aria-label": "Hour (UTC)" });
    const tolerance = h("input", { type: "number", min: "0", max: "50", step: "1", value: Math.round(s.tolerance * 100),
      style: "width:5em", "aria-label": "Tolerance in percentage points" });
    const chosen = (box) => [...box.querySelectorAll("input:checked")].map((i) => i.value);
    this.chart = chartBox(260);
    this.root.replaceChildren(section("eval-schedule", "Nightly runs and the promotion gate",
      "Quality changes when content, models or providers change, even with no deploy. Chosen sets run every night on the live flow (a drop raises the evaluation-regression alert), and a version goes live only if it matches or beats the live version on the gate sets, unless an admin gives a reason (kept in the audit log).",
      card({ children: [
        this.message ? h("div", { class: `alert ${this.message.kind}` }, this.message.text) : null,
        h("div", { class: "form-grid" },
          h("div", {}, h("label", { class: "sched-check" }, enabled, "Run every night at"), h("span", { class: "alert-inputs" }, hour, "UTC"),
            h("div", { class: "faint" }, "On the live flow:"), nightly,
            h("div", { class: "faint" }, s.last_run ? `Last nightly run: ${s.last_run}` : "No nightly run yet.")),
          h("div", {}, h("div", { class: "faint" }, "Gate sets (none chosen: every set both versions have run):"), gate,
            h("span", { class: "alert-inputs" }, "Allow a drop of up to", tolerance, "points"))),
        h("div", { class: "alert-actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: () => this.save({
          enabled: enabled.checked, hour: Number(hour.value), sets: chosen(nightly), gate_sets: chosen(gate),
          tolerance: Number(tolerance.value) / 100 }) }, "Save")),
        trends.length ? [h("div", { class: "faint", style: "margin-top:12px" }, "Pass rate per set over time (red team: refused as expected); hollow points are manual runs"), this.chart]
          : note("No finished evaluation runs in the last 90 days.")] })));
    if (trends.length) this.draw();
  }

  draw() {
    if (!window.Plotly) { setTimeout(() => this.draw(), 200); return; }
    const c = colours();
    const tones = TONES(c);
    plot(this.chart, this.data.trends.map((set, i) => ({ type: "scatter", mode: "lines+markers", name: set.set_name,
      x: set.runs.map((r) => r.at), y: set.runs.map((r) => r.rate * 100),
      text: set.runs.map((r) => `${r.flow_id} v${r.version}${r.scheduled ? " · nightly" : ""}`), hovertemplate: "%{y:.0f}% · %{text}<extra></extra>",
      line: { color: tones[i % tones.length] },
      marker: { color: tones[i % tones.length], symbol: set.runs.map((r) => (r.scheduled ? "circle" : "circle-open")), size: 8 } })),
    { xaxis: { type: "date" }, yaxis: { type: "linear", range: [0, 100], ticksuffix: "%" }, showlegend: true,
      legend: { orientation: "h", y: -0.2 }, margin: { l: 48, r: 16, t: 8, b: 72 } });
  }
}

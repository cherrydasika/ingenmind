// Monitoring → Alerts (R55, app/alerts.py): rules that tell admins when
// something needs them — pass rate, cost, errors, response time, ingestion,
// guardrail blocks, stale sources, evaluation regressions — where they go
// (Slack, email, webhooks), mute, a test per rule, and the history. Admins only.

import { api } from "./api.js";
import { badge, card, errorBox, h, loading, note, section } from "./ui.js";

const shown = (unit, v) => (v === null || v === undefined ? "—"
  : unit === "ratio" ? `${(v * 100).toFixed(1)}%` : unit === "usd" ? `$${Number(v).toFixed(2)}`
    : unit === "seconds" ? `${Number(v).toFixed(1)} s` : String(v));
const when = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
const MUTES = [[60, "1 hour"], [480, "8 hours"], [1440, "1 day"], [10080, "1 week"]];

export class AlertsPage {
  title = "Alerts";

  constructor(root) {
    this.root = root;
    this.message = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    await this.load();
  }

  async load() {
    try {
      this.data = await api.alerts();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load alerts: ${err.message}`));
      return;
    }
    this.render();
  }

  async act(work, done) {
    try {
      const out = await work();
      this.message = { kind: "ok", text: done(out) };
    } catch (err) {
      this.message = { kind: "err", text: err.message };
    }
    await this.load();
  }

  ruleRow(r) {
    const editable = r.default_threshold !== null;
    const threshold = editable ? h("input", { type: "number", min: "0", step: r.unit === "ratio" ? "0.01" : "any",
      max: r.unit === "ratio" ? "1" : null, value: r.threshold, "aria-label": `${r.label} threshold`, style: "width:7em" }) : null;
    const windowBox = r.default_window !== null ? h("input", { type: "number", min: "5", max: "10080", step: "5", value: r.window,
      "aria-label": `${r.label} window in minutes`, style: "width:6em" }) : null;
    const save = () => this.act(() => api.alertRule(r.kind, {
      ...(threshold ? { threshold: Number(threshold.value) } : {}), ...(windowBox ? { window: Number(windowBox.value) } : {}) }),
    () => `${r.label}: saved.`);
    const mute = h("select", { "aria-label": `Mute ${r.label}`, style: "width:auto", onchange: (e) => {
      const minutes = Number(e.target.value);
      this.act(() => api.alertRule(r.kind, { mute_minutes: minutes }), () => (minutes ? `${r.label}: muted.` : `${r.label}: unmuted.`));
    } }, h("option", { value: "" }, r.muted_until ? `Muted to ${when(r.muted_until)}` : "Mute…"),
    r.muted_until ? h("option", { value: "0" }, "Unmute") : null, MUTES.map(([m, label]) => h("option", { value: m }, `for ${label}`)));
    const state = !r.enabled ? badge("off") : r.state === "firing" ? badge("firing", "err") : badge("ok", "ok");
    return h("tr", {},
      h("td", {}, h("div", {}, h("strong", {}, r.label)), h("div", { class: "faint" }, r.what)),
      h("td", {}, state),
      h("td", { class: "num" }, r.breach === "event" ? "on event" : shown(r.unit, r.value)),
      h("td", {}, editable ? h("span", { class: "alert-inputs" }, r.breach, threshold,
        windowBox ? h("span", {}, "over", windowBox, "min") : r.kind === "daily_cost" ? "today" : null) : "—"),
      h("td", {}, h("div", { class: "alert-actions" },
        editable ? h("button", { class: "btn btn-sm", type: "button", onclick: save }, "Save") : null,
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.act(() => api.alertRule(r.kind, { enabled: !r.enabled }),
          () => `${r.label}: ${r.enabled ? "off" : "on"}.`) }, r.enabled ? "Turn off" : "Turn on"),
        mute,
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.act(() => api.alertTest(r.kind),
          (out) => `Test sent: ${Object.entries(out.sent).map(([k, v]) => `${k} ${v}`).join(", ") || "no channel is set up"}.`) }, "Send test"))));
  }

  channels() {
    const s = this.data.settings;
    const slack = h("input", { type: "text", inputmode: "url", placeholder: s.slack ? "Set (enter a new URL to replace it)" : "https://hooks.slack.com/services/…",
      "aria-label": "Slack incoming-webhook URL", autocomplete: "off" });
    const emails = h("input", { type: "text", value: s.emails.join(", "), placeholder: "ops@example.com, …", "aria-label": "Email addresses" });
    const save = () => this.act(() => api.alertSettings({ ...(slack.value.trim() ? { slack_webhook_url: slack.value.trim() } : {}),
      emails: emails.value.split(/[,\s]+/).filter(Boolean) }), () => "Channels saved.");
    return card({ title: "Where alerts go", children: [
      h("div", { class: "form-grid" },
        h("label", {}, h("span", {}, "Slack incoming webhook ", s.slack ? badge("set", "ok") : badge("not set")), slack),
        h("label", {}, h("span", {}, "Email to ", s.email_ready ? null : badge("SMTP not set up", "warn")), emails)),
      h("p", { class: "note" }, `Email is sent from ${s.email_from} through the server's SMTP settings (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM). Webhooks subscribed to ${s.webhook_event} on Admin → Integrations get every alert too.`),
      h("div", { class: "alert-actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: save }, "Save channels"),
        s.slack ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.act(() => api.alertSettings({ clear_slack: true }),
          () => "Slack removed.") }, "Remove Slack") : null)] });
  }

  render() {
    const d = this.data;
    const sent = (e) => (e.muted ? "muted: not sent" : Object.entries(e.sent).map(([k, v]) => `${k}: ${v}`).join(" · ") || "no channel set up");
    this.root.replaceChildren(h("div", { class: "stack" },
      this.message ? h("div", { class: `alert ${this.message.kind}` }, this.message.text) : null,
      section("alert-rules", "Rules", `Checked every ${Math.round(d.check_every / 60)} minutes over users' runs. A rule alerts once when it is breached and again when it clears; muted rules still show here but send nothing.`,
        card({ children: [h("div", { class: "table-wrap" }, h("table", { class: "alert-table" },
          h("thead", {}, h("tr", {}, ["Rule", "State", "Now", "Alert when", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.rules.map((r) => this.ruleRow(r))))),
        h("div", { class: "alert-actions" }, h("button", { class: "btn btn-ghost btn-sm", type: "button",
          onclick: () => this.act(() => api.alertsCheck(), (out) => `Checked: ${out.events.length} alert(s) raised.`) }, "Check now"))] })),
      section("alert-channels", "Channels", null, this.channels()),
      section("alert-history", "History", "The latest alerts and what each channel did with them.",
        card({ children: d.history.length ? h("div", { class: "table-wrap" }, h("table", { class: "alert-history" },
          h("thead", {}, h("tr", {}, ["When", "Rule", "", "Message", "Sent"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.history.map((e) => h("tr", {}, h("td", { class: "faint" }, when(e.at)), h("td", {}, e.label),
            h("td", {}, badge(e.status, e.status === "fired" ? "err" : e.status === "resolved" ? "ok" : "neutral")),
            h("td", {}, e.message), h("td", { class: "faint" }, sent(e)))))))
          : note("No alerts yet.") })),
    ));
  }
}

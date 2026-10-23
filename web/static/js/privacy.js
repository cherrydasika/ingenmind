// Profile → Privacy and your data (R59, app/data_lifecycle.py): what is kept
// about you, where and for how long; download all of it as JSON, or delete
// your account and data everywhere. Admins also set how long each kind of
// data is kept (applied once a day).

import { api } from "./api.js";
import { card, errorBox, h, loading, note } from "./ui.js";

const kept = (days) => (days ? `${days} day${days === 1 ? "" : "s"}` : "Until deleted");

export class PrivacyBox {
  constructor(root, admin) {
    this.root = root;
    this.admin = admin;
    this.message = null;
  }

  async load() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.privacy();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the privacy details: ${err.message}`));
      return;
    }
    this.render();
  }

  render() {
    const d = this.data;
    this.root.replaceChildren(...[
      this.message ? h("div", { class: `alert ${this.message.kind}` }, this.message.text) : null,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["What", "Where", "Details"].map((t) => h("th", {}, t)))),
        h("tbody", {}, d.where.map((w) => h("tr", {}, h("td", {}, w.what), h("td", {}, w.where), h("td", { class: "faint" }, w.detail)))))),
      h("p", { class: "note" }, "How long it is kept: ", d.kinds.map((k, i) => [i ? "; " : "", h("strong", {}, k.kind), ` ${kept(k.days).toLowerCase()}`]),
        ". Personal details in questions are masked before the agents or Langfuse see them."),
      h("div", { class: "alert-actions" },
        h("a", { class: "btn btn-sm", href: "/api/me/data", download: "" }, "Download my data (JSON)"),
        h("button", { class: "btn btn-sm btn-danger", type: "button", onclick: () => this.deleteMine() }, "Delete my account and data")),
      this.admin ? this.retention() : null].filter(Boolean));
  }

  async deleteMine() {
    const typed = window.prompt("This deletes your account, your sessions, feedback, memory and traces everywhere, and signs you out. It can't be undone.\n\nType DELETE to confirm.");
    if (typed === null) return;
    try {
      await api.deleteMyData(typed.trim());
      window.location.href = "/";
    } catch (err) {
      this.message = { kind: "err", text: err.message };
      this.render();
    }
  }

  retention() {
    const inputs = Object.fromEntries(this.data.kinds.map((k) => [k.kind, h("input", { type: "number", min: "1", step: "1",
      value: k.days ?? "", placeholder: "keep", style: "width:7em", "aria-label": `Days to keep ${k.kind}` })]));
    const last = this.data.last_applied;
    const save = async () => {
      try {
        const body = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value === "" ? null : Number(el.value)]));
        this.data = { ...this.data, ...(await api.retentionSave(body)) };
        this.message = { kind: "ok", text: "Saved. Retention is applied once a day, or now with Apply now." };
      } catch (err) {
        this.message = { kind: "err", text: err.message };
      }
      await this.load();
    };
    const applyNow = async () => {
      try {
        const run = await api.retentionApply();
        const total = Object.values(run.deleted).reduce((n, kind) => n + Object.values(kind).reduce((a, b) => a + b, 0), 0);
        this.message = { kind: run.errors.length ? "err" : "ok", text: `Applied: ${total} item(s) deleted.${run.errors.length ? ` ${run.errors.join("; ")}` : ""}` };
      } catch (err) {
        this.message = { kind: "err", text: err.message };
      }
      await this.load();
    };
    return card({ title: "Data retention (admins)", children: [
      h("p", { class: "note" }, "How long each kind of data is kept; empty keeps it until it is deleted. Older data is deleted once a day."),
      h("div", { class: "retention-grid" }, this.data.kinds.map((k) => h("label", {},
        h("span", {}, h("strong", {}, k.kind), h("span", { class: "faint" }, ` · ${k.what}`)),
        h("span", { class: "alert-inputs" }, inputs[k.kind], "days")))),
      h("div", { class: "alert-actions" },
        h("button", { class: "btn btn-sm", type: "button", onclick: save }, "Save"),
        h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: applyNow }, "Apply now")),
      last ? note(`Last applied ${last.at.slice(0, 16).replace("T", " ")}${last.errors.length ? ` (with errors: ${last.errors.join("; ")})` : ""}.`) : null] });
  }
}

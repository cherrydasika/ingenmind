// API keys (R46, app/api_keys.py): for other systems to call the versioned API
// (/api/v1/, docs at /api/v1/docs) as you, within the scopes you give the key.
// A key is shown once, when it is made; revoking it takes effect at once.
// KeysBox: your own keys (Profile). AllKeysBox: everyone's (Users, manage_users).

import { api } from "./api.js";
import { badge, errorBox, h, loading } from "./ui.js";

const SCOPE_TEXT = { ask: "ask questions", ingest: "add documents", evaluate: "run evaluations",
  admin: "everything else your permissions allow" };
const KIND = { active: "ok", revoked: "neutral", expired: "warn" };
const day = (iso) => (iso ? new Date(iso).toLocaleDateString() : "—");

function keyTable(keys, onRevoke, owner = false) {
  if (!keys.length) return h("p", { class: "note" }, "No keys yet.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, [owner ? "Owner" : null, "Name", "Key", "Scopes", "Status", "Expires", "Last used", ""]
      .filter((t) => t !== null).map((t) => h("th", {}, t)))),
    h("tbody", {}, keys.map((k) => h("tr", {},
      owner ? h("td", {}, k.owner) : null,
      h("td", {}, k.name), h("td", {}, h("code", {}, `${k.prefix}…`)), h("td", {}, k.scopes.join(", ")),
      h("td", {}, badge(k.status, KIND[k.status] || "neutral")), h("td", {}, day(k.expires_at)),
      h("td", {}, day(k.last_used_at)),
      h("td", {}, k.status === "active" ? h("button", { class: "btn btn-ghost btn-sm", type: "button",
        onclick: () => onRevoke(k.key_id) }, "Revoke") : null))))));
}

export class KeysBox {
  constructor(box) {
    this.box = box;
    this.made = null;     // the key just made, shown this once
    this.flash = null;
  }

  async load() {
    this.box.replaceChildren(loading());
    try {
      this.data = await api.myKeys();
    } catch (err) {
      this.box.replaceChildren(errorBox(`Couldn't load your API keys: ${err.message}`));
      return;
    }
    this.render();
  }

  async act(call) {
    try {
      const out = await call();
      this.made = out?.key || null;
      this.flash = null;
    } catch (err) {
      this.flash = err.message;
    }
    await this.load();
  }

  render() {
    const { keys, scopes, max_days: maxDays } = this.data;
    const name = h("input", { type: "text", placeholder: "What uses it, e.g. Help desk bot" });
    const days = h("input", { type: "number", min: 1, max: maxDays, value: 90, style: "width: 90px" });
    const boxes = scopes.map((s) => h("label", { class: "check-row" },
      h("input", { type: "checkbox", value: s, checked: s === "ask" }), `${s} — ${SCOPE_TEXT[s] || s}`));
    const make = () => this.act(() => api.createKey({ name: name.value,
      scopes: boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value),
      days: Number(days.value) }));
    this.box.replaceChildren(...[   // replaceChildren would show a null as text
      h("p", { class: "note" }, "For other systems to call the API as you (",
        h("a", { href: "/api/v1/docs", target: "_blank", rel: "noopener" }, "API docs"),
        "), within the scopes you give the key. Rate limits and the audit log apply as for you."),
      this.made ? h("div", { class: "alert ok", role: "status" }, "Your new key, shown only now: copy it somewhere safe. ",
        h("code", { style: "word-break: break-all" }, this.made)) : null,
      this.flash ? errorBox(this.flash) : null,
      keyTable(keys, (id) => this.act(() => api.revokeMyKey(id))),
      scopes.length ? h("div", { class: "stack", style: "margin-top: 12px" },
        h("label", {}, h("div", { class: "faint" }, "Name"), name),
        h("div", {}, h("div", { class: "faint" }, "Scopes"), ...boxes),
        h("label", {}, h("div", { class: "faint" }, `Lasts (days, up to ${maxDays})`), days),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: make }, "Make a key")))
        : h("p", { class: "note" }, "Your permissions don't cover any API scope.")].filter(Boolean));
  }
}

export class AllKeysBox {
  constructor(box) {
    this.box = box;
  }

  async load() {
    try {
      const { keys } = await api.allKeys();
      this.box.replaceChildren(keyTable(keys, async (id) => { await api.revokeKey(id); this.load(); }, true));
    } catch (err) {
      this.box.replaceChildren(errorBox(`Couldn't load API keys: ${err.message}`));
    }
  }
}

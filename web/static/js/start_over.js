// Start over, for example to move from trains to flights: empty what setup
// built (purpose, blueprint, sources, knowledge base, evaluations) so the setup
// agent asks again what the knowledge system is for. Admins may also reset
// everything (flows and sessions too). Before either, the person signs in
// again (/auth/confirm): their identity provider asks for the password, and
// the server allows one reset in the next few minutes (web_api.setup_start_over).

import { api } from "./api.js";
import { card, fmtInt, h } from "./ui.js";

const PENDING_KEY = "ingenmind.start-over";   // the choice, kept across the sign-in
const PENDING_MS = 10 * 60 * 1000;

function readPending() {
  try {
    const p = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
    return p && Date.now() - p.at < PENDING_MS ? p : null;
  } catch { return null; }
}
function writePending(value) {
  try {
    if (value) sessionStorage.setItem(PENDING_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(PENDING_KEY);
  } catch { /* private window: the choice is asked again */ }
}

export class StartOver {
  constructor({ user, onDone }) {
    this.user = user;
    this.onDone = onDone;   // after a reset: reload setup
    this.info = null;       // what a reset deletes and keeps (/api/knowledge-system)
    this.dialog = null;
  }

  async load() {
    this.info = await api.knowledgeSystem().catch(() => null);
    // Back from signing in again: finish what was started.
    const pending = readPending();
    if (pending) await this.resume(pending);
  }

  card() {
    const open = h("button", { class: "btn btn-danger", type: "button" }, "Start over…");
    open.addEventListener("click", () => this.choose());
    return card({
      title: "Start over with a new domain",
      sub: "Moving to another domain, for example from trains to flights? Starting over empties what setup built "
        + "(the purpose, the blueprint, the sources, the knowledge base and its evaluations), and the setup agent "
        + "asks again what the knowledge system is for. You confirm by signing in again.",
      children: h("div", { class: "toolbar", style: "justify-content:flex-start" }, open),
    });
  }

  // ---------- the dialog ----------

  show(...children) {
    this.close();
    this.dialog = h("div", { class: "modal-backdrop" },
      h("div", { class: "modal modal-wide", role: "dialog", "aria-modal": "true", "aria-labelledby": "start-over-title" },
        ...children));
    this.dialog.addEventListener("click", (e) => { if (e.target === this.dialog) this.cancel(); });
    this.dialog.addEventListener("keydown", (e) => { if (e.key === "Escape") this.cancel(); });
    document.body.append(this.dialog);
    this.dialog.querySelector("button.btn-primary, button.btn-danger")?.focus();
  }

  close() {
    this.dialog?.remove();
    this.dialog = null;
  }

  cancel() {
    writePending(null);
    this.close();
  }

  lists(everything) {
    const i = this.info;
    if (!i?.emptied) return null;
    const rows = i.emptied.filter((e) => e.rows);
    const deleted = rows.map((e) => h("li", {}, `${e.label}: `, h("strong", {}, fmtInt(e.rows))));
    if (everything) deleted.push(...(i.master?.emptied || []).map((label) => h("li", {}, label)));
    const kept = everything ? i.master?.kept || [] : i.kept || [];
    const total = rows.reduce((n, e) => n + e.rows, 0);
    return h("details", { style: "margin:0 0 14px" },
      h("summary", {}, `What is deleted (${fmtInt(total)} rows${everything ? ", plus flows and sessions" : ""}) and kept`),
      h("div", { class: "grid-auto details-body", style: "align-items:start" },
        h("div", {}, h("strong", {}, "Deleted"), deleted.length ? h("ul", {}, deleted) : h("p", {}, "Nothing yet.")),
        h("div", {}, h("strong", {}, "Kept"), h("ul", {}, kept.map((k) => h("li", {}, k))))));
  }

  // Step 1: what to reset, then confirm it's you.
  choose(everything = false) {
    const admin = this.user?.role === "admin";
    const option = (value, title, help) => {
      const input = h("input", { type: "radio", name: "start-over", value: String(value) });
      input.checked = value === everything;
      input.addEventListener("change", () => this.choose(value));
      return h("label", { class: `choice${value === everything ? " is-chosen" : ""}` }, input,
        h("span", {}, h("strong", {}, title), h("span", { class: "note" }, help)));
    };
    const go = h("button", { class: "btn btn-primary", type: "button" }, "Continue: sign in again");
    go.addEventListener("click", () => this.confirm(everything));
    const cancel = h("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
    cancel.addEventListener("click", () => this.cancel());
    this.show(
      h("h2", { id: "start-over-title" }, "Start over"),
      h("div", { class: "choices" },
        option(false, "Change the domain", "Empties what setup built; flows and people's sessions stay. "
          + "People's questions wait until the new domain goes live."),
        admin ? option(true, "Everything (admins)", "Back to a fresh install: also every flow and its versions, "
          + "the live flow and everyone's sessions.") : null),
      this.lists(everything),
      h("p", {}, "This cannot be undone. To confirm, sign in again: your identity provider asks for your password."),
      h("div", { class: "modal-actions" }, cancel, go));
  }

  async confirm(everything) {
    const me = await api.me().catch(() => null);
    if (me?.confirmed) return this.ready(everything);
    writePending({ everything, at: Date.now() });
    location.href = `/auth/confirm?next=${encodeURIComponent("/#/setup")}`;
  }

  async resume(pending) {
    const me = await api.me().catch(() => null);
    if (me?.confirmed) return this.ready(pending.everything);
    if (me?.confirm_error) {
      const again = h("button", { class: "btn btn-primary", type: "button" }, "Try again");
      again.addEventListener("click", () => this.confirm(pending.everything));
      const cancel = h("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
      cancel.addEventListener("click", () => this.cancel());
      return this.show(h("h2", { id: "start-over-title" }, "Not confirmed"),
        h("p", {}, me.confirm_error), h("div", { class: "modal-actions" }, cancel, again));
    }
    writePending(null);   // they came back without signing in: nothing to finish
  }

  // Step 2: signed in again; the reset itself.
  ready(everything, error = null) {
    writePending({ everything, at: Date.now() });
    const reset = h("button", { class: "btn btn-danger", type: "button" }, everything ? "Reset everything" : "Start over now");
    const cancel = h("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
    cancel.addEventListener("click", () => this.cancel());
    reset.addEventListener("click", async () => {
      reset.disabled = true;
      cancel.disabled = true;
      try {
        const { deleted } = await api.startOver(everything);
        writePending(null);
        this.close();
        const total = Object.values(deleted).reduce((a, b) => a + b, 0);
        this.onDone(`Started over: ${fmtInt(total)} rows deleted. Tell the setup agent about your new domain.`);
      } catch (err) {
        if (err.data?.confirm) { writePending(null); return this.choose(everything); }   // the confirmation ran out
        this.ready(everything, err.message);
      }
    });
    this.show(
      h("h2", { id: "start-over-title" }, everything ? "Reset everything?" : "Start over with a new domain?"),
      h("p", {}, `Confirmed: you are ${this.user?.first_name || "signed in"}. `
        + (everything ? "Everything below is deleted, flows and sessions too." : "What setup built is deleted.")),
      this.lists(everything),
      error ? h("div", { class: "alert err", style: "margin-bottom:12px" }, error) : null,
      h("div", { class: "modal-actions" }, cancel, reset));
  }
}

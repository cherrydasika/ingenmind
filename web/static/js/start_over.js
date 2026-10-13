// Start over, for example to move from trains to flights. The current domain
// is set aside, not deleted (knowledge_system.set_aside): the setup agent asks
// again what the knowledge system is for, and until the new domain goes live
// the person can go back to the old one. Going live is when it is deleted, and
// a pop-up says so first. Admins may also set aside everything (flows and
// sessions too). Starting over needs the person to sign in again
// (/auth/confirm): their identity provider asks for the password, and the
// server allows one start in the next few minutes (web_api.setup_start_over).

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

const rowsText = (n) => `${fmtInt(n)} row${n === 1 ? "" : "s"}`;

export class StartOver {
  /** user: who is signed in; view(): Setup's current view; onChange(text): after
   *  starting over or going back, Setup reloads and shows the text. */
  constructor({ user, view, onChange }) {
    this.user = user;
    this.view = view;
    this.onChange = onChange;
    this.info = null;       // what starting over sets aside and keeps (/api/knowledge-system)
    this.dialog = null;
  }

  async load() {
    this.info = await api.knowledgeSystem().catch(() => null);
    // Back from signing in again: finish what was started.
    const pending = readPending();
    if (pending) await this.resume(pending);
  }

  current() {
    return this.view()?.blueprint?.blueprint?.name || "the current domain";
  }

  card() {
    const aside = this.view()?.aside;
    const open = h("button", { class: "btn btn-danger", type: "button" }, aside ? "Start this setup again…" : "Start over…");
    open.addEventListener("click", () => this.choose());
    return card({
      title: aside ? "Start this setup again" : "Start over with a new domain",
      sub: aside
        ? `Discards this setup so far and starts it again from its first question. ${aside.name} stays set aside.`
        : "Moving to another domain, for example from trains to flights? Starting over sets the current domain aside "
          + "and the setup agent asks again what the knowledge system is for. Nothing is deleted until the new domain "
          + "goes live, and you can go back at any point before then. You confirm by signing in again.",
      children: h("div", { class: "toolbar", style: "justify-content:flex-start" }, open),
    });
  }

  // While a domain is set aside: say so on Setup, with the way back. From the
  // evaluation on (the step before going live), it warns what going live does.
  banner() {
    const aside = this.view()?.aside;
    if (!aside) return null;
    const back = h("button", { class: "btn btn-sm", type: "button" }, `Back to ${aside.name}`);
    back.addEventListener("click", () => this.confirmBack());
    const late = ["EVALUATING"].includes(this.view()?.state);
    return h("div", { class: `alert ${late ? "warn" : "info"} aside-banner` },
      h("div", {}, h("strong", {}, `${aside.name} is set aside`),
        h("div", {}, late
          ? `Going live with this domain deletes ${aside.name} (${rowsText(aside.total)}). Until then you can go back to it.`
          : `Its ${rowsText(aside.total)} are kept until this domain goes live. Questions wait until then.`)),
      back);
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
    this.dialog.querySelector(".modal-actions .btn-ghost")?.focus();
  }

  close() {
    this.dialog?.remove();
    this.dialog = null;
  }

  cancel() {
    writePending(null);
    this.close();
  }

  button(label, cls, onClick) {
    const b = h("button", { class: `btn ${cls}`, type: "button" }, label);
    b.addEventListener("click", () => onClick(b));
    return b;
  }

  lists(everything) {
    const i = this.info;
    if (!i?.emptied) return null;
    const rows = i.emptied.filter((e) => e.rows);
    const kept = rows.map((e) => h("li", {}, `${e.label}: `, h("strong", {}, fmtInt(e.rows))));
    if (everything) kept.push(...(i.master?.emptied || []).map((label) => h("li", {}, label)));
    const stays = everything ? i.master?.kept || [] : i.kept || [];
    const total = rows.reduce((n, e) => n + e.rows, 0);
    return h("details", { style: "margin:0 0 14px" },
      h("summary", {}, `What is set aside (${rowsText(total)}${everything ? ", plus flows and sessions" : ""})`),
      h("div", { class: "grid-auto details-body", style: "align-items:start" },
        h("div", {}, h("strong", {}, "Set aside"), kept.length ? h("ul", {}, kept) : h("p", {}, "Nothing yet.")),
        h("div", {}, h("strong", {}, "Stays as it is"), h("ul", {}, stays.map((k) => h("li", {}, k))))));
  }

  // Step 1: what to set aside, then confirm it's you.
  choose(everything = false) {
    const aside = this.view()?.aside;
    const admin = this.user?.role === "admin" && (!aside || aside.everything);
    const option = (value, title, help) => {
      const input = h("input", { type: "radio", name: "start-over", value: String(value) });
      input.checked = value === everything;
      input.addEventListener("change", () => this.choose(value));
      return h("label", { class: `choice${value === everything ? " is-chosen" : ""}` }, input,
        h("span", {}, h("strong", {}, title), h("span", { class: "note" }, help)));
    };
    this.show(
      h("h2", { id: "start-over-title" }, aside ? "Start this setup again" : "Start over"),
      aside
        ? h("p", {}, `This setup so far is discarded and starts again from its first question. ${aside.name} stays set aside.`)
        : h("div", { class: "choices" },
          option(false, "Change the domain", `${this.current()} is set aside: nothing is deleted until the new domain `
            + "goes live, and you can go back to it before then. Flows and people's sessions stay. Questions wait "
            + "until the new domain goes live."),
          admin ? option(true, "Everything (admins)", "Like a fresh install: every flow, the live flow and everyone's "
            + "sessions are set aside too.") : null),
      aside ? null : this.lists(everything),
      h("p", {}, "To confirm, sign in again: your identity provider asks for your password."),
      h("div", { class: "modal-actions" }, this.button("Cancel", "btn-ghost", () => this.cancel()),
        this.button("Continue: sign in again", "btn-primary", () => this.confirm(everything))));
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
      return this.show(h("h2", { id: "start-over-title" }, "Not confirmed"), h("p", {}, me.confirm_error),
        h("div", { class: "modal-actions" }, this.button("Cancel", "btn-ghost", () => this.cancel()),
          this.button("Try again", "btn-primary", () => this.confirm(pending.everything))));
    }
    writePending(null);   // they came back without signing in: nothing to finish
  }

  // Step 2: signed in again; start over.
  ready(everything, error = null) {
    writePending({ everything, at: Date.now() });
    const aside = this.view()?.aside;
    const name = aside ? aside.name : this.current();
    const go = this.button(aside ? "Start again now" : "Start over now", "btn-danger", async (b) => {
      b.disabled = true;
      try {
        await api.startOver(everything);
        writePending(null);
        this.close();
        await this.onChange("Started over. Tell the setup agent about your new domain.");
      } catch (err) {
        if (err.data?.confirm) { writePending(null); return this.choose(everything); }   // the confirmation ran out
        this.ready(everything, err.message);
      }
    });
    this.show(
      h("h2", { id: "start-over-title" }, aside ? "Start this setup again?" : "Start over with a new domain?"),
      h("p", {}, `Confirmed: you are ${this.user?.first_name || "signed in"}. `
        + (aside ? `This setup so far is discarded; ${name} stays set aside.`
          : `${name} is set aside${everything ? " with every flow and session" : ""}, not deleted. You can go back to it `
            + "until the new domain goes live.")),
      aside ? null : this.lists(everything),
      error ? h("div", { class: "alert err", style: "margin-bottom:12px" }, error) : null,
      h("div", { class: "modal-actions" }, this.button("Cancel", "btn-ghost", () => this.cancel()), go));
  }

  // ---------- going back, and going live ----------

  async back(button) {
    const aside = this.view()?.aside;
    button.disabled = true;
    try {
      await api.setupRestore();
      this.close();
      await this.onChange(`Back to ${aside.name}: it answers questions again, and the new setup was discarded.`);
    } catch (err) {
      button.disabled = false;
      this.dialog?.querySelector(".modal")?.append(h("div", { class: "alert err", style: "margin-top:12px" }, err.message));
    }
  }

  confirmBack() {
    const aside = this.view()?.aside;
    this.show(h("h2", { id: "start-over-title" }, `Go back to ${aside.name}?`),
      h("p", {}, `${aside.name} comes back as it was and answers questions again. This new domain's setup so far is `
        + "discarded."),
      h("div", { class: "modal-actions" }, this.button("Cancel", "btn-ghost", () => this.close()),
        this.button(`Back to ${aside.name}`, "btn-primary", (b) => this.back(b))));
  }

  /** Before going live while a domain is set aside: going live deletes it. */
  confirmReplace(onGo) {
    const aside = this.view()?.aside;
    const name = this.current();
    this.show(h("h2", { id: "start-over-title" }, `Going live deletes ${aside.name}`),
      h("p", {}, `${aside.name} (${rowsText(aside.total)}) has been set aside since you started over. Going live `
        + `replaces it with ${name} and deletes it for good.`),
      h("p", {}, "Changed your mind? You can still go back to it."),
      h("div", { class: "modal-actions" }, this.button("Cancel", "btn-ghost", () => this.close()),
        this.button(`Back to ${aside.name}`, "btn-ghost", (b) => this.back(b)),
        this.button(`Go live, delete ${aside.name}`, "btn-danger", () => { this.close(); onGo(); })));
  }
}

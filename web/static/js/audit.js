// Admin → Audit log (app/audit.py): every change an admin-level route made,
// sign-ins and sign-outs, and refused attempts; newest first. Filter by
// person, action, dates or text, and download what's shown as CSV. The log
// is append-only: nobody can edit or delete an entry.

import { api } from "./api.js";
import { badge, card, errorBox, fmtTimestamp, h, loading, note } from "./ui.js";

// Handler names → words (anything missing shows its own name).
const ACTIONS = {
  sign_in: "Signed in", sign_out: "Signed out", sign_in_refused: "Sign-in refused", signed_out_idle: "Signed out (idle)",
  identity_confirmed: "Confirmed it's them", identity_not_confirmed: "Confirmation failed",
  user_create: "Added a user", user_update: "Changed a user",
  flow_create: "Created a flow", flow_save_draft: "Saved a flow draft", flow_discard_draft: "Discarded a flow draft",
  flow_publish: "Published a flow version", flow_set_live: "Made a flow version live", flow_run: "Ran a flow (playground)",
  eval_set_save: "Saved an evaluation set", golden_start: "Wrote a golden set", eval_start: "Started a flow evaluation",
  setup_start_over: "Started over", setup_restore: "Went back to the set-aside domain", setup_go_live: "Went live",
  setup_plan_approve: "Approved the build plan", setup_blueprint_confirm: "Confirmed the blueprint",
  setup_build_start: "Started the build", knowledge_system_leave_demo: "Left the demo",
  trigger: "Started ingestion", remove_sources: "Removed sources", source_reports_remove: "Removed source reports",
  adhoc_submit: "Added an ad-hoc document",
};
const label = (action) => ACTIONS[action] || action.replace(/_/g, " ");

export class AuditPage {
  title = "Audit log";

  constructor(root) {
    this.root = root;
    this.filters = { user: "", action: "", since: "", until: "", q: "" };
    this.data = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    await this.load();
  }

  query() {
    return new URLSearchParams(Object.entries(this.filters).filter(([, v]) => v)).toString();
  }

  async load() {
    try {
      this.data = await api.audit(this.query());
      this.error = null;
    } catch (err) {
      this.error = err.message;
    }
    this.render();
  }

  render() {
    if (this.error && !this.data) return this.root.replaceChildren(errorBox(this.error));
    const { entries, actions, users } = this.data;
    const set = (key) => (e) => { this.filters[key] = e.target.value; this.load(); };
    const select = (key, options, all) => h("select", { onchange: set(key), "aria-label": all },
      h("option", { value: "" }, all), options.map(([v, t]) => h("option", { value: v, selected: this.filters[key] === v }, t)));
    const text = h("input", { type: "search", placeholder: "Search path, target, details…", value: this.filters.q,
      "aria-label": "Search", class: "audit-search" });
    let typing;
    text.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => { this.filters.q = text.value; this.load(); }, 350); });
    const date = (key, aria) => h("input", { type: "date", value: this.filters[key], onchange: set(key), "aria-label": aria });
    const csv = h("a", { class: "btn btn-sm", href: `/api/audit.csv?${this.query()}`, download: "" }, "Download CSV");
    this.root.replaceChildren(h("div", { class: "stack" },
      note("Every change made through an admin-level route, sign-ins and sign-outs, and refused attempts, with who, "
        + "when and from where. The log is append-only: nobody, admins included, can edit or delete an entry."),
      this.error ? errorBox(this.error) : null,
      card({ title: `Audit log (${entries.length}${entries.length >= 500 ? "+" : ""})`, children: [
        h("div", { class: "toolbar audit-filters" },
          select("user", users.map((u) => [u.user_id, u.first_name]), "Everyone"),
          select("action", actions.map((a) => [a, label(a)]), "Every action"),
          h("label", { class: "faint" }, "From ", date("since", "From")),
          h("label", { class: "faint" }, "to ", date("until", "To")),
          text, csv),
        entries.length ? h("div", { class: "table-wrap" }, h("table", { class: "audit" },
          h("thead", {}, h("tr", {}, ["When", "Who", "What", "Target", "Result", "Details", "From"].map((t) => h("th", {}, t)))),
          h("tbody", {}, entries.map((e) => h("tr", {},
            h("td", { class: "nowrap" }, fmtTimestamp(e.at)),
            h("td", {}, e.user_name || h("span", { class: "faint" }, e.user_id || "—")),
            h("td", { title: `${e.method || ""} ${e.path || ""}` }, label(e.action)),
            h("td", {}, e.target || "—"),
            h("td", {}, e.status ? badge(String(e.status), e.status < 400 ? "ok" : e.status < 500 ? "warn" : "err") : "—"),
            h("td", { class: "audit-details" }, details(e.details)),
            h("td", { class: "faint", title: e.request_id ? `request ${e.request_id}` : "" }, e.ip || "—"))))))
          : h("p", { class: "faint" }, "Nothing matches."),
      ] }),
    ));
  }
}

function details(d) {
  if (!d || !Object.keys(d).length) return "—";
  const values = d.values || {};
  const shown = Object.entries(values).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`);
  const hidden = (d.fields || []).filter((f) => !(f in values));
  const rest = Object.entries(d).filter(([k]) => k !== "values" && k !== "fields").map(([k, v]) => `${k}: ${v}`);
  return [...shown, ...rest, hidden.length ? `also set: ${hidden.join(", ")}` : null].filter(Boolean).join(" · ");
}

// Profile: who you are signed in as, your role and what you may do.

import { api } from "./api.js";
import { applyTheme } from "./theme.js";
import { card, dataTable, errorBox, h, loading } from "./ui.js";

const PERMISSION_TEXT = {
  view_knowledge: "View the knowledge base and its sources",
  query_rag: "Ask questions",
  upload_documents: "Add ad-hoc documents",
  manage_knowledge: "Run ingestion and manage sources",
  view_sessions: "View other users' sessions",
  view_agent_memory: "View agent memory",
  run_evaluations: "Run evaluations",
  manage_agents: "Build, publish and make flows live",
  manage_users: "Add, edit and deactivate users",
  manage_settings: "Change system settings",
};

function when(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

export class ProfilePage {
  title = "Profile";

  constructor(root) {
    this.root = root;
  }

  async mount() {
    this.root.replaceChildren(loading());
    let me;
    try {
      me = await api.me();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load your profile: ${err.message}`));
      return;
    }
    const u = me.user;
    this.root.replaceChildren(h("div", { class: "stack" },
      card({ title: u.first_name, children: dataTable(
        [{ key: "field", label: "" }, { key: "value", label: "" }],
        [
          { field: "Email", value: u.email || "—" },
          { field: "Role", value: u.role },
          { field: "Status", value: u.status },
          { field: "Signed in with", value: me.auth.provider },
          { field: "Last sign-in", value: when(u.last_login) },
          { field: "Account created", value: when(u.created_at) },
        ]) }),
      card({ title: "What you can do", children: u.permissions.length
        ? h("ul", { class: "permission-list" }, u.permissions.map((p) => h("li", {}, PERMISSION_TEXT[p] || p)))
        : h("p", { class: "note" }, "No permissions yet. Ask an administrator.") }),
      card({ title: "Appearance", children: this.appearance(u.theme) }),
      h("p", { class: "note" }, "This app never stores your password: your identity provider signs you in.")));
  }

  // Light / Dark / System, saved on your profile (any browser you sign in on).
  appearance(current) {
    const status = h("span", { class: "faint theme-status" });
    const choose = async (theme) => {
      applyTheme(theme);
      try {
        await api.setTheme(theme);
        status.textContent = "Saved";
      } catch (err) {
        status.textContent = `Not saved: ${err.message}`;
      }
    };
    return h("div", { class: "theme-choice", role: "radiogroup", "aria-label": "Appearance" },
      [["light", "Light"], ["dark", "Dark"], ["system", "System"]].map(([value, label]) => h("label", {},
        h("input", { type: "radio", name: "theme", value, checked: value === (current || "system"),
          onchange: () => choose(value) }), label)),
      status,
      h("p", { class: "faint" }, "System follows your device's light or dark setting."));
  }
}

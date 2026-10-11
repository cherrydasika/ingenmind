// Profile: who you are signed in as, your role and what you may do. For people
// who manage settings, the knowledge system (use your own domain, reset); for
// admins, the master reset back to a fresh install.

import { api } from "./api.js";
import { KnowledgeSystemPage } from "./knowledge_system.js";
import { applyTheme } from "./theme.js";
import { card, dataTable, errorBox, h, loading, note, section } from "./ui.js";

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
      h("p", { class: "note" }, "This app never stores your password: your identity provider signs you in."),
      u.permissions.includes("manage_settings") ? this.knowledgeSystem(u) : null));
  }

  // The knowledge system's page, here; then, for admins, the master reset.
  knowledgeSystem(user) {
    const body = h("div");
    const page = new KnowledgeSystemPage(body);
    page.mount().then(() => {
      if (user.role === "admin" && page.data?.master) body.append(this.masterReset(page.data.master));
    });
    return section("knowledge-system", "Knowledge system",
      "What this installation answers from, and starting again.", body);
  }

  // Back to a fresh install. Typed confirmation; the server checks the admin role.
  masterReset(master) {
    const input = h("input", { type: "text", placeholder: `Type ${master.confirmation}`, autocomplete: "off",
      "aria-label": `Type ${master.confirmation} to confirm`, style: "max-width:240px" });
    const button = h("button", { class: "btn btn-danger", type: "button", disabled: true }, "Reset everything");
    const result = h("div");
    input.addEventListener("input", () => { button.disabled = input.value !== master.confirmation; });
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const { deleted } = await api.masterReset(input.value);
        const total = Object.values(deleted).reduce((a, b) => a + b, 0);
        result.replaceChildren(h("div", { class: "alert ok" }, `Everything was reset (${total.toLocaleString()} rows). `
          + "Setup starts again; people keep their accounts."));
        window.dispatchEvent(new Event("rag:setup-changed"));
        setTimeout(() => { location.hash = "#/setup"; }, 1500);
      } catch (err) {
        result.replaceChildren(h("div", { class: "alert err" }, err.message));
        button.disabled = false;
      }
    });
    return card({
      title: "Master reset (admins only)",
      sub: "Back to a fresh install: everything the reset above empties, and also every flow and its versions, "
        + "the live flow, and everyone's sessions. This cannot be undone.",
      children: [
        h("div", { class: "grid-auto", style: "align-items:start" },
          h("div", {}, h("strong", {}, "Also deleted"), h("ul", {}, master.emptied.map((e) => h("li", {}, e)))),
          h("div", {}, h("strong", {}, "Kept"), h("ul", {}, master.kept.map((k) => h("li", {}, k))))),
        h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px" }, input, button),
        note("Wait for running ingestion jobs and evaluation runs to finish first."),
        result,
      ],
    });
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

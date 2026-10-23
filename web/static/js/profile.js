// Profile: who you are signed in as, your role and what you may do. For people
// who manage settings, the knowledge system (use your own domain instead of the
// demo). Starting over is on Setup (start_over.js).

import { api } from "./api.js";
import { PrivacyBox } from "./privacy.js";
import { KeysBox } from "./api_keys.js";
import { KnowledgeSystemPage } from "./knowledge_system.js";
import { applyTheme } from "./theme.js";
import { card, dataTable, errorBox, h, loading, section } from "./ui.js";

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
      card({ title: "Memory", children: this.memoryBox = h("div", {}, loading()) }),
      card({ title: "API keys", children: this.keysBox = h("div", {}, loading()) }),
      card({ title: "Privacy and your data", children: this.privacyBox = h("div", { id: "privacy" }, loading()) }),
      h("p", { class: "note" }, "This app never stores your password: your identity provider signs you in."),
      u.permissions.includes("manage_settings") ? this.knowledgeSystem() : null));
    this.loadMemory();
    new KeysBox(this.keysBox).load();
    new PrivacyBox(this.privacyBox, u.permissions.includes("manage_settings")).load();
  }

  // Long-term memory (R28): off unless you turn it on; every fact listed with
  // where it came from, editable and deletable; turning it off deletes them all.
  async loadMemory(data) {
    try {
      this.memory = data || await api.myMemory();
    } catch (err) {
      this.memoryBox.replaceChildren(errorBox(`Couldn't load your memory: ${err.message}`));
      return;
    }
    this.renderMemory();
  }

  async changeMemory(fn) {
    try {
      await this.loadMemory(await fn());
    } catch (err) {
      this.memoryBox.prepend(errorBox(err.message));
    }
  }

  renderMemory() {
    const m = this.memory;
    const toggle = h("label", { class: "check-inline memory-toggle" },
      h("input", { type: "checkbox", checked: m.opted_in, onchange: (e) => {
        if (!e.target.checked && m.facts.length
            && !confirm(`Turn memory off? Its ${m.facts.length} fact${m.facts.length === 1 ? "" : "s"} will be deleted.`)) {
          e.target.checked = true;
          return;
        }
        this.changeMemory(() => api.setMyMemory(e.target.checked));
      } }),
      " Remember lasting things I tell the assistant");
    const intro = h("p", { class: "note" }, "When this is on, the assistant notes lasting facts you mention in your "
      + "questions (your home station, a railcard you hold, accessibility needs, travelling with a bike, your "
      + "language), so you needn't repeat them. Only from your own questions; only used to answer you; never "
      + "shown to anyone else or sent to web searches. Turning it off deletes them all.");
    const rows = m.facts.map((f) => {
      const text = h("input", { type: "text", value: f.text, maxlength: 160, "aria-label": "Remembered fact", class: "memory-fact" });
      const from = [f.source?.asked_at ? when(f.source.asked_at) : null, f.source?.question ? `“${f.source.question}”` : null]
        .filter(Boolean).join(" · ");
      return h("li", { class: "memory-row" },
        h("div", { class: "memory-edit" }, text,
          h("button", { class: "btn btn-ghost btn-sm", type: "button",
            onclick: () => this.changeMemory(() => api.editMyFact(f.id, text.value)) }, "Save"),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `Delete: ${f.text}`,
            onclick: () => this.changeMemory(() => api.deleteMyFact(f.id)) }, "Delete")),
        from ? h("div", { class: "faint memory-from" }, `From: ${from}`) : null);
    });
    this.memoryBox.replaceChildren(intro, toggle,
      m.facts.length ? h("ul", { class: "memory-list" }, rows)
        : h("p", { class: "faint" }, m.opted_in ? "Nothing remembered yet." : "Memory is off."),
      m.facts.length ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
        if (confirm("Delete everything remembered about you?")) this.changeMemory(() => api.clearMyMemory());
      } }, "Delete all") : null);
  }

  // The knowledge system's page, here.
  knowledgeSystem() {
    const body = h("div");
    const page = new KnowledgeSystemPage(body);
    page.mount();
    return section("knowledge-system", "Knowledge system",
      "What this installation answers from.", body);
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

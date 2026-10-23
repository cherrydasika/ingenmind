// Admin → Integrations (R38, app/mcp_integration.py): MCP servers whose tools
// agents may call. Add a server by its Streamable HTTP URL (its tools are
// listed at once), enable the tools agents may use, and plug an MCP tool node
// into an agent in the Agent Builder. A token for a server is stored encrypted
// (INTEGRATIONS_KEY). IngenMind is an MCP server too: /api/v1/mcp.

import { api } from "./api.js";
import { badge, card, errorBox, h, loading, note } from "./ui.js";

export class IntegrationsPage {
  title = "Integrations";

  constructor(root) {
    this.root = root;
    this.flash = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.integrations();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load integrations: ${err.message}`));
      return;
    }
    this.render();
  }

  async change(call) {
    try {
      this.data = await call();
      this.flash = null;
    } catch (err) {
      this.flash = err.message;
    }
    this.render();
  }

  renderServer(s) {
    const boxes = s.tools.map((t) => h("label", { class: "check-row", title: t.description },
      h("input", { type: "checkbox", value: t.name, checked: s.enabled.includes(t.name) }),
      h("code", {}, t.name), h("span", { class: "faint" }, ` ${t.description.slice(0, 140)}`)));
    const save = () => this.change(() => api.enableTools(s.server_id,
      boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value)));
    return card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, h("strong", {}, s.name), " ", h("code", {}, s.server_id), " ",
          badge(`${s.enabled.length} of ${s.tools.length} tools enabled`, s.enabled.length ? "ok" : "neutral"),
          s.has_headers ? " " : null, s.has_headers ? badge("token stored", "neutral") : null),
        h("div", {},
          h("button", { class: "btn btn-ghost btn-sm", type: "button", title: "List its tools again",
            onclick: () => this.change(() => api.refreshIntegration(s.server_id)) }, "Refresh"), " ",
          h("button", { class: "btn btn-ghost btn-sm", type: "button",
            onclick: () => this.change(() => api.removeIntegration(s.server_id)) }, "Remove"))),
      h("div", { class: "faint" }, s.url),
      h("div", {}, ...boxes),
      h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: save }, "Save enabled tools")),
    ] });
  }

  renderAdd() {
    const field = (label, input) => h("label", {}, h("div", { class: "faint" }, label), input);
    const id = h("input", { type: "text", placeholder: "deepwiki" });
    const name = h("input", { type: "text", placeholder: "DeepWiki" });
    const url = h("input", { type: "text", placeholder: "https://mcp.example.com/mcp" });
    const header = h("input", { type: "text", placeholder: "Authorization (optional)" });
    const value = h("input", { type: "text", placeholder: "Bearer … (optional; stored encrypted)" });
    const add = () => this.change(() => api.addIntegration({ server_id: id.value.trim(), name: name.value, url: url.value.trim(),
      headers: header.value.trim() ? { [header.value.trim()]: value.value } : null }));
    return card({ title: "Add an MCP server", children: [
      h("div", { class: "stack" }, field("Id", id), field("Name", name), field("URL (Streamable HTTP)", url),
        field("Header for a token", header), field("Its value", value),
        this.data.secrets ? null : h("p", { class: "note" }, "INTEGRATIONS_KEY isn't set on the server, so servers that need a token can't be added yet."),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: add }, "Add and list its tools"))),
    ] });
  }

  render() {
    this.root.replaceChildren(h("div", { class: "stack" },
      note("MCP servers whose tools agents may call. Enable the tools you trust, then plug an MCP tool node into an "
        + "agent in the Agent Builder. Calls go through the tool guardrails (no personal data, links or "
        + "instruction-like text in their arguments, a limit per run) and their results reach the agent as untrusted "
        + "data. IngenMind is an MCP server too, at /api/v1/mcp (search_knowledge_base and ask), with an API key."),
      this.flash ? errorBox(this.flash) : null,
      ...this.data.servers.map((s) => this.renderServer(s)),
      this.renderAdd()));
  }
}

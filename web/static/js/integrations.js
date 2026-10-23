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

  // API tools from OpenAPI specs (R39): load a spec, pick operations, set auth, import; try one; remove.
  renderOpenApi() {
    const field = (label, input) => h("label", {}, h("div", { class: "faint" }, label), input);
    const url = h("input", { type: "text", placeholder: "https://…/openapi.json or .yml" });
    const box = h("div", {});
    const load = async () => {
      try {
        this.preview = { url: url.value.trim(), ...(await api.openapiPreview({ spec_url: url.value.trim() })) };
        this.flash = null;
      } catch (err) {
        this.flash = err.message;
      }
      this.render();
    };
    const tools = (this.data.api_tools || []).map((t) => {
      const args = h("textarea", { class: "input", rows: 2, style: "min-height: 50px", placeholder: '{"latitude": "53.38", "longitude": "-1.47"}' });
      const out = h("pre", { class: "faint", style: "white-space: pre-wrap; max-height: 160px; overflow: auto" });
      const test = async () => {
        let parsed;
        try { parsed = JSON.parse(args.value || "{}"); } catch { out.textContent = "The arguments must be JSON."; return; }
        try {
          const r = await api.testApiTool(t.name, parsed);
          out.textContent = r.ok ? JSON.stringify(r.summary, null, 1).slice(0, 1500) : `Failed: ${r.error}`;
        } catch (err) {
          out.textContent = err.message;
        }
      };
      return card({ children: [
        h("div", { class: "toolbar" },
          h("div", {}, h("strong", {}, t.title), " ", h("code", {}, t.name), " ",
            badge(t.side_effects ? "changes things: needs approval" : "read-only", t.side_effects ? "warn" : "ok"),
            t.has_secret ? " " : null, t.has_secret ? badge("secret stored", "neutral") : null),
          h("button", { class: "btn btn-ghost btn-sm", type: "button",
            onclick: () => this.change(() => api.removeApiTool(t.name)) }, "Remove")),
        h("div", { class: "faint" }, `${t.method} ${t.base_url}${t.path}`),
        t.side_effects ? null : h("div", {}, args, h("div", { class: "actions" },
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: test }, "Try it")), out),
      ] });
    });
    if (this.preview) {
      const p = this.preview;
      const prefix = h("input", { type: "text", placeholder: "a short prefix, e.g. openmeteo" });
      const base = h("input", { type: "text", value: p.base_url || "", placeholder: "https://api.example.com" });
      const kind = h("select", {}, [["none", "No auth"], ["api_key", "API key"], ["oauth_client_credentials", "OAuth client credentials"]]
        .map(([v, l]) => h("option", { value: v }, l)));
      const keyIn = h("select", {}, ["header", "query"].map((v) => h("option", { value: v }, v)));
      const keyName = h("input", { type: "text", placeholder: "X-Api-Key" });
      const secret = h("input", { type: "text", placeholder: "the key or client secret (stored encrypted)" });
      const tokenUrl = h("input", { type: "text", placeholder: "OAuth token URL" });
      const clientId = h("input", { type: "text", placeholder: "OAuth client id" });
      const picks = p.operations.map((op) => h("label", { class: "check-row" },
        h("input", { type: "checkbox", value: op.id, checked: op.method === "GET" }),
        h("code", {}, `${op.method} ${op.path}`), h("span", { class: "faint" }, ` ${op.summary}`),
        op.side_effects ? badge("changes things", "warn") : null));
      const doImport = () => {
        const auth = kind.value === "api_key" ? { type: "api_key", in: keyIn.value, name: keyName.value.trim(), value: secret.value }
          : kind.value === "oauth_client_credentials" ? { type: "oauth_client_credentials", token_url: tokenUrl.value.trim(),
            client_id: clientId.value.trim(), client_secret: secret.value } : { type: "none" };
        this.change(async () => {
          const out = await api.importOpenapi({ spec_url: p.url, prefix: prefix.value.trim(), base_url: base.value.trim(), auth,
            operations: picks.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value) });
          this.preview = null;
          return out;
        });
      };
      box.append(h("div", { class: "stack" },
        h("strong", {}, p.title || "Operations"), h("div", {}, ...picks),
        field("Prefix for the tool names", prefix), field("Base URL", base), field("Auth", kind),
        field("API key goes in", keyIn), field("API key name", keyName), field("Token URL (OAuth)", tokenUrl),
        field("Client id (OAuth)", clientId), field("Key or client secret", secret),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: doImport }, "Import as API tools"),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { this.preview = null; this.render(); } }, "Cancel"))));
    }
    return [card({ title: "API tools from OpenAPI specs", children: [
      h("p", { class: "note" }, "Import operations from an API's OpenAPI spec as API tools: no code. GET operations are "
        + "read-only; any other is flagged as changing things, so it only runs in a flow with an approval step. Once "
        + "imported, choose them in an API tools node in the Agent Builder."),
      h("div", { class: "stack" }, field("Spec URL", url),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: load }, "Load operations"))),
      box,
    ] }), ...tools];
  }

  // Webhooks (R47): signed outbound events, with their delivery log.
  renderWebhooks() {
    const field = (label, input) => h("label", {}, h("div", { class: "faint" }, label), input);
    const events = this.data.events || {};
    const name = h("input", { type: "text", placeholder: "Ops alerts" });
    const url = h("input", { type: "text", placeholder: "https://hooks.example.com/ingenmind" });
    const trusted = h("input", { type: "checkbox" });
    const picks = Object.entries(events).map(([e, about]) => h("label", { class: "check-row" },
      h("input", { type: "checkbox", value: e, checked: e !== "ping" }), h("code", {}, e), h("span", { class: "faint" }, ` ${about}`)));
    const add = async () => {
      try {
        const out = await api.addWebhook({ name: name.value, url: url.value.trim(), trusted: trusted.checked,
          events: picks.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value) });
        this.secret = { hook: out.hook.hook_id, value: out.secret };
        this.data = await api.integrations();
        this.flash = null;
      } catch (err) {
        this.flash = err.message;
      }
      this.render();
    };
    const log = (hookId) => (this.data.deliveries || []).filter((d) => d.hook_id === hookId).slice(0, 8);
    const hooks = (this.data.webhooks || []).map((w) => card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, h("strong", {}, w.name), " ", badge(w.trusted ? "trusted: gets question and answer text" : "ids and numbers only",
          w.trusted ? "warn" : "ok")),
        h("div", {},
          h("button", { class: "btn btn-ghost btn-sm", type: "button",
            onclick: () => this.change(async () => { await api.testWebhook(w.hook_id); await new Promise((r) => setTimeout(r, 1500)); return api.integrations(); }) }, "Send test"), " ",
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.change(() => api.removeWebhook(w.hook_id)) }, "Remove"))),
      h("div", { class: "faint" }, `${w.url} · ${w.events.join(", ")}`),
      this.secret?.hook === w.hook_id ? h("div", { class: "alert ok", role: "status" }, "Its signing secret, shown only now: ",
        h("code", { style: "word-break: break-all" }, this.secret.value)) : null,
      log(w.hook_id).length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Event", "Status", "Tries", "Reply", "When"].map((t) => h("th", {}, t)))),
        h("tbody", {}, log(w.hook_id).map((d) => h("tr", {}, h("td", {}, h("code", {}, d.event)),
          h("td", {}, badge(d.status, d.status === "delivered" ? "ok" : d.status === "failed" ? "err" : "neutral")),
          h("td", {}, String(d.attempts)), h("td", {}, d.last_code ? String(d.last_code) : (d.last_error || "—")),
          h("td", {}, String(d.created_at).slice(0, 19).replace("T", " "))))))) : h("p", { class: "note" }, "No deliveries yet."),
    ] }));
    return [card({ title: "Webhooks", children: [
      h("p", { class: "note" }, "Other systems hear what happens here. Each delivery is a signed POST (X-IngenMind-Signature: "
        + "sha256 HMAC of \"<timestamp>.<body>\" with the hook's secret); a failed one is retried with backoff. Payloads carry "
        + "ids, numbers and labels only, unless the hook is trusted."),
      h("div", { class: "stack" }, field("Name", name), field("URL", url), h("div", {}, ...picks),
        h("label", { class: "check-row" }, trusted, "Trusted: include question and answer text"),
        this.data.secrets ? null : h("p", { class: "note" }, "INTEGRATIONS_KEY isn't set on the server, so a webhook's secret can't be stored yet."),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: add }, "Add webhook"))),
    ] }), ...hooks];
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
      this.renderAdd(),
      ...this.renderOpenApi(),
      ...this.renderWebhooks()));
  }
}

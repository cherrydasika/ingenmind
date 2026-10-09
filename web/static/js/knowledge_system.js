// Knowledge system (Admin): whether it is set up, what it holds, its recent
// history, and the reset that empties it so setup can start again
// (app/knowledge_system.py). Setup itself is setup.js.

import { api } from "./api.js";
import { badge, card, errorBox, fmtInt, fmtTimestamp, h, loading, note } from "./ui.js";

const ORIGIN = { existing: "set up before guided setup existed", demo: "demo documents", setup: "guided setup" };

function stateBadge(state) {
  return badge(state === "READY" ? "Ready" : "Not set up", state === "READY" ? "ok" : "warn");
}

// The app shell listens for this to refresh its "being set up" notice.
function setupChanged() {
  window.dispatchEvent(new Event("rag:setup-changed"));
}

export class KnowledgeSystemPage {
  title = "Knowledge system";

  constructor(root) {
    this.root = root;
    this.flash = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.knowledgeSystem();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the knowledge system: ${err.message}`));
      return;
    }
    this.render();
  }

  async reset(input, button) {
    button.disabled = true;
    try {
      const { deleted } = await api.resetKnowledgeSystem(input.value);
      const total = Object.values(deleted).reduce((a, b) => a + b, 0);
      this.flash = { kind: "ok", text: `Reset done: ${fmtInt(total)} rows deleted. The knowledge system is ready to be set up again.` };
      setupChanged();
    } catch (err) {
      this.flash = { kind: "err", text: err.message };
    }
    await this.mount();
  }

  render() {
    const d = this.data;
    const confirmation = h("input", { type: "text", placeholder: `Type ${d.confirmation}`, autocomplete: "off",
      "aria-label": `Type ${d.confirmation} to confirm`, style: "max-width:220px" });
    const button = h("button", { class: "btn btn-danger", type: "button", disabled: true }, "Reset knowledge system");
    confirmation.addEventListener("input", () => { button.disabled = confirmation.value !== d.confirmation; });
    button.addEventListener("click", () => this.reset(confirmation, button));

    this.root.replaceChildren(h("div", { class: "stack" },
      this.flash ? h("div", { class: `alert ${this.flash.kind}` }, this.flash.text) : null,
      card({
        title: "State",
        children: h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {},
          h("tr", {}, h("th", { style: "width:180px" }, "State"), h("td", {}, stateBadge(d.state))),
          h("tr", {}, h("th", {}, "Origin"), h("td", {}, d.origin ? ORIGIN[d.origin] || d.origin : "—")),
          h("tr", {}, h("th", {}, "Set up"), h("td", {}, d.set_up_at ? fmtTimestamp(d.set_up_at) : "—",
            d.set_up_by ? ` by ${d.set_up_by}` : "")),
          h("tr", {}, h("th", {}, "URLs to ingest"), h("td", {}, fmtInt(d.urls))))))
      }),
      card({
        title: "Reset",
        sub: "Empties the knowledge base and everything learnt from it, so the knowledge system can be set up "
          + "again, for example for another domain. People keep their accounts and history. This cannot be undone.",
        children: [
          h("div", { class: "grid-auto", style: "align-items:start" },
            h("div", {}, h("strong", {}, "Deleted"),
              h("ul", {}, d.emptied.map((e) => h("li", {}, `${e.label}: `, h("strong", {}, fmtInt(e.rows))))) ),
            h("div", {}, h("strong", {}, "Kept"), h("ul", {}, d.kept.map((k) => h("li", {}, k))))),
          h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px" }, confirmation, button),
          note("Wait for running ingestion jobs and evaluation runs to finish first; the reset refuses while they run."),
        ],
      }),
      card({
        title: "History",
        children: d.events.length ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Event"), h("th", {}, "By"), h("th", {}, "Details"))),
          h("tbody", {}, d.events.map((e) => h("tr", {},
            h("td", {}, fmtTimestamp(e.at)), h("td", {}, e.event), h("td", {}, e.user_id || "—"),
            h("td", { class: "wrap note" }, describe(e))))))) : note("Nothing has happened yet."),
      }),
    ));
    this.flash = null;
  }
}

function describe(event) {
  const d = event.details || {};
  if (event.event === "reset") {
    const total = Object.values(d.deleted || {}).reduce((a, b) => a + b, 0);
    return `${fmtInt(total)} rows deleted`;
  }
  if (event.event === "created") return `started ${d.state}${d.urls_imported ? `, ${fmtInt(d.urls_imported)} URLs imported` : ""}`;
  if (event.event === "ready") return ORIGIN[d.origin] || d.origin || "";
  if (event.event === "urls_imported") return `${fmtInt(d.urls)} URLs imported`;
  return "";
}

// Knowledge system, shown on the Profile page for people who manage settings:
// whether it is set up, what it holds, its recent history, and using your own
// domain instead of the demo (app/knowledge_system.py). Setup itself, and
// starting over, are setup.js and start_over.js.

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

  // Leave the demo: the same reset, then setup (#60). Two clicks, no typing.
  async ownDomain(button) {
    if (!button.dataset.armed) {
      button.dataset.armed = "1";
      button.textContent = "Yes, replace the demo and start setup";
      return;
    }
    button.disabled = true;
    try {
      await api.leaveDemo();
      setupChanged();
      location.hash = "#/setup";
    } catch (err) {
      this.flash = { kind: "err", text: err.message };
      await this.mount();
    }
  }

  renderOwnDomain(d) {
    const start = h("button", { class: "btn btn-primary", type: "button", disabled: !d.web_search }, "Set up my own domain");
    start.addEventListener("click", () => this.ownDomain(start));
    return card({
      title: "Use your own domain",
      sub: "You're using the demo: fictional Lakeshore Rail documents. Set IngenMind up for your own domain "
        + "instead: the setup agent asks what it is for and researches the domain, then you choose the sources "
        + "and the content, approve the build, check the evaluation and go live. This replaces the demo documents.",
      children: [
        d.web_search ? null : h("div", { class: "alert", style: "margin-bottom:12px" },
          "Setup researches the web, which needs a Tavily API key (free tier at tavily.com). Add ",
          h("code", {}, "TAVILY_API_KEY=…"), " to ", h("code", {}, "~/.ingenmind/.env"),
          " (or .env in a clone), then restart: ", h("code", {}, "docker compose -f ~/.ingenmind/compose.yml up -d"), "."),
        h("div", { class: "toolbar", style: "justify-content:flex-start" }, start),
      ],
    });
  }

  render() {
    const d = this.data;

    this.root.replaceChildren(h("div", { class: "stack" },
      this.flash ? h("div", { class: `alert ${this.flash.kind}` }, this.flash.text) : null,
      d.state === "READY" && d.origin === "demo" ? this.renderOwnDomain(d) : null,
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
        title: "Start over",
        sub: "To set the knowledge system up again, for example for another domain, use Start over at the "
          + "bottom of Setup. It asks you to sign in again first.",
        children: h("a", { class: "btn", href: "#/setup" }, "Go to Setup"),
      }),
      card({
        title: "History",
        children: d.events.length ? h("details", {}, h("summary", {}, `The last ${Math.min(d.events.length, 10)} events`),
          h("div", { class: "table-wrap details-body" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Event"), h("th", {}, "By"), h("th", {}, "Details"))),
          h("tbody", {}, d.events.slice(0, 10).map((e) => h("tr", {},
            h("td", {}, fmtTimestamp(e.at)), h("td", {}, e.event), h("td", {}, e.user_id || "—"),
            h("td", { class: "wrap note" }, describe(e)))))))) : note("Nothing has happened yet."),
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

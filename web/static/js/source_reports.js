// Data source reports: what research found when a question needed live or
// structured data (app/source_profiles.py). Each report ranks the profiled
// sources and recommends how to integrate one; nothing is built from it.

import { api } from "./api.js";
import { badge, card, errorBox, fmtTimestamp, h, note } from "./ui.js";

const METHOD = {
  api_tool: ["New API tool", "ok"], feed_consumer: ["Feed consumer", "info"], bulk_ingest: ["Scheduled download", "info"],
  page_ingest: ["Ingest pages", "neutral"], none: ["No recommendation", "warn"],
};
const CONFIDENCE = { high: "ok", medium: "warn", low: "err" };

function link(url, label) {
  return url ? h("a", { href: url, target: "_blank", rel: "noopener" }, label) : null;
}

function profileRow(p) {
  const links = [link(p.docs_url, "docs"), link(p.signup_url, "sign-up"), link(p.spec_url, "spec")].filter(Boolean);
  return h("tr", {},
    h("td", { class: "wrap" }, h("strong", {}, p.name), h("div", { class: "note" }, p.provider)),
    h("td", {}, p.access_method.replace("_", " "), h("div", { class: "note" }, `auth: ${p.auth}`)),
    h("td", { class: "wrap" }, p.pricing, p.rate_limits !== "unknown" ? h("div", { class: "note" }, p.rate_limits) : null),
    h("td", {}, p.freshness),
    h("td", { class: "wrap" }, p.licence_and_terms),
    h("td", {}, badge(p.confidence, CONFIDENCE[p.confidence] || "neutral")),
    h("td", { class: "wrap" }, links.flatMap((a, i) => (i ? [" · ", a] : [a])),
      p.evidence_urls?.length ? h("div", { class: "note" }, `${p.evidence_urls.length} evidence page${p.evidence_urls.length === 1 ? "" : "s"}`) : null),
    h("td", { class: "wrap note" }, p.unknowns?.length ? p.unknowns.join("; ") : "—"));
}

export class SourceReports {
  constructor() {
    this.el = h("div");
    this.confirming = null;
    this.flash = null;
  }

  async load() {
    try {
      this.data = await api.sourceReports();
    } catch (err) {
      this.el.replaceChildren(card({ title: "Data source reports", children: errorBox(err.message) }));
      return this.el;
    }
    this.render();
    return this.el;
  }

  async remove(id, button) {
    button.disabled = true;
    try {
      await api.removeSourceReports([id]);
      this.flash = { kind: "ok", text: "Report removed." };
    } catch (err) {
      this.flash = { kind: "err", text: err.message };
    }
    this.confirming = null;
    await this.load();
  }

  render() {
    const { reports, can_manage: canManage } = this.data;
    this.el.replaceChildren(card({
      title: "Data source reports",
      sub: "When a question needs live or structured data the knowledge base cannot hold, research profiles the "
        + "sources (APIs, feeds, downloads) and recommends one. Nothing is built or ingested from a report.",
      children: [
        this.flash ? h("div", { class: `alert ${this.flash.kind}` }, this.flash.text) : null,
        reports.length ? reports.map((r) => this.renderReport(r, canManage)) : note("No data source reports yet."),
      ],
    }));
    this.flash = null;
  }

  renderReport(r, canManage) {
    const rec = r.recommendation || {};
    const [label, kind] = METHOD[rec.method] || METHOD.none;
    const integration = rec.integration || {};
    const sketch = Object.entries({ Tool: integration.tool_name, "What it does": integration.description,
      Inputs: integration.inputs, Endpoint: integration.endpoint, Notes: integration.notes }).filter(([, v]) => v);
    let actions = null;
    if (canManage) {
      if (this.confirming === r.report_id) {
        const yes = h("button", { class: "btn btn-sm btn-danger", type: "button" }, "Remove report");
        yes.addEventListener("click", () => this.remove(r.report_id, yes));
        actions = h("div", { class: "toolbar" }, h("span", { class: "note" }, "Remove this report? This cannot be undone."),
          h("span", { style: "display:flex;gap:8px" }, yes,
            h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => { this.confirming = null; this.render(); } }, "Cancel")));
      } else {
        actions = h("div", { class: "toolbar" }, h("span"), h("button", { class: "btn btn-sm btn-ghost", type: "button",
          onclick: () => { this.confirming = r.report_id; this.render(); } }, "Remove report"));
      }
    }
    return h("details", { open: this.confirming === r.report_id },
      h("summary", {}, `${fmtTimestamp(r.created_at)} · `, h("span", { style: "color:var(--text)" }, r.task), " · ",
        badge(label, kind), rec.recommended ? ` ${rec.recommended}` : ""),
      h("div", { class: "details-body stack" },
        r.missing?.length ? h("p", { class: "note" }, `Missing: ${r.missing.join("; ")}`) : null,
        rec.rationale ? h("p", {}, rec.rationale) : null,
        h("p", {}, h("strong", {}, "Recommended: "), rec.recommended || "none",
          rec.fallback ? [h("strong", {}, " · Fallback: "), rec.fallback] : null),
        sketch.length ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {},
          sketch.map(([k, v]) => h("tr", {}, h("th", { style: "width:140px" }, k), h("td", { class: "wrap" }, v)))))) : null,
        r.profiles?.length ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Source", "Access", "Pricing and limits", "Freshness", "Licence and terms",
            "Confidence", "Links", "Unknowns"].map((t) => h("th", {}, t)))),
          h("tbody", {}, r.profiles.map(profileRow)))) : note("No source could be profiled."),
        rec.not_recommended?.length ? h("div", {}, h("strong", {}, "Not recommended"),
          h("ul", {}, rec.not_recommended.map((x) => h("li", {}, h("strong", {}, x.name), `: ${x.reason}`)))) : null,
        actions));
  }
}

// Freshness (R49, app/recrawl.py): every web page's re-crawl schedule, when it
// was last checked and last changed, and what changed; stale pages flagged.
// Shown on the Ingestion tab.

import { api } from "./api.js";
import { badge, card, errorBox, h, note, shortUrl } from "./ui.js";

const KIND = { unchanged: "ok", changed: "info", failed: "err", gone: "err", new: "neutral" };
const when = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "—");

export class Freshness {
  constructor() {
    this.el = card({ title: "Freshness", children: note("Loading…") });
    this.flash = null;
  }

  async load() {
    try {
      this.data = await api.recrawl();
    } catch (err) {
      this.el.replaceWith(this.el = card({ title: "Freshness", children: errorBox(err.message) }));
      return;
    }
    this.render();
  }

  async act(call) {
    try {
      const out = await call();
      this.flash = out?.status ? `${shortUrl(out.url)}: ${out.status}${out.error ? ` (${out.error})` : ""}` : null;
    } catch (err) {
      this.flash = err.message;
    }
    await this.load();
  }

  render() {
    const { sources, intervals, can_manage: canManage } = this.data;
    const stale = sources.filter((s) => s.stale).length;
    const rows = sources.map((s) => {
      const every = h("select", { disabled: !canManage, onchange: (e) => this.act(() => api.recrawlSchedule(s.source_url, e.target.value)) },
        intervals.map((i) => h("option", { value: i, selected: i === s.every }, i)));
      const diff = s.diff ? `+${s.diff.added} −${s.diff.removed} (${s.diff.kept} kept${s.diff.embedded != null ? `, ${s.diff.embedded} embedded` : ""})` : "";
      return h("tr", {},
        h("td", { class: "wrap" }, h("a", { href: s.source_url, target: "_blank", rel: "noopener" }, shortUrl(s.source_url)),
          s.error ? h("div", { class: "faint" }, s.error) : null),
        h("td", {}, every),
        h("td", {}, badge(s.status, KIND[s.status] || "neutral"), s.stale ? " " : null, s.stale ? badge("stale", "warn") : null),
        h("td", {}, when(s.last_checked_at)), h("td", {}, when(s.last_changed_at)),
        h("td", { class: "wrap", title: s.diff?.sample_added || "" }, diff),
        h("td", {}, canManage ? h("button", { class: "btn btn-ghost btn-sm", type: "button",
          onclick: (e) => { e.target.disabled = true; this.act(() => api.recrawlCheck(s.source_url)); } }, "Check now") : null));
    });
    const fresh = card({ title: "Freshness", sub: `Web pages are checked on their schedule (a conditional GET; only a changed `
      + `page is re-ingested, and only its changed chunks embedded; its old chunks are removed). ${stale} stale.`, children: [
      this.flash ? note(this.flash) : null,
      sources.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Page", "Every", "Status", "Checked", "Changed", "Last change", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows))) : note("No web pages ingested yet."),
    ] });
    this.el.replaceWith(fresh);
    this.el = fresh;
  }
}

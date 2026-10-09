// Sources: every URL currently indexed in PostgreSQL, most recently ingested
// first. Mirrors app/sources_tab.py, plus a filter box.

import { api } from "./api.js";
import { sourcePicker } from "./source_picker.js";
import { card, daysLeft, errorBox, fmtInt, fmtTimestamp, h, loading, metric, note } from "./ui.js";

export class SourcesPage {
  title = "Sources";

  constructor(root) {
    this.root = root;
    this.data = null;
    this.flash = null;
  }

  async mount(force = false) {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.overview(force);
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load sources: ${err.message}`));
      return;
    }
    this.render();
  }

  render() {
    const { collection, count, rows } = this.data;
    if (count === null) {
      this.root.replaceChildren(h("div", { class: "alert info" },
        `Table '${collection}' doesn't exist yet — queue an ingestion job first.`));
      return;
    }
    const deduped = rows.filter((r) => r.dedup_chars !== null);
    const dedupParas = deduped.reduce((n, r) => n + r.dedup_paragraphs, 0);
    const dedupChars = deduped.reduce((n, r) => n + r.dedup_chars, 0);

    const num = (v) => h("td", { class: "num" }, v);
    const table = sourcePicker({
      rows, dataset: "curated", canManage: this.data.can_manage,
      onRemoved: (count) => {
        this.flash = `Removed ${count} page${count === 1 ? "" : "s"}. A configured URL comes back on the next ingestion run unless it is taken out of data/urls.json.`;
        this.mount(true);
      },
      columns: [
        { label: "Ingested", cell: (r) => h("td", {}, fmtTimestamp(r.ingested_at)) },
        { label: "TTL (days)", num: true, cell: (r) => num(r.ttl_days ?? "—") },
        { label: "Expires in", num: true, cell: (r) => num(daysLeft(r.expires_at)) },
        { label: "Chunks", num: true, cell: (r) => num(r.chunks) },
        { label: "Duplicate ¶ removed", num: true, cell: (r) => num(r.dedup_paragraphs ?? "—") },
        { label: "Duplicate chars removed", num: true, cell: (r) => num(r.dedup_chars !== null ? fmtInt(r.dedup_chars) : "—") },
      ],
    });
    this.root.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "toolbar" },
        h("p", { class: "note" }, "Every URL currently indexed in PostgreSQL, most recently ingested first."),
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => this.mount(true) }, "Refresh")),
      this.flash ? h("div", { class: "alert ok" }, this.flash) : null,
      h("div", { class: "grid-auto" },
        metric("Sources", fmtInt(rows.length)),
        metric("Chunks stored", fmtInt(count)),
        metric("Duplicate ¶ removed", fmtInt(dedupParas)),
        metric("Duplicate chars removed", fmtInt(dedupChars))),
      card({
        title: "Indexed sources",
        children: [
          table,
          deduped.length ? note(`Paragraph dedup removed ${dedupParas} duplicate paragraphs (${fmtInt(dedupChars)} chars) across ${deduped.length} sources — each repeated paragraph is stored once, by the first source that had it. “—” = ingested before dedup existed.`) : null,
        ],
      }),
    ));
    this.flash = null;
  }
}

// Sources: every URL currently indexed in PostgreSQL, most recently ingested
// first. Mirrors app/sources_tab.py, plus a filter box.

import { api } from "./api.js";
import { sourcePicker } from "./source_picker.js";
import { badge, card, daysLeft, errorBox, fmtInt, fmtTimestamp, h, labelsCell, loading, metric, note } from "./ui.js";

export class SourcesPage {
  title = "Sources";

  constructor(root) {
    this.root = root;
    this.data = null;
    this.flash = null;
    this.chunks = null;   // {url, chunks} of the page whose chunks are shown
  }

  async showChunks(url) {
    try {
      this.chunks = await api.sourceChunks(url);
    } catch (err) {
      this.flash = err.message;
    }
    this.render();
    this.root.querySelector(".src-chunks")?.scrollIntoView({ behavior: "smooth", block: "start" });
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
        { label: "Labels", cell: (r) => labelsCell(r.labels) },
        { label: "Ingested", cell: (r) => h("td", {}, fmtTimestamp(r.ingested_at)) },
        { label: "TTL (days)", num: true, cell: (r) => num(r.ttl_days ?? "—") },
        { label: "Expires in", num: true, cell: (r) => num(daysLeft(r.expires_at)) },
        { label: "Chunks", num: true, cell: (r) => h("td", { class: "num" }, h("button", {
          class: "btn btn-ghost btn-sm", type: "button", title: "Show this page's chunks and their headings",
          onclick: () => this.showChunks(r.url) }, String(r.chunks))) },
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
      this.chunks ? this.renderChunks(this.chunks) : null,
    ));
    this.flash = null;
  }

  // One page's chunks: the heading path each sits under (structure-aware chunking, R21).
  renderChunks({ url, chunks }) {
    return h("div", { class: "src-chunks" }, card({
      title: `Chunks of ${url}`,
      children: [
        h("div", { class: "toolbar" },
          note(chunks[0]?.chunking === "structure"
            ? "Cut along the page's headings and paragraphs: each chunk starts with the headings it sits under."
            : "Cut into fixed-size windows (CHUNKING=fixed): no headings."),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { this.chunks = null; this.render(); } }, "Close")),
        h("ol", { class: "src-chunk-list" }, chunks.map((c) => h("li", {},
          h("div", {}, c.headings.length ? badge(c.headings.join(" › "), "info") : h("span", { class: "faint" }, "no heading"),
            h("span", { class: "faint" }, ` · ${fmtInt(c.chars)} chars`)),
          h("div", { class: "src-chunk-text" }, c.text.length < c.chars ? `${c.text}…` : c.text)))),
      ],
    }));
  }
}

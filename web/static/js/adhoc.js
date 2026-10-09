// Ad-hoc: paste a document straight into the knowledge base — same
// chunking and embeddings as URL ingestion. Mirrors app/adhoc_tab.py.

import { api } from "./api.js";
import { card, daysLeft, errorBox, fmtInt, fmtTimestamp, h, loading, md, metric, note, richText } from "./ui.js";

const PREVIEW_DEBOUNCE_MS = 350;
const DAY_MS = 86_400_000;

function isoDate(d) {
  return d.toISOString().slice(0, 10);
}

function fmtUtc(date) {
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export class AdhocPage {
  title = "Ad-hoc documents";

  constructor(root) {
    this.root = root;
    this.settings = null;
    this.mode = "days";
    this.result = null;
    this.previewTimer = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      this.settings = await api.adhoc();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load: ${err.message}`));
      return;
    }
    this.render();
  }

  // ---------- Form ----------

  render() {
    const s = this.settings;
    this.title_ = h("input", { type: "text", id: "adhoc-title", placeholder: "e.g. Refund policy 2026",
      oninput: () => this.onChange() });
    this.text = h("textarea", { class: "input", id: "adhoc-text", placeholder: "Paste the document text here…",
      oninput: () => this.onChange() });
    this.days = h("input", { type: "number", min: 1, max: 3650, step: 1, value: s.default_ttl_days,
      oninput: () => this.updateLifetime() });
    this.until = h("input", { type: "date", min: isoDate(new Date()),
      value: isoDate(new Date(Date.now() + s.default_ttl_days * DAY_MS)), oninput: () => this.updateLifetime() });
    this.expires = h("div", { class: "hint" });
    this.submitBtn = h("button", { class: "btn", type: "button", disabled: true, onclick: () => this.submit() }, "Submit");
    this.modeHost = h("div");
    this.resultHost = h("div");
    this.previewHost = h("div");
    this.storedHost = h("div");

    const form = card({
      title: "New document",
      children: [
        h("div", { class: "field" }, h("label", { for: "adhoc-title" }, "Title"), this.title_,
          h("div", { class: "hint" }, "Identifies the document. Submitting the same title again replaces it.")),
        h("div", { class: "field" }, h("label", { for: "adhoc-text" }, "Content"), this.text),
        h("div", { class: "field" }, h("label", {}, "Lifetime"), this.modeHost, this.expires),
        h("div", { class: "actions" }, this.submitBtn,
          h("button", { class: "btn btn-ghost", type: "button", onclick: () => this.cancel() }, "Cancel")),
      ],
    });

    this.root.replaceChildren(h("div", { class: "stack" },
      h("p", { class: "note" }, "Paste a document to add it straight to the knowledge base — same chunking and embeddings as URL ingestion, no background job needed."),
      h("div", { class: "grid-3-2" }, form,
        h("div", { class: "stack" }, this.resultHost, this.previewHost, this.storedHost, this.faq()))));
    this.renderMode();
    this.renderResult();
    this.renderPreview(null);
    this.renderStored();
  }

  renderMode() {
    const seg = (mode, label) => h("button", { type: "button", class: this.mode === mode ? "active" : "",
      onclick: () => { this.mode = mode; this.renderMode(); } }, label);
    this.modeHost.replaceChildren(
      h("div", { class: "segmented", style: "margin-bottom:8px" }, seg("days", "Days"), seg("until", "Until date")),
      this.mode === "days"
        ? h("div", {}, h("label", {}, "Keep for (days)"), this.days)
        : h("div", {}, h("label", {}, "Keep until (end of day, UTC)"), this.until));
    this.updateLifetime();
  }

  // Lifetime in days, from either a number of days or an end date.
  ttlDays() {
    if (this.mode === "days") return Number(this.days.value);
    if (!this.until.value) return NaN;
    const end = new Date(`${this.until.value}T23:59:59Z`);
    return (end.getTime() - Date.now()) / DAY_MS;
  }

  updateLifetime() {
    const ttl = this.ttlDays();
    if (!(ttl > 0)) {
      this.expires.textContent = "Choose a lifetime in the future.";
    } else {
      this.expires.innerHTML = richText(
        `Expires **${fmtUtc(new Date(Date.now() + ttl * DAY_MS))}** (${ttl.toFixed(1)} days) — remove it with the manual \`prune_expired_documents\` job.`);
    }
    this.updateSubmit();
  }

  updateSubmit() {
    this.submitBtn.disabled = !(this.title_.value.trim() && this.text.value.trim() && this.ttlDays() > 0);
  }

  onChange() {
    this.updateSubmit();
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => this.loadPreview(), PREVIEW_DEBOUNCE_MS);
  }

  cancel() {
    this.title_.value = "";
    this.text.value = "";
    this.result = { status: "cancelled" };
    this.renderResult();
    this.renderPreview(null);
    this.updateSubmit();
  }

  async submit() {
    this.submitBtn.disabled = true;
    this.submitBtn.textContent = "Chunking and embedding…";
    try {
      this.result = await api.adhocSubmit(this.title_.value, this.text.value, this.ttlDays());
      if (this.result.status !== "empty") {
        this.title_.value = "";
        this.text.value = "";
        this.renderPreview(null);
      }
      this.settings = await api.adhoc();
      this.renderStored();
    } catch (err) {
      this.result = { status: "error", error: err.message };
    }
    this.submitBtn.textContent = "Submit";
    this.renderResult();
    this.updateSubmit();
  }

  // ---------- Side panel ----------

  async loadPreview() {
    const title = this.title_.value;
    const text = this.text.value;
    if (!title.trim() && !text.trim()) {
      this.renderPreview(null);
      return;
    }
    try {
      this.renderPreview(await api.adhocPreview(title, text));
    } catch (err) {
      this.previewHost.replaceChildren(errorBox(`Preview failed: ${err.message}`));
    }
  }

  renderPreview(p) {
    const s = this.settings;
    const children = !p ? [note("Fill in a title and paste some content to see what will be stored.")] : [
      h("div", { html: richText(`**Source id:** \`${p.source_id || "— (needs a title)"}\``) }),
      h("div", { class: "metrics-2", style: "margin:10px 0" },
        metric("Characters", fmtInt(p.chars)), metric("Chunks", fmtInt(p.chunks))),
      note(`${s.chunk_size} chars per chunk, ${s.chunk_overlap} overlap.`),
      p.exists
        ? h("div", { class: "alert", html: richText("A document with this title already exists — submitting will **replace** it (or just renew its lifetime if the content is identical).") })
        : p.source_id ? note("New document — submitting will **add** it.") : null,
    ];
    this.previewHost.replaceChildren(card({ title: "Preview", children }));
  }

  dedupNote(stats) {
    if (!stats || !stats.removed_chars) return null;
    return note(`Deduplicated: ${stats.removed_cross_page} paragraph(s) already stored by another source and ${stats.removed_within_page} repeated within this document were skipped (${fmtInt(stats.removed_chars)} chars).`);
  }

  renderResult() {
    const r = this.result;
    if (!r) {
      this.resultHost.replaceChildren();
      return;
    }
    const box = (kind, text) => h("div", { class: `alert ${kind}`, html: richText(text) });
    let content;
    if (r.status === "cancelled") content = box("info", "Cancelled — nothing was added.");
    else if (r.status === "added" || r.status === "replaced") {
      content = [box("ok", `${r.status === "added" ? "Added" : "Replaced"} \`${r.source_id}\` — ${r.chunks} chunks embedded in ${r.embed_seconds.toFixed(1)}s (${r.seconds.toFixed(1)}s total). It's searchable on the Retrieval page now.`),
        this.dedupNote(r.dedup)];
    } else if (r.status === "unchanged_ttl_refreshed") {
      content = box("ok", `\`${r.source_id}\` already stored with identical content — lifetime renewed, nothing re-embedded.`);
    } else if (r.status === "empty") {
      content = [box("", r.dedup?.removed_chars ? "Nothing to store — after removing duplicate paragraphs, no content was left."
        : "Nothing to store — the content produced no chunks."), this.dedupNote(r.dedup)];
    } else {
      content = box("err", `Failed: ${r.error}`);
    }
    this.resultHost.replaceChildren(h("div", {}, content));
  }

  renderStored() {
    const docs = this.settings.documents;
    this.storedHost.replaceChildren(card({
      title: "Ad-hoc documents in the database",
      children: !docs.length ? note("None yet.") : h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", { class: "num" }, "Chunks"), h("th", {}, "Added"), h("th", { class: "num" }, "Expires in"))),
        h("tbody", {}, docs.map((d) => h("tr", {},
          h("td", {}, d.title), h("td", { class: "num" }, d.chunks), h("td", {}, fmtTimestamp(d.ingested_at)),
          h("td", { class: "num" }, daysLeft(d.expires_at))))))),
    }));
  }

  faq() {
    return h("details", {},
      h("summary", {}, "Does adding a document re-embed the whole dataset?"),
      h("div", { class: "details-body" }, md(
        "**No — it's incremental.** Only the new document's chunks are embedded and written; the existing chunks are untouched.\n\n" +
        "- **Dense**: each chunk's vector depends only on its own text (Titan V2), so old vectors in the current collection stay valid.\n" +
        "- **Full text**: PostgreSQL generates the searchable terms from each chunk; its GIN index updates when rows change.\n" +
        "- **Index**: pgvector and PostgreSQL maintain the vector and full-text indexes as chunks are added.\n\n" +
        "A full re-embed is only needed if you change an **embedding model** or the **chunking settings** — then old and new chunks would no longer be comparable.")));
  }
}

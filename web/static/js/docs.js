// Docs: the ingestion pipeline, step by step — click a step on the left,
// read how it works on the right. Content comes from app/pipeline_flow.py.
// Below it, the vector quantisation demo.

import { api } from "./api.js";
import { QuantisationDemo } from "./quantisation.js";
import { card, dataTable, errorBox, h, loading, md, note, section } from "./ui.js";

export class DocsPage {
  title = "Pipeline docs";

  constructor(root) {
    this.root = root;
    this.data = null;
    this.selected = "scrape";
    this.sample = null;
    this.quantisation = new QuantisationDemo();
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      [this.data, this.cfg] = await Promise.all([api.docs(), api.config()]);
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load docs: ${err.message}`));
      return;
    }
    this.render();
  }

  render() {
    const flow = h("div", { class: "flow" }, this.data.steps.flatMap((step, i) => [
      h("button", {
        class: `flow-step ${step.key === this.selected ? "active" : ""}`, type: "button",
        onclick: () => { this.selected = step.key; this.render(); },
      }, step.title),
      i < this.data.steps.length - 1 ? h("div", { class: "flow-arrow" }, "↓") : null,
    ]));
    this.detailHost = h("div");
    this.root.replaceChildren(h("div", { class: "stack" },
      h("p", { class: "note" }, "How the ingestion pipeline works, step by step."),
      h("div", { class: "grid-2-3" },
        card({ title: "Pipeline flow", sub: "Click a step to see how it works →", children: flow }),
        card({ title: "Explanation", children: this.detailHost })),
      section("quantisation", ["Vector quantisation", this.quantisation.toggleButton()],
        "Every chunk's vector is stored as float32 today. Quantisation stores each number in fewer bits: less memory and faster search, for a slightly blurred vector. Try the formats on a small example vector. **The 8 numbers are values of an embedding vector** stored in pgvector, not model weights.",
        this.quantisation.element({ embeddings: this.cfg.embeddings }))));
    this.renderDetail();
  }

  renderDetail() {
    const step = this.data.steps.find((s) => s.key === this.selected);
    const parts = [
      h("h3", { style: "margin:0 0 6px" }, step.title),
      h("p", { style: "margin:0 0 12px" }, step.summary),
      md(step.markdown),
    ];
    if (step.key === "store") {
      const sampleHost = h("div", {}, note("Loading…"));
      parts.push(
        h("div", { class: "step-title" }, "Schema — one record per chunk"),
        dataTable([{ key: "Field", label: "Field" }, { key: "Type", label: "Type" }, { key: "Description", label: "Description" }],
          this.data.schema),
        h("div", { class: "step-title" }, "Sample record (live from your database)"),
        sampleHost,
      );
      this.loadSample(sampleHost);
    }
    this.detailHost.replaceChildren(...parts);
  }

  async loadSample(host) {
    try {
      this.sample = this.sample || await api.docsSample();
      host.replaceChildren(this.sample.record
        ? h("pre", { class: "json" }, JSON.stringify(this.sample.record, null, 2))
        : note(this.sample.message));
    } catch (err) {
      host.replaceChildren(errorBox(`Couldn't load a sample record: ${err.message}`));
    }
  }
}

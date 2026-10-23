// Knowledge base (Data): what is stored and how it got there, as tabs on one
// page: Sources (every page indexed, with its labels), Ingestion (the jobs
// that read them, and the pages research added) and Collections (several
// knowledge bases, R35). The tab is in the address: #/knowledge-base/sources,
// #/knowledge-base/ingestion or #/knowledge-base/collections.

import { CollectionsPage } from "./collections.js";
import { IngestionPage } from "./ingestion.js";
import { SourcesPage } from "./sources.js";
import { h } from "./ui.js";

const TABS = [
  ["sources", "Sources", SourcesPage, "Every page in the knowledge base, with what it is about"],
  ["ingestion", "Ingestion", IngestionPage, "Reading pages in: jobs, the pages research added, data-source reports"],
  ["collections", "Collections", CollectionsPage, "Several knowledge bases: their sources and who may search each (R35)"],
];

export class KnowledgeBasePage {
  title = "Knowledge base";

  constructor(root, context) {
    this.root = root;
    this.context = context;
    this.inner = null;
  }

  tab() {
    const asked = location.hash.replace(/^#\//, "").split("/")[1];
    return TABS.some(([key]) => key === asked) ? asked : "sources";
  }

  async mount() {
    const active = this.tab();
    const body = h("div");
    const tabs = h("div", { class: "tabs", role: "tablist" }, TABS.map(([key, label, , hint]) =>
      h("a", { href: `#/knowledge-base/${key}`, role: "tab", title: hint, "aria-selected": String(key === active),
        class: `tab${key === active ? " is-active" : ""}` }, label)));
    this.root.replaceChildren(h("div", { class: "stack" }, tabs, body));
    this.inner?.unmount?.();
    const [, , Page] = TABS.find(([key]) => key === active);
    this.inner = new Page(body, this.context);
    await this.inner.mount();
  }

  unmount() {
    this.inner?.unmount?.();
  }
}

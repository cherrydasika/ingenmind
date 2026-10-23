// Knowledge base → Collections (R35, app/knowledge_collections.py): several
// knowledge bases in one deployment. Each collection is a named set of sources
// with a description (what its knowledge agent covers) and the roles that may
// search it; sources not moved elsewhere are in the main one. A knowledge agent
// is an Agent whose Retriever is set to a collection (Agent Builder →
// Architectures → Knowledge agents builds one per collection, under a router).

import { api } from "./api.js";
import { badge, card, errorBox, h, loading, note, shortUrl } from "./ui.js";

const MAIN = "main";

export class CollectionsPage {
  title = "Collections";

  constructor(root) {
    this.root = root;
    this.flash = null;
    this.editing = null;   // collection id whose form is open; "" for a new one
    this.picked = new Set();
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.collections();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load collections: ${err.message}`));
      return;
    }
    this.render();
  }

  async change(call) {
    try {
      this.data = await call();
      this.flash = null;
      this.editing = null;
      this.picked.clear();
    } catch (err) {
      this.flash = err.message;
    }
    this.render();
  }

  form(c) {
    const isNew = !c;
    const id = h("input", { type: "text", placeholder: "id, e.g. hr_handbook", value: c?.collection_id || "", disabled: !isNew });
    const name = h("input", { type: "text", placeholder: "Name, e.g. HR handbook", value: c?.name || "" });
    const about = h("textarea", { class: "input", rows: 3, style: "min-height: 70px",
      placeholder: "What it covers: its knowledge agent's description, which the router reads" }, c?.description || "");
    const isMain = c?.collection_id === MAIN;   // everyone may always search the main collection
    const roles = isMain ? [] : this.data.roles.map((role) => h("label", { class: "check-row" },
      h("input", { type: "checkbox", value: role, checked: (c?.roles || []).includes(role) }), role));
    const save = () => {
      const body = { name: name.value, description: about.value,
        roles: roles.map((r) => r.querySelector("input")).filter((i) => i.checked).map((i) => i.value) };
      this.change(isNew ? () => api.createCollection({ collection_id: id.value.trim(), ...body })
        : () => api.updateCollection(c.collection_id, body));
    };
    return card({ children: [
      // Each field in a label: .stack hides :empty children, and an input has none.
      h("div", { class: "stack" }, h("label", {}, h("div", { class: "faint" }, "Id"), id),
        h("label", {}, h("div", { class: "faint" }, "Name"), name),
        h("label", {}, h("div", { class: "faint" }, "Description"), about),
        isMain ? null : h("div", {}, h("div", { class: "faint" }, "Who may search it (none ticked: everyone)"), ...roles),
        h("div", { class: "actions" },
          h("button", { class: "btn btn-sm", type: "button", onclick: save }, isNew ? "Create" : "Save"),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { this.editing = null; this.render(); } }, "Cancel"))),
    ] });
  }

  renderCollection(c) {
    const manage = this.data.can_manage;
    if (this.editing === c.collection_id) return this.form(c);
    return card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, h("strong", {}, c.name), " ", h("code", {}, c.collection_id), " ",
          badge(`${c.sources} source${c.sources === 1 ? "" : "s"}`, "neutral"), " ",
          badge(c.roles.length ? `roles: ${c.roles.join(", ")}` : "everyone", c.roles.length ? "warn" : "ok")),
        manage ? h("div", {},
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { this.editing = c.collection_id; this.render(); } }, "Edit"),
          c.collection_id === MAIN ? null : " ",
          c.collection_id === MAIN ? null : h("button", { class: "btn btn-ghost btn-sm", type: "button",
            title: "Its sources go back to the main collection",
            onclick: () => this.change(() => api.deleteCollection(c.collection_id)) }, "Delete")) : null),
      h("div", { class: "faint" }, c.description || "No description yet: add one so the router knows what it covers."),
    ] });
  }

  renderSources() {
    const { sources, collections } = this.data;
    const name = Object.fromEntries(collections.map((c) => [c.collection_id, c.name]));
    const target = h("select", {}, collections.map((c) => h("option", { value: c.collection_id }, c.name)));
    const move = h("button", { class: "btn btn-sm", type: "button", onclick: () => {
      if (this.picked.size) this.change(() => api.assignToCollection(target.value, [...this.picked]));
    } }, "Move ticked sources");
    const rows = sources.map((s) => h("tr", {},
      h("td", {}, this.data.can_manage ? h("input", { type: "checkbox", checked: this.picked.has(s.url),
        onchange: (e) => (e.target.checked ? this.picked.add(s.url) : this.picked.delete(s.url)) }) : null),
      h("td", { class: "wrap" }, s.title || shortUrl(s.url), h("div", { class: "faint" }, shortUrl(s.url))),
      h("td", {}, name[s.collection] || s.collection),
      h("td", { class: "num" }, String(s.chunks))));
    return card({ title: "Sources", sub: "Which collection each source is in. Re-ingesting a page keeps it there.", children: [
      this.data.can_manage ? h("div", { class: "actions" }, target, move) : null,
      sources.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["", "Source", "Collection", "Chunks"].map((t) => h("th", { class: t === "Chunks" ? "num" : null }, t)))),
        h("tbody", {}, rows))) : h("div", { class: "empty-state" }, "Nothing ingested yet."),
    ] });
  }

  render() {
    const d = this.data;
    this.root.replaceChildren(h("div", { class: "stack" },
      note("Several knowledge bases in one deployment. Each collection has its own sources, a description of "
        + "what it covers and the roles that may search it; a user only searches the collections their role may "
        + "see. Sources not moved elsewhere are in the main collection. In the Agent Builder, Architectures → "
        + "Knowledge agents makes one knowledge agent per collection under a router, and answers cite which "
        + "collection each source is from."),
      this.flash ? errorBox(this.flash) : null,
      ...d.collections.map((c) => this.renderCollection(c)),
      d.can_manage ? (this.editing === "" ? this.form(null)
        : h("div", {}, h("button", { class: "btn btn-sm", type: "button", onclick: () => { this.editing = ""; this.render(); } }, "+ New collection")))
        : null,
      this.renderSources()));
  }
}

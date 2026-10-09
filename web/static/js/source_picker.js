// A filterable table of ingested pages; with manage_knowledge, pages can be
// ticked and removed from the knowledge base (every chunk of each page).
// Used by the Ingestion page (research pages) and the Sources page.

import { api } from "./api.js";
import { fmtInt, h, note } from "./ui.js";
import { whyButton } from "./why.js";

// why: a "Why?" button tracing the page back to the plan that put it there (curated knowledge).
export function urlCell(url, why = false) {
  return h("td", { class: "wrap" }, url.startsWith("http") ? h("a", { href: url, target: "_blank", rel: "noopener" }, url) : url,
    why ? [" ", whyButton(url)] : null);
}

// columns: [{ label, num, cell: (row) => <td> }] after the URL column.
// onRemoved(count) runs after a removal, to reload the page.
export function sourcePicker({ rows, columns, dataset, canManage, onRemoved, empty = "No sources ingested yet." }) {
  const selected = new Set();
  let filter = "";
  let confirming = false;
  let flash = null;
  const host = h("div");

  const visible = () => {
    const needle = filter.trim().toLowerCase();
    return rows.filter((r) => !needle || r.url.toLowerCase().includes(needle));
  };

  async function remove(button) {
    button.disabled = true;
    try {
      const { removed } = await api.removeSources(dataset, [...selected]);
      selected.clear();
      confirming = false;
      onRemoved(removed);
    } catch (err) {
      flash = err.message;
      confirming = false;
      render();
    }
  }

  function actions() {
    if (!canManage) return null;
    const count = selected.size;
    const pages = `${count} page${count === 1 ? "" : "s"}`;
    if (confirming) {
      const yes = h("button", { class: "btn btn-sm btn-danger", type: "button" }, `Remove ${pages}`);
      yes.addEventListener("click", () => remove(yes));
      return h("div", { class: "toolbar" },
        h("span", { class: "note" }, `Delete every chunk of ${pages} from the knowledge base? This cannot be undone.`),
        yes,
        h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => { confirming = false; render(); } }, "Cancel"));
    }
    return h("button", {
      class: "btn btn-sm btn-danger", type: "button", disabled: !count,
      onclick: () => { confirming = true; render(); },
    }, count ? `Remove selected (${count})` : "Remove selected");
  }

  // The filter box is built once, so typing keeps its focus.
  const search = h("input", {
    type: "search", placeholder: "Filter by URL…", style: "max-width:360px",
    oninput: (e) => { filter = e.target.value; render(); },
  });
  const actionsHost = h("span");
  const body = h("div");
  host.append(h("div", { class: "toolbar", style: "margin-bottom:12px" }, search, actionsHost), body);

  function render() {
    if (!rows.length) {
      host.replaceChildren(note(empty));
      return;
    }
    const shown = visible();
    const box = (checked, onchange, label) => h("input", { type: "checkbox", checked, onchange, "aria-label": label });
    const all = shown.length > 0 && shown.every((r) => selected.has(r.url));
    const toggleAll = (e) => {
      for (const r of shown) e.target.checked ? selected.add(r.url) : selected.delete(r.url);
      render();
    };
    const toggle = (url) => (e) => {
      e.target.checked ? selected.add(url) : selected.delete(url);
      render();
    };
    actionsHost.replaceChildren(actions() || "");
    body.replaceChildren(...[
      flash ? h("div", { class: "alert err" }, flash) : null,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {},
          canManage ? h("th", {}, box(all, toggleAll, "Select all shown")) : null,
          h("th", {}, "URL"),
          columns.map((c) => h("th", c.num ? { class: "num" } : {}, c.label)))),
        h("tbody", {}, shown.map((r) => h("tr", {},
          canManage ? h("td", {}, box(selected.has(r.url), toggle(r.url), `Select ${r.url}`)) : null,
          urlCell(r.url, dataset === "curated"),
          columns.map((c) => c.cell(r))))))),
      h("div", { class: "note" }, `${fmtInt(shown.length)} of ${fmtInt(rows.length)} pages`),
    ].filter(Boolean));
    flash = null;
  }

  render();
  return host;
}

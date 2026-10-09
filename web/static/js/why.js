// "Why is this here?": a stored page traced back to the ingestion plan entry,
// section, source and approval that put it in the knowledge base (#22).
import { api } from "./api.js";
import { fmtTimestamp, h } from "./ui.js";

export function whyButton(url) {
  const panel = h("div", { class: "why-panel", hidden: true });
  const button = h("button", { class: "btn btn-sm btn-ghost", type: "button", "aria-expanded": "false",
    title: "Why is this page in the knowledge base?" }, "Why?");
  button.addEventListener("click", async () => {
    const open = panel.hidden;
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (!open || panel.dataset.loaded) return;
    panel.replaceChildren(h("span", { class: "note" }, "Looking it up…"));
    try {
      panel.replaceChildren(...explain(await api.provenance(url)));
      panel.dataset.loaded = "1";
    } catch (err) {
      panel.replaceChildren(h("span", { class: "note" }, err.message));
    }
  });
  return h("span", { class: "why" }, button, panel);
}

function explain(why) {
  if (why.origin !== "setup") {
    return [h("div", {}, `Added from the ${why.origin}, not by guided setup.`),
      why.ingested_at ? h("div", { class: "note" }, `Read ${fmtTimestamp(why.ingested_at)}`) : null];
  }
  const p = why.plan || {};
  const approved = p.approved_at ? `approved by ${p.approver || "an admin"} on ${fmtTimestamp(p.approved_at)}` : "";
  return [
    h("div", {}, h("strong", {}, "Source: "), why.source?.name || why.source?.host || "?",
      why.source?.authority ? h("span", { class: "note" }, ` · ${why.source.authority} authority`) : null),
    h("div", {}, h("strong", {}, "Section: "), why.section?.name || "?",
      why.section?.reason ? h("span", { class: "note" }, ` — ${why.section.reason}`) : null),
    h("div", {}, h("strong", {}, "Plan: "), `version ${p.version}${approved ? `, ${approved}` : ""}`,
      p.status && p.status !== "approved" ? h("span", { class: "note" }, ` (${p.status})`) : null),
    why.entry ? h("div", { class: "note" }, `Read ${fmtTimestamp(why.entry.ingested_at)} · ${why.entry.chunks} chunks · `
      + `refreshed every ${why.entry.ttl_days} days`) : null,
  ];
}

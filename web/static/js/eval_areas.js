// Evaluation results per knowledge area (flows/evals.py summarise_areas), for
// the setup page and the Evaluations page: one row per area, each metric
// with its definition beside the table.

import { badge, h } from "./ui.js";

// [key, label, definition]
export const AREA_METRICS = [
  ["retrieval_relevance", "Retrieval", "Answer questions whose expected page was among the pages retrieved."],
  ["answer_correctness", "Correctness", "Mean of the answer check's correctness score."],
  ["groundedness", "Grounded", "Mean of the answer check's faithfulness score: claims backed by the evidence."],
  ["citation_accuracy", "Cites the page", "Answer questions whose answer cited the expected page."],
  ["coverage", "Coverage", "Answer questions answered and passing the check; 0 for an area with no pages."],
  ["refusals_right", "Refusals right", "Not-covered, live and out-of-scope questions handled as expected: "
    + "“not available”, a live tool, or blocked."],
];

export const KIND_LABEL = { answer: "from the pages", not_covered: "no pages", live: "live tool",
  out_of_scope: "out of scope" };

const MEAN_METRICS = new Set(["answer_correctness", "groundedness"]);   // scores, not shares of questions

function value(key, v) {
  if (v === null || v === undefined) return h("span", { class: "faint" }, "—");
  const text = MEAN_METRICS.has(key) ? Number(v).toFixed(2) : `${Math.round(v * 100)}%`;
  const kind = v >= 0.8 ? "ok" : v >= 0.5 ? "warn" : "err";
  return badge(text, kind);
}

/** areas: summary.areas; names: {area key: name}. */
export function areaTable(areas, names = {}) {
  const keys = Object.keys(areas || {}).sort((a, b) => (a === "out_of_scope") - (b === "out_of_scope")
    || (names[a] || a).localeCompare(names[b] || b));
  if (!keys.length) return null;
  return h("div", { class: "stack" },
    h("div", { class: "table-wrap" }, h("table", { class: "area-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Knowledge area"), h("th", { class: "num" }, "Met"),
        AREA_METRICS.map(([key, label, definition]) => h("th", { class: "num", title: definition }, label)))),
      h("tbody", {}, keys.map((key) => {
        const a = areas[key];
        return h("tr", {},
          h("th", { scope: "row" }, names[key] || key.replace(/_/g, " "),
            key === "out_of_scope" ? null
              : h("div", { class: "faint" }, (a.kinds || []).map((k) => KIND_LABEL[k] || k).join(", "))),
          h("td", { class: "num" }, `${a.met}/${a.questions}`),
          AREA_METRICS.map(([metric]) => h("td", { class: "num" }, value(metric, a[metric]))));
      })))),
    h("details", {}, h("summary", {}, "What the columns mean"),
      h("dl", { class: "details-body area-defs" }, AREA_METRICS.flatMap(([, label, definition]) =>
        [h("dt", {}, label), h("dd", {}, definition)]))));
}

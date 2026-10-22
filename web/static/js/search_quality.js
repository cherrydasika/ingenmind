// Evaluations → Search quality (R20, app/flows/search_eval.py): does the
// search find the right passage, before any model answers? A test set is
// written from passages spread over the knowledge base (each question knows
// its passage), or real questions are added by hand with the page that
// answers them. A run searches each question once and scores every setting:
// a heatmap of hit rate (or MRR) by method and results kept, the current
// setting outlined, compared with an earlier run of the same set; a cell's
// misses listed below it.

import { api } from "./api.js";
import { badge, card, errorBox, h, heatCell, note, shortUrl } from "./ui.js";
import { waiting } from "./wait_scene.js";

const POLL_MS = 2000;
const pct = (v) => `${Math.round(v * 100)}%`;
const fmtWhen = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "—");
const MODE_LABEL = { dense: "Meaning only", keyword: "Keywords only", hybrid: "Both" };

function rowLabel(cell) {
  return cell.mode === "hybrid" ? `Both, RRF_K ${cell.rrf_k}` : MODE_LABEL[cell.mode];
}

function settingText(cell) {
  const label = rowLabel(cell);
  return `${label.charAt(0).toLowerCase()}${label.slice(1)}, top ${cell.top_k}`;
}

const sameCell = (a, b) => a && b && a.mode === b.mode && a.top_k === b.top_k && (a.mode !== "hybrid" || a.rrf_k === b.rrf_k);

export class SearchQuality {
  constructor() {
    this.root = h("div");
    this.data = null;          // {job, sets, runs, counts, current}
    this.count = 50;
    this.setId = null;
    this.set = null;           // the chosen set, with its questions
    this.runId = null;
    this.run = null;
    this.compareId = "";
    this.compare = null;
    this.metric = "hit_rate";
    this.cell = null;          // the cell whose misses are listed
    this.error = null;
    this.timer = null;
  }

  stop() {
    clearTimeout(this.timer);
  }

  async load() {
    clearTimeout(this.timer);
    try {
      const wasRunning = this.data?.job?.status === "running";
      this.data = await api.searchQuality();
      const job = this.data.job;
      if (wasRunning && job?.status === "done") {        // pick what the job just made
        if (job.set_id) this.setId = job.set_id;
        if (job.run_id) { this.runId = job.run_id; this.setId = job.set_id; }
      }
      const sets = this.data.sets;
      if (!sets.some((s) => s.id === this.setId)) this.setId = sets[0]?.id || null;
      const runs = this.runsOfSet();
      if (!runs.some((r) => r.id === this.runId)) this.runId = runs[0]?.id || null;
      if (!runs.some((r) => r.id === this.compareId) || this.compareId === this.runId) this.compareId = "";
      [this.set, this.run, this.compare] = await Promise.all([
        this.setId ? api.searchSet(this.setId) : null,
        this.runId ? api.searchRunDetail(this.runId) : null,
        this.compareId ? api.searchRunDetail(this.compareId) : null,
      ]);
      if (this.run && !this.run.cells.some((c) => sameCell(c, this.cell))) this.cell = this.data.current;
    } catch (err) {
      this.error = err.message;
    }
    this.render();
    if (this.data?.job?.status === "running") this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  runsOfSet() {
    return (this.data?.runs || []).filter((r) => r.set_id === this.setId);
  }

  async act(fn) {
    this.error = null;
    try { await fn(); } catch (err) { this.error = err.message; }
    await this.load();
  }

  // ---------- render ----------

  render() {
    const d = this.data;
    if (!d) {
      this.root.replaceChildren(card({ title: "Search quality", children: [this.error ? errorBox(this.error) : null] }));
      return;
    }
    const running = d.job?.status === "running";
    const sizes = h("div", { class: "seg", role: "radiogroup", "aria-label": "How many questions" },
      d.counts.map((n) => h("button", {
        type: "button", role: "radio", "aria-checked": String(n === this.count), disabled: running,
        class: `seg-btn${n === this.count ? " is-on" : ""}`, onclick: () => { this.count = n; this.render(); },
      }, String(n))));
    const children = [
      h("p", { class: "note" }, "Checks the search on its own, before any model writes an answer: does it find the passage "
        + "that answers each question? Write a test set from your knowledge base (each question comes from one passage), "
        + "add real questions by hand, then run it: the search runs once per question and every setting is scored, "
        + "with no model calls."),
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;flex-wrap:wrap" },
        h("span", { class: "metric-label" }, "Questions"), sizes,
        h("button", { class: "btn", type: "button", disabled: running,
          onclick: () => this.act(() => api.searchWriteSet(this.count)) }, "Write a test set")),
      this.error ? errorBox(this.error) : null,
    ];
    if (running) {
      const j = d.job;
      children.push(waiting(j.kind === "run" ? `Searching: ${j.done} of ${j.total} questions…`
        : j.phase === "picking" ? "Choosing passages across the knowledge base…" : `Writing questions: ${j.done} of ${j.total}…`));
    } else if (d.job?.status === "failed") {
      children.push(errorBox(`The last ${d.job.kind === "run" ? "run" : "set"} didn't finish: ${d.job.error}`));
    }
    if (d.sets.length) children.push(this.renderSet(running));
    else if (!running) children.push(h("div", { class: "empty-state" }, "No test set yet: write one above."));
    if (this.run) children.push(this.renderRun());
    this.root.replaceChildren(card({ title: "Search quality", children }));
  }

  renderSet(running) {
    const d = this.data;
    const pick = h("select", { class: "sq-select", "aria-label": "Test set", onchange: (e) => { this.setId = e.target.value; this.runId = null; this.load(); } },
      d.sets.map((s) => h("option", { value: s.id, selected: s.id === this.setId },
        `${s.name} · ${s.questions} questions · ${s.runs} run${s.runs === 1 ? "" : "s"}`)));
    const questions = this.set?.questions || [];
    const question = h("input", { type: "text", placeholder: "A real question", "aria-label": "Question", class: "sq-input" });
    const source = h("input", { type: "url", placeholder: "The page that answers it (its address)", "aria-label": "Page", class: "sq-input" });
    const add = h("button", { class: "btn btn-sm", type: "button", onclick: () => this.act(() =>
      api.searchSetChange(this.setId, { question: question.value, source: source.value })) }, "Add");
    return h("div", { class: "stack", style: "margin-top:12px" },
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" },
        pick,
        h("button", { class: "btn btn-primary", type: "button", disabled: running || !questions.length,
          onclick: () => this.act(() => api.searchRun(this.setId)) }, "Run the search test"),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: running, onclick: () => {
          if (confirm("Delete this test set and its runs?")) this.act(() => api.searchSetDelete(this.setId));
        } }, "Delete set")),
      h("details", { class: "sources" }, h("summary", {}, `Questions (${questions.length})`),
        h("div", { class: "details-body" },
          h("ol", { class: "sq-questions" }, questions.map((q, i) => h("li", {},
            q.question, " ", q.by === "hand" ? badge("by hand", "info") : null,
            h("span", { class: "faint" }, ` · ${shortUrl(q.source)}`), " ",
            h("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Remove", "aria-label": `Remove question ${i + 1}`,
              disabled: running, onclick: () => this.act(() => api.searchSetChange(this.setId, { remove: i })) }, "×")))),
          h("div", { class: "metric-label", style: "margin-top:10px" }, "Add a real question"),
          note("A question you add counts as found when any passage of its page comes back."),
          h("div", { class: "toolbar", style: "justify-content:flex-start;gap:8px;flex-wrap:wrap" }, question, source, add))));
  }

  renderRun() {
    const run = this.run;
    const current = this.data.current;
    const cells = run.cells;
    const at = (cell) => cells.find((c) => sameCell(c, cell));
    const now = at(current);
    const best = cells.reduce((a, c) => (c.hit_rate > a.hit_rate || (c.hit_rate === a.hit_rate && c.mrr > a.mrr) ? c : a));
    const runs = this.runsOfSet();
    const runPick = h("select", { class: "sq-select", "aria-label": "Run", onchange: (e) => { this.runId = e.target.value; this.load(); } },
      runs.map((r) => h("option", { value: r.id, selected: r.id === this.runId },
        `${fmtWhen(r.created_at)} · hit rate ${r.current ? pct(r.current.hit_rate) : "—"}`)));
    const comparePick = h("select", { class: "sq-select", "aria-label": "Compare with", onchange: (e) => { this.compareId = e.target.value; this.load(); } },
      h("option", { value: "" }, "nothing"),
      runs.filter((r) => r.id !== this.runId).map((r) => h("option", { value: r.id, selected: r.id === this.compareId },
        `${fmtWhen(r.created_at)} · hit rate ${r.current ? pct(r.current.hit_rate) : "—"}`)));
    const metricTabs = h("div", { class: "segmented" }, [["hit_rate", "Hit rate"], ["mrr", "MRR"]].map(([key, label]) =>
      h("button", { type: "button", class: this.metric === key ? "active" : null,
        onclick: () => { this.metric = key; this.render(); } }, label)));
    const k = run.knowledge || {};
    return h("div", { class: "stack", style: "margin-top:16px" },
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" },
        h("span", { class: "metric-label" }, "Run"), runPick, h("span", { class: "metric-label" }, "Compare with"), comparePick),
      h("div", { class: "note" }, `${run.questions} questions · ${(k.chunks || 0).toLocaleString()} passages · ${k.embedding_model || ""}`
        + ` · ${run.seconds.toFixed(1)} s` + (k.gone ? ` · ${k.gone} passage${k.gone === 1 ? "" : "s"} gone since the set was written (matched by page)` : "")),
      now ? h("div", { class: `alert ${now.hit_rate >= 0.8 ? "ok" : ""}` },
        `Current setting (${settingText(now)}): the right passage was found for ${pct(now.hit_rate)} of the questions, `
        + `MRR ${now.mrr.toFixed(2)}.`
        + (best.hit_rate === now.hit_rate && best.mrr === now.mrr ? " No other setting does better."
          : ` Best: ${settingText(best)}, ${pct(best.hit_rate)}, MRR ${best.mrr.toFixed(2)}.`)) : null,
      this.compare && this.compare.questions !== run.questions
        ? note(`The earlier run had ${this.compare.questions} questions: the set changed since, so the differences are partly that.`) : null,
      h("div", { class: "matrix-bar" }, metricTabs,
        h("div", { class: "heat-legend" }, h("span", {}, "0"), h("span", { class: "heat-ramp" }), h("span", {}, "1"))),
      h("div", { class: "table-wrap" }, this.renderHeatmap(cells, current)),
      note("**Hit rate**: how often the right passage is among the results kept. **MRR**: 1 when it's first, ½ second, "
        + "0 missing, averaged. Outlined: what the Ask page uses. Click a cell to list its misses."),
      this.renderMisses(at(this.cell) || now));
  }

  renderHeatmap(cells, current) {
    const topKs = [...new Set(cells.map((c) => c.top_k))];
    const rows = [];
    for (const c of cells) if (!rows.some((r) => r.mode === c.mode && r.rrf_k === c.rrf_k)) rows.push(c);
    const before = (cell) => this.compare?.cells.find((c) => sameCell(c, cell));
    const value = (cell) => cell[this.metric];
    const show = (v) => (this.metric === "hit_rate" ? pct(v) : v.toFixed(2));
    return h("table", { class: "matrix sq-matrix" },
      h("thead", {}, h("tr", {}, h("th", {}, "Method"), topKs.map((k) => h("th", { class: "num" }, `Top ${k}`)))),
      h("tbody", {}, rows.map((row) => h("tr", {},
        h("th", { scope: "row" }, rowLabel(row)),
        topKs.map((k) => {
          const cell = cells.find((c) => sameCell(c, { ...row, top_k: k }));
          const prev = cell && before(cell);
          const delta = prev ? value(cell) - value(prev) : null;
          const deltaText = delta == null || Math.abs(delta) < 0.005 ? ""
            : ` ${delta > 0 ? "+" : "−"}${this.metric === "hit_rate" ? Math.round(Math.abs(delta) * 100) : Math.abs(delta).toFixed(2)}`;
          const td = heatCell(cell ? value(cell) : null, {
            text: cell ? `${show(value(cell))}${deltaText}` : null,
            cls: [sameCell(cell, current) ? "heat-current" : "", sameCell(cell, this.cell) ? "heat-picked" : ""].join(" "),
            title: cell ? `${settingText(cell)}: hit rate ${pct(cell.hit_rate)}, MRR ${cell.mrr.toFixed(2)}, ${cell.missed.length} missed`
              + (prev ? `\nbefore: hit rate ${pct(prev.hit_rate)}, MRR ${prev.mrr.toFixed(2)}` : "") : "",
          });
          if (cell) {
            td.tabIndex = 0;
            td.addEventListener("click", () => { this.cell = cell; this.render(); });
            td.addEventListener("keydown", (e) => { if (e.key === "Enter") { this.cell = cell; this.render(); } });
          }
          return td;
        })))));
  }

  renderMisses(cell) {
    if (!cell) return null;
    const per = this.run.per_question;
    const where = (n) => (n ? `#${n}` : "not in top 30");
    if (!cell.missed.length) return h("div", { class: "alert ok" }, `No misses with ${settingText(cell)}.`);
    return h("div", { class: "stack" },
      h("div", { class: "metric-label" }, `Not found with ${settingText(cell)}: ${cell.missed.length} of ${per.length}`),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Question"), h("th", {}, "Its page"),
          h("th", { class: "num", title: "Where the right passage came in the meaning search (top 30)" }, "Meaning"),
          h("th", { class: "num", title: "Where it came in the keyword search (top 30)" }, "Keywords"))),
        h("tbody", {}, cell.missed.map((i) => {
          const q = per[i];
          return h("tr", {},
            h("td", { class: "wrap" }, q.question, " ", q.by === "hand" ? badge("by hand", "info") : null,
              q.gone ? badge("passage gone", "warn") : null),
            h("td", {}, h("a", { href: q.source, target: "_blank", rel: "noopener" }, shortUrl(q.source))),
            h("td", { class: "num" }, where(q.dense)), h("td", { class: "num" }, where(q.keyword)));
        })))));
  }
}

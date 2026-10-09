import { api } from "./api.js";
import { badge, card, errorBox, fmtTime, h, loading, markdown, note, shortUrl } from "./ui.js";
import { FlowEvals } from "./flow_evals.js";

const POLL_MS = 2000;
const SELECTED_KEY = "rag.eval.selected";
const STATUS_KIND = { done: "ok", running: "info", queued: "neutral", cancelled: "warn", interrupted: "warn", failed: "err" };
const PHASE_LABEL = {
  retrieval: "Retrieving chunks", gold: "Gold answers", loading: "Loading model", local: "Local answers",
  judge: "Judging against gold (Claude)", langfuse: "Recording in Langfuse", done: "Done",
};
const SCORE_LABEL = { correctness: "correct", faithfulness: "faithful", similarity: "similar" };

// Sequential blue (one hue): on the dark surface, low = dim, high = bright.
const HEAT = ["#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5",
  "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6", "#cde2fb"];
const MATRIX_VIEWS = [
  ["summary", "Summary"], ["correctness", "Correctness"], ["faithfulness", "Faithfulness"], ["similarity", "Similarity"],
];

function heatCell(value, { title = "", digits = 2, cls = "" } = {}) {
  if (value == null) return h("td", { class: `heat heat-empty ${cls}`, title: title || "Not scored" }, "—");
  const index = Math.round(Math.max(0, Math.min(1, value)) * (HEAT.length - 1));
  // Dark text from step 5 on: ≥ 4.3:1 either side of the switch.
  return h("td", {
    class: `heat ${index >= 5 ? "heat-light" : ""} ${cls}`, style: `background:${HEAT[index]}`, title,
  }, value.toFixed(digits));
}

function scoreKind(v) {
  return v >= 0.75 ? "ok" : v >= 0.4 ? "warn" : "err";
}

function statusBadge(status) {
  return badge(status, STATUS_KIND[status] || "neutral");
}

function fmtWhen(iso) {
  return iso ? String(iso).slice(0, 16).replace("T", " ") : "—";
}

export class EvaluationsPage {
  title = "Evaluations";

  constructor(root) {
    this.root = root;
    this.meta = null;
    this.run = null;
    this.flash = null;
    this.timer = null;
    this.matrixView = "summary";
    this.flowEvals = new FlowEvals();
    try { this.selected = sessionStorage.getItem(SELECTED_KEY); } catch { this.selected = null; }
  }

  unmount() {
    clearTimeout(this.timer);
    this.timer = null;
    this.flowEvals.stop();
  }

  async mount() {
    this.root.replaceChildren(loading());
    await Promise.all([this.refresh(), this.flowEvals.load()]);
  }

  async refresh() {
    clearTimeout(this.timer);
    try {
      this.meta = await api.evaluations();
      const id = this.selected || this.meta.active || this.meta.runs[0]?.id;
      this.run = id ? await api.evaluation(id).catch(() => null) : null;
      if (this.run) this.select(this.run.id, false);
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load evaluations: ${err.message}`));
      return;
    }
    this.render();
    if (this.meta.active) this.timer = setTimeout(() => this.refresh(), POLL_MS);
  }

  select(id, reload = true) {
    this.selected = id;
    try { sessionStorage.setItem(SELECTED_KEY, id); } catch { /* per-tab nicety only */ }
    if (reload) this.refresh();
  }

  async act(fn) {
    try { await fn(this.run.id); this.flash = null; } catch (err) { this.flash = err.message; }
    await this.refresh();
  }

  // ---------- Layout ----------

  render() {
    this.root.replaceChildren(h("div", { class: "stack" },
      this.flowEvals.root,
      h("h2", { class: "fe-archive-title" }, "Archived local-model evaluations"),
      note("Earlier runs of the local-model workflow, read-only."),
      this.flash ? errorBox(this.flash) : null,
      this.renderRuns(),
      this.run ? this.renderRun(this.run) : null,
    ));
  }

  renderRuns() {
    const runs = this.meta.runs;
    const body = runs.length
      ? h("div", { class: "eval-runs" }, runs.map((r) => h("button", {
        type: "button", class: `eval-run ${this.run?.id === r.id ? "active" : ""}`, onclick: () => this.select(r.id),
      },
      h("div", { class: "eval-run-head" }, h("strong", {}, r.name), statusBadge(r.status)),
      h("div", { class: "faint" }, `${fmtWhen(r.created_at)} · ${r.questions} questions × ${r.models.length + 1} models`))))
      : h("div", { class: "empty-state" }, "No evaluations yet.");
    return card({ title: "Runs", children: body });
  }

  renderRun(run) {
    const p = run.progress || {};
    const total = p.total || 1;
    const pct = Math.round(((p.done || 0) / total) * 100);
    const running = run.status === "running";
    const where = ["local", "gold", "judge"].includes(p.phase)
      ? ` · ${p.model} · question ${p.question_index + 1} of ${run.questions.length}`
      : p.phase === "loading" ? ` · ${p.model}` : p.phase === "retrieval" ? ` · question ${p.question_index + 1} of ${run.questions.length}` : "";
    const actions = [
      running ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.act(api.evaluationCancel) }, "Cancel") : null,
    ];
    return h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, run.name, statusBadge(run.status), actions)),
      h("p", { class: "section-desc" },
        `${fmtWhen(run.created_at)} · ${run.embedding.model} (${run.embedding.dim}-dim) chunks · gold ${run.gold.model} · local: ${run.models.join(" → ")}`),
      run.error ? errorBox(run.error) : null,
      run.langfuse?.recorded_at ? h("div", { class: "alert info eval-langfuse" },
        "Recorded in Langfuse — dataset ", h("code", {}, run.langfuse.dataset), ", experiment run ", h("strong", {}, run.langfuse.run_name), ". ",
        h("a", { href: run.langfuse.url, target: "_blank", rel: "noopener" }, "Open in Langfuse ↗"),
        h("span", { class: "faint" }, " (Datasets → runs; scores are named per model, e.g. “correctness · <model>”)")) : null,
      running || p.phase ? h("div", { class: "eval-progress" },
        h("div", { class: "eval-bar" }, h("span", { style: `width:${pct}%` })),
        h("div", { class: "faint" }, `${PHASE_LABEL[p.phase] || "Queued"}${where} · ${p.done || 0} of ${total} steps`)) : null,
      this.renderMatrix(run),
      this.resultsTable(run));
  }

  // Scores as a matrix: per-model averages, or questions × models for one
  // metric. The same values the run sent to Langfuse, read from the run file.
  renderMatrix(run) {
    const scored = (s) => s && !s.error;
    if (!run.scores || !Object.values(run.scores).some((list) => list.some(scored))) return null;
    const metrics = Object.keys(SCORE_LABEL);
    const avg = (model, key) => {
      const vals = run.scores[model].filter(scored).map((s) => s[key]).filter((v) => typeof v === "number");
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    };
    const tabs = h("div", { class: "segmented" }, MATRIX_VIEWS.map(([key, label]) => h("button", {
      type: "button", class: this.matrixView === key ? "active" : null,
      onclick: () => { this.matrixView = key; this.render(); },
    }, label)));

    let table;
    if (this.matrixView === "summary") {
      table = h("table", { class: "matrix" },
        h("thead", {}, h("tr", {}, h("th", {}, "Model"), metrics.map((k) => h("th", { class: "num" }, k)), h("th", { class: "num" }, "Scored"))),
        h("tbody", {}, run.models.map((m) => h("tr", {},
          h("th", { scope: "row" }, h("code", {}, m)),
          metrics.map((k) => heatCell(avg(m, k), { title: `${m} · average ${k}` })),
          h("td", { class: "num faint" }, `${run.scores[m].filter(scored).length} / ${run.questions.length}`)))));
    } else {
      const key = this.matrixView;
      table = h("table", { class: "matrix" },
        h("thead", {}, h("tr", {}, h("th", {}, "Question"), run.models.map((m) => h("th", { class: "num" }, h("code", {}, m))))),
        h("tbody", {}, run.questions.map((q, i) => h("tr", {},
          h("th", { scope: "row", class: "matrix-q", title: q.question }, `${i + 1}. ${q.question}`),
          run.models.map((m) => {
            const s = run.scores[m][i];
            const title = !s ? "Not scored yet" : s.error ? `Not scored: ${s.error}`
              : `${m} · ${key} ${s[key]}${s.reason ? `\n${s.reason}` : ""}`;
            return heatCell(scored(s) ? s[key] : null, { title, digits: key === "similarity" ? 2 : 1 });
          })))),
        h("tfoot", {}, h("tr", {}, h("th", { scope: "row" }, "Average"),
          run.models.map((m) => heatCell(avg(m, key), { title: `${m} · average ${key}` })))));
    }
    return card({
      title: "Score matrix",
      sub: "Each local model against Claude's gold answer. **correctness** and **faithfulness** are the judge's 0 / 0.5 / 1; **similarity** is the bge-m3 cosine of the two answers. Hover a cell for the judge's reason.",
      children: [
        h("div", { class: "matrix-bar" }, tabs,
          h("div", { class: "heat-legend" }, h("span", {}, "0"), h("span", { class: "heat-ramp" }), h("span", {}, "1"))),
        h("div", { class: "table-wrap" }, table),
      ],
    });
  }

  resultsTable(run) {
    const p = run.progress || {};
    const cols = [{ key: "gold", label: `☁ ${run.gold.model}`, gold: true }, ...run.models.map((m) => ({ key: m, label: `▣ ${m}` }))];
    const scoreChips = (s) => {
      if (!s) return null;
      if (s.error) return h("div", { class: "eval-scores faint" }, `not scored: ${s.error}`);
      return h("div", { class: "eval-scores", title: s.reason || "" },
        Object.keys(SCORE_LABEL).filter((k) => s[k] != null).map((k) =>
          h("span", { class: `score score-${scoreKind(s[k])}` }, `${SCORE_LABEL[k]} ${k === "similarity" ? s[k].toFixed(2) : s[k]}`)));
    };
    const cell = (answer, current, score) => {
      if (!answer) {
        return h("td", { class: "eval-cell pending" }, current ? h("span", { class: "spinner" }) : "—");
      }
      const meta = [answer.duration != null ? fmtTime(answer.duration) : null,
        answer.output_tokens != null ? `${answer.output_tokens} tok` : null].filter(Boolean).join(" · ");
      return h("td", { class: "eval-cell" },
        answer.error ? h("div", { class: "eval-error" }, answer.error)
          : h("div", { class: "eval-answer", html: markdown(answer.answer || "") }),
        meta ? h("div", { class: "reply-meta" }, meta) : null,
        scoreChips(score));
    };
    const avg = (model, key) => {
      const vals = (run.scores?.[model] || []).map((s) => s?.[key]).filter((v) => typeof v === "number");
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    };
    const footer = run.scores && Object.values(run.scores).some((list) => list.some((s) => s && !s.error))
      ? h("tfoot", {}, h("tr", {}, h("td", { class: "eval-q" }, h("strong", {}, "Average"), h("div", { class: "faint" }, "vs the gold answer")),
        cols.map((c) => h("td", { class: "eval-cell" }, c.gold ? h("span", { class: "faint" }, "reference")
          : h("div", { class: "eval-scores" }, Object.keys(SCORE_LABEL).map((k) => {
            const v = avg(c.key, k);
            return v == null ? null : h("span", { class: `score score-${scoreKind(v)}` }, `${SCORE_LABEL[k]} ${v.toFixed(2)}`);
          }))))))
      : null;
    const isCurrent = (key, i) => run.status === "running" && p.question_index === i
      && ((key === "gold" && p.phase === "gold") || (key === p.model && p.phase === "local"));
    return h("div", { class: "table-wrap eval-table" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Question"), cols.map((c) => h("th", { class: c.gold ? "gold" : null }, c.label)))),
      h("tbody", {}, run.questions.map((q, i) => h("tr", {},
        h("td", { class: "eval-q" },
          h("div", {}, h("span", { class: "faint" }, `${i + 1}. `), q.question),
          q.error ? h("div", { class: "eval-error" }, `Retrieval: ${q.error}`)
            : q.sources ? h("details", { class: "sources" }, h("summary", {}, `Sources (${q.sources.length})`),
              h("div", { class: "details-body" }, h("ul", {}, q.sources.map((s) =>
                h("li", {}, h("a", { href: s, target: "_blank", rel: "noopener" }, shortUrl(s))))))) : null),
        cols.map((c) => cell(c.gold ? run.gold_answers[i] : run.answers[c.key]?.[i], isCurrent(c.key, i),
          c.gold ? null : run.scores?.[c.key]?.[i]))))),
      footer));
  }
}

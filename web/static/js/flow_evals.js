// Evaluate flow versions: run a question set against up to three versions of
// a flow, then compare them side by side (answer pass rate, scores,
// guardrail blocks, time, tokens) before making one live. Runs execute on
// the server one question at a time; this card polls while any is going.

import { api } from "./api.js";
import { badge, card, errorBox, fmtTime, h, markdown } from "./ui.js";

const POLL_MS = 3000;
const ACTIVE = new Set(["queued", "running"]);
const STATUS_KIND = { done: "ok", running: "info", queued: "neutral", cancelled: "warn", interrupted: "warn", failed: "err" };
const MAX_VERSIONS = 3;
const SECONDS_PER_QUESTION = 35;   // typical agent run, for the cost hint

const versionLabel = (v) => (v === "draft" ? "draft" : `v${v}`);
// Published versions in order, then the draft.
const byVersion = (a, b) => String(a.version).localeCompare(String(b.version), undefined, { numeric: true });
const plain = (text) => text.replace(/\*\*/g, "").replace(/\s+/g, " ");
const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

/** Question-set text: one per line; "question => expected"; "[blocked] question". */
function parseQuestions(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const blocked = /^\[blocked\]\s*/i.test(line);
    const [question, expected = ""] = line.replace(/^\[blocked\]\s*/i, "").split(/\s*=>\s*/, 2);
    return { question, expected, expect_blocked: blocked };
  });
}

function questionsText(questions) {
  return questions.map((q) => `${q.expect_blocked ? "[blocked] " : ""}${q.question}${q.expected ? ` => ${q.expected}` : ""}`).join("\n");
}

export class FlowEvals {
  constructor() {
    this.root = h("div", { class: "stack" });
    this.sets = [];
    this.flows = [];
    this.flowDetail = null;
    this.runs = [];
    this.batch = null;          // the batch being compared
    this.batchRuns = [];        // its runs, in detail
    this.form = { set: "travel_basics", flow: "travel_assistant", versions: new Set() };
    this.editor = null;         // {id, name, text} while editing a set
    this.archive = null;
    this.error = null;
    this.timer = null;
  }

  stop() {
    clearTimeout(this.timer);
    this.timer = null;
  }

  async load() {
    try {
      [this.sets, this.flows, this.runs] = await Promise.all([api.evalSets(), api.flowList(), api.evalRuns()]);
      if (!this.sets.some((s) => s.id === this.form.set)) this.form.set = this.sets[0]?.id;
      await this.loadFlow(this.form.flow);
      this.batch ||= this.runs[0]?.batch || null;
      await this.loadBatch();
    } catch (err) {
      this.error = `Couldn't load flow evaluations: ${err.message}`;
    }
    this.render();
    this.schedule();
  }

  async loadFlow(id) {
    this.form.flow = id;
    this.flowDetail = await api.flowDetail(id);
    const published = this.flowDetail.versions.map((v) => v.version);
    this.form.versions = new Set(published.length ? [published[0]] : []);
  }

  async loadBatch() {
    const ids = this.runs.filter((r) => r.batch === this.batch).map((r) => r.id);
    this.batchRuns = await Promise.all(ids.map((id) => api.evalRun(id)));
    this.batchRuns.sort(byVersion);
  }

  schedule() {
    clearTimeout(this.timer);
    if (this.runs.some((r) => ACTIVE.has(r.status))) {
      this.timer = setTimeout(async () => {
        try {
          this.runs = await api.evalRuns();
          await this.loadBatch();
        } catch { /* keep the last view */ }
        this.render();
        this.schedule();
      }, POLL_MS);
    }
  }

  async start() {
    this.error = null;
    try {
      const versions = [...this.form.versions];
      const { runs } = await api.evalStart({ set_id: this.form.set, flow_id: this.form.flow, versions });
      this.runs = await api.evalRuns();
      this.batch = this.runs.find((r) => r.id === runs[0])?.batch || this.batch;
      await this.loadBatch();
    } catch (err) {
      this.error = err.message;
    }
    this.render();
    this.schedule();
  }

  async cancel() {
    for (const run of this.batchRuns.filter((r) => ACTIVE.has(r.status))) await api.evalCancel(run.id).catch(() => {});
    this.runs = await api.evalRuns();
    await this.loadBatch();
    this.render();
    this.schedule();
  }

  async saveSet() {
    this.error = null;
    try {
      const saved = await api.evalSaveSet({ id: this.editor.id.trim(), name: this.editor.name.trim() || this.editor.id.trim(),
        questions: parseQuestions(this.editor.text) });
      this.sets = await api.evalSets();
      this.form.set = saved.id;
      this.editor = null;
    } catch (err) {
      this.error = err.message;
    }
    this.render();
  }

  // ---------- render ----------

  render() {
    // replaceChildren would turn a null into the text "null": keep only real parts.
    this.root.replaceChildren(...[
      card({
        title: "Evaluate flow versions",
        sub: "Run a question set against up to three versions of a flow and compare them before making one live. "
          + "Each question is a real agent run; only the answer users would see and its numbers are kept.",
        children: [this.error ? errorBox(this.error) : null, this.controls(), this.editor ? this.setEditor() : null],
      }),
      this.runList(),
      this.batchRuns.length ? this.comparison() : null,
    ].filter(Boolean));
  }

  controls() {
    const set = this.sets.find((s) => s.id === this.form.set);
    const versions = this.flowDetail ? [
      ...(this.flowDetail.source === "draft" ? ["draft"] : []),
      ...this.flowDetail.versions.map((v) => v.version),
    ] : [];
    const picked = [...this.form.versions];
    const runs = (set?.count || 0) * picked.length;
    const busy = this.runs.some((r) => ACTIVE.has(r.status));
    return h("div", { class: "fe-controls" },
      h("label", { class: "fe-field" }, h("span", {}, "Question set"),
        h("select", { onchange: (e) => { this.form.set = e.target.value; this.render(); } },
          this.sets.map((s) => h("option", { value: s.id, selected: s.id === this.form.set },
            `${s.name} (${s.count})${s.builtin ? " · built-in" : ""}`))),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => this.openEditor(set) },
          set && !set.builtin ? "Edit set" : "New set…")),
      h("label", { class: "fe-field" }, h("span", {}, "Flow"),
        h("select", { onchange: async (e) => { await this.loadFlow(e.target.value); this.render(); } },
          this.flows.map((f) => h("option", { value: f.id, selected: f.id === this.form.flow },
            `${f.name} (${f.id})${f.live_version !== null && f.live_version !== undefined ? ` · live v${f.live_version}` : ""}`)))),
      h("div", { class: "fe-field" }, h("span", {}, `Versions (up to ${MAX_VERSIONS})`),
        h("div", { class: "fe-versions" }, versions.length ? versions.map((v) => h("label", { class: "fe-check" },
          h("input", { type: "checkbox", checked: this.form.versions.has(v),
            disabled: !this.form.versions.has(v) && this.form.versions.size >= MAX_VERSIONS,
            onchange: (e) => { e.target.checked ? this.form.versions.add(v) : this.form.versions.delete(v); this.render(); } }),
          versionLabel(v))) : h("span", { class: "faint" }, "Nothing published yet."))),
      set?.description ? h("p", { class: "faint fe-desc" }, set.description) : null,
      h("div", { class: "fe-run" },
        h("button", { type: "button", class: "btn", disabled: !picked.length || !set || busy,
          title: busy ? "An evaluation is already running" : "", onclick: () => this.start() },
          busy ? "Evaluation running…" : `Run ${runs} agent ${runs === 1 ? "run" : "runs"}`),
        picked.length && set ? h("span", { class: "faint" },
          `${set.count} questions × ${picked.length} version${picked.length > 1 ? "s" : ""}, one at a time: about `
          + `${Math.max(1, Math.round((runs * SECONDS_PER_QUESTION) / 60))} min of real agent and model calls.`) : null));
  }

  async openEditor(set) {
    this.editor = set && !set.builtin
      ? { id: set.id, name: set.name, text: questionsText(set.questions), existing: true }
      : { id: "", name: "", text: set ? questionsText(set.questions) : "", existing: false };
    if (this.archive === null) this.archive = await api.evalArchive().catch(() => []);
    this.render();
  }

  setEditor() {
    const e = this.editor;
    return h("div", { class: "fe-editor" },
      h("div", { class: "fe-controls" },
        h("label", { class: "fe-field" }, h("span", {}, "Id"),
          h("input", { value: e.id, placeholder: "e.g. austria_trains", disabled: e.existing, oninput: (ev) => { e.id = ev.target.value; } })),
        h("label", { class: "fe-field" }, h("span", {}, "Name"),
          h("input", { value: e.name, oninput: (ev) => { e.name = ev.target.value; } })),
        this.archive?.length ? h("label", { class: "fe-field" }, h("span", {}, "Start from an archived run"),
          h("select", { onchange: (ev) => {
            const run = this.archive.find((r) => r.id === ev.target.value);
            if (run) { e.text = questionsText(run.questions); e.name ||= run.name; this.render(); }
          } }, h("option", { value: "" }, "—"), this.archive.map((r) => h("option", { value: r.id }, `${r.name} (${r.questions.length})`)))) : null),
      h("textarea", { class: "fe-text", rows: 10, oninput: (ev) => { e.text = ev.target.value; } }, e.text),
      h("p", { class: "faint" }, "One question per line. Add ", h("code", {}, "=> expected answer"),
        " to show a reference beside the answers; start a line with ", h("code", {}, "[blocked]"),
        " for a question the guardrail should refuse. At most 50."),
      h("div", { class: "fe-run" },
        h("button", { type: "button", class: "btn btn-sm", onclick: () => this.saveSet() }, "Save set"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { this.editor = null; this.render(); } }, "Cancel")));
  }

  runList() {
    const batches = [];
    for (const run of this.runs) {
      let b = batches.find((x) => x.batch === run.batch);
      if (!b) batches.push(b = { batch: run.batch, runs: [] });
      b.runs.push(run);
    }
    if (!batches.length) return null;
    return card({
      title: "Flow evaluation runs",
      children: h("div", { class: "eval-runs" }, batches.slice(0, 12).map((b) => {
        const first = b.runs[0];
        return h("button", { type: "button", class: `eval-run ${this.batch === b.batch ? "active" : ""}`,
          onclick: async () => { this.batch = b.batch; await this.loadBatch(); this.render(); } },
        h("div", { class: "eval-run-head" }, h("strong", {}, `${first.set_name} · ${first.flow_id}`)),
        h("div", { class: "fe-chips" }, b.runs.slice().sort(byVersion).map((r) => h("span", { class: "fe-chip" },
          versionLabel(r.version), " ", badge(ACTIVE.has(r.status) ? `${r.completed}/${r.total}` : r.status, STATUS_KIND[r.status] || "neutral"),
          r.summary?.pass_rate !== undefined && r.summary?.pass_rate !== null ? ` ${pct(r.summary.pass_rate)}` : ""))),
        h("div", { class: "faint" }, String(first.created_at).slice(0, 16).replace("T", " ")));
      })),
    });
  }

  comparison() {
    const runs = this.batchRuns;
    const questions = runs[0].questions;
    const active = runs.some((r) => ACTIVE.has(r.status));
    const scoreKeys = [...new Set(runs.flatMap((r) => Object.keys(r.summary?.scores || {})))];
    // [label, value(summary), format, higher is better?]
    const rows = [
      ["Expectations met", (s) => (s.expectations ? s.expectations_met / s.expectations : null),
        (v, s) => `${pct(v)} (${s.expectations_met}/${s.expectations})`, true],
      ["Answer evaluator pass", (s) => s.pass_rate, (v, s) => `${pct(v)} (${s.passed}/${s.evaluated})`, true],
      ["Average overall", (s) => s.overall, (v) => num(v), true],
      ...scoreKeys.map((k) => [`  ${k.replace(/_/g, " ")}`, (s) => s.scores?.[k] ?? null, (v) => num(v), true]),
      ["Input / output blocked", (s) => s.input_blocked + s.output_blocked, (v, s) => `${s.input_blocked} / ${s.output_blocked}`, null],
      ["Failed runs", (s) => s.failed, (v) => String(v), false],
      ["Median time", (s) => s.p50_seconds, (v) => (v === null ? "—" : fmtTime(v)), false],
      ["Tokens", (s) => s.tokens, (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)), false],
    ];
    const best = (pick, higher) => {
      if (higher === null || runs.length < 2) return null;
      const vals = runs.map((r) => (r.summary ? pick(r.summary) : null)).filter((v) => v !== null && v !== undefined);
      if (vals.length < 2 || new Set(vals).size === 1) return null;
      return higher ? Math.max(...vals) : Math.min(...vals);
    };
    const summary = h("div", { class: "table-wrap" }, h("table", { class: "fe-summary" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), runs.map((r) => h("th", { class: "num" },
        `${r.flow_id} ${versionLabel(r.version)}`, " ", badge(ACTIVE.has(r.status) ? `${r.completed}/${r.total}` : r.status,
          STATUS_KIND[r.status] || "neutral"))))),
      h("tbody", {}, rows.map(([label, pick, fmt, higher]) => {
        const top = best(pick, higher);
        return h("tr", {}, h("th", { scope: "row", class: label.startsWith("  ") ? "fe-sub" : null }, label.trim()),
          runs.map((r) => {
            const v = r.summary ? pick(r.summary) : null;
            return h("td", { class: `num ${top !== null && v === top ? "fe-best" : ""}` }, r.summary ? fmt(v, r.summary) : "—");
          }));
      }))));

    const cell = (run, i) => {
      const r = run.results.find((x) => x.idx === i);
      if (!r) return h("td", { class: "eval-cell pending" }, ACTIVE.has(run.status) && run.completed === i ? h("span", { class: "spinner" }) : "—");
      const kind = r.failed ? ["failed", "err"] : r.input_blocked ? ["input blocked", "warn"] : r.output_blocked ? ["output blocked", "warn"]
        : r.passed === true ? ["passed", "ok"] : r.passed === false ? ["failed check", "err"] : ["not evaluated", "neutral"];
      return h("td", { class: "eval-cell" },
        h("div", { class: "fe-result" }, badge(kind[0], kind[1]),
          r.expectation_met === false ? badge("unexpected", "err") : null,
          r.overall !== null ? h("span", { class: "faint" }, ` overall ${num(r.overall)}`) : null,
          h("span", { class: "faint" }, ` · ${fmtTime(r.seconds)}`)),
        r.failed_on?.length ? h("div", { class: "faint" }, `below threshold: ${r.failed_on.join(", ")}`) : null,
        r.answer ? h("details", { class: "fe-answer" }, h("summary", {}, plain(r.answer).slice(0, 110) + (plain(r.answer).length > 110 ? "…" : "")),
          h("div", { class: "eval-answer", html: markdown(r.answer) })) : null);
    };
    const table = h("div", { class: "table-wrap eval-table" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Question"), runs.map((r) => h("th", {}, `${versionLabel(r.version)}`)))),
      h("tbody", {}, questions.map((q, i) => h("tr", {},
        h("td", { class: "eval-q" }, h("div", {}, h("span", { class: "faint" }, `${i + 1}. `), q.question,
          q.expect_blocked ? h("span", { class: "faint" }, " · should be blocked") : null),
          q.expected ? h("details", { class: "sources" }, h("summary", {}, "Expected"), h("div", { class: "details-body eval-answer", html: markdown(q.expected) })) : null),
        runs.map((r) => cell(r, i)))))));

    return h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, `${runs[0].set_name} · ${runs[0].flow_id}`),
        active ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => this.cancel() }, "Cancel") : null,
        h("a", { class: "btn btn-ghost btn-sm", href: "#/flows" }, "Open the flow builder")),
      h("p", { class: "section-desc" }, "Best value per row is highlighted. ‘Expectations met’: answered and passed the answer evaluator, "
        + "or blocked when the question should be. Make the winner live from the flow builder's History."),
      summary, table);
  }
}

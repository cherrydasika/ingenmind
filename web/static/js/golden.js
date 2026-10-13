// Evaluations → Golden set from your documents (app/flows/golden.py): pick how
// many questions, and passages spread over the whole knowledge base become a
// question with its expected answer each. Before saving, a map shows where in
// the knowledge base they come from, and a bar per topic compares its share of
// the questions with its share of the knowledge base, so clusters stand out.
// Remove any question, then save it as an evaluation set.

import { api } from "./api.js";
import { badge, card, errorBox, h, note, shortUrl } from "./ui.js";
import { waiting } from "./wait_scene.js";

const POLL_MS = 2500;
const SECONDS_PER_QUESTION = 3;   // model calls run four at a time
// Ten distinguishable hues for topics on the map (the rest are grey).
const HUES = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2", "#ca8a04", "#dc2626", "#4f46e5", "#059669"];
const pct = (v) => `${Math.round(v * 100)}%`;

export class GoldenSet {
  constructor({ onSaved }) {
    this.onSaved = onSaved;     // a set was saved: the flow evaluation card reloads its sets
    this.root = h("div");
    this.counts = [5, 10, 20, 50, 100];
    this.count = 20;
    this.run = null;
    this.removed = new Set();   // questions removed before saving (their index)
    this.error = null;
    this.saved = null;
    this.timer = null;
  }

  stop() {
    clearTimeout(this.timer);
  }

  async load() {
    try {
      const { run, counts } = await api.goldenStatus();
      this.run = run;
      this.counts = counts;
    } catch (err) {
      this.error = err.message;
    }
    this.render();
    this.poll();
  }

  poll() {
    clearTimeout(this.timer);
    if (this.run?.status === "running") this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  async start() {
    this.error = null;
    this.saved = null;
    this.removed = new Set();
    try {
      this.run = await api.goldenStart(this.count);
    } catch (err) {
      this.error = err.message;
    }
    this.render();
    this.poll();
  }

  async save(name) {
    const questions = this.run.questions.filter((_, i) => !this.removed.has(i)).map((q) => ({
      question: q.question, expected: q.expected, kind: "answer", area: q.topic, expected_source: q.source }));
    const id = `golden_${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")}`;
    try {
      const set = await api.evalSaveSet({ id, name: name || `Golden set (${questions.length} questions)`,
        description: `Written from the knowledge base: ${questions.length} questions over `
          + `${this.run.spread.covered} of ${this.run.spread.of} topics.`, questions });
      this.saved = set;
      this.onSaved?.(set);
    } catch (err) {
      this.error = err.message;
    }
    this.render();
  }

  // ---------- render ----------

  render() {
    const r = this.run;
    const running = r?.status === "running";
    const sizes = h("div", { class: "seg", role: "radiogroup", "aria-label": "How many questions" },
      this.counts.map((n) => {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(n === this.count),
          class: `seg-btn${n === this.count ? " is-on" : ""}`, disabled: running }, String(n));
        b.addEventListener("click", () => { this.count = n; this.render(); });
        return b;
      }));
    const go = h("button", { class: "btn btn-primary", type: "button", disabled: running },
      r?.status === "done" ? "Write a new set" : "Write the questions");
    go.addEventListener("click", () => this.start());
    const children = [
      h("p", { class: "note" }, "Questions with their expected answers, written from passages spread over your whole "
        + "knowledge base: pages share them evenly, and no topic gets more than about twice its share. Review them, remove "
        + "any, then save them as a set to run below."),
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;flex-wrap:wrap" },
        h("span", { class: "metric-label" }, "Questions"), sizes, go,
        h("span", { class: "note" }, `about ${Math.max(1, Math.round((this.count * SECONDS_PER_QUESTION) / 60))} min`)),
      this.error ? errorBox(this.error) : null,
    ];
    if (running) {
      children.push(waiting(r.phase === "picking" ? "Choosing passages across the knowledge base…"
        : `Writing questions: ${r.done} of ${r.total}…`,
      r.skipped ? `${r.skipped} passage${r.skipped === 1 ? "" : "s"} had nothing to ask about and were swapped.` : null));
    } else if (r?.status === "failed") {
      children.push(errorBox(`Writing the set didn't finish: ${r.error}`));
    } else if (r?.status === "done") {
      children.push(this.renderResult(r));
    }
    this.root.replaceChildren(card({ title: "Golden set from your documents", children }));
  }

  renderResult(r) {
    const s = r.spread;
    const colour = topicColours(s.topics.filter((t) => t.questions).map((t) => t.topic));
    const kept = r.questions.length - this.removed.size;
    const clusters = s.topics.filter((t) => t.flag === "cluster");
    const missing = s.topics.filter((t) => t.flag === "missing");
    const verdict = clusters.length
      ? h("div", { class: "alert" }, `Clustered: ${clusters.map((t) => t.topic).join(", ")} ${clusters.length === 1 ? "has" : "have"} `
        + "more questions than their share of the knowledge base. Remove some, or write a new set.")
      : h("div", { class: "alert ok" }, `Spread: ${s.covered} of ${s.of} topics have questions, with no cluster.`);
    const name = h("input", { type: "text", value: `Golden set (${kept} questions)`, "aria-label": "Set name",
      class: "golden-name" });
    const save = h("button", { class: "btn btn-primary", type: "button", disabled: !kept || !!this.saved }, "Save as a set");
    save.addEventListener("click", () => this.save(name.value.trim()));
    return h("div", { class: "stack", style: "margin-top:12px" },
      h("div", { class: "note" }, `${r.questions.length} questions from ${r.pages.toLocaleString()} pages`
        + (r.skipped ? ` · ${r.skipped} passage${r.skipped === 1 ? "" : "s"} skipped` : "")
        + (s.closest ? ` · the two most alike passages: ${pct(s.closest.similarity)} similar` : "")),
      r.questions.length < r.count
        ? note(`You asked for ${r.count}: the knowledge base has ${r.chunks} passage${r.chunks === 1 ? "" : "s"} long enough to `
          + "ask about, and some had nothing to ask. Add pages to the knowledge base for a bigger set.") : null,
      verdict,
      missing.length ? note(`No question yet on: ${missing.map((t) => t.topic).join(", ")}.`) : null,
      h("div", { class: "golden-spread" }, this.renderMap(r.map, colour), this.renderTopics(s, colour)),
      this.renderQuestions(r, colour),
      this.saved
        ? h("div", { class: "alert ok" }, `Saved as “${this.saved.name}”: pick it in Evaluate flow versions below.`)
        : h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px" }, name, save,
          h("span", { class: "note" }, `${kept} question${kept === 1 ? "" : "s"}`)));
  }

  // Where they come from: every dot a passage (grey: the rest of the knowledge base).
  renderMap(points, colour) {
    const W = 520;
    const H = 300;
    const pad = 14;
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "golden-map");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Map of the knowledge base: grey dots are passages, coloured numbered dots the questions' passages");
    const at = (p) => [pad + p.x * (W - 2 * pad), pad + (1 - p.y) * (H - 2 * pad)];
    for (const p of points.filter((q) => q.q === null)) {
      const [x, y] = at(p);
      const dot = document.createElementNS(ns, "circle");
      Object.entries({ cx: x, cy: y, r: 2.2, class: "golden-bg" }).forEach(([k, v]) => dot.setAttribute(k, v));
      svg.append(dot);
    }
    for (const p of points.filter((q) => q.q !== null && !this.removed.has(q.q))) {
      const [x, y] = at(p);
      const g = document.createElementNS(ns, "g");
      const dot = document.createElementNS(ns, "circle");
      Object.entries({ cx: x, cy: y, r: 7, fill: colour(p.topic) }).forEach(([k, v]) => dot.setAttribute(k, v));
      const label = document.createElementNS(ns, "text");
      Object.entries({ x, y: y + 3, "text-anchor": "middle", class: "golden-num" }).forEach(([k, v]) => label.setAttribute(k, v));
      label.textContent = String(p.q + 1);
      const title = document.createElementNS(ns, "title");
      title.textContent = `Q${p.q + 1} · ${p.topic}: ${this.run.questions[p.q]?.question || ""}`;
      g.append(dot, label, title);
      svg.append(g);
    }
    return h("figure", { class: "golden-figure" }, svg,
      h("figcaption", { class: "note" }, "The knowledge base, laid out by meaning: passages close together say similar "
        + "things. Grey dots are passages; numbered dots, the questions' passages. Well spread is good."));
  }

  // Per topic: its share of the questions against its share of the knowledge base.
  renderTopics(s, colour) {
    const n = Math.max(1, this.run.questions.length - this.removed.size);
    const counts = {};
    this.run.questions.forEach((q, i) => { if (!this.removed.has(i)) counts[q.topic] = (counts[q.topic] || 0) + 1; });
    return h("div", { class: "golden-topics" },
      h("div", { class: "metric-label" }, "Topics: questions vs. the knowledge base"),
      s.topics.slice(0, 14).map((t) => {
        const got = counts[t.topic] || 0;
        return h("div", { class: "golden-topic" },
          h("div", { class: "golden-topic-name" }, h("span", { class: "golden-swatch", style: `background:${got ? colour(t.topic) : "var(--border-strong)"}` }),
            t.topic, t.flag === "cluster" ? [" ", badge("cluster", "warn")] : t.flag === "missing" && !got ? [" ", badge("no question", "neutral")] : null),
          h("div", { class: "golden-bar", title: `${got} question${got === 1 ? "" : "s"} (${pct(got / n)}); ${pct(t.share)} of the knowledge base` },
            h("span", { class: "golden-bar-q", style: `width:${(100 * got) / n}%;background:${colour(t.topic)}` }),
            h("span", { class: "golden-bar-kb", style: `left:${100 * t.share}%` })),
          h("div", { class: "note golden-topic-n" }, `${got}`));
      }),
      s.topics.length > 14 ? note(`and ${s.topics.length - 14} more topics`) : null,
      note("Bar: share of the questions. Tick: share of the knowledge base."));
  }

  renderQuestions(r, colour) {
    return h("details", { open: r.questions.length <= 20 },
      h("summary", {}, `The questions (${r.questions.length - this.removed.size})`),
      h("ol", { class: "golden-list details-body" }, r.questions.map((q, i) => {
        const removed = this.removed.has(i);
        const toggle = h("button", { class: "btn btn-sm btn-ghost", type: "button", disabled: !!this.saved },
          removed ? "Put back" : "Remove");
        toggle.addEventListener("click", () => { if (removed) this.removed.delete(i); else this.removed.add(i); this.render(); });
        return h("li", { class: removed ? "is-removed" : "" },
          h("div", { class: "golden-q" }, h("span", { class: "golden-swatch", style: `background:${colour(q.topic)}` }),
            h("strong", {}, q.question), toggle),
          h("div", {}, h("span", { class: "metric-label" }, "Expected "), q.expected),
          h("div", { class: "note" }, `${q.topic} · `, h("a", { href: q.source, target: "_blank", rel: "noopener" }, shortUrl(q.source))));
      })));
  }
}

function topicColours(topics) {
  const index = new Map(topics.map((t, i) => [t, i]));
  return (topic) => (index.has(topic) && index.get(topic) < HUES.length ? HUES[index.get(topic)] : "#94a3b8");
}

// Monitoring → Scraper trials (app/scraper_trials.py): does a new scraping
// library help, change nothing, or make things worse? A trial fetches and
// extracts the same pages with today's scraper (before) and a candidate
// (after) and scores both, with no model calls. Every trial is kept, so the
// record shows what each library tried did; libraries tried and removed
// are listed with what they did (scraper_trials.TRIED). Admins only.

import { api } from "./api.js";
import { badge, card, dataTable, errorBox, fmtTimestamp, h, note, shortUrl } from "./ui.js";
import { waiting } from "./wait_scene.js";

const POLL_MS = 2000;
const VERDICT = { better: ["Helped", "ok"], no_change: ["No change", "neutral"], worse: ["Made it worse", "err"] };
const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const num = (v, digits = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(digits));
const secs = (v) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(2)} s`);
const signed = (v, fmt) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${fmt(v)}`);

// Score → [label, format, higher is better (null: neither), what it means]
const SCORES = [
  ["fetch_rate", "Pages fetched", pct, true, "Pages that came back (2xx with a body)"],
  ["text_rate", "Pages with text", pct, true, "Pages the extractor found text on"],
  ["facts", "Answer facts found", pct, true, "Numbers and key words of each evaluation answer found on its page"],
  ["hit_at_5", "Keyword search hit (top 5)", pct, true, "The answer's page among the first 5 keyword (BM25) results"],
  ["mrr", "Keyword search MRR", (v) => num(v), true, "1 / the answer's page's position, 0 when missing"],
  ["boilerplate", "Boilerplate", pct, false, "Text in lines repeated across a site's pages (menus, footers)"],
  ["words", "Words per page", (v) => num(v, 0), null, "Median"],
  ["agreement", "Same words as before", pct, null, "How much the text differs from today's (not better or worse)"],
  ["fetch_s", "Fetch time", secs, false, "Median per page (shown, never decides the verdict)"],
];

const verdictBadge = (v) => badge(...(VERDICT[v] || [v, "neutral"]));
const versions = (v) => Object.entries(v || {}).map(([name, ver]) => `${name} ${ver || "?"}`).join(", ");

function change(key, delta) {
  const spec = SCORES.find((s) => s[0] === key);
  if (!spec || delta === null || delta === undefined) return "—";
  const [, , fmt, higher] = spec;
  const text = key === "fetch_s" || key === "words" ? signed(delta, fmt) : signed(delta, key === "mrr" ? fmt : pct);
  if (higher === null || Math.abs(delta) < 1e-9) return text;
  const good = higher ? delta > 0 : delta < 0;
  return h("span", { class: good ? "trial-up" : "trial-down" }, text);
}

export class ScraperTrialsPage {
  title = "Scraper trials";

  constructor(root) {
    this.root = root;
    this.data = null;           // {job, candidates, trials, tried}
    this.candidate = null;
    this.note = "";
    this.trialId = null;
    this.trial = null;          // the chosen trial, with per page and per question detail
    this.error = null;
    this.timer = null;
  }

  async mount() {
    await this.load();
  }

  unmount() {
    clearTimeout(this.timer);
  }

  async load() {
    clearTimeout(this.timer);
    try {
      const wasRunning = this.data?.job?.status === "running";
      this.data = await api.scraperTrials();
      const job = this.data.job;
      if (wasRunning && job?.status === "done" && job.trial_id) this.trialId = job.trial_id;
      const usable = this.data.candidates.filter((c) => c.available);
      if (!usable.some((c) => c.key === this.candidate)) this.candidate = usable[0]?.key || null;
      if (!this.data.trials.some((t) => t.id === this.trialId)) this.trialId = this.data.trials[0]?.id || null;
      this.trial = this.trialId ? await api.scraperTrial(this.trialId) : null;
    } catch (err) {
      this.error = err.message;
    }
    this.render();
    if (this.data?.job?.status === "running") this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  async act(fn) {
    this.error = null;
    try { await fn(); } catch (err) { this.error = err.message; }
    await this.load();
  }

  render() {
    const d = this.data;
    if (!d) {
      this.root.replaceChildren(card({ title: "Scraper trials", children: [this.error ? errorBox(this.error) : null] }));
      return;
    }
    this.root.replaceChildren(this.renderStart(), this.renderTried(), this.renderRecord(), this.trial ? this.renderTrial() : null);
  }

  renderStart() {
    const d = this.data;
    const running = d.job?.status === "running";
    const chosen = d.candidates.find((c) => c.key === this.candidate);
    const pick = h("select", { "aria-label": "Candidate", style: "width:auto", disabled: running,
      onchange: (e) => { this.candidate = e.target.value; this.render(); } },
    d.candidates.map((c) => h("option", { value: c.key, selected: c.key === this.candidate ? "" : null, disabled: c.available ? null : "" },
      c.available ? c.label : `${c.label} (not installed)`)));
    const noteInput = h("input", { type: "text", value: this.note, placeholder: "Why you're trying it (optional)",
      "aria-label": "Note", style: "min-width:260px;flex:1", oninput: (e) => { this.note = e.target.value; } });
    const children = [
      note("Fetches and extracts the same pages with today's scraper and with a candidate library, then scores both: "
        + "pages fetched, text found, boilerplate, the evaluation answers' facts on their pages and keyword search. "
        + "**No model is called.** The pages: the latest ingestion plan's, every source's home page and the pages "
        + "evaluation questions name. A trial takes a few minutes (each fetch waits politely)."),
      d.candidates.length ? null : note("No library is waiting to be tried. To try one, add it to "
        + "`requirements-trials.txt` and as a candidate in `app/scraper_trials.py`, then build the image with "
        + "`TRIAL_LIBRARIES=1`."),
      d.candidates.length ? h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" }, pick, noteInput,
        h("button", { class: "btn", type: "button", disabled: running || !this.candidate ? "" : null,
          onclick: () => this.act(() => api.scraperTrialStart(this.candidate, this.note).then(() => { this.note = ""; })) },
        "Run trial")) : null,
      chosen ? h("p", { class: "note" }, `${chosen.about} Installed: ${versions(chosen.versions)}.`) : null,
      this.error ? errorBox(this.error) : null,
    ];
    if (running) {
      const j = d.job;
      children.push(waiting(j.phase === "fetching" ? `Fetching: ${j.done} of ${j.total} pages…`
        : j.phase === "scoring" ? "Scoring both sides…" : "Choosing pages…"));
    } else if (d.job?.status === "failed") {
      children.push(errorBox(`The last trial didn't finish: ${d.job.error}`));
    }
    return card({ title: "Try a library", children });
  }

  renderTried() {
    const tried = this.data.tried || [];
    if (!tried.length) return null;
    return card({ title: "Tried and removed", sub: "Libraries already tried against today's scraper and taken out again.", children: [
      dataTable([{ key: "library", label: "Library" }, { key: "tried", label: "Tried" }, { key: "candidate", label: "As" },
        { key: "verdict", label: "Verdict" }, { key: "summary", label: "What it did" }],
      tried.map((t) => ({ library: `${t.library} ${t.version}`, tried: t.tried, candidate: t.candidate,
        verdict: h("span", {}, verdictBadge(t.verdict), " ", badge(t.outcome, "neutral")), summary: t.summary }))),
    ] });
  }

  renderRecord() {
    const trials = this.data.trials;
    if (!trials.length) {
      return card({ title: "Record", children: [h("div", { class: "empty-state" }, "No trials yet: run one above.")] });
    }
    const rows = trials.map((t) => h("tr", { class: t.id === this.trialId ? "trial-on" : null, style: "cursor:pointer",
      onclick: () => { this.trialId = t.id; this.load(); } },
    h("td", {}, fmtTimestamp(t.created_at)),
    h("td", {}, t.label, h("div", { class: "muted" }, versions(t.versions))),
    h("td", {}, verdictBadge(t.verdict)),
    h("td", { class: "num" }, change("fetch_rate", t.deltas.fetch_rate)),
    h("td", { class: "num" }, change("facts", t.deltas.facts)),
    h("td", { class: "num" }, change("hit_at_5", t.deltas.hit_at_5)),
    h("td", { class: "num" }, change("boilerplate", t.deltas.boilerplate)),
    h("td", { class: "num" }, `${t.pages} / ${t.questions}`),
    h("td", {}, t.note || "—", t.started_by ? h("div", { class: "muted" }, t.started_by) : null)));
    return card({ title: "Record", sub: "Every trial, newest first. Click one to see it in full.", children: [
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["When", "Candidate", "Verdict", "Fetched", "Facts", "Search hit", "Boilerplate",
          "Pages / questions", "Note"].map((label, i) => h("th", { class: i >= 3 && i <= 7 ? "num" : null }, label)))),
        h("tbody", {}, rows))),
    ] });
  }

  renderTrial() {
    const t = this.trial;
    const scores = dataTable(
      [{ key: "score", label: "Score" }, { key: "before", label: "Before (today)", num: true },
        { key: "after", label: "After (candidate)", num: true }, { key: "change", label: "Change", num: true },
        { key: "means", label: "What it means" }],
      SCORES.map(([key, label, fmt, , means]) => ({ score: label, before: fmt(t.baseline[key]), after: fmt(t.result[key]),
        change: change(key, t.deltas[key]), means })));
    // Pages that changed: fetched on one side only, or text that differs a lot.
    const changed = t.per_page.filter((p) => Boolean(p.before.html_bytes) !== Boolean(p.after.html_bytes)
      || (p.agreement !== null && p.agreement < 0.8));
    const pageState = (s) => (s.error ? h("span", { class: "trial-down" }, s.error) : `${s.words} words`);
    const pages = changed.length
      ? dataTable([{ key: "url", label: "Page" }, { key: "before", label: "Before" }, { key: "after", label: "After" },
        { key: "same", label: "Same words", num: true }],
      changed.map((p) => ({ url: h("a", { href: p.url, target: "_blank", rel: "noopener" }, shortUrl(p.url)),
        before: pageState(p.before), after: pageState(p.after), same: pct(p.agreement) })))
      : note("No page changed much: every page was fetched on both sides with mostly the same words.");
    const failed = t.per_page.filter((p) => p.before.error && p.after.error);
    const rank = (r) => (r ? `#${r}` : "missed");
    const questions = t.per_question.length
      ? dataTable([{ key: "q", label: "Question" }, { key: "facts", label: "Facts before → after", num: true },
        { key: "rank", label: "Search before → after", num: true }],
      t.per_question.map((q) => ({ q: h("span", {}, q.question, h("div", { class: "muted" }, shortUrl(q.source))),
        facts: `${pct(q.before.facts)} → ${pct(q.after.facts)}`, rank: `${rank(q.before.rank)} → ${rank(q.after.rank)}` })))
      : note("No evaluation question names one of these pages, so facts and search weren't scored. "
        + "Prepare the setup evaluation, or write a search test set, to score them.");
    return card({ title: `${t.label} · ${fmtTimestamp(t.created_at)}`, children: [
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px;flex-wrap:wrap" }, verdictBadge(t.verdict),
        h("span", { class: "muted" }, `${t.pages} pages, ${t.questions} questions, ${Math.round(t.seconds)} s · ${versions(t.versions)}`),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", style: "margin-left:auto",
          onclick: () => this.act(() => api.scraperTrialDelete(t.id).then(() => { this.trialId = null; })) }, "Delete trial")),
      t.note ? note(t.note) : null,
      note("**Helped**: a score that matters improved and none got worse. **Made it worse**: any got worse "
        + "(by 1 point for pages, 2 points for the rest). Fetch time is shown but never decides."),
      scores,
      h("h3", {}, `Pages that changed (${changed.length})`), pages,
      failed.length ? note(`${failed.length} page${failed.length === 1 ? "" : "s"} failed on both sides, e.g. `
        + `${shortUrl(failed[0].url)}: ${failed[0].after.error}.`) : null,
      h("h3", {}, "Questions"), questions,
    ] });
  }
}

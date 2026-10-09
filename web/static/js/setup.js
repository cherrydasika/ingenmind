// Set up your knowledge system: the Initialization Agent's conversation
// (app/initialization/), the six steps, and what it has learnt so far.
// Later steps (blueprint, sources, content, build) arrive with epic #19.

import { api } from "./api.js";
import { KIND_LABEL, areaTable } from "./eval_areas.js";
import { badge, errorBox, fmtTime, h, loading, markdown, md, note } from "./ui.js";

const FIELDS = [
  ["purpose", "Purpose"], ["audience", "Who asks"], ["regions", "Regions"], ["question_types", "Questions"],
  ["live_information", "Live information (answered by tools)"], ["organisations", "Organisations"],
  ["topics_out_of_scope", "Out of scope"], ["authority", "Sources must be"], ["language", "Language"],
];
const CONVERSING = ["NEW", "DISCOVERING_DOMAIN", "CLARIFYING"];
const CHOOSING_SOURCES = ["DISCOVERING_SOURCES", "AWAITING_SOURCE_SELECTION"];
const AUTHORITY = { high: "ok", medium: "warn", low: "err" };
const PAGES_SHOWN = 200;   // of a section's pages, when it is opened
const BUILDING = ["INGESTING", "INDEXING"];
const PAGE_STATUS = { pending: "waiting", ingested: "read", unchanged: "unchanged", skipped: "no text",
  failed: "failed", blocked: "not allowed" };
const POLL_MS = 4000;   // while the blueprint research runs
const RUNNING = ["queued", "running"];   // an evaluation run that is not finished
// How each knowledge area is answered (blueprint.py KnowledgeClass).
const ANSWERED_BY = [
  ["STATIC_KNOWLEDGE", "From the knowledge base", "Stable facts that pages hold"],
  ["STRUCTURED_DATA", "From datasets", "Tables and files, ingested on a schedule"],
  ["DYNAMIC_KNOWLEDGE", "From live tools", "Changes constantly: answered by live tools, not stored"],
  ["EXTERNAL_TOOL_API", "From live tools", "Changes constantly: answered by live tools, not stored"],
];

// Readiness (readiness.py): every score is a share of 0–1, but groundedness is a mean score.
const GAP_LABEL = { no_source: "no source", no_content: "no content", few_pages: "few pages",
  failing_question: "question missed", live_unanswered: "no live tool", not_refused: "not refused" };
const pctText = (v) => `${Math.round(v * 100)}%`;
const scoreText = (key, v) => (v === null || v === undefined ? "not measured yet"
  : key === "groundedness" ? Number(v).toFixed(2) : pctText(v));

// The app shell refreshes its "being set up" notice on this.
const setupChanged = () => window.dispatchEvent(new Event("rag:setup-changed"));

export class SetupPage {
  title = "Set up your knowledge system";

  constructor(root) {
    this.root = root;
    this.busy = false;
    this.error = null;
    this.pending = null;   // the user's message while the agent answers
    this.changing = false; // the blueprint's "Change something" box is open
    this.opened = {};      // content_id → the section with its pages, while open
    this.review = null;    // the ingestion plan under review (before Build RAG)
    this.confirmingLive = false;   // Go live with gaps: asking to confirm them
    this.timer = null;
  }

  unmount() {
    clearTimeout(this.timer);
  }

  async mount() {
    this.root.replaceChildren(loading());
    try {
      this.view = await api.setup();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load setup: ${err.message}`));
      return;
    }
    this.render();
  }

  async send(text) {
    if (this.busy || !text.trim()) return;
    this.busy = true;
    this.error = null;
    this.pending = text.trim();
    this.render();
    try {
      const before = this.view.state;
      this.view = await api.setupMessage(this.pending);
      this.draft = "";
      if (this.view.state !== before) setupChanged();
    } catch (err) {
      this.error = err.message;
      this.draft = text;     // kept, to send again
      await this.refresh();  // the turn may have been saved before the agent failed
    }
    this.busy = false;
    this.pending = null;
    this.render();
  }

  async refresh() {
    try { this.view = await api.setup(); } catch { /* keep what is shown */ }
  }

  async confirm(button) {
    button.disabled = true;
    this.error = null;
    try {
      this.view = await api.setupConfirm();
      setupChanged();
    } catch (err) {
      this.error = err.message;
    }
    this.render();
  }

  render() {
    const v = this.view;
    const conversing = CONVERSING.includes(v.state);
    const onBlueprint = v.state === "DOMAIN_READY";
    const pastBlueprint = !conversing && !onBlueprint;
    const onContent = Boolean(v.content);
    if (v.state !== "AWAITING_CONTENT_SELECTION") this.review = null;
    const main = conversing ? this.renderConversation(v) : onBlueprint ? this.renderBlueprint(v)
      : v.state === "READY" && v.readiness?.went_live ? this.renderLive(v)
      : v.build ? this.renderBuild(v) : this.review ? this.renderReview(v)
      : onContent ? this.renderContent(v) : this.renderSources(v);
    const side = onContent ? this.renderContentCoverage(v)
      : pastBlueprint && v.sources ? this.renderCoverage(v) : this.renderLearnt(v);
    this.root.replaceChildren(h("div", { class: "stack" },
      this.renderSteps(v),
      h("div", { class: "setup-layout" }, main, side),
      v.state === "EVALUATING" && v.build ? this.renderEvaluation(v) : null,
      v.state === "EVALUATING" && v.readiness ? this.renderReadiness(v) : null,
      v.build || this.review ? h("details", {}, h("summary", {}, "Content"),
        h("div", { class: "details-body" }, this.renderContent(v))) : null,
      onContent && v.sources ? h("details", {}, h("summary", {}, "Sources"),
        h("div", { class: "details-body" }, this.renderSources(v))) : null,
      pastBlueprint && v.blueprint?.blueprint ? h("details", {}, h("summary", {}, `Blueprint (version ${v.blueprint.version})`),
        h("div", { class: "details-body" }, this.renderOutline(v.blueprint.blueprint, v.blueprint.research))) : null,
      conversing ? null : h("details", {}, h("summary", {}, "Conversation"),
        h("div", { class: "details-body" }, this.renderTurns(v)))));
    this.poll(v);
    const box = this.root.querySelector("textarea");
    if (box && !this.busy) box.focus();
    const chat = this.root.querySelector(".chat");
    if (chat) chat.scrollTop = chat.scrollHeight;
  }

  renderSteps(v) {
    const at = v.steps.findIndex((s) => s.key === v.step);
    return h("ol", { class: "setup-steps", "aria-label": "Setup steps" }, v.steps.map((s, i) =>
      h("li", { class: i < at ? "done" : i === at ? "current" : "", "aria-current": i === at ? "step" : null },
        h("span", { class: "setup-step-n" }, i < at ? "✓" : String(i + 1)), s.label)));
  }

  // While research or discovery runs, ask again every few seconds.
  poll(v) {
    clearTimeout(this.timer);
    const researching = v.state === "DOMAIN_READY" && (!v.blueprint || v.blueprint.status === "researching");
    const discovering = CHOOSING_SOURCES.includes(v.state)
      && (v.state === "DISCOVERING_SOURCES" && v.sources?.run?.status !== "failed" || v.sources?.run?.status === "researching");
    const analysing = v.content && (v.state === "ANALYSING_SOURCES"
      || v.content.sites.some((site) => site.analysis?.status === "analysing"));
    const building = BUILDING.includes(v.state) && ["queued", "running"].includes(v.build?.job?.status);
    const e = v.evaluation;
    const evaluating = v.state === "EVALUATING" && (!e || e.status === "preparing" || RUNNING.includes(e.run?.status));
    if (researching || discovering || analysing || building || evaluating) {
      this.timer = setTimeout(async () => { await this.refresh(); this.render(); }, POLL_MS);
    }
  }

  renderContent(v) {
    const c = v.content;
    const choosing = v.state === "AWAITING_CONTENT_SELECTION";
    const names = Object.fromEntries((v.blueprint?.blueprint?.knowledge_areas || []).map((a) => [a.key, a.name]));
    const children = [];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    children.push(h("p", {}, v.state === "ANALYSING_SOURCES"
      ? "Reading each site you chose: its robots.txt, its sitemap or menus, and how its pages group into sections. "
        + "Nothing is read into the knowledge base yet."
      : "Tick the sections to read into the knowledge base; open a section to untick single pages. Recommended "
        + "sections are suggestions."));
    for (const site of c.sites) children.push(this.renderSite(site, names, choosing));
    if (choosing) {
      const back = h("button", { class: "btn btn-ghost", type: "button" }, "Change the sources");
      back.addEventListener("click", () => this.act(() => api.setupBackToSources(), back));
      children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:12px" },
        h("strong", {}, `${c.pages_chosen.toLocaleString()} page${c.pages_chosen === 1 ? "" : "s"} chosen`), back),
        this.reviewButton(c));
    }
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Content")),
      children);
  }

  reviewButton(c) {
    const button = h("button", { class: "btn btn-primary", type: "button", disabled: !c.pages_chosen },
      "Review the plan");
    button.addEventListener("click", async () => {
      button.disabled = true;
      this.error = null;
      try { this.review = await api.setupPlan(); } catch (err) { this.error = err.message; }
      this.render();
      window.scrollTo({ top: 0 });
    });
    return h("div", { style: "margin-top:12px" }, button,
      note("The plan lists every page to read, how often each section is refreshed, and what changes. Nothing is "
        + "read until you choose Build RAG."));
  }

  // The ingestion plan, before Build RAG.
  renderReview(v) {
    const r = this.review;
    const t = r.plan.totals;
    const children = [];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    children.push(h("p", {}, `Plan version ${r.plan.version}: `, h("strong", {}, `${t.pages.toLocaleString()} pages`),
      ` from ${t.sections} section${t.sections === 1 ? "" : "s"} of ${t.sites} site${t.sites === 1 ? "" : "s"}`
      + `${t.pdfs ? `, ${t.pdfs} PDFs` : ""}; reading them takes about ${Math.max(t.minutes, 1)} minute`
      + `${t.minutes > 1 ? "s" : ""}.`));
    if (r.problem) children.push(h("div", { class: "alert err" }, `Build RAG needs a change: ${r.problem}.`));
    const d = r.differences;
    if (d) {
      children.push(h("div", { class: "alert warn" }, `Against the plan built before (version ${d.against}): `
        + `${d.added} page${d.added === 1 ? "" : "s"} added, ${d.removed} removed`
        + (d.removed ? `; the removed pages' ${d.removed_chunks ?? "stored"} chunks leave the knowledge base` : "")
        + "."));
    }
    if (r.uncovered.length) {
      children.push(h("div", { class: "alert warn" }, `No chosen content covers: ${r.uncovered.join(", ")}. `
        + "Answers about these will be weak unless you add content for them."));
    }
    const names = Object.fromEntries((v.blueprint?.blueprint?.knowledge_areas || []).map((a) => [a.key, a.name]));
    children.push(h("div", { class: "table-wrap" }, h("table", { class: "plan-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Site"), h("th", {}, "Section"), h("th", { class: "num" }, "Pages"),
        h("th", {}, "Refresh every"), h("th", {}, "Covers"))),
      h("tbody", {}, r.sections.map((g) => h("tr", {},
        h("td", {}, g.site), h("td", {}, g.section),
        h("td", { class: "num" }, g.pages.toLocaleString(), g.pdfs ? h("span", { class: "note" }, ` (${g.pdfs} PDF)`) : null),
        h("td", {}, this.ttlInput(g)),
        h("td", { class: "note" }, (g.areas || []).map((k) => names[k] || k).join(" · "))))))));
    const buildRag = h("button", { class: "btn btn-primary", type: "button", disabled: Boolean(r.problem) }, "Build RAG");
    buildRag.addEventListener("click", () => this.act(() => api.setupBuildRag(r.plan.version), buildRag));
    const back = h("button", { class: "btn btn-ghost", type: "button" }, "Back to the content");
    back.addEventListener("click", () => { this.review = null; this.error = null; this.render(); });
    children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:12px" }, buildRag, back),
      note("Build RAG records that you approved this plan, then reads its pages politely in the background, "
        + "checking each site's robots.txt again. Every piece of the knowledge base keeps a link to this plan."));
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Ingestion plan")),
      children);
  }

  ttlInput(g) {
    const input = h("input", { type: "number", min: 1, max: 365, value: g.ttl_days, class: "input",
      "aria-label": `Refresh ${g.section} every so many days` });
    input.addEventListener("change", async () => {
      const days = Number(input.value);
      this.error = null;
      try {
        this.review = await api.setupPlanTtl(g.content_id, days === g.ttl_suggested ? null : days);
      } catch (err) {
        this.error = err.message;
      }
      this.render();
    });
    const suggested = g.ttl_suggested && g.ttl_days !== g.ttl_suggested
      ? h("span", { class: "note" }, ` (suggested ${g.ttl_suggested})`) : null;
    return h("span", {}, input, " days", suggested);
  }

  // Build and evaluate: progress of the approved plan, failures, the check after indexing.
  renderBuild(v) {
    const b = v.build;
    const children = [];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    if (!b.plan) return h("div", { class: "card" }, children, loading("Loading the build…"));
    const progress = b.progress || {};
    const total = Object.values(progress).reduce((a, n) => a + n, 0);
    const done = total - (progress.pending || 0);
    const job = b.job;
    const stuck = BUILDING.includes(v.state) && (!job || job.status === "failed" || job.status === "success");
    const lines = {
      INGESTION_APPROVED: "The plan is approved, but its build has not started (another ingestion job may have "
        + "been running).",
      INGESTING: stuck ? "The build stopped before it finished." : "Reading the plan's pages, politely, one at a time…",
      INDEXING: stuck ? "The build stopped while checking the index." : "Checking the index…",
      EVALUATING: "The knowledge base is built. Next, check how well it answers: see Evaluation below.",
    };
    children.push(h("p", {}, lines[v.state] || ""));
    if (job?.status === "failed" && job.error) children.push(h("div", { class: "alert err" }, `The job failed: ${job.error}`));
    children.push(h("div", { class: "build-bar", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": total,
      "aria-valuenow": done }, h("span", { style: `width:${total ? (100 * done) / total : 0}%` })),
    h("div", {}, h("strong", {}, `${done.toLocaleString()} of ${total.toLocaleString()} pages`), " · ",
      Object.entries(progress).map(([k, n]) => `${n} ${PAGE_STATUS[k] || k}`).join(", "),
      b.chunks ? ` · ${b.chunks.toLocaleString()} chunks written` : ""),
    h("div", { class: "note" }, `Plan version ${b.plan.version}, approved ${String(b.plan.approved_at).slice(0, 16).replace("T", " ")}`));
    const check = b.plan.build;
    if (check) {
      children.push(h("div", { class: check.missing.length ? "alert warn" : "alert ok", style: "margin-top:10px" },
        `Index check: ${check.pages_with_chunks} page${check.pages_with_chunks === 1 ? "" : "s"} with `
        + `${check.chunks.toLocaleString()} chunks`
        + (check.missing.length ? `; ${check.missing.length} read but without chunks.` : "; every page read has chunks.")));
    }
    if (b.failures?.length) {
      children.push(h("details", { open: true }, h("summary", {}, `Pages not read (${b.failures.length})`),
        h("ul", { class: "details-body" }, b.failures.map((f) => h("li", {},
          h("a", { href: f.url, target: "_blank", rel: "noopener" }, new URL(f.url).pathname), " ",
          badge(PAGE_STATUS[f.status] || f.status, f.status === "blocked" ? "neutral" : "err"),
          h("span", { class: "note" }, ` ${f.section} · ${f.error || ""}`))))));
    }
    const actions = [];
    if (v.state === "INGESTION_APPROVED") {
      const start = h("button", { class: "btn btn-primary", type: "button" }, "Start the build");
      start.addEventListener("click", () => this.act(() => api.setupBuildStart(b.plan.version), start));
      actions.push(start);
    }
    const failed = progress.failed > 0;
    if ((stuck || failed) && v.state !== "INGESTION_APPROVED" && !["queued", "running"].includes(job?.status)) {
      const retry = h("button", { class: "btn", type: "button" }, failed ? "Retry failed pages" : "Carry on");
      retry.addEventListener("click", () => this.act(() => api.setupBuildRetry(), retry));
      actions.push(retry);
    }
    if (!["queued", "running"].includes(job?.status)) {
      const back = h("button", { class: "btn btn-ghost", type: "button" }, "Change the content");
      back.addEventListener("click", () => this.act(() => api.setupBackToContent(), back));
      actions.push(back);
    }
    if (actions.length) children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:12px" }, actions));
    const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Build")),
      children);
    return card;
  }

  // Readiness (#17): the scores, each defined and computed; the gaps and what to do; Go live.
  renderReadiness(v) {
    const r = v.readiness;
    const children = [h("p", {}, "How well the knowledge system covers what the blueprint says it should. Every "
      + "score is computed from the build and the evaluation; none is a model's opinion.")];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    if (!r.evaluation.current) {
      children.push(h("div", { class: "alert warn" }, "The scores from the evaluation need a finished run of the "
        + "current plan's questions: run the evaluation above."));
    }
    children.push(h("div", { class: "readiness-scores" }, r.scores.map((s) => h("div", { class: "readiness-score" },
      h("div", { class: "readiness-label" }, s.label),
      h("div", { class: "readiness-value" }, scoreText(s.key, s.value)),
      s.numbers ? h("div", { class: "note" }, `${s.numbers[0]} of ${s.numbers[1]}`) : null,
      h("div", { class: "note" }, s.definition)))));
    children.push(h("div", { class: `alert ${r.overall.value === null ? "" : r.overall.value >= 0.8 ? "ok" : "warn"}`,
      style: "margin-top:12px" },
    h("strong", {}, `Overall: ${r.overall.value === null ? "not measured yet" : pctText(r.overall.value)}`),
    h("div", { class: "note" }, r.overall.worked ? `${r.overall.formula} ${r.overall.worked}` : r.overall.formula)));
    children.push(this.renderGaps(r));
    children.push(this.renderGoLive(r));
    return h("div", { class: "card", id: "readiness-card" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Readiness")), children);
  }

  renderGaps(r) {
    if (!r.gaps.length) return h("p", {}, "No gaps: every knowledge area has a source, enough pages, and its "
      + "questions did what they should.");
    const actions = {
      review_sources: ["Review sources", () => api.setupBackToSources()],
      review_content: ["Review content", () => api.setupBackToContent()],
      review_blueprint: ["Review the blueprint", () => api.setupBack()],
      run_again: ["Run the evaluation", null],
    };
    return h("div", { style: "margin-top:16px" }, h("h3", { style: "margin:0 0 4px" }, `Gaps (${r.gaps.length})`),
      h("ul", { class: "gap-list" }, r.gaps.map((g) => {
        const action = actions[g.action];
        let button = null;
        if (action && action[1]) {
          button = h("button", { class: "btn btn-sm btn-ghost", type: "button" }, action[0]);
          button.addEventListener("click", () => this.act(async () => { await action[1](); return api.setup(); }, button));
        } else if (action) {
          button = h("button", { class: "btn btn-sm btn-ghost", type: "button" }, action[0]);
          button.addEventListener("click", () => document.getElementById("evaluation-card")?.scrollIntoView({ behavior: "smooth" }));
        }
        return h("li", {}, h("div", {}, h("strong", {}, g.name), " ", badge(GAP_LABEL[g.kind] || g.kind, "warn")),
          g.question ? h("div", {}, `“${g.question}”: ${g.detail}`) : h("div", {}, g.detail),
          h("div", { class: "note" }, g.suggestion), button);
      })),
      h("p", { class: "note" }, "Going back to the sources or the content keeps your choices. After a rebuild the "
        + "evaluation is prepared again for the new plan."));
  }

  renderGoLive(r) {
    const e = r.evaluation;
    const children = [];
    if (this.confirmingLive) {
      const yes = h("button", { class: "btn btn-primary", type: "button" }, `Go live with ${r.gaps.length} gap${r.gaps.length === 1 ? "" : "s"}`);
      yes.addEventListener("click", () => this.goLive(true, yes));
      const no = h("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
      no.addEventListener("click", () => { this.confirmingLive = false; this.render(); });
      children.push(h("div", { class: "alert warn" }, h("strong", {}, "Go live with these gaps?"),
        h("div", {}, `Users will get answers from the assistant that was evaluated (${e.flow_id} v${e.flow_version}). `
          + "The flow live now stays in its History in the flow builder, so it can be put back."),
        h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:8px" }, yes, no)));
      return h("div", { style: "margin-top:16px" }, children);
    }
    const go = h("button", { class: "btn btn-primary", type: "button", disabled: !r.can_go_live }, "Go live");
    go.addEventListener("click", () => {
      if (r.gaps.length) { this.confirmingLive = true; this.render(); } else this.goLive(false, go);
    });
    children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:16px" }, go,
      h("span", { class: "note" }, r.can_go_live
        ? `Makes ${e.flow_id} v${e.flow_version}, the assistant the evaluation measured, what users get.`
        : "Run the evaluation of the current plan first: what goes live is what was measured.")));
    return h("div", {}, children);
  }

  async goLive(confirmGaps, button) {
    await this.act(async () => { await api.setupGoLive(confirmGaps); return api.setup(); }, button);
    this.confirmingLive = false;
    this.render();
  }

  // Live (#17): what went live, when, and the report at that moment.
  renderLive(v) {
    const w = v.readiness.went_live;
    const labels = Object.fromEntries(v.readiness.scores.map((s) => [s.key, s.label]));
    const back = (label, call) => {
      const button = h("button", { class: "btn btn-ghost", type: "button" }, label);
      button.addEventListener("click", () => this.act(async () => { await call(); return api.setup(); }, button));
      return button;
    };
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Live")),
      this.error ? h("div", { class: "alert err" }, this.error) : null,
      h("div", { class: "alert ok" }, h("strong", {}, "The knowledge system is live."),
        h("div", {}, `Since ${String(w.at).slice(0, 16).replace("T", " ")}: users get answers from `,
          h("code", {}, `${w.flow_id} v${w.flow_version}`), ` (it replaced ${w.replaced.flow_id} v${w.replaced.version}).`)),
      h("p", {}, `When it went live: overall ${w.overall === null ? "—" : pctText(w.overall)}, with `
        + `${w.gaps.length} gap${w.gaps.length === 1 ? "" : "s"}.`),
      h("ul", {}, Object.entries(w.scores).map(([k, value]) => h("li", {}, `${labels[k] || k}: ${scoreText(k, value)}`))),
      h("p", { class: "note" }, "To improve it, go back to the sources or the content: your choices are kept. "
        + "Users can't ask questions until it goes live again."),
      h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px" },
        back("Review sources", () => api.setupBackToSources()), back("Review content", () => api.setupBackToContent())));
  }

  // Evaluation (#16): the set written from the blueprint, its run on the candidate flow, results per area.
  renderEvaluation(v) {
    const e = v.evaluation;
    const head = h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Evaluation"));
    const wrap = (...children) => h("div", { class: "card", id: "evaluation-card" }, head, ...children);
    const intro = h("p", {}, "Questions written from the blueprint and the pages you built, for every knowledge "
      + "area, plus questions it should refuse. They run on the assistant the blueprint describes, before it goes "
      + "live. Evaluations never add pages to the knowledge base.");
    if (!e || e.status === "preparing") {
      return wrap(intro, h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px" },
        h("div", { class: "spinner" }), h("span", { class: "note" },
          "Writing the questions: one model call per knowledge area…")));
    }
    const prepare = h("button", { class: "btn btn-ghost", type: "button" }, "Write the questions again");
    prepare.addEventListener("click", () => this.act(async () => { await api.setupEvaluationPrepare(); return api.setup(); },
      prepare));
    if (e.status === "failed") {
      prepare.className = "btn btn-primary";
      return wrap(intro, h("div", { class: "alert err" }, `Writing the questions didn't finish: ${e.error}`),
        h("div", { class: "toolbar", style: "justify-content:flex-start" }, prepare));
    }
    const areas = e.record?.areas || {};
    const names = Object.fromEntries(Object.entries(areas).map(([k, a]) => [k, a.name]));
    const run = e.run;
    const running = RUNNING.includes(run?.status);
    const children = [intro];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    children.push(h("div", { class: "note" }, `${e.questions.length} questions · assistant `,
      h("code", {}, `${e.flow_id} v${e.flow_version}`), " (not live) · ",
      h("a", { href: "#/evaluations" }, "edit the questions on the Evaluations page")));
    children.push(this.renderEvaluationSet(e, names));
    if (running) {
      children.push(h("div", { class: "build-bar", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": run.total,
        "aria-valuenow": run.completed }, h("span", { style: `width:${run.total ? (100 * run.completed) / run.total : 0}%` })),
      h("div", {}, h("strong", {}, `${run.completed} of ${run.total} questions answered`),
        h("span", { class: "note" }, " · one at a time, each a full answer")));
    } else {
      const start = h("button", { class: "btn btn-primary", type: "button", disabled: !e.questions.length },
        run ? "Run the evaluation again" : "Run the evaluation");
      start.addEventListener("click", () => this.act(async () => { await api.setupEvaluationRun(); return api.setup(); },
        start));
      children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px;margin-top:12px" }, start,
        prepare, h("span", { class: "note" }, `About ${Math.max(1, e.estimate.minutes)} min of real agent and model `
          + `calls (${e.estimate.questions} questions).`)));
    }
    if (run && run.summary?.areas) children.push(this.renderEvaluationResults(run, names));
    return wrap(...children);
  }

  renderEvaluationSet(e, names) {
    const groups = {};
    e.questions.forEach((q, i) => { (groups[q.area || "other"] ||= []).push([q, i]); });
    const dropped = e.record?.dropped || [];
    return h("div", { class: "stack" },
      h("details", {}, h("summary", {}, `The questions (${e.questions.length}, by knowledge area)`),
        h("div", { class: "details-body" }, Object.entries(groups).map(([area, items]) => h("div", { class: "eval-set-area" },
          h("strong", {}, names[area] || area.replace(/_/g, " ")), " ",
          badge(KIND_LABEL[items[0][0].kind] || items[0][0].kind || "question", items[0][0].kind === "answer" ? "info" : "neutral"),
          h("ol", {}, items.map(([q]) => h("li", {}, q.question,
            q.expected_source ? h("div", { class: "note" }, "From ",
              h("a", { href: q.expected_source, target: "_blank", rel: "noopener" }, new URL(q.expected_source).pathname)) : null))))))),
      dropped.length ? h("details", {}, h("summary", {}, `Questions left out (${dropped.length})`),
        h("ul", { class: "details-body" }, dropped.map((d) => h("li", {},
          d.question ? `“${d.question}”` : names[d.area] || d.area, h("span", { class: "note" },
            ` · ${names[d.area] || d.area} · ${d.reason}`))))) : null);
  }

  renderEvaluationResults(run, names) {
    const s = run.summary;
    const outcome = (r) => r.failed ? ["didn't run", "err"] : r.expectation_met ? ["as expected", "ok"] : ["missed", "err"];
    return h("div", { class: "stack", style: "margin-top:16px" },
      h("h3", {}, `Results · ${String(run.finished_at || run.created_at).slice(0, 16).replace("T", " ")}`),
      h("div", {}, h("strong", {}, `${s.expectations_met} of ${s.expectations} as expected`),
        h("span", { class: "note" }, ` · ${s.input_blocked + s.output_blocked} refused · median ${s.p50_seconds === null ? "—"
          : fmtTime(s.p50_seconds)} per question · ${Math.round((s.tokens || 0) / 1000)}k tokens`)),
      areaTable(s.areas, names),
      h("details", {}, h("summary", {}, "Each question"),
        h("ul", { class: "details-body eval-outcomes" }, run.results.map((r) => {
          const q = run.questions[r.idx] || {};
          const [label, kind] = outcome(r);
          return h("li", {}, badge(label, kind), " ", q.question || `Question ${r.idx + 1}`,
            h("div", { class: "note" }, [names[q.area] || q.area, KIND_LABEL[q.kind] || q.kind,
              r.input_blocked || r.output_blocked ? "refused" : r.answer_type?.replace("_", " "),
              q.expected_source ? (r.expected_retrieved ? "page found" : "page not found") : null,
              q.expected_source && r.expected_retrieved ? (r.expected_cited ? "and cited" : "but not cited") : null,
              r.live_tools?.length ? `tools: ${r.live_tools.join(", ")}` : null].filter(Boolean).join(" · ")),
            r.answer ? h("details", {}, h("summary", {}, "Answer"),
              h("div", { class: "details-body eval-answer", html: markdown(r.answer) })) : null);
        }))));
  }

  renderSite(site, names, choosing) {
    const a = site.analysis;
    const head = h("div", { class: "content-site-head" }, h("strong", {}, site.name), " ",
      h("a", { href: site.base_url, target: "_blank", rel: "noopener", class: "note" }, site.host));
    if (!a || a.status === "analysing") {
      return h("div", { class: "content-site" }, head, h("div", { class: "toolbar", style: "justify-content:flex-start;gap:10px" },
        h("div", { class: "spinner" }), h("span", { class: "note" }, "Reading the site…")));
    }
    if (a.status !== "ready") {
      const again = h("button", { class: "btn btn-sm btn-ghost", type: "button" }, "Analyse again");
      again.addEventListener("click", () => this.act(() => api.setupContentAnalyse(site.source_id), again));
      return h("div", { class: "content-site" }, head,
        h("div", { class: "alert err" }, a.status === "blocked" ? `This site can't be read: ${a.error}.`
          : `Reading this site didn't finish: ${a.error}`), choosing ? again : null);
    }
    const how = `${a.page_count.toLocaleString()} pages listed from its ${a.how === "sitemap" ? "sitemap" : "menus"}`
      + `${a.robots?.found ? ", following its robots.txt" : ""}`;
    const shown = site.sections.filter((sec) => sec.recommended || sec.status === "selected");
    const rest = site.sections.filter((sec) => !shown.includes(sec));
    return h("div", { class: "content-site" }, head, h("div", { class: "note" }, how),
      h("ul", { class: "source-list" }, shown.map((sec) => this.renderSection(sec, names, choosing))),
      rest.length ? h("details", {}, h("summary", {}, `${rest.length} more section${rest.length === 1 ? "" : "s"}, not recommended`),
        h("ul", { class: "source-list details-body" }, rest.map((sec) => this.renderSection(sec, names, choosing)))) : null);
  }

  renderSection(sec, names, choosing) {
    const box = h("input", { type: "checkbox", checked: sec.status === "selected", disabled: !choosing,
      "aria-label": `Read ${sec.name}` });
    box.addEventListener("change", () => this.act(() => api.setupContentChoose(sec.content_id,
      box.checked ? "selected" : "candidate")));
    const excluded = (sec.excluded_urls || []).length;
    const pages = excluded ? `${sec.url_count - excluded} of ${sec.url_count} pages` : `${sec.url_count} pages`;
    const open = this.opened[sec.content_id];
    const toggle = h("button", { class: "btn btn-sm btn-ghost", type: "button" }, open ? "Hide pages" : "Pages");
    toggle.addEventListener("click", async () => {
      if (open) { delete this.opened[sec.content_id]; this.render(); return; }
      try { this.opened[sec.content_id] = await api.setupContentSection(sec.content_id); } catch (err) { this.error = err.message; }
      this.render();
    });
    const remove = choosing && sec.status !== "selected"
      ? h("button", { class: "btn btn-sm btn-ghost", type: "button", title: "Hide this section" }, "Remove") : null;
    remove?.addEventListener("click", () => this.act(() => api.setupContentChoose(sec.content_id, "removed"), remove));
    const areas = (sec.areas || []).map((k) => names[k] || k);
    return h("li", { class: `source-item${sec.status === "selected" ? " selected" : ""}` },
      h("label", { class: "source-check" }, box),
      h("div", { class: "source-body" },
        h("div", {}, h("strong", {}, sec.name), " ", h("span", { class: "note" }, `${sec.path_prefix || ""} · ${pages}`), " ",
          sec.recommended ? badge("recommended", "info") : null,
          sec.large ? [" ", badge(`large: over ${this.view.content.large_section} pages`, "warn")] : null,
          sec.pdf_count ? [" ", badge(`${sec.pdf_count} PDF${sec.pdf_count === 1 ? "" : "s"}`, "neutral")] : null,
          (sec.flags || []).map((f) => [" ", badge(f, "neutral")])),
        h("div", { class: "note" }, sec.reason),
        areas.length ? h("div", { class: "note" }, `Covers: ${areas.join(" · ")}`) : null,
        open ? this.renderPages(open, choosing) : null),
      h("div", { class: "source-actions" }, toggle, remove));
  }

  renderPages(section, choosing) {
    const excluded = new Set(section.excluded_urls || []);
    const urls = section.urls.slice(0, PAGES_SHOWN);
    const set = async (url, keep) => {
      if (keep) excluded.delete(url); else excluded.add(url);
      try {
        const { section: updated, view } = await api.setupContentExclude(section.content_id, [...excluded]);
        this.opened[section.content_id] = updated;
        this.view = view;
      } catch (err) {
        this.error = err.message;
      }
      this.render();
    };
    return h("div", { class: "content-pages" },
      h("ul", {}, urls.map((u) => {
        const box = h("input", { type: "checkbox", checked: !excluded.has(u.url), disabled: !choosing,
          "aria-label": `Read ${u.url}` });
        box.addEventListener("change", () => set(u.url, box.checked));
        return h("li", {}, h("label", {}, box, " ", h("a", { href: u.url, target: "_blank", rel: "noopener" },
          new URL(u.url).pathname)), u.lastmod ? h("span", { class: "note" }, ` · ${u.lastmod.slice(0, 10)}`) : null);
      })),
      section.urls.length > PAGES_SHOWN ? note(`and ${section.urls.length - PAGES_SHOWN} more pages`) : null);
  }

  renderContentCoverage(v) {
    const c = v.content;
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Coverage")),
      h("p", { class: "note" }, "Each part of the blueprint and the chosen sections that cover it."),
      c.coverage.map((area) => h("div", { class: "setup-fact" }, h("div", { class: "metric-label" }, area.name),
        area.sections.length ? h("div", {}, area.sections.join(", ")) : h("div", { class: "note" }, "⚠ no chosen section yet"))),
      v.sources?.live_areas?.length ? h("div", { class: "setup-fact", style: "margin-top:12px" },
        h("div", { class: "metric-label" }, "Answered by live tools, not sources"),
        h("div", { class: "note" }, v.sources.live_areas.join(", "))) : null);
  }

  renderSources(v) {
    const sv = v.sources || { sources: [], run: null };
    const head = h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Sources"));
    const children = [];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    const running = sv.run?.status === "researching" || (v.state === "DISCOVERING_SOURCES" && !sv.run);
    if (running) {
      children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px" }, h("div", { class: "spinner" }),
        h("div", {}, h("strong", {}, "Looking for sources…"),
          h("div", { class: "note" }, "Searching for the sites that cover each part of the blueprint. Nothing is read "
            + "into the knowledge base yet."))));
      if (!sv.sources.length) return h("div", { class: "card" }, head, children);
    }
    if (sv.run?.status === "failed" && v.state === "DISCOVERING_SOURCES") {
      const retry = h("button", { class: "btn", type: "button" }, "Try again");
      retry.addEventListener("click", () => this.act(() => api.setupSourcesDiscover(), retry));
      children.push(h("div", { class: "alert err" }, `Looking for sources didn't finish: ${sv.run.error}`), retry);
      return h("div", { class: "card" }, head, children);
    }
    const choosing = v.state === "AWAITING_SOURCE_SELECTION";
    const names = Object.fromEntries((v.blueprint?.blueprint?.knowledge_areas || []).map((a) => [a.key, a.name]));
    const recommended = sv.sources.filter((s) => s.recommended);
    if (choosing && recommended.length) {
      children.push(h("p", {}, `I found ${sv.sources.length} sites. These ${recommended.length} look the most `
        + "authoritative for what the blueprint covers; tick the ones you trust. Nothing is read until later steps."));
    }
    // Once chosen, only the chosen sources are shown.
    const shown = choosing || running ? sv.sources : sv.sources.filter((src) => src.status === "selected");
    if (!choosing && !running) children.push(h("p", {}, `The ${shown.length} source${shown.length === 1 ? "" : "s"} you chose:`));
    children.push(h("ul", { class: "source-list" }, shown.map((src) => this.renderSource(src, names, choosing))));
    if (choosing) {
      const url = h("input", { type: "url", placeholder: "https://www.example.org", "aria-label": "Add a site",
        style: "flex:1" });
      const add = h("button", { class: "btn btn-ghost", type: "button" }, "Add a site");
      add.addEventListener("click", () => url.value.trim() && this.act(() => api.setupSourceAdd(url.value), add));
      url.addEventListener("keydown", (e) => { if (e.key === "Enter") add.click(); });
      const selected = sv.sources.filter((s) => s.status === "selected").length;
      const go = h("button", { class: "btn", type: "button", disabled: !selected },
        selected ? `Continue with ${selected} source${selected === 1 ? "" : "s"}` : "Continue");
      go.addEventListener("click", () => this.act(() => api.setupSourcesContinue(), go));
      const again = h("button", { class: "btn btn-ghost", type: "button", disabled: running }, "Look again");
      again.addEventListener("click", () => this.act(() => api.setupSourcesDiscover(), again));
      children.push(h("div", { class: "setup-composer" }, url, add),
        h("div", { class: "toolbar", style: "justify-content:flex-start;gap:8px;margin-top:12px" }, go, again));
    } else if (!running) {
      children.push(note("Sources chosen. Mapping their content is the next step; it is not in this version yet."));
    }
    return h("div", { class: "card" }, head, children);
  }

  renderSource(src, names, choosing) {
    const box = h("input", { type: "checkbox", checked: src.status === "selected", disabled: !choosing,
      "aria-label": `Use ${src.name}` });
    box.addEventListener("change", () => this.act(() => api.setupSourceChoose(src.source_id,
      box.checked ? "selected" : "candidate")));
    const remove = choosing && src.status !== "selected"
      ? h("button", { class: "btn btn-sm btn-ghost", type: "button", title: "Hide this site" }, "Remove") : null;
    remove?.addEventListener("click", () => this.act(() => api.setupSourceChoose(src.source_id, "removed"), remove));
    const areas = (src.areas || []).map((k) => names[k] || k);
    return h("li", { class: `source-item${src.status === "selected" ? " selected" : ""}` },
      h("label", { class: "source-check" }, box),
      h("div", { class: "source-body" },
        h("div", {}, h("strong", {}, src.name), " ",
          h("a", { href: src.base_url, target: "_blank", rel: "noopener", class: "note" }, src.host), " ",
          badge(`${src.authority} authority`, AUTHORITY[src.authority] || "neutral"),
          src.recommended ? [" ", badge("recommended", "info")] : null,
          src.origin === "user" ? [" ", badge("added by you", "neutral")] : null,
          src.kind !== "website" ? [" ", badge(src.kind, "neutral")] : null),
        h("div", { class: "note" }, src.reason),
        areas.length ? h("div", { class: "note" }, `Covers: ${areas.join(" · ")}`) : null,
        src.evidence?.length ? h("details", { class: "source-evidence" }, h("summary", {}, "Why it was found"),
          h("ul", {}, src.evidence.map((e) => h("li", {}, h("a", { href: e.url, target: "_blank", rel: "noopener" },
            e.title || e.url), e.area ? h("span", { class: "note" }, ` (${names[e.area] || e.area})`) : null)))) : null),
      remove);
  }

  renderCoverage(v) {
    const sv = v.sources;
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Coverage")),
      h("p", { class: "note" }, "Each part of the blueprint and the chosen sites that cover it."),
      sv.coverage.map((c) => h("div", { class: "setup-fact" }, h("div", { class: "metric-label" }, c.name),
        c.sources.length ? h("div", {}, c.sources.join(", "))
          : h("div", { class: "note" }, "⚠ no chosen source yet"))),
      sv.live_areas?.length ? h("div", { class: "setup-fact", style: "margin-top:12px" },
        h("div", { class: "metric-label" }, "Answered by live tools, not sources"),
        h("div", { class: "note" }, sv.live_areas.join(", "))) : null);
  }

  async act(call, button) {
    if (button) button.disabled = true;
    this.error = null;
    try {
      const before = this.view.state;
      this.view = await call();
      this.changing = false;
      if (this.view.state !== before) setupChanged();
    } catch (err) {
      this.error = err.message;
    }
    this.render();
  }

  renderTurns(v) {
    return h("div", { class: "chat" }, v.turns.map((t) => t.role === "user"
      ? h("div", { class: "turn" }, h("div", { class: "q" }, t.text))
      : h("div", { class: "turn" }, h("div", { class: "reply" }, h("div", { class: "reply-role" }, "Setup agent"),
        md(t.text, "reply-body")))));
  }

  renderBlueprint(v) {
    const b = v.blueprint;
    const head = h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Blueprint"),
      b ? h("span", { class: "note" }, `version ${b.version}`) : null);
    const children = [];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    if (!b || b.status === "researching") {
      children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:12px" },
        h("div", { class: "spinner" }),
        h("div", {}, h("strong", {}, b?.feedback ? "Revising the blueprint…" : "Researching the domain…"),
          h("div", { class: "note" }, b?.feedback ? `Your change: ${b.feedback}`
            : "Searching the web and reading the main pages. This takes a minute or two."))));
      return h("div", { class: "card" }, head, children);
    }
    if (b.status === "failed") {
      const retry = h("button", { class: "btn", type: "button" }, "Try again");
      retry.addEventListener("click", () => this.act(() => api.setupBlueprint(), retry));
      children.push(h("div", { class: "alert err" }, `The research didn't finish: ${b.error}`), retry);
      return h("div", { class: "card" }, head, children);
    }
    children.push(...this.renderOutline(b.blueprint, b.research));
    if (v.state === "DOMAIN_READY") children.push(this.renderBlueprintActions(v));
    else children.push(note(b.confirmed_at ? "Blueprint confirmed. Finding sources is the next step; it is not in this "
      + "version yet." : ""));
    return h("div", { class: "card" }, head, children);
  }

  renderOutline(bp, research) {
    const groups = [];
    const seen = new Set();
    for (const [cls, title, sub] of ANSWERED_BY) {
      if (seen.has(title)) continue;
      const areas = bp.knowledge_areas.filter((a) => ANSWERED_BY.find(([c]) => c === a.knowledge_class)?.[1] === title);
      seen.add(title);
      if (!areas.length) continue;
      groups.push(h("div", { class: "blueprint-group" }, h("div", { class: "step-title" }, title),
        h("div", { class: "note" }, sub),
        h("ul", { class: "blueprint-areas" }, areas.map((a) => h("li", {},
          h("strong", {}, a.name), ` — ${a.description}`,
          a.example_questions?.length ? h("div", { class: "note" }, `e.g. ${a.example_questions.join(" · ")}`) : null,
          h("div", { class: "note" }, a.evidence_urls.length
            ? [`${a.evidence_urls.length} page${a.evidence_urls.length === 1 ? "" : "s"}: `,
              ...a.evidence_urls.flatMap((u, i) => [i ? ", " : "", h("a", { href: u, target: "_blank", rel: "noopener" },
                new URL(u).hostname.replace(/^www\./, ""))])]
            : "from your requirements"))))));
    }
    const list = (items) => items?.length ? items.join(" · ") : "—";
    return [
      h("p", {}, h("strong", {}, bp.name), ` — ${bp.purpose}`),
      h("p", { class: "note" }, `For ${list(bp.audience)} · ${list(bp.regions)} · ${bp.language}`),
      ...groups,
      bp.organisations.length ? h("div", { class: "blueprint-group" }, h("div", { class: "step-title" }, "Organisations"),
        h("ul", { class: "blueprint-areas" }, bp.organisations.map((o) => h("li", {}, h("strong", {}, o.name), " ",
          badge(o.role, o.role === "regulator" ? "info" : "neutral"),
          o.website ? [" ", h("a", { href: o.website, target: "_blank", rel: "noopener" },
            new URL(o.website).hostname.replace(/^www\./, ""))] : null)))) : null,
      h("details", {}, h("summary", {}, "How the assistant will be set up"), h("div", { class: "details-body" },
        h("p", {}, h("strong", {}, "Brief: "), bp.flow.brief),
        h("p", {}, h("strong", {}, "Scope: "), bp.flow.scope),
        h("p", {}, h("strong", {}, "Routing: "), bp.flow.supervisor_instructions),
        h("p", { class: "note" }, `Domain: ${bp.flow.domain} · Web research country: ${bp.flow.search_country || "any"}`),
        h("p", { class: "note" }, `Entity types: ${list(bp.entities.map((e) => e.name))}`),
        h("p", { class: "note" }, `Metadata kept per page: ${list(bp.metadata_fields.map((m) => m.field))}`))),
      bp.assumptions.length || bp.unknowns.length ? h("details", {}, h("summary", {}, "Assumptions and unknowns"),
        h("div", { class: "details-body" },
          bp.assumptions.length ? [h("strong", {}, "Assumed"), h("ul", {}, bp.assumptions.map((a) => h("li", {}, a)))] : null,
          bp.unknowns.length ? [h("strong", {}, "Not found"), h("ul", {}, bp.unknowns.map((u) => h("li", {}, u)))] : null))
        : null,
      research?.pages?.length ? h("details", {}, h("summary", {}, `Research: ${research.queries?.length || 0} searches, `
        + `${research.pages.length} pages read`), h("div", { class: "details-body" },
        h("ul", {}, (research.queries || []).map((q) => h("li", { class: "note" }, q))),
        h("ul", {}, research.pages.map((pg) => h("li", {}, h("a", { href: pg.url, target: "_blank", rel: "noopener" },
          pg.title || pg.url)))))) : null,
    ];
  }

  renderBlueprintActions() {
    const yes = h("button", { class: "btn", type: "button" }, "Looks right");
    yes.addEventListener("click", () => this.act(() => api.setupBlueprintConfirm(), yes));
    const change = h("button", { class: "btn btn-ghost", type: "button" }, "Change something");
    change.addEventListener("click", () => { this.changing = !this.changing; this.render(); });
    const back = h("button", { class: "btn btn-ghost", type: "button" }, "Change the requirements");
    back.addEventListener("click", () => this.act(() => api.setupBack(), back));
    const out = [h("div", { class: "toolbar", style: "justify-content:flex-start;gap:8px;margin-top:12px" }, yes, change, back)];
    if (this.changing) {
      const box = h("textarea", { rows: 3, placeholder: "What should change? For example: also cover Eurostar",
        "aria-label": "What should change" });
      const send = h("button", { class: "btn", type: "button" }, "Revise");
      send.addEventListener("click", () => box.value.trim() && this.act(() => api.setupBlueprintRevise(box.value), send));
      out.push(h("div", { class: "setup-composer" }, box, send));
    }
    return h("div", {}, out);
  }

  renderConversation(v) {
    const turns = v.turns.map((t) => t.role === "user"
      ? h("div", { class: "turn" }, h("div", { class: "q" }, t.text))
      : h("div", { class: "turn" }, h("div", { class: "reply" }, h("div", { class: "reply-role" }, "Setup agent"),
        md(t.text, "reply-body"))));
    if (this.pending) {
      turns.push(h("div", { class: "turn" }, h("div", { class: "q" }, this.pending)));
      turns.push(h("div", { class: "turn" }, h("div", { class: "reply" }, h("div", { class: "reply-role" }, "Setup agent"),
        h("div", { class: "reply-body note" }, "Thinking…"))));
    }
    const children = [h("div", { class: "chat" }, turns)];
    if (this.error) children.push(h("div", { class: "alert err" }, this.error));
    if (CONVERSING.includes(v.state)) {
      if (v.complete && v.state === "CLARIFYING" && !this.busy) {
        const yes = h("button", { class: "btn", type: "button" }, "Looks right");
        yes.addEventListener("click", () => this.confirm(yes));
        children.push(h("div", { class: "toolbar", style: "justify-content:flex-start;gap:8px" }, yes,
          h("span", { class: "note" }, "or say below what to change.")));
      }
      children.push(this.renderComposer());
    } else if (v.state === "DOMAIN_READY") {
      children.push(note("Requirements confirmed. The next step researches the domain and writes the blueprint; "
        + "it is not in this version yet."));
    } else if (v.state === "READY") {
      children.push(note("The knowledge system is set up."));
    }
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Conversation")),
      children);
  }

  renderComposer() {
    const box = h("textarea", { rows: 3, placeholder: this.view.turns.length > 1 ? "Your answer…" : "For example: information about UK trains for passengers",
      "aria-label": "Your message", disabled: this.busy });
    box.value = this.draft || "";
    box.addEventListener("input", () => { this.draft = box.value; });
    const send = h("button", { class: "btn", type: "button", disabled: this.busy }, this.busy ? "Sending…" : "Send");
    send.addEventListener("click", () => this.send(box.value));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.send(box.value); }
    });
    return h("div", { class: "setup-composer" }, box, send);
  }

  renderLearnt(v) {
    const r = v.requirements;
    const rows = FIELDS.filter(([key]) => (Array.isArray(r[key]) ? r[key].length : r[key])).map(([key, label]) =>
      h("div", { class: "setup-fact" }, h("div", { class: "metric-label" }, label),
        h("div", {}, Array.isArray(r[key]) ? r[key].join(", ") : r[key])));
    if (r.assumed?.length) {
      rows.push(h("div", { class: "setup-fact" }, h("div", { class: "metric-label" }, "Assumed until you say otherwise"),
        h("div", { class: "note" }, r.assumed.join("; "))));
    }
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, "What I've learnt")),
      rows.length ? rows : note("Nothing yet: answer the first question."),
      v.missing.length && CONVERSING.includes(v.state)
        ? h("div", { class: "note", style: "margin-top:12px" }, "Still needed: ", v.missing.join("; "), ".") : null,
      v.confirmed_at ? h("div", { class: "alert ok", style: "margin-top:12px" }, "Confirmed.") : null);
  }
}

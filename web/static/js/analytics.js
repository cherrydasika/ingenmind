// Monitoring → Analytics (R52, app/analytics.py): where questions fail, which
// sources help, quality and cost over time, for a date range. Admins only.
// Charts are Plotly (served by the app), coloured from the theme's tokens.

import { api } from "./api.js";
import { card, errorBox, fmtInt, h, loading, note, section, shortUrl } from "./ui.js";

const DAY = 86400000;
const iso = (d) => d.toISOString().slice(0, 10);
const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

export function colours() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return { text: v("--text", "#162026"), faint: v("--faint", "#74838a"), border: v("--border", "#d8dfde"),
    primary: v("--primary", "#1e3a8a"), ok: v("--ok", "#16a34a"), err: v("--err", "#dc2626"), warn: v("--warn", "#ca8a04") };
}

export function plot(el, data, layout = {}) {
  const c = colours();
  const base = { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: c.text, size: 12 },
    margin: { l: 48, r: 16, t: 8, b: 40 }, autosize: true, showlegend: false };
  const axis = { gridcolor: c.border, zerolinecolor: c.border, linecolor: c.border };
  const { xaxis = {}, yaxis = {}, ...rest } = layout;   // axis settings add to the theme's, not replace them
  window.Plotly.react(el, data, { ...base, ...rest, xaxis: { ...axis, ...xaxis }, yaxis: { ...axis, ...yaxis } }, { responsive: true, displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d", "toImage"] });
}

export const chartBox = (height = 320) => h("div", { class: "chart", style: `height:${height}px` });

export class AnalyticsPage {
  title = "Analytics";

  constructor(root) {
    this.root = root;
    const today = new Date();
    this.range = { from: iso(new Date(today - 29 * DAY)), to: iso(today) };
    this.observer = new MutationObserver(() => this.data && this.draw());   // the theme changed: redraw
  }

  unmount() {
    this.observer.disconnect();
  }

  async mount() {
    this.observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    await this.load();
  }

  async load() {
    this.root.replaceChildren(loading());
    try {
      this.data = await api.analytics(this.range);
    } catch (err) {
      this.root.replaceChildren(this.filter(), errorBox(`Couldn't load analytics: ${err.message}`));
      return;
    }
    this.render();
  }

  filter() {
    const from = h("input", { type: "date", value: this.range.from, "aria-label": "From", style: "width:auto" });
    const to = h("input", { type: "date", value: this.range.to, "aria-label": "To", style: "width:auto" });
    const preset = (days) => h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
      this.range = { from: iso(new Date(Date.now() - (days - 1) * DAY)), to: iso(new Date()) };
      this.load();
    } }, `${days} days`);
    return h("div", { class: "toolbar analytics-filter" },
      h("div", { class: "analytics-dates" }, from, h("span", { class: "faint" }, "to"), to,
        h("button", { class: "btn btn-sm", type: "button", onclick: () => { this.range = { from: from.value, to: to.value }; this.load(); } }, "Apply")),
      h("div", {}, preset(7), preset(30), preset(90)));
  }

  render() {
    const d = this.data;
    this.charts = { journey: chartBox(340), funnel: chartBox(240), map: chartBox(420), questions: chartBox(240),
      speed: chartBox(240), tokens: chartBox(240), freshness: chartBox(200) };
    const s = d.sources;
    const sourceRow = (r) => h("tr", {}, h("td", {}, h("a", { href: r.url, target: "_blank", rel: "noopener" }, shortUrl(r.url))),
      h("td", { class: "num" }, fmtInt(r.answers)), h("td", { class: "num" }, fmtInt(r.citations)), h("td", { class: "num" }, fmtInt(r.found)));
    const table = (rows) => h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Page", "Answers citing it", "Citations", "Times found"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows.map(sourceRow))));
    const f = d.funnel;
    const t = d.trends;
    const fresh = d.freshness;
    this.root.replaceChildren(h("div", { class: "stack" },
      this.filter(),
      section("journey", "Question journey", "Each band's width is a number of questions: whether they were let in, whether enough evidence was found, and whether the answer passed its check.",
        card({ children: d.journey.asked ? this.charts.journey : note("No questions in this period.") })),
      section("funnel", "Search funnel", "How many passages the searches found, how many were kept for the answer, and how many the answer actually cited.",
        card({ children: f.questions ? [this.charts.funnel, h("p", { class: "note" },
          `${fmtInt(f.questions)} questions, ${fmtInt(f.searches)} searches: ${fmtInt(f.dense)} found by meaning and ${fmtInt(f.sparse)} by words, before merging.`)]
          : note("No searches recorded in this period (recorded from this version on).") })),
      section("gaps", "Knowledge gaps", `What people asked that the knowledge base couldn't answer, grouped into topics${d.gaps.grouped_by === "text" ? " (by identical wording: no embedding model was reachable)" : ""}: what content to add next. "Still unanswered" questions got no answer that passed its check; a topic that drops off after content is added was closed.`,
        card({ children: d.gaps.topics.length ? h("div", { class: "table-wrap" }, h("table", { class: "gap-table" },
          h("thead", {}, h("tr", {}, ["Topic", "Questions", "Still unanswered", "Example questions", "Last asked"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.gaps.topics.map((g) => h("tr", {},
            h("td", { title: g.items.join("\n") }, g.topic), h("td", { class: "num" }, fmtInt(g.questions)),
            h("td", { class: "num" }, fmtInt(g.unanswered)),
            h("td", {}, h("ul", { class: "gap-examples" }, g.examples.map((q) => h("li", {}, q)))),
            h("td", { class: "faint" }, g.last_asked.slice(0, 10)))))))
          : note("No knowledge gaps in this period (recorded from this version on).") })),
      section("sources", "Most and least used sources", "Pages ranked by how many answers cite them. Pages no search found in this period may be outdated, duplicated or badly written.",
        card({ title: "Most cited", children: s.most.length ? table(s.most) : note("No cited sources in this period.") }),
        s.least.length ? card({ title: "Least cited", children: table(s.least) }) : null,
        card({ title: `Never found (${fmtInt(s.never_count)} of ${fmtInt(s.pages)} pages)`, children: s.tracked
          ? (s.never.length ? h("ul", { class: "never-list" }, s.never.map((u) => h("li", {}, h("a", { href: u, target: "_blank", rel: "noopener" }, shortUrl(u)))))
            : note("Every page was found at least once."))
          : note("Which pages searches find is recorded from this version on.") })),
      section("map", "Knowledge map by use", "Passages placed by meaning (similar ones close together), coloured by how often searches found them; grey areas went unused.",
        card({ children: d.map.points.length ? [this.charts.map, h("p", { class: "note" },
          `${fmtInt(d.map.sampled)} of ${fmtInt(d.map.total)} passages shown (every used one, then a random sample).`)] : note("Not enough passages for a map.") })),
      section("trends", "Quality and cost over time", `Per day, for real users' questions. Cost is approximate: tokens × the list price of ${t.model}${t.priced ? "" : " (no price known, so tokens only)"}.`,
        card({ children: t.days.length ? h("div", { class: "analytics-grid" },
          h("div", {}, h("div", { class: "faint" }, "Questions and pass rate"), this.charts.questions),
          h("div", {}, h("div", { class: "faint" }, "Response time: typical and slowest (95%)"), this.charts.speed),
          h("div", {}, h("div", { class: "faint" }, t.priced ? "Tokens and approximate cost" : "Tokens"), this.charts.tokens))
          : note("No questions from users in this period.") })),
      this.exportCard(),
      section("freshness", "Content freshness", "Pages by when they leave the knowledge base, so they can be refreshed (re-crawled) first.",
        card({ children: [this.charts.freshness,
          fresh.stale !== null ? h("p", { class: "note" }, `${fmtInt(fresh.stale)} page(s) flagged stale by the re-crawl (gone, failing or overdue).`) : null,
          fresh.soonest.length ? h("ul", { class: "never-list" }, fresh.soonest.map((p) => h("li", {}, shortUrl(p.url), h("span", { class: "faint" }, ` · ${p.expires_at.slice(0, 10)}`)))) : null] })),
    ));
    this.draw();
  }

  // R57: the same data as Parquet files for DuckDB, offline (app/analytics_export.py).
  exportCard() {
    const text = h("input", { type: "checkbox" });
    const link = h("a", { class: "btn btn-sm", href: "/api/analytics/export", download: "" }, "Download export (.zip)");
    text.addEventListener("change", () => { link.href = `/api/analytics/export${text.checked ? "?include_text=1" : ""}`; });
    return section("export", "Export for offline analysis", "Every question, run, evaluation, ingestion job and page summary as Parquet files with a DuckDB starter (explore.sql), for one-off analysis away from the live database. People are pseudonymous and never named; what they wrote is left out unless you include it.",
      card({ children: h("div", { class: "alert-actions" }, link,
        h("label", { class: "sched-check" }, text, "Include what people wrote (questions, answers, comments)")) }));
  }

  draw() {
    if (!window.Plotly) { setTimeout(() => this.draw(), 200); return; }   // loaded with defer
    const c = colours();
    const d = this.data;
    if (d.journey.asked) {
      const names = [...new Set(d.journey.links.flatMap((l) => [l.source, l.target]))];
      const tone = (n) => (/passed|Enough|Allowed|Asked/.test(n) ? c.ok : /withheld|Blocked|Not enough/.test(n) ? c.err : c.faint);
      plot(this.charts.journey, [{ type: "sankey", arrangement: "snap",
        node: { label: names, color: names.map((n) => (n === "Asked" ? c.primary : tone(n))), pad: 18, thickness: 14, line: { width: 0 } },
        link: { source: d.journey.links.map((l) => names.indexOf(l.source)), target: d.journey.links.map((l) => names.indexOf(l.target)),
          value: d.journey.links.map((l) => l.value), color: d.journey.links.map(() => `${c.border}`) } }], { margin: { l: 8, r: 8, t: 8, b: 8 } });
    }
    const f = d.funnel;
    if (f.questions) {
      plot(this.charts.funnel, [{ type: "funnel", y: ["Found (both searches)", "Kept for the answer", "Cited in the answer"],
        x: [f.dense + f.sparse, f.kept, f.cited], marker: { color: [c.primary, c.primary, c.ok] }, textinfo: "value+percent initial" }],
      { margin: { l: 150, r: 16, t: 8, b: 24 } });
    }
    if (d.map.points.length) {
      const p = d.map.points;
      plot(this.charts.map, [{ type: "scatter3d", mode: "markers", x: p.map((q) => q.x), y: p.map((q) => q.y), z: p.map((q) => q.z),
        text: p.map((q) => `${shortUrl(q.url)}<br>found ${q.uses}×`), hoverinfo: "text",
        marker: { size: p.map((q) => (q.uses ? 9 : 5)), color: p.map((q) => (q.uses ? Math.log2(1 + q.uses) : 0)),
          colorscale: [[0, c.border], [0.001, c.primary], [1, c.ok]], cmin: 0, showscale: false, opacity: 0.9 } }],
      { margin: { l: 0, r: 0, t: 0, b: 0 }, scene: { xaxis: { visible: false }, yaxis: { visible: false }, zaxis: { visible: false } } });
    }
    const days = d.trends.days;
    if (days.length) {
      const x = days.map((r) => r.day);
      plot(this.charts.questions, [{ type: "bar", x, y: days.map((r) => r.runs), marker: { color: c.primary }, name: "Questions" },
        { type: "scatter", mode: "lines+markers", x, y: days.map((r) => (r.pass_rate === null ? null : r.pass_rate * 100)), yaxis: "y2",
          line: { color: c.ok }, name: "Pass rate %" }],
      { xaxis: { type: "date" }, yaxis: { type: "linear", rangemode: "tozero" },
        yaxis2: { type: "linear", overlaying: "y", side: "right", range: [0, 100], gridcolor: "rgba(0,0,0,0)", ticksuffix: "%" }, margin: { l: 40, r: 44, t: 8, b: 40 } });
      plot(this.charts.speed, [{ type: "scatter", mode: "lines+markers", x, y: days.map((r) => r.p50), line: { color: c.primary }, name: "Typical" },
        { type: "scatter", mode: "lines+markers", x, y: days.map((r) => r.p95), line: { color: c.warn, dash: "dot" }, name: "Slowest 5%" }],
      { xaxis: { type: "date" }, yaxis: { type: "linear", ticksuffix: " s", rangemode: "tozero" }, showlegend: true, legend: { orientation: "h", y: -0.25 } });
      const priced = d.trends.priced;
      plot(this.charts.tokens, [{ type: "bar", x, y: days.map((r) => r.input_tokens + r.output_tokens), marker: { color: c.primary }, name: "Tokens" },
        ...(priced ? [{ type: "scatter", mode: "lines+markers", x, y: days.map((r) => r.cost), yaxis: "y2", line: { color: c.warn }, name: "Cost $" }] : [])],
      { xaxis: { type: "date" }, yaxis: { type: "linear", rangemode: "tozero" },
        ...(priced ? { yaxis2: { type: "linear", overlaying: "y", side: "right", tickprefix: "$", rangemode: "tozero", gridcolor: "rgba(0,0,0,0)" },
          margin: { l: 48, r: 48, t: 8, b: 40 } } : {}) });
    }
    const b = d.freshness.buckets;
    plot(this.charts.freshness, [{ type: "bar", orientation: "h", y: ["In 7 days", "In 8–30 days", "Later"],
      x: [b.within_7_days, b.within_30_days, b.later], marker: { color: [c.err, c.warn, c.ok] } }],
    { margin: { l: 90, r: 16, t: 8, b: 32 }, xaxis: { type: "linear", rangemode: "tozero" }, yaxis: { type: "category", autorange: "reversed" } });
  }
}

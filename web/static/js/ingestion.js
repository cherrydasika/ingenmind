// Ingestion: PostgreSQL chunks and manually triggered jobs.

import { api } from "./api.js";
import { sourcePicker } from "./source_picker.js";
import { SourceReports } from "./source_reports.js";
import { card, dataTable, daysLeft, errorBox, fmtInt, fmtTimestamp, h, labelsCell, loading, md, metric, note } from "./ui.js";

const STATE_KIND = { success: "ok", failed: "err", running: "info", queued: "neutral", up_for_retry: "warn", upstream_failed: "err" };
const STATE_ICON = { success: "✓", failed: "✕", running: "↻", queued: "…", up_for_retry: "↺", upstream_failed: "⛔" };

function stateBadge(state) {
  return h("span", { class: `badge badge-${STATE_KIND[state] || "neutral"}` }, `${STATE_ICON[state] || "•"} ${state}`);
}

export class IngestionPage {
  title = "Ingestion";

  constructor(root) {
    this.root = root;
    this.data = null;
    this.flash = null;
    this.reports = new SourceReports();
  }

  async mount(force = false) {
    if (!this.data) this.root.replaceChildren(loading());
    try {
      this.data = await api.ingestion(force);
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load ingestion status: ${err.message}`));
      return;
    }
    this.render();
    this.reports.load();   // its own card; a failure there leaves the rest of the page
  }

  async trigger(kind, button) {
    button.disabled = true;
    try {
      await api.trigger(kind);
      this.flash = { kind: "ok", text: `Queued ${kind}.` };
    } catch (err) {
      this.flash = { kind: "err", text: err.message };
    }
    await this.mount(true);
  }

  render() {
    const { collection, count, urls } = this.data;
    this.root.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "toolbar" },
        h("p", { class: "note" }, "Scrape → chunk → embed → store. URL runs are manual and limited to the first 10 configured sources."),
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => this.mount(true) }, "Refresh")),
      this.flash ? h("div", { class: `alert ${this.flash.kind}` }, this.flash.text) : null,
      card({
        title: "PostgreSQL chunks",
        children: count === null
          ? h("div", { class: "alert info" }, `Table '${collection}' doesn't exist yet — queue an ingestion job first.`)
          : h("div", { class: "metrics-2" }, metric("Total chunks stored", fmtInt(count)), metric("Distinct URLs ingested", fmtInt(urls))),
      }),
      this.renderResearch(),
      this.reports.el,
      this.data.jobs.map((job) => this.renderJob(job)),
    ));
    this.flash = null;
  }

  // Pages the research agent added to fill knowledge gaps (research_chunks).
  renderResearch() {
    const num = (v) => h("td", { class: "num" }, v);
    const overall = (scores) => scores
      ? (Object.values(scores).reduce((a, b) => a + b, 0) / Object.values(scores).length).toFixed(2) : "—";
    return card({
      title: "Pages added by research",
      sub: "Ingested automatically when the knowledge base had a gap. Remove any that don't belong.",
      children: sourcePicker({
        rows: this.data.research,
        dataset: "research",
        canManage: this.data.can_manage,
        empty: "The research agent hasn't added any pages yet.",
        onRemoved: (count) => {
          this.flash = { kind: "ok", text: `Removed ${count} page${count === 1 ? "" : "s"}.` };
          this.mount(true);
        },
        columns: [
          { label: "Added for", cell: (r) => h("td", { class: "wrap" }, r.task || "—") },
          { label: "Publisher", cell: (r) => h("td", {}, r.publisher || "—") },
          { label: "Labels", cell: (r) => labelsCell(r.labels) },
          { label: "Score", num: true, cell: (r) => num(overall(r.scores)) },
          { label: "Ingested", cell: (r) => h("td", {}, fmtTimestamp(r.ingested_at)) },
          { label: "Expires in", num: true, cell: (r) => num(daysLeft(r.expires_at)) },
          { label: "Chunks", num: true, cell: (r) => num(r.chunks) },
        ],
      }),
    });
  }

  renderJob(job) {
    const button = h("button", { class: "btn", type: "button" }, "Run");
    button.addEventListener("click", () => this.trigger(job.id, button));
    const children = [md(job.description, "prose note")];
    if (job.error) {
      children.push(h("div", { class: "alert" }, job.error));
    } else if (!job.runs.length) {
      children.push(note("No runs yet."));
    } else {
      const latest = job.runs[0];
      children.push(h("div", { class: "note" }, "Latest run: ", stateBadge(latest.state),
        ` ${latest.next_index}/${latest.total} processed · started ${fmtTimestamp(latest.start_date)}`));
      children.push(h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Status"), h("th", {}, "Run ID"), h("th", {}, "Started"), h("th", {}, "Ended"))),
        h("tbody", {}, job.runs.map((r) => h("tr", {},
          h("td", {}, stateBadge(r.state)), h("td", {}, h("code", {}, r.job_id)),
          h("td", {}, fmtTimestamp(r.start_date)), h("td", {}, fmtTimestamp(r.end_date))))))));
      if (job.summary) children.push(this.renderSummary(job.summary));
    }
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, h("span", {}, job.icon), job.label), button),
      children);
  }

  renderSummary(summary) {
    const labels = this.data.status_labels;
    const failed = summary.failed || [];
    return h("div", {},
      h("div", { class: "step-title" },
        `Last run summary · ${fmtInt(summary.urls)} URLs in ${summary.batches} batches · ${fmtInt(summary.chunks_written)} chunks written · ${fmtInt(Math.round(summary.seconds))}s`),
      h("div", { class: "grid-auto" }, Object.entries(labels).map(([status, label]) => metric(label, fmtInt(summary.counts[status] || 0)))),
      failed.length ? h("details", { open: failed.length <= 20 },
        h("summary", {}, `Failed URLs (${fmtInt(failed.length)})`),
        h("div", { class: "details-body" },
          dataTable([{ key: "url", label: "URL" }, { key: "stage", label: "Stage" }, { key: "error", label: "Error" }], failed),
          note("Re-running retries failed URLs; URLs already stored are skipped."))) : null,
    );
  }
}

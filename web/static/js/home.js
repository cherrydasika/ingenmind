// Home: every local service with live status, shortcuts, project links.
// Mirrors app/home_tab.py; the lists come from app/services.py.

import { api } from "./api.js";
import { card, errorBox, h, loading, md, section } from "./ui.js";

function linkCard({ icon, name, url, description, up }) {
  const status = up === undefined ? null
    : h("span", { class: `status-pill ${up ? "up" : "down"}` }, up ? "● up" : "● down");
  return card({
    cls: "link-card",
    title: [h("span", {}, icon), url ? h("a", { href: url, target: "_blank", rel: "noopener" }, name) : name, status],
    children: md(description, "prose note"),
  });
}

// Which user-facing setup step a state is in (initialization/state.py STEPS).
const STEP = { NEW: "Purpose", DISCOVERING_DOMAIN: "Purpose", CLARIFYING: "Purpose", DOMAIN_READY: "Blueprint",
  DISCOVERING_SOURCES: "Sources", AWAITING_SOURCE_SELECTION: "Sources", ANALYSING_SOURCES: "Content",
  AWAITING_CONTENT_SELECTION: "Content", INGESTION_APPROVED: "Build and evaluate", INGESTING: "Build and evaluate",
  INDEXING: "Build and evaluate", EVALUATING: "Build and evaluate", READY: "Go live" };

// The knowledge system: its state for everyone; its readiness and setup for those who manage it.
function knowledgeCard(status, readiness, canManage) {
  const ready = status.state === "READY";
  const pill = h("span", { class: `status-pill ${ready ? "up" : "down"}` },
    ready ? "● ready" : status.state === "NEW" ? "● not set up" : "● being set up");
  const lines = [h("p", { class: "note" }, ready
    ? `Ready${status.set_up_at ? ` since ${String(status.set_up_at).slice(0, 10)}` : ""}`
      + `${status.origin === "setup" ? ", built by guided setup" : ""}.`
    : `Setup step: ${STEP[status.state] || status.state}.`)];
  const overall = readiness?.went_live && ready ? readiness.went_live.overall : readiness?.overall?.value;
  if (overall !== null && overall !== undefined) {
    lines.push(h("p", { class: "note" }, `Readiness: ${Math.round(overall * 100)}%`
      + `${readiness.gaps?.length ? `, ${readiness.gaps.length} gap${readiness.gaps.length === 1 ? "" : "s"}` : ""}.`));
  }
  if (canManage) lines.push(h("a", { href: "#/setup" }, ready ? "Open setup" : "Continue setup"));
  return card({ cls: "link-card", title: [h("span", {}, "🧭"), "Setup", pill], children: lines });
}

export class HomePage {
  title = "Home";

  constructor(root, { session } = {}) {
    this.root = root;
    this.session = session;
  }

  async mount() {
    this.root.replaceChildren(loading());
    let data;
    try {
      data = await api.home();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load services: ${err.message}`));
      return;
    }
    const grid = (items) => h("div", { class: "grid-2" }, items.map(linkCard));
    const canManage = Boolean(this.session?.can("manage_settings"));
    const [status, readiness] = await Promise.all([
      api.knowledgeSystem().catch(() => null),
      canManage ? api.setupReadiness().catch(() => null) : null,
    ]);
    this.root.replaceChildren(h("div", { class: "stack" },
      status ? section("knowledge", "Knowledge system", null,
        h("div", { class: "grid-2" }, knowledgeCard(status, readiness, canManage))) : null,
      section("services", "Services",
        "Every local service in this stack, with live status — checked from inside the app's container every 30s. Langfuse and MinIO are down unless the optional overlay is started.",
        grid(data.services)),
      data.shortcuts.length ? section("shortcuts", "Shortcuts", null, grid(data.shortcuts)) : null,
      section("project", "Project", null, grid(data.project)),
    ));
  }
}

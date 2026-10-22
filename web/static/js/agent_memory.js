// Agent memory: what the agents have learned across runs (app/agent_memory.py).
// Outcomes only — web sites, tools, decisions, counts and scores — never
// anyone's conversation; each user's session memory is kept separately.

import { api } from "./api.js";
import { card, dataTable, errorBox, h, loading, note } from "./ui.js";

const pct = (rate) => (rate == null ? "—" : `${Math.round(rate * 100)}%`);
const score = (value) => (value == null ? "—" : value.toFixed(2));
const when = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

export class AgentMemoryPage {
  title = "Agent memory";

  constructor(root) {
    this.root = root;
  }

  async mount() {
    this.root.replaceChildren(loading());
    let data;
    try {
      data = await api.agentMemory();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load agent memory: ${err.message}`));
      return;
    }
    const intro = note("What the agents have learned across all runs: which web sites pass validation, which get cited, how often evidence checks and query rewrites succeed, and which tools fail. It is built from outcomes only — never from questions, answers or anything typed — so it carries nothing from anyone's conversation. Each user's session memory is kept apart, per user and session. "
      + (data.in_use ? "The agents use it: the research agent sees each site's record and tries reliable sites first, a tool that failed most recent calls is skipped, and the query rewriter sees which kinds of rewrite worked. " : "The agents don't read it on this install (AGENT_MEMORY_USE=false). ")
      + `Counts fade: each halves every ${data.half_life_days} days, unless the entry is pinned.`);
    if (!data.entries.length) {
      this.root.replaceChildren(h("div", { class: "stack" }, intro,
        card({ children: note("Nothing learned yet: the agents learn as questions are answered.") })));
      return;
    }
    const sections = Object.entries(data.agents).map(([agent, name]) => {
      const rows = data.entries.filter((e) => e.agent === agent);
      if (!rows.length) return null;
      const kinds = [...new Set(rows.map((r) => r.kind))];
      return card({ title: name, children: kinds.map((kind) => h("div", { class: "memory-kind" },
        h("div", { class: "step-title" }, data.kinds[kind] || kind),
        dataTable([
          { key: "key", label: kind === "tool" ? "Tool" : kind === "evidence_decision" ? "Decision" : kind === "query_rewrite" ? "Outcome" : kind === "rewrite_strategy" ? "Kind of rewrite" : "Web site" },
          { key: "total", label: "Seen", num: true },
          { key: "rate", label: "Success", num: true },
          { key: "score", label: "Mean score", num: true },
          { key: "note", label: "Last note" },
          { key: "seen", label: "Last seen" },
          ...(data.can_manage ? [{ key: "actions", label: "" }] : []),
        ], rows.filter((r) => r.kind === kind).map((r) => ({
          key: r.pinned ? [r.key, " ", h("span", { class: "badge badge-info", title: "Pinned: it doesn't fade" }, "pinned")] : r.key,
          total: Math.round(r.total * 10) / 10, rate: pct(r.success_rate), score: score(r.mean_score),
          note: r.note || "", seen: when(r.last_seen),
          actions: data.can_manage ? this.actions(r) : null,
        }))))) });
    });
    this.root.replaceChildren(h("div", { class: "stack" }, intro, ...sections.filter(Boolean)));
  }

  // Pin (stops fading) or forget an entry (R27; manage_agents).
  actions(entry) {
    const act = async (action) => {
      if (action === "forget" && !confirm(`Forget ${entry.key}? The agents start over on it.`)) return;
      try { await api.agentMemoryEntry({ agent: entry.agent, kind: entry.kind, key: entry.key }, action); } catch (err) { alert(err.message); }
      this.mount();
    };
    return h("span", { class: "memory-actions" },
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => act(entry.pinned ? "unpin" : "pin") },
        entry.pinned ? "Unpin" : "Pin"),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => act("forget") }, "Forget"));
  }
}

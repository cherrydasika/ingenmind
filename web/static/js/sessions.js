// Sessions: your earlier conversations, grouped by day; open one to read it
// again. With view_sessions, everyone's (filter by user). #/sessions/<id>
// opens one session.

import { api } from "./api.js";
import { card, errorBox, fmtTime, h, loading, markdown, note, sourceItem } from "./ui.js";

function dayLabel(iso) {
  const day = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (day.toDateString() === today.toDateString()) return "Today";
  if (day.toDateString() === yesterday.toDateString()) return "Yesterday";
  return day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function minutes(from, to) {
  const m = Math.max(1, Math.round((new Date(to) - new Date(from)) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

export class SessionsPage {
  title = "Sessions";

  constructor(root, { session }) {
    this.root = root;
    this.session = session;
    this.filter = "me";
  }

  async mount() {
    const id = location.hash.replace(/^#\/sessions\/?/, "");
    if (id) await this.showSession(decodeURIComponent(id));
    else await this.showList();
  }

  // ---------- the list ----------

  async showList() {
    this.root.replaceChildren(loading());
    let data;
    try {
      data = await api.sessions(this.filter);
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load sessions: ${err.message}`));
      return;
    }
    const byDay = new Map();
    for (const s of data.sessions) {
      const label = dayLabel(s.last_activity);
      if (!byDay.has(label)) byDay.set(label, []);
      byDay.get(label).push(s);
    }
    const everyone = this.filter !== "me";
    const filter = data.can_view_all ? h("div", { class: "segmented" },
      [["me", "My sessions"], ["all", "Everyone's"]].map(([value, label]) => h("button", {
        type: "button", class: (value === "me") === !everyone ? "active" : null,
        onclick: () => { this.filter = value; this.showList(); },
      }, label))) : null;
    this.root.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "sessions-head" }, filter,
        note(`Sessions start automatically and end after 30 minutes idle or when you sign out. History is kept for ${data.retention_days} days.`)),
      data.sessions.length ? [...byDay].map(([label, list]) => card({ title: label, children:
        h("ul", { class: "session-list" }, list.map((s) => h("li", {},
          h("a", { href: `#/sessions/${encodeURIComponent(s.session_id)}` },
            h("span", { class: "session-time" }, clock(s.started_at)),
            h("span", { class: "session-title" }, s.title || "Untitled"),
            h("span", { class: "faint session-meta" }, [
              everyone ? s.first_name : null,
              `${s.turns} question${s.turns === 1 ? "" : "s"}`,
              s.status === "active" ? "current" : null,
            ].filter(Boolean).join(" · "))))))
      })) : card({ children: note("No sessions yet: ask a question on the Retrieval page.") })));
  }

  // ---------- one session ----------

  async showSession(id) {
    this.root.replaceChildren(loading());
    let s;
    try {
      s = (await api.sessionDetail(id)).session;
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the session: ${err.message}`),
        h("p", {}, h("a", { href: "#/sessions" }, "← All sessions")));
      return;
    }
    const mine = s.user_id === this.session.userId;
    this.root.replaceChildren(h("div", { class: "stack" },
      h("p", {}, h("a", { href: "#/sessions" }, "← All sessions")),
      card({ title: s.title || "Session", children: h("div", { class: "session-facts" },
        h("span", {}, h("span", { class: "faint" }, "User "), mine ? `${s.first_name} (you)` : s.first_name),
        h("span", {}, h("span", { class: "faint" }, "Started "), `${dayLabel(s.started_at)} ${clock(s.started_at)}`),
        h("span", {}, h("span", { class: "faint" }, "Duration "), minutes(s.started_at, s.ended_at || s.last_activity)),
        h("span", {}, h("span", { class: "faint" }, "Status "), s.status === "active" ? "current" : "ended"),
        h("span", {}, h("span", { class: "faint" }, "Questions "), String(s.turns.length))) }),
      card({ title: "Conversation", children: h("div", { class: "chat session-chat" }, s.turns.map((t) => this.turn(t))) })));
  }

  turn(t) {
    const r = t.result || {};
    const check = r.answer_check;
    const blocked = Object.values(r.guardrails || {}).some((g) => g.decision && g.decision !== "ALLOW");
    const verdict = blocked ? h("span", { class: "ae-verdict fail" }, "blocked by a guardrail")
      : check ? h("span", { class: `ae-verdict ${check.passed ? "pass" : "fail"}` },
        check.passed ? `✓ answer check passed · ${check.overall.toFixed(2)}` : "✕ answer check failed")
        : null;
    const meta = [clock(t.asked_at), r.total ? fmtTime(r.total) : null,
      r.flow ? `flow ${r.flow.id} v${r.flow.version}` : null].filter(Boolean).join(" · ");
    return h("div", { class: "turn" },
      h("div", { class: "q" }, t.question),
      h("div", { class: `reply ${check && !check.passed ? "withheld" : ""}` },
        h("div", { class: "reply-role" }, "◆ Answer", verdict),
        h("div", { class: "reply-body", html: markdown(t.answer) }),
        h("div", { class: "reply-meta" }, meta)),
      r.sources?.length ? h("details", { class: "sources" },
        h("summary", {}, `Sources (${r.sources.length})`),
        h("div", { class: "details-body" }, h("ul", { class: "chunk-list" }, r.sources.map(sourceItem)))) : null);
  }
}

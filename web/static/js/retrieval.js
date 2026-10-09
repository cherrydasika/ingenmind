// Retrieval page: ask the live flow a question and read the answer, with the
// sources it cites. The workflow chart lights up as the agents run. Testing
// and the run's details (per-node traces, versions, metrics) are on the Agent
// flow page; full diagnostics are in Langfuse.

import { agentReady } from "./agent.js";
import { api } from "./api.js";
import { AgenticFlow, shortModel } from "./flow.js";
import { card, errorBox, fmtTime, h, markdown, section, sourceItem } from "./ui.js";
import { whyButton } from "./why.js";

export class RetrievalPage {
  title = "Retrieval";

  constructor(root, { session }) {
    this.root = root;
    this.session = session;
    this.cfg = null;
    this.info = null;
    this.messages = [];
    this.latest = null;
    this.busy = false;
  }

  async mount() {
    this.root.replaceChildren(h("div", { class: "empty-state" }, h("div", { class: "spinner" })));
    const [cfg, info, current] = await Promise.all([
      api.config(),
      api.agent().catch((err) => ({ configured: true, error: err.message })),
      api.currentSession().catch(() => ({ session: null })),
    ]);
    this.cfg = cfg;
    this.info = info;
    this.flow = new AgenticFlow(cfg, info);
    this.load(current.session);
    if (this.latest) this.flow.show(this.latest);
    this.render();
  }

  // The conversation is the server's current session (sessions.py): it
  // survives reloads and new tabs, and is gone once the session ends (sign
  // out, or 30 minutes idle). Earlier sessions are on the Sessions page.
  load(session) {
    const turns = session?.turns || [];
    this.messages = turns.map((t) => ({ question: t.question, interaction_id: String(t.turn_id),
      result: { ...t.result, question: t.question } }));
    this.latest = this.messages.length ? this.messages[this.messages.length - 1].result : null;
  }

  // ---------- Layout ----------

  render() {
    const draft = this.root.querySelector("#question")?.value;
    this.root.replaceChildren(
      h("div", { class: "stack" },
        this.renderAsk(),
        section("responses", "Answer",
          "Answers come from the live flow and are checked against their evidence before you see them. Follow-up questions keep the conversation's context; a new session starts after 30 minutes idle, and earlier ones are on the Sessions page. To test a flow and see each step, use the Agent flow page.",
          card({ children: this.renderChat() })),
        section("workflow", "Multi-agent workflow", null, card({ cls: "wf-card", children: this.flow.element })),
      ),
    );
    const input = this.root.querySelector("#question");
    if (input && draft) input.value = draft;
    this.root.querySelectorAll(".chat").forEach((el) => { el.scrollTop = el.scrollHeight; });
  }

  renderAsk() {
    const ready = agentReady(this.info);
    const input = h("textarea", {
      id: "question", rows: 1, placeholder: "Ask about trains in the UK, or the weather…", disabled: !ready,
      onkeydown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.ask(); }
      },
    });
    const button = h("button", { class: "btn", id: "ask-btn", type: "button", disabled: !ready || this.busy,
      onclick: () => this.ask() }, this.busy ? "Working…" : "Ask");
    const problem = ready ? null : errorBox(`Agent unavailable: ${this.info?.error || "not configured"}. ${this.info?.harness?.runtime === "local"
      ? "Configure the chat model in .env (see .env.example)."
      : this.info?.configured === false
        ? "Set AGENT_HARNESS_ARN for the web app (see agentcore-kb/README.md)."
        : "Check the web app's AWS permissions for the harness."}`);
    return card({ children: [h("div", { class: "ask" }, input, button), problem] });
  }

  // ---------- Chat ----------

  renderChat() {
    const body = h("div", { class: "chat" });
    if (!this.messages.length) body.append(h("div", { class: "chat-empty" }, "Ask a question above to get started."));
    for (const msg of this.messages) body.append(this.renderTurn(msg));
    return body;
  }

  renderTurn(msg) {
    const turn = h("div", { class: "turn", "data-turn": msg.interaction_id }, h("div", { class: "q" }, msg.question));
    if (msg.error) {
      turn.append(h("div", { class: "reply error" }, h("div", { class: "reply-body" }, `Something went wrong: ${msg.error}`)));
      return turn;
    }
    if (msg.notice) {
      turn.append(h("div", { class: "reply" }, h("div", { class: "reply-body" }, msg.notice)));
      return turn;
    }
    // Nothing reaches the user before the answer evaluator passes it.
    const r = msg.result;
    if (msg.pending && !r) {
      turn.append(h("div", { class: "run-status" }, h("span", { class: "spinner" }),
        msg.checking ? "Checking the answer against its evidence…"
          : msg.writing ? "Writing the answer…"
            : msg.searching ? "Searching the knowledge base…" : "Working…"));
      return turn;
    }
    const meta = [fmtTime(r.total), shortModel(this.info?.harness?.model), r.flow ? `flow ${r.flow.id} v${r.flow.version}` : null]
      .filter(Boolean).join(" · ");
    const ae = r.answer_check;
    const verdict = ae ? h("span", { class: `ae-verdict ${ae.passed ? "pass" : "fail"}`,
      title: Object.entries(ae.scores).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(" · ") },
    ae.passed ? `✓ answer check passed · ${ae.overall.toFixed(2)}` : `✕ answer check failed on ${ae.failed_on.join(", ")}`) : null;
    turn.append(h("div", { class: `reply ${ae && !ae.passed ? "withheld" : ""}` },
      h("div", { class: "reply-role" }, "◆ Answer", verdict),
      h("div", { class: "reply-body", html: markdown(r.answer) }),
      h("div", { class: "reply-meta" }, meta)));
    if (r.sources?.length) {
      turn.append(h("details", { class: "sources" },
        h("summary", {}, `Sources (${r.sources.length})`),
        h("div", { class: "details-body" }, h("ul", { class: "chunk-list" }, r.sources.map((s) => {
          const item = sourceItem(s);
          if (/^https?:\/\//.test(s.url)) item.append(" ", whyButton(s.url));
          return item;
        })))));
    }
    return turn;
  }

  async ask() {
    const input = document.getElementById("question");
    const question = input.value.trim();
    if (!question || this.busy || !agentReady(this.info)) return;
    const msg = { question, interaction_id: crypto.randomUUID(), pending: true };
    this.messages.push(msg);
    input.value = "";
    this.flow.start(question);
    this.busy = true;
    this.render();
    try {
      const result = await api.agentStream({ question, session_id: this.session.id, user_id: this.session.userId },
        (event) => {
          this.flow.event(event);
          this.onEvent(msg, event);
        });
      this.latest = result;
      msg.result = result;
      Object.assign(msg, { pending: false, checking: undefined, writing: undefined, searching: undefined });
      this.flow.finish(result);
    } catch (err) {
      if (err.setup) {   // not a failure: nothing ran, the knowledge system is still being set up
        Object.assign(msg, { pending: false, notice: err.message });
        this.flow.reset();
      } else {
        Object.assign(msg, { pending: false, error: err.message });
        this.flow.fail(err.message);
      }
    } finally {
      this.busy = false;
      this.render();
    }
  }

  // Progress for the pending turn, without re-rendering the whole page.
  onEvent(msg, event) {
    if (event.type === "answer_eval" && event.state === "start") msg.checking = true;
    else if (event.type === "text" && event.agent === "supervisor") msg.writing = true;
    else if (event.type === "search" && event.state === "start") msg.searching = true;
    else return;
    this.root.querySelector(`[data-turn="${msg.interaction_id}"]`)?.replaceWith(this.renderTurn(msg));
  }
}

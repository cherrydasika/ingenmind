// Ask: put a question to the live flow and read the answer, with the sources it
// cites. The workflow chart is the live flow's own shape and lights up as it
// runs. Testing and the run's details (per-node traces, versions, metrics) are
// in the Agent Builder; full diagnostics are in Langfuse.

import { agentReady } from "./agent.js";
import { api } from "./api.js";
import { linkCitations } from "./citations.js";
import { AgenticFlow, shortModel } from "./flow.js";
import { card, errorBox, fmtTime, h, markdown, section, sourceItem } from "./ui.js";
import { feedbackBar } from "./feedback.js";
import { whyButton } from "./why.js";

// The answer's [n] become previews of the passages they cite (R23).
function linked(body, citations) {
  linkCitations(body, citations);
  return body;
}

// What the question box suggests: the set-up domain's first example question.
function askPlaceholder(domain) {
  if (domain?.examples?.length) return `Ask a question, e.g. ${domain.examples[0]}`;
  if (domain?.name) return `Ask about ${domain.name}…`;
  return "Ask a question…";
}

export class RetrievalPage {
  title = "Ask";

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
    const [cfg, info, current, services] = await Promise.all([
      api.config(),
      api.agent().catch((err) => ({ configured: true, error: err.message })),
      api.currentSession().catch(() => ({ session: null })),
      api.status().catch(() => []),
    ]);
    this.cfg = cfg;
    this.info = info;
    // A local model server that is down (services.py) fails every question: say so before one is asked.
    this.modelDown = services.find((s) => !s.up && s.name.startsWith("Ollama"));
    this.flow = new AgenticFlow(cfg, info);
    this.load(current.session);
    if (this.latest) this.flow.show(this.latest);
    this.render();
    this.awaitApproval();
  }

  unmount() {
    this.gone = true;
  }

  // The conversation is the server's current session (sessions.py): it
  // survives reloads and new tabs, and is gone once the session ends (sign
  // out, or 30 minutes idle). Earlier sessions are on the Sessions page.
  load(session) {
    const turns = session?.turns || [];
    this.messages = turns.map((t) => ({ question: t.question, interaction_id: String(t.turn_id),
      result: { ...t.result, question: t.question, turn_id: t.turn_id }, feedback: t.feedback }));
    this.latest = this.messages.length ? this.messages[this.messages.length - 1].result : null;
  }

  // ---------- Layout ----------

  render() {
    const draft = this.root.querySelector("#question")?.value;
    this.root.replaceChildren(
      h("div", { class: "stack" },
        this.renderAsk(),
        section("responses", "Answer",
          "Answers come from the live flow and are checked against their evidence before you see them. Follow-up questions keep the conversation's context; a new session starts after 30 minutes idle or with New conversation, and earlier ones are on the Sessions page. To test a flow and see each step, use the Agent Builder.",
          card({ children: [this.renderChatBar(), this.renderChat()] })),
        section("workflow", "Workflow", "The live flow, as built in the Agent Builder; each step lights up as it runs.", card({ cls: "wf-card", children: this.flow.element })),
      ),
    );
    const input = this.root.querySelector("#question");
    if (input && draft) input.value = draft;
    this.root.querySelectorAll(".chat").forEach((el) => { el.scrollTop = el.scrollHeight; });
  }

  renderAsk() {
    const ready = agentReady(this.info);
    const input = h("textarea", {
      id: "question", rows: 1, placeholder: askPlaceholder(this.info?.domain), disabled: !ready,
      onkeydown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.ask(); }
      },
    });
    const button = this.busy
      ? h("button", { class: "btn btn-ghost", id: "ask-btn", type: "button", title: "Stop this question: its agents stop",
        onclick: () => this.abort?.abort() }, "Cancel")
      : h("button", { class: "btn", id: "ask-btn", type: "button", disabled: !ready, onclick: () => this.ask() }, "Ask");
    const problem = ready ? null : errorBox(`Agent unavailable: ${this.info?.error || "not configured"}. ${this.info?.harness?.runtime === "local"
      ? "Configure the chat model in .env (see .env.example)."
      : this.info?.configured === false
        ? "Set AGENT_HARNESS_ARN for the web app (see agentcore-kb/README.md)."
        : "Check the web app's AWS permissions for the harness."}`);
    const down = this.modelDown && h("div", { class: "alert", role: "status", style: "margin-bottom:10px" },
      `The local AI model isn't running (${this.modelDown.name}), so questions can't be answered yet.`
      + (this.session?.can("manage_settings") && this.modelDown.fix ? ` ${this.modelDown.fix}` : ""));
    return card({ children: [down, h("div", { class: "ask" }, input, button), problem] });
  }

  // ---------- Chat ----------

  // New conversation (R26): ends the session, so earlier questions stop being context.
  renderChatBar() {
    if (!this.messages.length) return null;
    return h("div", { class: "chat-bar" }, h("button", {
      class: "btn btn-ghost btn-sm", type: "button", disabled: this.busy,
      title: "Start over: earlier questions are no longer used to read follow-ups (they stay on the Sessions page)",
      onclick: async () => {
        try { await api.newConversation(); } catch { /* the next question still starts fresh after 30 minutes */ }
        this.messages = [];
        this.latest = null;
        this.flow.reset();
        this.render();
      },
    }, "New conversation"));
  }

  renderChat() {
    const body = h("div", { class: "chat" });
    if (!this.messages.length) body.append(this.renderEmpty());
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
    if (r.status === "waiting") return this.renderWaiting(turn, r);
    const meta = [r.cached ? "from the answer cache" : fmtTime(r.total), r.cached ? null : shortModel(this.info?.harness?.model),
      r.flow ? `flow ${r.flow.id} v${r.flow.version}` : null]
      .filter(Boolean).join(" · ");
    const ae = r.answer_check;
    const verdict = ae ? h("span", { class: `ae-verdict ${ae.passed ? "pass" : "fail"}`,
      title: Object.entries(ae.scores).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(" · ") },
    ae.passed ? `✓ answer check passed · ${ae.overall.toFixed(2)}` : `✕ answer check failed on ${ae.failed_on.join(", ")}`) : null;
    if (r.budget?.hit) {   // the flow's budget ran out (R15): the answer uses what was found by then
      turn.append(h("div", { class: "privacy-note", role: "note" },
        `Stopped early: ${r.budget.hit} ran out, so this answer uses what was found by then.`));
    }
    const masked = r.privacy?.masked || r.privacy?.refused;
    if (masked?.length) {
      turn.append(h("div", { class: "privacy-note", role: "note" }, r.privacy.masked
        ? `Your question had ${personalData(masked)}: it was removed before anything else saw it.`
        : `Your question had ${personalData(masked)}, so it wasn't sent on.`));
    }
    if (r.read_as) {   // a follow-up, as the agents got it (R26)
      turn.append(h("div", { class: "read-as", role: "note" }, "Read as: ", h("em", {}, r.read_as)));
    }
    turn.append(h("div", { class: `reply ${ae && !ae.passed ? "withheld" : ""}` },
      h("div", { class: "reply-role" }, "◆ Answer", verdict),
      linked(h("div", { class: "reply-body", html: markdown(r.answer) }), r.citations),
      h("div", { class: "reply-meta" }, meta),
      r.turn_id && r.answer ? feedbackBar(r.turn_id, msg.feedback, (f) => { msg.feedback = f; }) : null));
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

  // A run paused at an Ask a human step (R32): a question for the user, or an answer awaiting approval.
  renderWaiting(turn, r) {
    if (r.waiting?.kind === "clarify") {
      turn.append(h("div", { class: "reply" }, h("div", { class: "reply-role" }, "◆ A question first"),
        h("div", { class: "reply-body", html: markdown(r.answer) })),
      h("div", { class: "read-as", role: "note" }, "Reply in the box above and your question goes on with it."));
    } else {
      turn.append(h("div", { class: "run-status" }, h("span", { class: "spinner" }), r.answer));
    }
    return turn;
  }

  // While an answer waits for approval, watch the session: the approved (or rejected) answer replaces it.
  async awaitApproval() {
    if (this.watching) return;
    this.watching = true;
    const waiting = (turns) => turns.some((t) => t.result?.status === "waiting" && t.result?.waiting?.kind === "approve");
    try {
      while (!this.gone && waiting(this.messages)) {
        await new Promise((r) => setTimeout(r, 10000));
        if (this.busy) continue;
        const { session } = await api.currentSession().catch(() => ({ session: null }));
        if (session && !waiting((session.turns || []).map((t) => ({ result: t.result })))) {
          this.load(session);
          if (this.latest) this.flow.show(this.latest);
          this.render();
        }
      }
    } finally {
      this.watching = false;
    }
  }

  // Before the first question: the domain's example questions, one click to ask.
  renderEmpty() {
    const examples = agentReady(this.info) ? this.info?.domain?.examples || [] : [];
    if (!examples.length) return h("div", { class: "chat-empty" }, "Ask a question above to get started.");
    return h("div", { class: "chat-empty" },
      h("div", {}, "Ask a question above, or try one of these:"),
      h("div", { class: "examples" }, ...examples.map((question) => h("button", {
        class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.ask(question),
      }, question))));
  }

  async ask(example) {
    const input = document.getElementById("question");
    if (typeof example === "string") input.value = example;
    const question = input.value.trim();
    if (!question || this.busy || !agentReady(this.info)) return;
    const msg = { question, interaction_id: crypto.randomUUID(), pending: true };
    this.messages.push(msg);
    input.value = "";
    this.flow.start(question);
    this.busy = true;
    this.abort = new AbortController();
    this.render();
    try {
      const result = await api.agentStream({ question, session_id: this.session.id, user_id: this.session.userId },
        (event) => {
          this.flow.event(event);
          this.onEvent(msg, event);
        }, this.abort.signal);
      this.latest = result;
      msg.result = result;
      Object.assign(msg, { pending: false, checking: undefined, writing: undefined, searching: undefined });
      this.flow.finish(result);
      if (result.status === "waiting") setTimeout(() => this.awaitApproval());
    } catch (err) {
      if (err.name === "AbortError") {   // the person cancelled: the server stops the run
        Object.assign(msg, { pending: false, notice: "Cancelled." });
        this.flow.reset();
      } else if (err.setup) {   // not a failure: nothing ran, the knowledge system is still being set up
        Object.assign(msg, { pending: false, notice: err.message });
        this.flow.reset();
      } else if (err instanceof TypeError) {   // the connection dropped (the app restarted?): the run resumes there (R30)
        Object.assign(msg, { pending: false, notice: "The connection was lost. If the app restarted, your question "
          + "carries on there and its answer will appear here when it's done." });
        this.flow.reset();
        this.awaitResumed(msg);
      } else {
        Object.assign(msg, { pending: false, error: err.message });
        this.flow.fail(err.message);
      }
    } finally {
      this.busy = false;
      this.abort = null;
      this.render();
    }
  }

  // After a lost connection: watch the session for the resumed run's answer (for up to five minutes).
  async awaitResumed(msg, tries = 60) {
    const known = this.messages.length - 1;
    for (let i = 0; i < tries && this.messages.includes(msg); i++) {
      await new Promise((r) => setTimeout(r, 5000));
      const { session } = await api.currentSession().catch(() => ({ session: null }));
      if ((session?.turns?.length || 0) > known) {
        this.load(session);
        if (this.latest) this.flow.show(this.latest);
        this.render();
        return;
      }
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

// app/pii.py's kinds, in words: "an email address and a phone number".
const PERSONAL = { email: "an email address", card: "a card number", iban: "a bank account number",
  ni_number: "a National Insurance number", phone: "a phone number", ip: "an IP address" };
function personalData(kinds) {
  const words = kinds.map((k) => PERSONAL[k] || k);
  return words.length > 1 ? `${words.slice(0, -1).join(", ")} and ${words.at(-1)}` : words[0];
}

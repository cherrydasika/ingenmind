// Monitoring → Approvals (R32, app/human.py): answers and actions an Ask a
// human step is holding for a person. Approve and the user gets the answer and
// the chosen actions run; reject (or let it time out) and nothing runs and
// the user gets the flow's rejection message.

import { api } from "./api.js";
import { badge, card, errorBox, h, loading, markdown, note } from "./ui.js";

const POLL_MS = 10000;
const KIND = { open: "warn", answered: "ok", expired: "neutral", cancelled: "neutral" };
const when = (iso) => (iso ? String(iso).slice(0, 19).replace("T", " ") : "—");

export class ApprovalsPage {
  title = "Approvals";

  constructor(root) {
    this.root = root;
    this.timer = null;
    this.flash = null;
    this.unticked = {};   // request id → action numbers the approver unticked
  }

  unmount() {
    clearTimeout(this.timer);
  }

  async mount() {
    this.root.replaceChildren(loading());
    await this.load();
  }

  async load() {
    clearTimeout(this.timer);
    try {
      this.data = await api.approvals();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load approvals: ${err.message}`));
      return;
    }
    this.render();
    this.timer = setTimeout(() => this.load(), POLL_MS);
  }

  async decide(request, approved) {
    const skip = this.unticked[request.request_id] || new Set();
    const actions = request.actions.map((a) => a.n).filter((n) => !skip.has(n));
    try {
      await api.decideApproval(request.request_id, { approved, ...(request.actions.length ? { actions } : {}) });
      this.flash = null;
    } catch (err) {
      this.flash = err.message;
    }
    setTimeout(() => this.load(), 600);
  }

  renderActions(r) {
    if (!r.actions.length) return null;
    const skip = (this.unticked[r.request_id] ||= new Set());
    return h("div", {}, h("div", { class: "faint" }, "Proposed actions (run only if approved):"),
      ...r.actions.map((a) => h("label", { class: "check-row" },
        r.status === "open" ? h("input", { type: "checkbox", checked: !skip.has(a.n),
          onchange: (e) => (e.target.checked ? skip.delete(a.n) : skip.add(a.n)) }) : null,
        h("strong", {}, a.title || a.tool), " ", h("code", {}, JSON.stringify(a.input)))));
  }

  renderRequest(r) {
    const decided = r.response?.approved === true ? "approved" : r.response?.approved === false ? "rejected" : null;
    return card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, badge(r.status === "answered" && decided ? decided : r.status, KIND[r.status] || "neutral"),
          " ", h("span", { class: "faint" }, `asked ${when(r.created_at)}`
            + (r.status === "open" ? ` · times out ${when(r.expires_at)}` : r.answered_at ? ` · decided ${when(r.answered_at)}` : ""))),
        r.status === "open" ? h("div", {},
          h("button", { class: "btn btn-sm", type: "button", onclick: () => this.decide(r, true) }, "Approve"),
          " ",
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.decide(r, false) }, "Reject")) : null),
      h("div", { class: "q" }, r.asked),
      r.draft ? h("div", { class: "reply" }, h("div", { class: "reply-role" }, "◆ Answer to approve"),
        h("div", { class: "reply-body", html: markdown(r.draft) })) : null,
      this.renderActions(r),
    ] });
  }

  render() {
    const requests = this.data.requests;
    const open = requests.filter((r) => r.status === "open");
    this.root.replaceChildren(h("div", { class: "stack" },
      note("Answers and actions waiting for a person, from flows with an Ask a human (approve) step. The run is "
        + "paused, and stays paused across restarts, until you decide. Approve: the user gets the answer and the "
        + "ticked actions run. Reject, or no decision before it times out: nothing runs and the user gets the "
        + "flow's rejection message."),
      this.flash ? errorBox(this.flash) : null,
      open.length ? null : card({ children: h("div", { class: "empty-state" }, "Nothing is waiting for approval.") }),
      ...requests.map((r) => this.renderRequest(r))));
  }
}

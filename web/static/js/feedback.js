// Answer feedback (R51, app/answer_feedback.py): thumbs up or down under each
// answer on the Ask page, with a reason and a comment when it is down; on the
// Sessions page it shows what was given. Evaluations → From answer feedback
// lists the thumbs-down, and an admin can add one to a golden set with the
// corrected answer.

import { api } from "./api.js";
import { badge, card, errorBox, h, markdown, note } from "./ui.js";

export const REASONS = { wrong: "Wrong", incomplete: "Incomplete", out_of_date: "Out of date", not_cited: "Not cited", other: "Other" };

const said = (f) => [REASONS[f.reason], f.comment].filter(Boolean).join(" — ");

// What was given, read-only (Sessions).
export function feedbackShown(f) {
  if (!f) return null;
  return h("div", { class: "feedback-shown faint" }, f.rating === "up" ? "👍 helpful" : "👎 not helpful",
    said(f) ? `: ${said(f)}` : "", f.promoted_to ? " " : null, f.promoted_to ? badge(`in golden set ${f.promoted_to}`, "ok") : null);
}

// The asker's own thumbs (Ask page). turnId: the session turn the answer is.
export function feedbackBar(turnId, current, onSaved) {
  const box = h("div", { class: "feedback" });
  let state = current || null;
  let open = false;
  const send = async (rating, reason = null, comment = "") => {
    try {
      const { feedback } = await api.giveFeedback(turnId, { rating, reason, comment });
      state = feedback;
      open = false;
      onSaved?.(feedback);
      draw();
    } catch (err) {
      draw(err.message);
    }
  };
  const draw = (problem) => {
    const thumb = (rating, icon, label) => h("button", {
      type: "button", class: `btn btn-ghost btn-sm thumb ${state?.rating === rating ? "on" : ""}`,
      "aria-pressed": String(state?.rating === rating), title: label, "aria-label": label,
      onclick: () => (rating === "up" ? send("up") : (open = !open, draw())),
    }, icon);
    const row = h("div", { class: "feedback-row" }, thumb("up", "👍", "Helpful"), thumb("down", "👎", "Not helpful"),
      state && !open ? h("span", { class: "faint" }, state.rating === "up" ? "Thanks." : `Thanks${said(state) ? `: ${said(state)}` : "."}`) : null);
    let form = null;
    if (open) {
      const reason = h("select", { "aria-label": "What was wrong" },
        h("option", { value: "" }, "What was wrong? (optional)"),
        Object.entries(REASONS).map(([k, v]) => h("option", { value: k, selected: state?.reason === k }, v)));
      const comment = h("input", { type: "text", maxlength: 1000, placeholder: "Anything to add? (optional)", value: state?.comment || "" });
      form = h("div", { class: "feedback-form" }, reason, comment,
        h("button", { class: "btn btn-sm", type: "button", onclick: () => send("down", reason.value || null, comment.value) }, "Send"));
    }
    box.replaceChildren(...[row, form, problem ? errorBox(problem) : null].filter(Boolean));
  };
  draw();
  return box;
}

// Evaluations → From answer feedback.
export class FeedbackReview {
  constructor() {
    this.root = h("div");
    this.flash = null;
  }

  async load() {
    try {
      this.data = await api.feedback();
    } catch (err) {
      this.root.replaceChildren();   // without view_sessions: not shown
      return;
    }
    this.render();
  }

  item(f) {
    const expected = h("textarea", { class: "input", rows: 3, style: "min-height: 70px", placeholder: "The corrected answer", "aria-label": "Corrected answer" });
    const add = async () => {
      try {
        const out = await api.promoteFeedback(f.turn_id, { expected: expected.value });
        this.flash = { ok: `Added to golden set ${out.set_id} (${out.questions} question${out.questions === 1 ? "" : "s"}).` };
      } catch (err) {
        this.flash = { error: err.message };
      }
      await this.load();
    };
    return h("div", { class: "feedback-item" },
      h("div", {}, h("strong", {}, f.question)),
      h("div", { class: "faint" }, `${f.first_name || "someone"} · ${new Date(f.updated_at).toLocaleString()}`,
        f.flow ? ` · flow ${f.flow.id} v${f.flow.version}` : ""),
      h("div", {}, badge(REASONS[f.reason] || "no reason given", "warn"), f.comment ? ` ${f.comment}` : ""),
      h("details", {}, h("summary", {}, "The answer"), h("div", { class: "reply-body", html: markdown(f.answer) })),
      f.promoted_to ? h("div", {}, badge(`in golden set ${f.promoted_to}`, "ok"))
        : h("div", { class: "feedback-promote" }, expected,
          h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: add }, "Add to golden set"))));
  }

  render() {
    const { feedback, summary } = this.data;
    const reasons = Object.entries(summary.reasons).map(([k, n]) => `${REASONS[k] || "no reason"} ${n}`).join(" · ");
    this.root.replaceChildren(card({ title: "From answer feedback", children: [
      h("p", { class: "note" }, `Last ${summary.days} days: 👍 ${summary.up} · 👎 ${summary.down}${reasons ? ` (${reasons})` : ""}. `
        + `Each rating is also the "user_feedback" score on the answer's Langfuse trace. Add a thumbs-down to the golden set `
        + `"${this.data.feedback_set}" with the corrected answer, and evaluate flows against it below.`),
      this.flash?.error ? errorBox(this.flash.error) : null,
      this.flash?.ok ? h("div", { class: "alert ok", role: "status" }, this.flash.ok) : null,
      feedback.length ? h("div", { class: "stack" }, feedback.map((f) => this.item(f))) : note("No thumbs-down yet."),
    ] }));
  }
}

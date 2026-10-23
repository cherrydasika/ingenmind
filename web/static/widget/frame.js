// The widget's chat page (R50, app/channels/widget.py), inside the frame on
// the embedding site: questions go to /widget/<id>/ask, answers come back
// with their sources and 👍 / 👎 (answer_feedback.py).

import { h, markdown } from "../js/ui.js";

const id = document.body.dataset.widget;
const log = document.getElementById("log");
const form = document.getElementById("ask");
const input = document.getElementById("q");
const REASONS = { wrong: "Wrong", incomplete: "Incomplete", out_of_date: "Out of date", not_cited: "Not cited", other: "Other" };

// Who this visitor is to the widget (its conversation), kept in this frame's storage when it can be.
let visitor = null;
try { visitor = localStorage.getItem("ingenmind-visitor"); } catch { /* storage blocked: one conversation per page */ }
if (!visitor) {
  visitor = crypto.randomUUID();
  try { localStorage.setItem("ingenmind-visitor", visitor); } catch { /* fine */ }
}

const post = async (path, body) => {
  const reply = await fetch(`/widget/${encodeURIComponent(id)}/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "same-origin" });
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) throw Object.assign(new Error(data.error || `HTTP ${reply.status}`), { status: reply.status, data });
  return data;
};

function thumbs(turnId) {
  const box = h("div", { class: "thumbs" });
  const rate = async (rating, reason = null) => {
    try {
      await post("feedback", { visitor, turn_id: turnId, rating, reason });
      box.replaceChildren(rating === "up" ? "Thanks." : "Thanks for telling us.");
    } catch (err) {
      box.replaceChildren(err.message);
    }
  };
  const why = () => {
    const pick = h("select", { "aria-label": "What was wrong" }, h("option", { value: "" }, "What was wrong?"),
      Object.entries(REASONS).map(([k, v]) => h("option", { value: k }, v)));
    pick.addEventListener("change", () => rate("down", pick.value || null));
    box.replaceChildren(pick, h("button", { type: "button", onclick: () => rate("down") }, "Skip"));
  };
  box.append(h("button", { type: "button", "aria-label": "Helpful", onclick: () => rate("up") }, "👍"),
    h("button", { type: "button", "aria-label": "Not helpful", onclick: why }, "👎"));
  return box;
}

function answer(out) {
  const sources = (out.sources || []).filter((s) => s.cited?.length)
    .sort((a, b) => Math.min(...a.cited) - Math.min(...b.cited))
    .map((s) => h("span", {}, `[${[...s.cited].sort((x, y) => x - y).join(", ")}] `,
      h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.organisation || new URL(s.url).hostname), " "));
  return h("div", { class: "a" }, h("div", { html: markdown(out.answer || "") }),
    sources.length ? h("div", { class: "sources" }, sources) : null,
    out.answer_check && !out.answer_check.passed ? h("div", { class: "note" }, "This answer didn't pass the answer check.") : null,
    out.turn_id ? thumbs(out.turn_id) : null);
}

function scroll() { log.scrollTop = log.scrollHeight; }

if (document.body.dataset.greeting) log.append(h("div", { class: "a" }, document.body.dataset.greeting));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  const button = form.querySelector("button");
  button.disabled = true;
  const waiting = h("div", { class: "note" }, "Looking that up…");
  log.append(h("div", { class: "q" }, question), waiting);
  scroll();
  try {
    waiting.replaceWith(answer(await post("ask", { question, visitor })));
  } catch (err) {
    waiting.replaceWith(err.status === 401
      ? h("div", { class: "note" }, "Sign in to ask: ", h("a", { href: "/", target: "_blank", rel: "noopener" }, "open IngenMind"), ", then ask again.")
      : h("div", { class: "note" }, err.message));
  } finally {
    button.disabled = false;
    scroll();
    input.focus();
  }
});

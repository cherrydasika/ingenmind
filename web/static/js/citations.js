// Citation previews (R23): each [n] in an answer becomes a button; hovering,
// focusing or tapping it shows the passage it cites (app/citations.py), the
// sentences backing the claim highlighted, with a link that opens the page
// scrolled to them (a text fragment, where the browser supports it).
// Escape or a tap elsewhere closes it. Text is set as text, never as HTML.

import { h } from "./ui.js";

let open = null;   // {button, card, at}

function close({ refocus = false } = {}) {
  if (!open) return;
  open.button.setAttribute("aria-expanded", "false");
  open.card.remove();
  if (refocus) open.button.focus();
  open = null;
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) close({ refocus: true }); });
document.addEventListener("pointerdown", (e) => {
  if (open && !open.card.contains(e.target) && e.target !== open.button) close();
});
window.addEventListener("resize", () => close());
document.addEventListener("scroll", () => close(), true);   // the card is fixed to where the citation was

// The passage with its marked sentences as <mark>.
function passage(text, marks) {
  const out = [];
  let at = 0;
  for (const [start, end] of marks || []) {
    if (start < at || end > text.length) continue;
    if (start > at) out.push(text.slice(at, start));
    out.push(h("mark", {}, text.slice(start, end)));
    at = end;
  }
  out.push(text.slice(at));
  return out;
}

// Opens the page at the first marked sentence (scroll-to-text fragment).
function pageLink(c) {
  if (!/^https?:\/\//.test(c.url)) return null;
  const first = c.marks?.[0];
  let href = c.url.split("#")[0];
  if (first) {
    const words = c.text.slice(first[0], first[1]).replace(/[\n\r]+/g, " ").trim().split(/\s+/);
    const enc = (s) => encodeURIComponent(s).replace(/-/g, "%2D");
    href += words.length > 8
      ? `#:~:text=${enc(words.slice(0, 4).join(" "))},${enc(words.slice(-4).join(" "))}`
      : `#:~:text=${enc(words.join(" "))}`;
  }
  return h("a", { href, target: "_blank", rel: "noopener", class: "cite-open" }, "Open the page ↗");
}

function show(button, n, c) {
  if (open?.button === button) return;
  close();
  const id = `cite-card-${n}-${Math.random().toString(36).slice(2, 7)}`;
  // Right after the citation, so Tab reaches its link next; shown fixed beside it.
  const card = h("span", { class: "cite-card", id, role: "dialog", "aria-label": `Source ${n}` },
    h("span", { class: "cite-head" },
      h("span", { class: "cite-num" }, `[${n}]`),
      h("span", { class: "cite-title" }, c.title || c.site || c.url),
      c.title && c.site ? h("span", { class: "faint" }, ` · ${c.site}`) : null),
    c.headings?.length ? h("span", { class: "cite-path faint" }, c.headings.join(" › ")) : null,
    h("span", { class: "cite-text" }, passage(c.text, c.marks)),
    h("span", { class: "cite-foot" },
      c.marks?.length ? h("span", { class: "faint" }, "Highlighted: the sentences backing the claim") : h("span", { class: "faint" }, "The cited passage"),
      pageLink(c)));
  button.after(card);
  button.setAttribute("aria-expanded", "true");
  button.setAttribute("aria-controls", id);
  // Below the citation (above when there's no room); a bottom sheet on phones (CSS).
  const r = button.getBoundingClientRect();
  const w = card.offsetWidth;
  const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left - 20));
  const below = r.bottom + card.offsetHeight + 12 < window.innerHeight;
  card.style.left = `${left}px`;
  card.style.top = `${Math.max(8, below ? r.bottom + 6 : r.top - card.offsetHeight - 6)}px`;
  card.addEventListener("mouseleave", (e) => { if (e.relatedTarget !== button) close(); });
  card.addEventListener("focusout", (e) => { if (!card.contains(e.relatedTarget) && e.relatedTarget !== button) close(); });
  open = { button, card, at: Date.now() };
}

// Turns [n] in the rendered answer into preview buttons where a passage exists.
export function linkCitations(container, citations) {
  if (!citations || !Object.keys(citations).length) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (/\[\d+\]/.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const parts = node.nodeValue.split(/(\[\d+\])/);
    const frag = document.createDocumentFragment();
    for (const part of parts) {
      const n = part.match(/^\[(\d+)\]$/)?.[1];
      const c = n && citations[n];
      if (!c) { if (part) frag.append(part); continue; }
      const button = h("button", { type: "button", class: "cite", "aria-expanded": "false",
        "aria-label": `Source ${n}: show the cited passage` }, `[${n}]`);
      button.addEventListener("click", (e) => {
        e.preventDefault();
        // A tap focuses first (which opens it): only a later click closes it.
        if (open?.button === button && Date.now() - open.at > 400) close(); else show(button, n, c);
      });
      button.addEventListener("blur", (e) => { if (open?.button === button && !open.card.contains(e.relatedTarget)) close(); });
      button.addEventListener("focus", () => show(button, n, c));
      button.addEventListener("mouseenter", () => show(button, n, c));
      button.addEventListener("mouseleave", (e) => { if (open?.button === button && !open.card.contains(e.relatedTarget)) close(); });
      frag.append(button);
    }
    node.replaceWith(frag);
  }
}

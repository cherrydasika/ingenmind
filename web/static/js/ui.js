// DOM helpers shared by every page. Content is always set as text or built
// from escaped strings — model output never goes into innerHTML raw.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value; // only for our own escaped markup
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(2)}s`;
}

export function fmtInt(n) {
  return Number(n).toLocaleString("en-US");
}

export function round(n, digits = 3) {
  return Number(n.toFixed(digits));
}

export function shortUrl(url) {
  try {
    return new URL(url).pathname || url;
  } catch {
    return url;
  }
}

const LIST = { ul: /^\s*[-*]\s+/, ol: /^\s*\d+[.)]\s+/ };

// Minimal Markdown for LLM answers: escape first, then headings, paragraphs,
// lists, **bold**, *italic*, `code`, and http(s) links. Enough for short answers.
export function markdown(text) {
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Headings become blocks of their own, even without blank lines around them.
  const blocks = escapeHtml(text || "").replace(/^ {0,3}#{1,6}\s+(.+)$/gm, "\n\n\u0000$1\n\n").trim().split(/\n{2,}/);
  return blocks.map((block) => {
    if (block.startsWith("\u0000")) return `<h4>${inline(block.slice(1))}</h4>`;
    // A block may mix text and list lines ("For example:" then "- …" with no
    // blank line between, as models often write): each run becomes its own element.
    const runs = [];
    for (const line of block.split("\n")) {
      const kind = LIST.ul.test(line) ? "ul" : LIST.ol.test(line) ? "ol" : "p";
      if (runs.length && runs[runs.length - 1].kind === kind) runs[runs.length - 1].lines.push(line);
      else runs.push({ kind, lines: [line] });
    }
    return runs.map(({ kind, lines }) => kind === "p"
      ? `<p>${lines.map(inline).join("<br>")}</p>`
      : `<${kind}>${lines.map((l) => `<li>${inline(l.replace(LIST[kind], ""))}</li>`).join("")}</${kind}>`).join("");
  }).join("");
}

// Code-ish inline text: `like this` → <code>, the rest escaped.
export function richText(text) {
  return escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

export function note(text) {
  return h("p", { class: "note", html: richText(text) });
}

export function badge(text, kind = "neutral") {
  return h("span", { class: `badge badge-${kind}` }, text);
}

export function tag(letter) {
  return h("span", { class: `tag tag-${letter.toLowerCase()}` }, letter);
}

export function check(matches, what) {
  return matches
    ? h("div", { class: "check ok" }, `✓ ${what} recomputed from the numbers above — matches Qdrant's scores exactly.`)
    : h("div", { class: "check bad", html: `⚠ ${escapeHtml(what)} recomputed from the numbers above — does <strong>not</strong> match Qdrant's scores.` });
}

// "Setting | Value | What it means" table, value rendered as code.
export function setupTable(rows) {
  return h("div", { class: "table-wrap" },
    h("table", { class: "setup" },
      h("thead", {}, h("tr", {}, h("th", {}, "Setting"), h("th", {}, "Value"), h("th", {}, "What it means"))),
      h("tbody", {}, rows.map(([name, value, meaning]) =>
        h("tr", {}, h("td", {}, name), h("td", {}, h("code", {}, value)), h("td", {}, meaning)))),
    ));
}

// Plain data table. columns: [{key, label, num?}] ; rows: objects.
export function dataTable(columns, rows) {
  return h("div", { class: "table-wrap" },
    h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : null }, c.label)))),
      h("tbody", {}, rows.map((row) =>
        h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : null }, row[c.key] ?? "—"))))),
    ));
}

export function card({ title, sub, shared = false, children = [], cls = "" }) {
  return h("div", { class: `card ${cls}` },
    title ? h("div", { class: "card-head" },
      h("div", { class: "card-title" }, title, shared ? h("span", { class: "shared-tag" }, "Shared") : null)) : null,
    sub ? h("p", { class: "card-sub", html: richText(sub) }) : null,
    children,
  );
}

// Show/Hide button for a collapsible body. Hidden unless this browser last
// left it open (localStorage, best effort). onOpen runs after revealing —
// for content that can only be measured while visible, like Plotly charts.
export function foldButton(key, body, { onOpen } = {}) {
  const storageKey = `rag.open.${key}`;
  let open = false;
  try { open = localStorage.getItem(storageKey) === "1"; } catch { /* default hidden */ }
  const btn = h("button", { class: "btn btn-ghost btn-sm fold-btn", type: "button" });
  const apply = () => {
    body.hidden = !open;
    btn.textContent = open ? "Hide ▴" : "Show ▾";
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", () => {
    open = !open;
    try { localStorage.setItem(storageKey, open ? "1" : "0"); } catch { /* per-viewer nicety only */ }
    apply();
    if (open) onOpen?.();
  });
  apply();
  return btn;
}

// Subsection whose h3 carries a Show/Hide button; the one-line summary stays
// visible, everything else folds away.
export function foldSection(key, title, summary, children, opts) {
  const body = h("div", { class: "fold-body" }, children);
  return h("div", { class: "subsection" },
    h("h3", {}, title, foldButton(key, body, opts)),
    summary ? h("p", { class: "section-desc", html: richText(summary) }) : null,
    body);
}

export function section(id, title, desc, ...children) {
  return h("section", { class: "section", id },
    h("div", { class: "section-head" }, h("h2", {}, title)),
    desc ? h("p", { class: "section-desc", html: richText(desc) }) : null,
    children,
  );
}

// "2026-09-27T19:54:53.123+00:00" → "2026-09-27 19:54:53"
export function fmtTimestamp(iso) {
  return iso ? String(iso).slice(0, 19).replace("T", " ") : "—";
}

// Days until a unix timestamp: "12.3d" / "expired".
export function daysLeft(expiresAt) {
  if (expiresAt === null || expiresAt === undefined) return "—";
  const days = (expiresAt - Date.now() / 1000) / 86400;
  return days < 0 ? "⚠ expired" : `${days.toFixed(1)}d`;
}

export function metric(label, value) {
  return h("div", { class: "metric" }, h("div", { class: "metric-label" }, label), h("div", { class: "metric-value" }, value));
}

export function loading() {
  return h("div", { class: "empty-state" }, h("div", { class: "spinner", style: "margin:0 auto" }));
}

export function errorBox(message) {
  return h("div", { class: "alert err" }, message);
}

// Markdown block (descriptions, docs) — same escaping renderer as answers.
export function md(text, cls = "prose") {
  return h("div", { class: cls, html: markdown(text) });
}

// One source an answer cites: a link (ad-hoc documents have no web address),
// who published it and its date when the page is labelled (#15), and the
// citation numbers that point to it.
export function sourceItem({ url, cited, organisation, effective_date }) {
  const page = /^https?:\/\//.test(url) ? h("a", { href: url, target: "_blank", rel: "noopener" }, shortUrl(url)) : h("span", {}, url);
  const about = [organisation, effective_date].filter(Boolean).join(", ");
  return h("li", {}, page, h("span", { class: "faint" }, `${about ? ` · ${about}` : ""} · ${cited.map((n) => `[${n}]`).join(" ")}`));
}

// A label key as words: "tickets_and_railcards" → "Tickets and railcards".
export function labelText(key) {
  const words = String(key).replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// What a stored page is about (#15): its topics, kind of page and publisher, for a table cell.
export function labelsCell(labels) {
  if (!labels) return h("td", { class: "faint" }, "—");
  const kind = [labels.content_type && labelText(labels.content_type), labels.organisation].filter(Boolean).join(" · ");
  return h("td", { class: "wrap" }, (labels.topic || []).map((t) => badge(labelText(t))),
    kind ? h("div", { class: "faint", style: "white-space:nowrap" }, kind) : null);
}

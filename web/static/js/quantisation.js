// Quantisation demo on the Pipeline docs page: an 8-dim example vector, stored in
// a few formats, drawn original-vs-quantised so the precision lost for the
// bytes saved is visible. Runs entirely in the browser — nothing is sent to
// Qdrant. Add a format to FORMATS and it shows up everywhere below.

import { floatLayouts, intMappings } from "./quant_explainer.js";
import { h, metric, note, richText } from "./ui.js";

const DIMS = 8;
const EXAMPLE = [0.42, -0.31, 0.08, 0.67, -0.55, 0.12, -0.04, 0.29];
const EXAMPLE_POINTS = 1_000_000;
const OPEN_KEY = "rag.quant.open";

// Scalar quantisation onto 2^bits evenly spaced levels between min and max.
// Here the range is this vector's own; Qdrant takes it from the collection.
function scalar(bits) {
  return (vector) => {
    const lo = Math.min(...vector);
    const hi = Math.max(...vector);
    const levels = 2 ** bits - 1;
    const step = hi > lo ? (hi - lo) / levels : 1;
    const codes = vector.map((x) => Math.round((x - lo) / step));
    return {
      values: codes.map((c) => lo + c * step),
      codes,
      levels: Array.from({ length: levels + 1 }, (_, i) => lo + i * step),
      stored: `codes 0–${levels}: [${codes.join(", ")}]\nrange [${lo.toFixed(4)}, ${hi.toFixed(4)}] → step ${step.toFixed(4)}`,
    };
  };
}

function toFloat16(x) {
  if (typeof Float16Array !== "undefined") return new Float16Array([x])[0];
  if (x === 0) return 0;
  const exp = Math.max(Math.floor(Math.log2(Math.abs(x))), -14);
  const step = 2 ** (exp - 10); // 10 mantissa bits
  return Math.round(x / step) * step;
}

const floats = (vector) => `[${vector.map((v) => v.toFixed(4)).join(", ")}]`;

const FORMATS = [
  {
    key: "float32", label: "float32", bits: 32,
    about: "What Qdrant stores today: 4 bytes per dimension, exact.",
    quantise: (v) => ({ values: v.map(Math.fround), stored: floats(v.map(Math.fround)) }),
  },
  {
    key: "float16", label: "float16", bits: 16,
    about: "Half precision (Qdrant `datatype: float16`): ~3 significant digits — invisible at this scale.",
    quantise: (v) => {
      const values = v.map(toFloat16);
      return { values, stored: floats(values) };
    },
  },
  {
    key: "int8", label: "int8 · scalar", bits: 8,
    about: "Qdrant scalar quantisation: 256 levels between the range's min and max, 1 byte per dimension. The go-to 4× saving.",
    quantise: scalar(8),
  },
  {
    key: "int4", label: "int4", bits: 4,
    about: "16 levels — shown to make the steps visible; Qdrant's scalar quantisation is int8 only.",
    quantise: scalar(4),
  },
  {
    key: "binary", label: "binary", bits: 1,
    about: "Qdrant binary quantisation: keep only the sign, 1 bit per dimension, compared by Hamming distance. Drawn as ±mean|x|. Needs rescoring with the originals to rank well.",
    quantise: (v) => {
      const mean = v.reduce((s, x) => s + Math.abs(x), 0) / v.length;
      const bits = v.map((x) => (x > 0 ? 1 : 0));
      const bytes = [];
      for (let i = 0; i < bits.length; i += 8) {
        bytes.push(parseInt(bits.slice(i, i + 8).join("").padEnd(8, "0"), 2));
      }
      return {
        values: bits.map((b) => (b ? mean : -mean)),
        levels: [-mean, mean],
        stored: `bits: ${bits.join("")}\n= ${bytes.map((b) => `0x${b.toString(16).padStart(2, "0").toUpperCase()}`).join(" ")}`,
      };
    },
  },
];

function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] ** 2; nb += b[i] ** 2; }
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}

function bytesFor(format, dims) {
  return Math.ceil((dims * format.bits) / 8);
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0)} ${units[i]}`;
}

function niceMax(vector) {
  const peak = Math.max(0.05, ...vector.map(Math.abs)) * 1.15;
  const pow = 10 ** Math.floor(Math.log10(peak));
  return Math.ceil(peak / pow * 2) / 2 * pow;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}

// PAD_L / PAD_R also line the value boxes up under the columns (.quant-row).
const W = 560, H = 234, PAD_L = 44, PAD_R = 8, PAD_T = 12, PAD_B = 10;

export class QuantisationDemo {
  constructor() {
    this.vector = [...EXAMPLE];
    this.yMax = niceMax(this.vector);
    this.format = FORMATS.find((f) => f.key === "int8");
    this.dragging = null;
    this.selected = 0;
    this.clip = null;
    this.el = null;
    try { this.open = localStorage.getItem(OPEN_KEY) === "1"; } catch { this.open = false; }
  }

  // Built once and reused, so the chosen format and edited vector survive the
  // page's full re-renders. points: the collection's chunk count, if known.
  // The whole section body — demo, under the hood, scaled up — hides behind
  // one Show/Hide button, which goes in the section heading (toggleButton()).
  element({ embeddings, points }) {
    this.ctx = { embeddings, points };
    if (!this.el) this.build();
    this.update();
    return this.el;
  }

  toggleButton() {
    if (!this.el) this.build();
    return this.toggleBtn;
  }

  build() {
    this.segmented = h("div", { class: "segmented" });
    this.svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "quant-chart", role: "img" });
    this.svg.addEventListener("pointerdown", (e) => this.drag(e, true));
    this.svg.addEventListener("pointermove", (e) => this.drag(e, false));
    const stop = () => { this.dragging = null; };
    this.svg.addEventListener("pointerup", stop);
    this.svg.addEventListener("pointercancel", stop);
    this.info = h("div", {});
    // One box per dimension: typing sets the value exactly and selects it for
    // Under the hood, which follows on every keystroke.
    this.inputs = Array.from({ length: DIMS }, (_, i) => h("input", {
      type: "number", step: "0.01", "aria-label": `d${i}`, class: "quant-input",
      onfocus: () => { if (this.selected !== i) { this.selected = i; this.clip = null; this.update(); } },
      oninput: (e) => {
        const v = Number(e.target.value);
        if (e.target.value === "" || !Number.isFinite(v)) return;
        this.vector[i] = v;
        this.selected = i;
        this.clip = null;
        if (Math.abs(v) > this.yMax) this.yMax = niceMax(this.vector);
        this.update();
      },
    }));
    this.outputs = Array.from({ length: DIMS }, () => h("span", { class: "quant-out-val" }));
    this.storage = h("div", {});
    this.toggleBtn = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-controls": "quantisation-body", onclick: () => this.toggle() });
    this.explainer = h("div", { class: "stack" });
    const explainerCard = h("div", { class: "card" },
      h("div", { class: "card-title" }, "Under the hood — floats, bits and int8 mappings"),
      h("p", { class: "card-sub", style: "margin:4px 0 0" }, "How one number is laid out in float32, float16 and bfloat16, and symmetric vs asymmetric int8 — after Maarten Grootendorst's ",
        h("a", { href: "https://newsletter.maartengrootendorst.com/p/a-visual-guide-to-quantization", target: "_blank", rel: "noopener" }, "A Visual Guide to Quantization"), "."),
      this.explainer);
    this.el = h("div", { class: "stack", id: "quantisation-body" },
      h("div", { class: "card" },
        h("div", { class: "quant-controls" },
          this.segmented,
          h("div", { class: "actions", style: "margin:0" },
            h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.randomise() }, "Randomise"),
            h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.load(EXAMPLE) }, "Reset"))),
        h("div", { class: "grid-2", style: "margin-top:14px" },
          h("div", {},
            this.svg,
            h("div", { class: "quant-row", style: `--pad-l:${(PAD_L / W) * 100}%;--pad-r:${(PAD_R / W) * 100}%` },
              h("span", { class: "quant-row-label" }, "x"), this.inputs),
            h("div", { class: "quant-row quant-out", style: `--pad-l:${(PAD_L / W) * 100}%;--pad-r:${(PAD_R / W) * 100}%` },
              h("span", { class: "quant-row-label" }, "quant"), this.outputs),
            h("div", { class: "quant-legend" },
              h("span", {}, h("i", { class: "sw sw-orig" }), "original"),
              h("span", {}, h("i", { class: "sw sw-quant" }), "quantised"),
              h("span", {}, h("i", { class: "sw sw-level" }), "levels it can store"),
              h("span", { class: "faint" }, "Type a value or drag a column"))),
          this.info)),
      explainerCard,
      this.storage);
  }

  toggle() {
    this.open = !this.open;
    try { localStorage.setItem(OPEN_KEY, this.open ? "1" : "0"); } catch { /* per-viewer nicety only */ }
    this.update();
  }

  load(vector) {
    this.vector = [...vector];
    this.clip = null;
    this.yMax = niceMax(this.vector);
    this.update();
  }

  randomise() {
    this.load(Array.from({ length: DIMS }, () => Math.round((Math.random() * 1.6 - 0.8) * 100) / 100));
  }

  drag(e, start) {
    if (start) {
      this.svg.setPointerCapture(e.pointerId);
      this.dragging = true;
    }
    if (!this.dragging) return;
    const rect = this.svg.getBoundingClientRect();
    const x = ((e.clientX - rect.left) * W) / rect.width;
    const y = ((e.clientY - rect.top) * H) / rect.height;
    const col = Math.floor(((x - PAD_L) / (W - PAD_L - PAD_R)) * DIMS);
    if (col < 0 || col >= DIMS) return;
    this.selected = col;
    this.clip = null;
    const plotH = H - PAD_T - PAD_B;
    const value = this.yMax - ((y - PAD_T) / plotH) * 2 * this.yMax;
    this.vector[col] = Math.round(Math.max(-this.yMax, Math.min(this.yMax, value)) * 1000) / 1000;
    this.update();
  }

  update() {
    const q = this.format.quantise(this.vector);
    this.segmented.replaceChildren(...FORMATS.map((f) => h("button", {
      type: "button", class: f === this.format ? "active" : null,
      onclick: () => { this.format = f; this.update(); },
    }, f.label)));
    this.toggleBtn.textContent = this.open ? "Hide ▴" : "Show ▾";
    this.toggleBtn.setAttribute("aria-expanded", String(this.open));
    this.el.hidden = !this.open;
    if (!this.open) return;
    this.drawChart(q);
    this.renderInfo(q);
    this.renderStorage();
    this.renderExplainer();
  }

  renderExplainer() {
    // Side by side: one number's float layouts | the vector's int8 mappings.
    this.explainer.replaceChildren(h("div", { class: "grid-2 under-hood" },
      h("div", {},
        h("h3", { class: "step-title" }, "32 → 16 bits: floating-point layouts"),
        floatLayouts(this.vector[this.selected], this.selected)),
      h("div", {},
        h("h3", { class: "step-title" }, "Floats → int8: symmetric vs asymmetric"),
        note("Integers have no exponent: a **scale** s stretches the float range over the 256 int8 levels. The choice is which range — shown on the same 8 values, the selected one highlighted."),
        intMappings(this.vector, this.selected, this.clip, (clip) => { this.clip = clip; }))),
    );
  }

  drawChart(q) {
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const y = (v) => PAD_T + ((this.yMax - v) / (2 * this.yMax)) * plotH;
    const colW = plotW / DIMS;
    const nodes = [];

    for (const t of [-this.yMax, -this.yMax / 2, 0, this.yMax / 2, this.yMax]) {
      nodes.push(s("line", { x1: PAD_L, x2: W - PAD_R, y1: y(t), y2: y(t), class: t === 0 ? "axis" : "grid" }));
      nodes.push(s("text", { x: PAD_L - 6, y: y(t) + 4, class: "tick", "text-anchor": "end" }, +t.toFixed(3)));
    }
    // Draw the levels only while they're far enough apart to see.
    if (q.levels && ((q.levels[1] - q.levels[0]) / (2 * this.yMax)) * plotH >= 4) {
      for (const lv of q.levels) nodes.push(s("line", { x1: PAD_L, x2: W - PAD_R, y1: y(lv), y2: y(lv), class: "level" }));
    }

    this.vector.forEach((orig, i) => {
      const cx = PAD_L + colW * (i + 0.5);
      const bar = (v, width, cls) => s("rect", {
        x: cx - width / 2, width, y: Math.min(y(v), y(0)), height: Math.max(1, Math.abs(y(v) - y(0))), class: cls, rx: 2,
      });
      nodes.push(s("rect", { x: cx - colW / 2, width: colW, y: PAD_T, height: plotH, class: "hit" }));
      if (i === this.selected) nodes.push(s("rect", { x: cx - colW / 2 + 2, width: colW - 4, y: PAD_T, height: plotH, class: "selected", rx: 4 }));
      nodes.push(bar(orig, colW * 0.62, "orig"));
      nodes.push(bar(q.values[i], colW * 0.3, "quant"));
    });
    this.inputs.forEach((input, i) => {
      if (document.activeElement !== input) input.value = String(+this.vector[i].toFixed(4));
      input.classList.toggle("sel", i === this.selected);
    });
    this.outputs.forEach((out, i) => { out.textContent = q.values[i].toFixed(3); });
    this.svg.replaceChildren(...nodes);
  }

  renderInfo(q) {
    const f32 = FORMATS[0];
    const errors = this.vector.map((x, i) => Math.abs(x - q.values[i]));
    const bytes = bytesFor(this.format, DIMS);
    const ratio = bytesFor(f32, DIMS) / bytes;
    this.info.replaceChildren(
      h("div", { class: "metrics-2" },
        metric(`Bytes · ${DIMS} dims`, `${bytesFor(f32, DIMS)} → ${bytes} B`),
        metric("Smaller by", `${ratio}×`),
        metric("Cosine to original", cosine(this.vector, q.values).toFixed(4)),
        metric("Max error", Math.max(...errors).toFixed(4))),
      h("p", { class: "note", html: richText(this.format.about) }),
      h("div", { class: "step-title" }, `Stored as ${this.format.label}`),
      h("pre", { class: "vector" }, q.stored),
    );
  }

  // Both embeddings: each chunk carries a vector from each, so the collection
  // pays for both — and Ⓑ's are 1024/384 ≈ 2.7× bigger.
  renderStorage() {
    const { embeddings, points } = this.ctx;
    const embs = ["A", "B"].map((letter) => ({ letter, ...embeddings[letter] }))
      .filter((e) => e.dim && e.enabled !== false);
    const count = points || EXAMPLE_POINTS;
    const max = Math.max(...embs.map((e) => bytesFor(FORMATS[0], e.dim))) * count;
    const both = embs.reduce((n, e) => n + bytesFor(this.format, e.dim), 0) * count;
    this.storage.replaceChildren(
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("div", { class: "card-title" },
          `Scaled up — ${count.toLocaleString("en-US")} chunks × ${embs.length} embeddings`)),
        h("p", { class: "card-sub", html: richText((points
          ? "Your collection's dense vectors, per format"
          : `An example collection of ${EXAMPLE_POINTS.toLocaleString("en-US")} chunks`) +
          `. Every chunk stores one vector per embedding: ${embs.map((e) => `${e.letter === "A" ? "Ⓐ" : "Ⓑ"} ${e.model}, ${e.dim}-dim`).join("; ")}. Click a row to pick its format.`) }),
        h("div", { class: "storage-legend" }, embs.map((e) =>
          h("span", {}, h("i", { class: `sw sw-${e.letter.toLowerCase()}` }), `${e.model} · ${e.dim}-dim`))),
        h("div", { class: "storage-bars" }, FORMATS.map((f) => h("button", {
          type: "button", class: `storage-row ${f === this.format ? "active" : ""}`,
          onclick: () => { this.format = f; this.update(); },
        },
        h("span", { class: "storage-label" }, f.label),
        h("span", { class: "storage-tracks" }, embs.map((e) => h("span", { class: "storage-track" },
          h("span", { class: `storage-fill fill-${e.letter.toLowerCase()}`, style: `width:${Math.max(0.4, (bytesFor(f, e.dim) * count / max) * 100)}%` })))),
        h("span", { class: "storage-size" }, embs.map((e) =>
          h("span", {}, `${e.letter === "A" ? "Ⓐ" : "Ⓑ"} ${fmtBytes(bytesFor(f, e.dim) * count)} · ${bytesFor(f, e.dim)} B`)))))),
        note(`At **${this.format.label}** both together take ${fmtBytes(both)}. Vectors only — payloads, the HNSW graphs and the sparse index come on top. With quantisation Qdrant keeps the float32 originals on disk and the quantised copy in RAM: search runs on the small copy, then rescores the top candidates with the originals.`),
      ),
    );
  }
}

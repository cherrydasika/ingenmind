// "Under the hood" for the quantisation demo: how one number is laid out in
// float32 / float16 / bfloat16 bits, and how the whole vector maps onto int8
// symmetrically (absmax) or asymmetrically (zero-point). Follows Maarten
// Grootendorst's "A Visual Guide to Quantization".

import { h, note, richText } from "./ui.js";

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}

// ---------- Floating point bit layouts ----------

function float32Bits(x) {
  const view = new DataView(new ArrayBuffer(4));
  view.setFloat32(0, x);
  return view.getUint32(0);
}

function float32FromBits(bits) {
  const view = new DataView(new ArrayBuffer(4));
  view.setUint32(0, bits >>> 0);
  return view.getFloat32(0);
}

// IEEE half precision, round-to-nearest. Normal and subnormal values only —
// embedding values never come near float16's ±65,504 limit.
function float16Bits(x) {
  const sign = x < 0 || Object.is(x, -0) ? 1 : 0;
  const a = Math.abs(x);
  if (a === 0) return sign << 15;
  let exp = Math.floor(Math.log2(a));
  if (exp < -14) return (sign << 15) | Math.round(a / 2 ** -24); // subnormal
  let mant = Math.round((a / 2 ** exp - 1) * 1024);
  if (mant === 1024) { mant = 0; exp += 1; }
  if (exp > 15) return (sign << 15) | (0x1f << 10); // overflow → inf
  return (sign << 15) | ((exp + 15) << 10) | mant;
}

function float16FromBits(bits) {
  const sign = bits >> 15 ? -1 : 1;
  const exp = (bits >> 10) & 0x1f;
  const mant = bits & 0x3ff;
  if (exp === 0) return sign * mant * 2 ** -24;
  if (exp === 0x1f) return sign * Infinity;
  return sign * (1 + mant / 1024) * 2 ** (exp - 15);
}

// bfloat16 = the top 16 bits of float32, rounded to nearest even.
function bfloat16Bits(x) {
  const b = float32Bits(x);
  return ((b + 0x7fff + ((b >>> 16) & 1)) >>> 16) & 0xffff;
}

const FLOATS = [
  {
    name: "float32", exp: 8, mant: 23, max: "3.4 × 10³⁸",
    bits: float32Bits, decode: float32FromBits,
    about: "Full precision: what the embedding model outputs and Qdrant stores.",
  },
  {
    name: "float16", exp: 5, mant: 10, max: "65,504",
    bits: float16Bits, decode: float16FromBits,
    about: "Half the bits, taken from both exponent and fraction: smaller range and coarser steps.",
  },
  {
    name: "bfloat16", exp: 8, mant: 7, max: "3.4 × 10³⁸",
    bits: bfloat16Bits, decode: (b) => float32FromBits(b << 16),
    about: "Keeps float32's 8 exponent bits (same range), gives up fraction bits (coarser steps). Popular for model weights.",
  },
];

function exponentOf(x) {
  return x === 0 ? 0 : Math.floor(Math.log2(Math.abs(x)));
}

function bitRow(format, x) {
  const total = 1 + format.exp + format.mant;
  const bits = format.bits(x).toString(2).padStart(total, "0");
  const stored = format.decode(parseInt(bits, 2));
  const step = 2 ** (Math.max(exponentOf(stored), format.name === "float16" ? -14 : -126) - format.mant);
  const cells = [...bits].map((b, i) => h("span", {
    class: `bit ${i === 0 ? "bit-sign" : i <= format.exp ? "bit-exp" : "bit-mant"}`,
  }, b));
  return h("div", { class: "bit-row" },
    h("div", { class: "bit-head" },
      h("strong", {}, format.name),
      h("span", { class: "faint" }, ` ${total} bits · 1 sign · ${format.exp} exponent · ${format.mant} fraction`)),
    h("div", { class: "bits" }, cells),
    h("div", { class: "bit-meta" },
      h("span", {}, "stores ", h("code", {}, stored.toPrecision(10).replace(/\.?0+$/, ""))),
      h("span", {}, "error ", h("code", {}, Math.abs(stored - x).toExponential(2))),
      h("span", {}, "step here ", h("code", {}, step.toExponential(2))),
      h("span", {}, "range ", h("code", {}, `±${format.max}`))),
    h("div", { class: "note", style: "margin:2px 0 0" }, format.about));
}

export function floatLayouts(x, dimIndex) {
  return [
    note(`Dimension d${dimIndex} = **${x}** — type in another value box (or drag a column) above to pick it; edits show here as you type. A float is **sign × 2^exponent × 1.fraction**: exponent bits set the range, fraction bits set how finely values are spaced.`),
    h("div", { class: "bit-legend" },
      h("span", {}, h("i", { class: "bit bit-sign" }), "sign"),
      h("span", {}, h("i", { class: "bit bit-exp" }), "exponent (range)"),
      h("span", {}, h("i", { class: "bit bit-mant" }), "fraction / mantissa (precision)")),
    FLOATS.map((f) => bitRow(f, x)),
  ];
}

// ---------- int8: symmetric vs asymmetric ----------

function symmetric(vector, clip) {
  const alpha = clip || 1e-9;
  const scale = 127 / alpha;
  const q = vector.map((x) => Math.max(-127, Math.min(127, Math.round(scale * x))));
  return { lo: -alpha, hi: alpha, qlo: -127, qhi: 127, scale, zero: 0, q, deq: q.map((v) => v / scale) };
}

function asymmetric(vector) {
  const beta = Math.min(...vector);
  const alpha = Math.max(...vector);
  const scale = 255 / (alpha - beta || 1e-9);
  const zero = -Math.round(scale * beta) - 128;
  const q = vector.map((x) => Math.max(-128, Math.min(127, Math.round(scale * x) + zero)));
  return { lo: beta, hi: alpha, qlo: -128, qhi: 127, scale, zero, q, deq: q.map((v) => (v - zero) / scale) };
}

const LW = 520, LH = 132, LX0 = 26, LX1 = LW - 26, TOP = 34, BOT = 100;

function mappingSvg(vector, m, selected, symmetricMode) {
  const fx = (v) => LX0 + ((v - m.lo) / (m.hi - m.lo || 1)) * (LX1 - LX0);
  const qx = (v) => LX0 + ((v - m.qlo) / (m.qhi - m.qlo)) * (LX1 - LX0);
  const nodes = [
    s("line", { x1: LX0, x2: LX1, y1: TOP, y2: TOP, class: "axis" }),
    s("line", { x1: LX0, x2: LX1, y1: BOT, y2: BOT, class: "axis" }),
    s("text", { x: LX0, y: 14, class: "lbl" }, symmetricMode ? `−α = ${m.lo.toFixed(3)}` : `β = min = ${m.lo.toFixed(3)}`),
    s("text", { x: LX1, y: 14, class: "lbl", "text-anchor": "end" }, symmetricMode ? `α = ${m.hi.toFixed(3)}` : `α = max = ${m.hi.toFixed(3)}`),
    s("text", { x: LX0, y: LH - 4, class: "lbl" }, String(m.qlo)),
    s("text", { x: LX1, y: LH - 4, class: "lbl", "text-anchor": "end" }, String(m.qhi)),
    s("text", { x: 2, y: TOP + 4, class: "lbl dim" }, "f32"),
    s("text", { x: 2, y: BOT + 4, class: "lbl dim" }, "int8"),
  ];
  // Where float 0 lands: exactly int 0 when symmetric, the zero-point z when not.
  if (m.lo <= 0 && m.hi >= 0) {
    nodes.push(s("line", { x1: fx(0), x2: qx(m.zero), y1: TOP, y2: BOT, class: "zero-link" }));
    nodes.push(s("text", { x: fx(0), y: TOP - 8, class: "lbl zero", "text-anchor": "middle" }, "0"));
    nodes.push(s("text", { x: qx(m.zero), y: BOT + 18, class: "lbl zero", "text-anchor": "middle" }, symmetricMode ? "0" : `z = ${m.zero}`));
  }
  vector.forEach((x, i) => {
    const clipped = x < m.lo || x > m.hi;
    const cls = `${i === selected ? "sel" : ""} ${clipped ? "clipped" : ""}`;
    const x1 = fx(Math.max(m.lo, Math.min(m.hi, x)));
    nodes.push(s("line", { x1, x2: qx(m.q[i]), y1: TOP, y2: BOT, class: `link ${cls}` }));
    nodes.push(s("circle", { cx: x1, cy: TOP, r: i === selected ? 5 : 3.5, class: `pt ${cls}` }));
    nodes.push(s("circle", { cx: qx(m.q[i]), cy: BOT, r: i === selected ? 5 : 3.5, class: `pt q ${cls}` }));
  });
  const svg = s("svg", { viewBox: `0 0 ${LW} ${LH}`, class: "map-chart" });
  svg.append(...nodes);
  return svg;
}

function mappingTable(vector, m, selected) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["dim", "x", "x_q", "back", "error"].map((c, i) => h("th", { class: i ? "num" : null }, c)))),
    h("tbody", {}, vector.map((x, i) => h("tr", { class: i === selected ? "sel-row" : null },
      h("td", {}, `d${i}`),
      h("td", { class: "num" }, x.toFixed(3)),
      h("td", { class: "num" }, m.q[i]),
      h("td", { class: "num" }, m.deq[i].toFixed(4)),
      h("td", { class: "num" }, Math.abs(x - m.deq[i]).toFixed(4)))))));
}

// onClip only records the chosen α; the symmetric card redraws itself, so the
// slider stays in place while it's dragged.
export function intMappings(vector, selected, clip, onClip) {
  const absmax = Math.max(...vector.map(Math.abs)) || 1;
  const f = (n) => n.toFixed(3);
  const symBody = h("div", {});
  const symTable = h("div", {});
  const clipLabel = h("label", {});
  const drawSymmetric = (alpha) => {
    const sym = symmetric(vector, alpha);
    const clippedCount = vector.filter((x) => Math.abs(x) > alpha).length;
    clipLabel.replaceChildren(`Clip α to ${f(alpha)}`,
      h("span", { class: "faint" }, clippedCount ? ` · ${clippedCount} value${clippedCount > 1 ? "s" : ""} clipped to ±127` : " · nothing clipped"));
    symBody.replaceChildren(mappingSvg(vector, sym, selected, true),
      h("pre", { class: "vector" }, `s   = 127 / α = 127 / ${f(alpha)} = ${sym.scale.toFixed(2)}\nx_q = round(s · x)\nx   ≈ x_q / s`));
    symTable.replaceChildren(mappingTable(vector, sym, selected));
  };
  const alpha = Math.min(clip ?? absmax, absmax);
  const slider = h("input", {
    type: "range", min: (absmax * 0.2).toFixed(4), max: absmax.toFixed(4), step: (absmax / 200).toFixed(5), value: alpha,
    oninput: (e) => { const v = Number(e.target.value); onClip(v); drawSymmetric(v); },
  });
  drawSymmetric(alpha);
  const asym = asymmetric(vector);
  return h("div", { class: "stack" },
    h("div", { class: "card" },
      h("div", { class: "card-title" }, "Symmetric — absmax"),
      h("p", { class: "card-sub", html: richText("Range centred on zero, **[−α, α]** with α = the largest |x|, mapped to [−127, 127]. Float 0 is always int 0, but if the values lean to one side, part of the int range goes unused.") }),
      symBody,
      h("div", { class: "clip" }, clipLabel, slider),
      note("Clipping trades a big error on the outliers for finer steps (smaller error) on everything else — how calibration picks α from percentiles instead of the true max."),
      symTable),
    h("div", { class: "card" },
      h("div", { class: "card-title" }, "Asymmetric — zero-point"),
      h("p", { class: "card-sub", html: richText("Uses the actual **[β = min, α = max]**, mapped to [−128, 127], so every int level covers real data. Float 0 lands on the **zero-point z** instead of 0 — one extra number to store and apply.") }),
      mappingSvg(vector, asym, selected, false),
      h("pre", { class: "vector" },
        `s   = 255 / (α − β) = 255 / ${f(asym.hi - asym.lo)} = ${asym.scale.toFixed(2)}\nz   = −round(s · β) − 128 = ${asym.zero}\nx_q = round(s · x) + z\nx   ≈ (x_q − z) / s`),
      note("The **int8 · scalar** format above is this min–max mapping (on 0–255), like Qdrant's scalar quantisation, which takes the range from a quantile of the whole collection."),
      mappingTable(vector, asym, selected)));
}

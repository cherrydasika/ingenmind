// A light, interactive scene for long waits (researching, finding sources,
// reading sites, building, evaluating): dots drift and link up like pages
// joining a knowledge base, one now and then lighting up as it is read. Near
// the pointer they lean towards it and draw faint lines to it, and give way
// when it comes too close.
//
// Kept cheap: one canvas for the whole page, reused across re-renders; about
// forty dots; it stops when off screen, when the tab is hidden, or when no
// wait is shown, and is a still picture for people who ask for reduced motion.

import { h } from "./ui.js";

const COUNT = 40;
const LINK = 92;        // px: dots closer than this are linked
const REACH = 150;      // px: how far the pointer is felt
const PERSONAL = 34;    // px: closer than this, a dot gives way
const SPEED = 0.28;     // px per frame, roughly

class Scene {
  constructor() {
    this.canvas = h("canvas", { class: "wait-canvas", "aria-hidden": "true" });
    this.ctx = this.canvas.getContext("2d");
    this.dots = [];
    this.pointer = null;
    this.running = false;
    this.visible = true;
    this.width = 0;
    this.height = 0;
    this.frameNo = 0;
    this.reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    new ResizeObserver(() => this.resize()).observe(this.canvas);
    new IntersectionObserver(([entry]) => { this.visible = entry.isIntersecting; this.kick(); }).observe(this.canvas);
    window.addEventListener("pointermove", (e) => {
      const r = this.canvas.getBoundingClientRect();
      this.pointer = { x: e.clientX - r.left, y: e.clientY - r.top };
    }, { passive: true });
    document.addEventListener("pointerleave", () => { this.pointer = null; });
    document.addEventListener("visibilitychange", () => this.kick());
  }

  resize() {
    const w = this.canvas.clientWidth;
    const hgt = this.canvas.clientHeight;
    if (!w || !hgt) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(hgt * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Keep the dots where they were, in proportion; seed them the first time.
    if (this.dots.length && this.width) {
      for (const d of this.dots) { d.x *= w / this.width; d.y *= hgt / this.height; }
    } else {
      this.dots = Array.from({ length: COUNT }, () => {
        const angle = Math.random() * Math.PI * 2;
        return { x: Math.random() * w, y: Math.random() * hgt, vx: Math.cos(angle) * SPEED, vy: Math.sin(angle) * SPEED,
          r: Math.random() < 0.2 ? 2.6 : 1.7, glow: 0 };
      });
    }
    this.width = w;
    this.height = hgt;
    this.colour();
    this.draw();
  }

  colour() {
    const css = getComputedStyle(document.documentElement);
    this.primary = css.getPropertyValue("--primary").trim() || "#2563eb";
    this.faint = css.getPropertyValue("--faint").trim() || "#94a3b8";
  }

  kick() {
    if (this.reduced || this.running || !this.canvas.isConnected || !this.visible || document.hidden) return;
    this.running = true;
    requestAnimationFrame(() => this.frame());
  }

  frame() {
    if (!this.canvas.isConnected || !this.visible || document.hidden) { this.running = false; return; }
    if (++this.frameNo % 120 === 0) this.colour();   // follows a theme change
    this.step();
    this.draw();
    requestAnimationFrame(() => this.frame());
  }

  step() {
    const { width: w, height: hgt, pointer: p } = this;
    if (Math.random() < 0.025) this.dots[Math.floor(Math.random() * this.dots.length)].glow = 1;   // a page read
    for (const d of this.dots) {
      if (p) {
        const dx = p.x - d.x;
        const dy = p.y - d.y;
        const dist = Math.hypot(dx, dy) || 1;
        if (dist < REACH) {
          // Lean towards the pointer, but give way inside its personal space.
          const pull = dist < PERSONAL ? -0.06 : 0.012 * (1 - dist / REACH);
          d.vx += (dx / dist) * pull;
          d.vy += (dy / dist) * pull;
        }
      }
      // Ease back to a gentle drift.
      const speed = Math.hypot(d.vx, d.vy) || 1;
      const target = speed > SPEED * 4 ? 0.94 : speed < SPEED ? 1.02 : 0.995;
      d.vx *= target;
      d.vy *= target;
      d.x += d.vx;
      d.y += d.vy;
      if (d.x < 0 || d.x > w) { d.vx = -d.vx; d.x = Math.max(0, Math.min(w, d.x)); }
      if (d.y < 0 || d.y > hgt) { d.vy = -d.vy; d.y = Math.max(0, Math.min(hgt, d.y)); }
      d.glow *= 0.96;
    }
  }

  draw() {
    const { ctx, dots, pointer: p } = this;
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.lineWidth = 1;
    ctx.strokeStyle = this.primary;
    for (let i = 0; i < dots.length; i++) {
      for (let j = i + 1; j < dots.length; j++) {
        const a = dots[i];
        const b = dots[j];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (dist < LINK) {
          ctx.globalAlpha = (1 - dist / LINK) * 0.28;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
    }
    if (p) {
      for (const d of dots) {
        const dist = Math.hypot(p.x - d.x, p.y - d.y);
        if (dist < REACH) {
          ctx.globalAlpha = (1 - dist / REACH) * 0.45;
          ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(d.x, d.y); ctx.stroke();
        }
      }
    }
    for (const d of dots) {
      if (d.glow > 0.05) {
        ctx.globalAlpha = d.glow * 0.35;
        ctx.fillStyle = this.primary;
        ctx.beginPath(); ctx.arc(d.x, d.y, d.r + 6 * d.glow, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 0.55 + 0.45 * d.glow;
      ctx.fillStyle = d.glow > 0.05 ? this.primary : this.faint;
      ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
}

let scene = null;

/** The waiting scene with what is happening under it. One scene per page: a
 *  second call moves it to the newest place. */
export function waiting(title, detail = null) {
  scene ||= new Scene();
  queueMicrotask(() => scene.kick());   // once the caller has put it on the page
  return h("div", { class: "wait", role: "status" }, scene.canvas,
    h("div", { class: "wait-text" }, h("strong", {}, title), detail ? h("div", { class: "note" }, detail) : null));
}

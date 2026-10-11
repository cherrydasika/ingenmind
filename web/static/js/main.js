// App shell: who is signed in, the sign-in screen, the user menu, the
// sidebar (only the pages the user may use), service status, and routing.

import { api } from "./api.js";
import { applyTheme } from "./theme.js";
import { h } from "./ui.js";
import { AdhocPage } from "./adhoc.js";
import { AgentMemoryPage } from "./agent_memory.js";
import { EvaluationsPage } from "./evaluations.js";
import { FlowsPage } from "./flows.js";
import { KnowledgeBasePage } from "./knowledge_base.js";
import { MonitoringPage } from "./monitoring.js";
import { ProfilePage } from "./profile.js";
import { RetrievalPage } from "./retrieval.js";
import { SessionsPage } from "./sessions.js";
import { SetupPage } from "./setup.js";
import { UsersPage } from "./users.js";

const STATUS_REFRESH_MS = 30_000;
const SESSION_KEY = "rag.session";

// The browser's session: the server keeps it apart per signed-in user. It
// lives as long as the tab; signing out forgets it. There is no "New
// session" button: sessions are managed for the user.
export const session = {
  get id() {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem(SESSION_KEY, id);
    }
    return id;
  },
  user: null,
  setupState: null,   // the knowledge system's state (refreshSetup)
  demo: false,        // the demo is the live knowledge system (refreshSetup)
  get userId() {
    return this.user?.user_id || null;
  },
  can(permission) {
    return !permission || Boolean(this.user?.permissions?.includes(permission));
  },
};

// route → [page class, sidebar group (for the breadcrumb), permission needed (null: any signed-in user)]
const pages = {
  retrieval: [RetrievalPage, "Ask", "query_rag"],
  sessions: [SessionsPage, "Monitoring", null],
  flows: [FlowsPage, "Build", "manage_agents"],
  evaluations: [EvaluationsPage, "Evaluate", "run_evaluations"],
  "knowledge-base": [KnowledgeBasePage, "Data", "view_knowledge"],
  adhoc: [AdhocPage, "Data", "upload_documents"],
  monitoring: [MonitoringPage, "Monitoring", null],
  memory: [AgentMemoryPage, "Monitoring", "view_agent_memory"],
  users: [UsersPage, "Admin", "manage_users"],
  setup: [SetupPage, "Admin", "manage_settings"],
  profile: [ProfilePage, "Account", null],
};
// Addresses from before the sidebar was reorganised, so old links still land.
const MOVED = { home: "monitoring", ingestion: "knowledge-base/ingestion", sources: "knowledge-base/sources",
  knowledge: "profile", docs: "monitoring" };
let current = null;
let statusTimer = null;

const SIGN_IN_ERRORS = {
  not_invited: "Your account isn't set up for this app yet. Ask an administrator to add you.",
  disabled: "Your account has been deactivated. Ask an administrator.",
  email_unverified: "Your identity provider hasn't verified your email address.",
  provider: "Sign-in didn't complete. Please try again.",
  oidc_off: "Sign-in with an identity provider isn't configured.",
};

// ---------- signed out ----------

function showSignIn(auth, reason) {
  current?.unmount?.();
  current = null;
  clearInterval(statusTimer);
  document.getElementById("shell").hidden = true;
  document.getElementById("signin").hidden = false;
  document.title = "Sign in · IngenMind";
  document.getElementById("signin-provider").textContent = auth ? `Using ${auth.provider}` : "";
  const error = document.getElementById("signin-error");
  error.textContent = reason || "";
  error.hidden = !reason;
}

// Forget everything this tab held for the user: chat, session, options.
function forgetUser() {
  current?.unmount?.();
  current = null;
  session.user = null;
  try { sessionStorage.clear(); } catch { /* nothing stored */ }
  document.getElementById("content").replaceChildren();
}

async function logOut() {
  let providerLogout = null;
  try {
    providerLogout = (await api.logout()).provider_logout;
  } catch { /* signed out locally anyway */ }
  forgetUser();
  if (providerLogout) location.assign(providerLogout);   // the provider signs out too, then returns here
  else location.assign("/");
}

// A request said the sign-in is gone (expired, or the user was deactivated).
let signedOutShown = false;
window.addEventListener("rag:signed-out", () => {
  if (signedOutShown || !session.user) return;
  signedOutShown = true;
  forgetUser();
  showSignIn(null, "Your sign-in has ended. Please sign in again.");
});

// ---------- signed in ----------

function showUser(user) {
  document.getElementById("user-name").textContent = user.first_name;
  document.getElementById("user-menu-name").textContent = user.first_name;
  document.getElementById("user-menu-detail").textContent = `${user.email || ""} · ${user.role}`;
  // Only the pages this user may use; a group label goes when its pages do.
  const nav = document.getElementById("nav");
  for (const link of nav.querySelectorAll("a[data-page]")) {
    link.hidden = !session.can(pages[link.dataset.page]?.[2]);
  }
  let label = null;
  for (const el of nav.children) {
    if (el.classList.contains("nav-label")) {
      if (label) label.hidden = !label.dataset.visible;
      label = el;
      label.dataset.visible = "";
    } else if (label && !el.hidden) {
      label.dataset.visible = "1";
    }
  }
  if (label) label.hidden = !label.dataset.visible;
}

function setMenu(open) {
  document.getElementById("user-menu-panel").hidden = !open;
  document.getElementById("user-menu-btn").setAttribute("aria-expanded", String(open));
}

document.getElementById("user-menu-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  setMenu(document.getElementById("user-menu-panel").hidden);
});
document.addEventListener("click", (event) => {
  if (!document.getElementById("user-menu").contains(event.target)) setMenu(false);
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setMenu(false); });
document.getElementById("user-menu-panel").addEventListener("click", (event) => {
  if (event.target.closest("a")) setMenu(false);
});
document.getElementById("logout").addEventListener("click", () => { setMenu(false); logOut(); });

// Narrow screens: the sidebar slides in over the page (app.css hides it).
function setDrawer(open) {
  document.getElementById("shell").classList.toggle("nav-open", open);
  document.getElementById("menu-btn").setAttribute("aria-expanded", String(open));
}
document.getElementById("menu-btn").addEventListener("click", () =>
  setDrawer(!document.getElementById("shell").classList.contains("nav-open")));
document.getElementById("nav-backdrop").addEventListener("click", () => setDrawer(false));
document.getElementById("nav").addEventListener("click", (event) => {
  if (event.target.closest("a")) setDrawer(false);
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setDrawer(false); });

async function refreshServices() {
  const list = document.getElementById("services");
  try {
    const services = await api.status();
    // Monitoring → Langfuse: a link out, for people who build flows, while it is up.
    const langfuse = services.find((s) => s.name === "Langfuse");
    const link = document.querySelector('#nav a[data-link="langfuse"]');
    if (link) {
      link.hidden = !(langfuse?.up && langfuse.url && session.can("manage_agents"));
      if (langfuse?.url) link.href = langfuse.url;
    }
    list.replaceChildren(...services.map((s) =>
      h("li", { title: s.up ? "Reachable" : "Not reachable" },
        h("span", { class: `dot ${s.up ? "up" : "down"}` }),
        h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.name))));
    document.getElementById("services-age").textContent = "· live";
  } catch (err) {
    list.replaceChildren(h("li", { class: "muted" }, `Status unavailable (${err.message})`));
  }
}

function firstAllowedPage() {
  // While the knowledge system is not set up, an admin starts at setup.
  if (session.setupState && session.setupState !== "READY" && session.can("manage_settings")) return "setup";
  return ["retrieval", "monitoring"].find((key) => session.can(pages[key][2])) || "monitoring";
}

// "Being set up": a notice on every page (but setup's own) until the knowledge system is ready.
async function refreshSetup() {
  try {
    const ks = await api.knowledgeSystem();
    session.setupState = ks.state;
    session.demo = ks.state === "READY" && ks.origin === "demo";
  } catch {
    session.setupState = null;   // unknown: no notice; questions say it themselves
    session.demo = false;
  }
  drawSetupBanner();
}

// Langfuse is another app: say so before leaving, unless the user said not to ask again (this browser).
const EXTERNAL_KEY = "ingenmind.external-ok";
function externalOk() {
  try { return localStorage.getItem(EXTERNAL_KEY) === "1"; } catch { return false; }
}

function confirmExternal(url, name) {
  const dialog = document.getElementById("external-dialog");
  const remember = h("input", { type: "checkbox", id: "external-remember" });
  const close = () => { dialog.hidden = true; dialog.replaceChildren(); };
  const open = h("a", { class: "btn btn-primary", href: url, target: "_blank", rel: "noopener", onclick: () => {
    if (remember.checked) { try { localStorage.setItem(EXTERNAL_KEY, "1"); } catch { /* asks again */ } }
    close();
  } }, `Open ${name}`);
  dialog.replaceChildren(h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "external-title" },
    h("h2", { id: "external-title" }, `${name} opens in a new tab`),
    h("p", {}, `${name} is a separate application at `, h("code", {}, url),
      ". It shows the traces of every question: each agent's steps, prompts, timings and costs."),
    h("label", { class: "modal-check" }, remember, " Don't ask me again"),
    h("div", { class: "modal-actions" },
      h("button", { class: "btn btn-ghost", type: "button", onclick: close }, "Cancel"), open)));
  dialog.hidden = false;
  open.focus();
  dialog.onclick = (event) => { if (event.target === dialog) close(); };
}

document.getElementById("nav").addEventListener("click", (event) => {
  const link = event.target.closest('a[data-link="langfuse"]');
  if (!link || externalOk()) return;
  event.preventDefault();
  confirmExternal(link.href, "Langfuse");
});
document.addEventListener("keydown", (event) => {
  const dialog = document.getElementById("external-dialog");
  if (event.key === "Escape" && dialog && !dialog.hidden) { dialog.hidden = true; dialog.replaceChildren(); }
});

// While the demo is live, an admin is offered their own domain once; "Not now" hides it in this browser.
const DEMO_PROMPT_KEY = "ingenmind.demo-prompt-dismissed";
function demoPromptDismissed() {
  try { return localStorage.getItem(DEMO_PROMPT_KEY) === "1"; } catch { return false; }
}

function drawSetupBanner() {
  const banner = document.getElementById("setup-banner");
  const page = location.hash.replace(/^#\//, "").split("/")[0];
  const onSetup = page === "setup";
  if (session.demo && session.can("manage_settings") && page !== "profile" && !demoPromptDismissed()) {
    banner.hidden = false;
    const later = h("button", { class: "btn btn-ghost", type: "button", style: "margin-left:12px" }, "Not now");
    later.addEventListener("click", () => {
      try { localStorage.setItem(DEMO_PROMPT_KEY, "1"); } catch { /* the prompt just comes back */ }
      banner.hidden = true;
    });
    banner.replaceChildren(h("div", { class: "alert info" },
      "You're exploring the demo (fictional Lakeshore Rail). ",
      h("a", { href: "#/profile", style: "font-weight:600;text-decoration:underline" }, "Set IngenMind up for your own domain"),
      " — any time from Profile → Knowledge system.", later));
    return;
  }
  banner.hidden = !session.setupState || session.setupState === "READY" || onSetup;
  if (banner.hidden) return;
  banner.replaceChildren(h("div", { class: "alert info" }, session.can("manage_settings")
    ? ["The knowledge system isn't set up yet. ", h("a", { href: "#/setup" }, "Set it up"), "."]
    : "This knowledge system is being set up. Questions will work once it is ready."));
}
window.addEventListener("rag:setup-changed", refreshSetup);

async function route() {
  if (!session.user) return;
  let name = location.hash.replace(/^#\//, "").split("/")[0];
  if (MOVED[name]) {
    history.replaceState(null, "", `#/${MOVED[name]}`);
    name = MOVED[name].split("/")[0];
  }
  const key = pages[name] ? name : firstAllowedPage();
  if (key === "setup" && name !== "setup") history.replaceState(null, "", "#/setup");   // so the notice knows
  drawSetupBanner();
  const [Page, group, permission] = pages[key];
  document.querySelectorAll("#nav a[data-page]").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === key));
  const content = document.getElementById("content");
  current?.unmount?.(); // e.g. stop a page's polling
  current = null;
  if (!session.can(permission)) {
    document.getElementById("page-title").textContent = "No access";
    document.getElementById("page-crumb").textContent = "No access";
    document.getElementById("page-group").textContent = group;
    content.replaceChildren(h("div", { class: "card" }, h("p", {},
      "You don't have permission for this page. Ask an administrator if you need it.")));
    return;
  }
  current = new Page(content, { session });
  document.getElementById("page-title").textContent = current.title;
  document.getElementById("page-crumb").textContent = current.title;
  document.getElementById("page-group").textContent = group;
  document.title = `${current.title} · IngenMind`;
  window.scrollTo(0, 0);
  await current.mount();
}

async function start() {
  let me;
  try {
    me = await api.me();
  } catch (err) {
    showSignIn(null, `The app can't be reached (${err.message}).`);
    return;
  }
  const params = new URLSearchParams(location.search);
  const error = params.get("auth_error");
  if (error) history.replaceState(null, "", location.pathname + location.hash);   // not on reload
  if (!me.user) {
    showSignIn(me.auth, error ? SIGN_IN_ERRORS[error] || "Sign-in didn't complete." : null);
    return;
  }
  session.user = me.user;
  applyTheme(me.user.theme);
  document.getElementById("signin").hidden = true;
  document.getElementById("shell").hidden = false;
  showUser(me.user);
  await refreshSetup();
  refreshServices();
  statusTimer = setInterval(refreshServices, STATUS_REFRESH_MS);
  window.addEventListener("hashchange", route);
  route();
}

start();

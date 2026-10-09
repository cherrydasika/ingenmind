// App shell: who is signed in, the sign-in screen, the user menu, the
// sidebar (only the pages the user may use), service status, and routing.

import { api } from "./api.js";
import { applyTheme } from "./theme.js";
import { h } from "./ui.js";
import { AdhocPage } from "./adhoc.js";
import { AgentMemoryPage } from "./agent_memory.js";
import { EvaluationsPage } from "./evaluations.js";
import { FlowsPage } from "./flows.js";
import { DocsPage } from "./docs.js";
import { HomePage } from "./home.js";
import { IngestionPage } from "./ingestion.js";
import { KnowledgeSystemPage } from "./knowledge_system.js";
import { ProfilePage } from "./profile.js";
import { RetrievalPage } from "./retrieval.js";
import { SessionsPage } from "./sessions.js";
import { SetupPage } from "./setup.js";
import { SourcesPage } from "./sources.js";
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
  get userId() {
    return this.user?.user_id || null;
  },
  can(permission) {
    return !permission || Boolean(this.user?.permissions?.includes(permission));
  },
};

// route → [page class, sidebar group (for the breadcrumb), permission needed (null: any signed-in user)]
const pages = {
  home: [HomePage, "Overview", null],
  retrieval: [RetrievalPage, "Workspace", "query_rag"],
  sessions: [SessionsPage, "Workspace", null],
  adhoc: [AdhocPage, "Workspace", "upload_documents"],
  evaluations: [EvaluationsPage, "Workspace", "run_evaluations"],
  flows: [FlowsPage, "Workspace", "manage_agents"],
  memory: [AgentMemoryPage, "Workspace", "view_agent_memory"],
  ingestion: [IngestionPage, "Data", "view_knowledge"],
  sources: [SourcesPage, "Data", "view_knowledge"],
  docs: [DocsPage, "Reference", null],
  profile: [ProfilePage, "Account", null],
  users: [UsersPage, "Admin", "manage_users"],
  knowledge: [KnowledgeSystemPage, "Admin", "manage_settings"],
  setup: [SetupPage, "Setup", "manage_settings"],
};
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
  document.title = "Sign in · RAG Systems";
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
  return ["retrieval", "home"].find((key) => session.can(pages[key][2])) || "home";
}

// "Being set up": a notice on every page (but setup's own) until the knowledge system is ready.
async function refreshSetup() {
  try {
    session.setupState = (await api.knowledgeSystem()).state;
  } catch {
    session.setupState = null;   // unknown: no notice; questions say it themselves
  }
  drawSetupBanner();
}

function drawSetupBanner() {
  const banner = document.getElementById("setup-banner");
  const onSetup = location.hash.replace(/^#\//, "").split("/")[0] === "setup";
  banner.hidden = !session.setupState || session.setupState === "READY" || onSetup;
  if (banner.hidden) return;
  banner.replaceChildren(h("div", { class: "alert info" }, session.can("manage_settings")
    ? ["The knowledge system isn't set up yet. ", h("a", { href: "#/setup" }, "Set it up"), "."]
    : "This knowledge system is being set up. Questions will work once it is ready."));
}
window.addEventListener("rag:setup-changed", refreshSetup);

async function route() {
  if (!session.user) return;
  const name = location.hash.replace(/^#\//, "").split("/")[0];
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
  document.title = `${current.title} · RAG Systems`;
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

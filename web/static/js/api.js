// Thin wrapper over the JSON API in app/web_api.py.

// 401: the sign-in is gone (expired, or the user was deactivated); the app
// shell (main.js) then shows the sign-in screen.
function checkSignedIn(resp) {
  if (resp.status === 401) window.dispatchEvent(new Event("rag:signed-out"));
}

async function request(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  checkSignedIn(resp);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`${path}: ${resp.status} ${resp.statusText}`);
  }
  if (!resp.ok) throw new Error(data?.error || `${path}: ${resp.status}`);
  return data;
}

const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body) });

// POST that answers with server-sent events (see /api/ask/stream): calls
// onStage for each `stage` event, resolves with the final `result`.
async function postStream(path, body, onStage) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  checkSignedIn(resp);
  if (!resp.ok) {
    const data = await resp.json().catch(() => null);
    const error = new Error(data?.error || `${path}: ${resp.status}`);
    error.setup = Boolean(data?.setup);   // refused: the knowledge system is not set up yet
    throw error;
  }
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new Error(`${path}: stream ended without a result`);
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const data = JSON.parse(/^data: (.*)$/m.exec(block)?.[1] ?? "null");
      if (event === "stage") onStage(data);
      else if (event === "result") return data;
      else if (event === "error") throw new Error(data?.error || "request failed");
    }
  }
}
const fresh = (path, force) => (force ? `${path}?fresh=1` : path);

export const api = {
  me: () => request("/api/me"),
  logout: () => request("/auth/logout", { method: "POST" }),
  setTheme: (theme) => request("/api/me/theme", { method: "PUT", body: JSON.stringify({ theme }) }),
  config: () => request("/api/config"),
  models: () => request("/api/models"),
  status: () => request("/api/status"),
  ask: (payload) => post("/api/ask", payload),
  askStream: (payload, onStage) => postStream("/api/ask/stream", payload, onStage),
  agent: (force = false) => request(fresh("/api/agent", force)),
  agentStream: (payload, onStep) => postStream("/api/agent/stream", payload, onStep),
  agentResume: (payload, onStep) => postStream("/api/agent/resume", payload, onStep),
  flowList: () => request("/api/flows"),
  flowDetail: (id) => request(`/api/flows/${encodeURIComponent(id)}`),
  evalSets: () => request("/api/flow-evals/sets"),
  evalSaveSet: (set) => post("/api/flow-evals/sets", set),
  evalArchive: () => request("/api/flow-evals/archive"),
  evalRuns: () => request("/api/flow-evals/runs"),
  evalRun: (id) => request(`/api/flow-evals/runs/${encodeURIComponent(id)}`),
  evalStart: (payload) => post("/api/flow-evals/runs", payload),
  evalCancel: (id) => post(`/api/flow-evals/runs/${encodeURIComponent(id)}/cancel`, {}),
  agentSession: (sessionId) => request(`/api/agent/session?session_id=${encodeURIComponent(sessionId)}`),
  resetSession: (sessionId) => post("/api/session/reset", { session_id: sessionId }),
  currentSession: () => request("/api/sessions/current"),
  agentMemory: () => request("/api/agent-memory"),
  users: () => request("/api/users"),
  addUser: (user) => post("/api/users", user),
  updateUser: (id, changes) => request(`/api/users/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(changes) }),
  sessions: (user = "me") => request(`/api/sessions?user=${encodeURIComponent(user)}`),
  sessionDetail: (id) => request(`/api/sessions/${encodeURIComponent(id)}`),
  home: () => request("/api/home"),
  knowledgeSystem: () => request("/api/knowledge-system"),
  resetKnowledgeSystem: (confirm) => post("/api/knowledge-system/reset", { confirm }),
  setup: () => request("/api/setup"),
  setupMessage: (text) => post("/api/setup/message", { text }),
  setupConfirm: () => post("/api/setup/confirm", {}),
  setupBlueprint: () => post("/api/setup/blueprint", {}),
  setupBlueprintRevise: (feedback) => post("/api/setup/blueprint/revise", { feedback }),
  setupBlueprintConfirm: () => post("/api/setup/blueprint/confirm", {}),
  setupBack: () => post("/api/setup/back", {}),
  setupSourcesDiscover: () => post("/api/setup/sources/discover", {}),
  setupSourceChoose: (id, status) => post(`/api/setup/sources/${encodeURIComponent(id)}`, { status }),
  setupSourceAdd: (url) => post("/api/setup/sources/add", { url }),
  setupSourcesContinue: () => post("/api/setup/sources/continue", {}),
  setupContentAnalyse: (sourceId = null) => post("/api/setup/content/analyse", sourceId ? { source_id: sourceId } : {}),
  setupContentSection: (id) => request(`/api/setup/content/${encodeURIComponent(id)}`),
  setupContentChoose: (id, status) => post(`/api/setup/content/${encodeURIComponent(id)}`, { status }),
  setupContentExclude: (id, excluded) => post(`/api/setup/content/${encodeURIComponent(id)}/urls`, { excluded }),
  setupBackToSources: () => post("/api/setup/back-to-sources", {}),
  setupPlan: () => request("/api/setup/plan"),
  setupPlanTtl: (id, ttlDays) => post(`/api/setup/content/${encodeURIComponent(id)}/ttl`, { ttl_days: ttlDays }),
  setupBuildRag: (version) => post("/api/setup/plan/approve", { version }),
  setupBuildStart: (version) => post("/api/setup/build/start", { version }),
  setupBuildRetry: () => post("/api/setup/build/retry", {}),
  setupBackToContent: () => post("/api/setup/back-to-content", {}),
  setupRelabel: () => post("/api/setup/relabel", {}),
  setupEvaluation: () => request("/api/setup/evaluation"),
  setupEvaluationPrepare: () => post("/api/setup/evaluation/prepare", {}),
  setupEvaluationRun: () => post("/api/setup/evaluation/run", {}),
  setupReadiness: () => request("/api/setup/readiness"),
  setupGoLive: (confirmGaps) => post("/api/setup/go-live", { confirm_gaps: Boolean(confirmGaps) }),
  provenance: (url) => request(`/api/knowledge/provenance?url=${encodeURIComponent(url)}`),
  overview: (force = false) => request(fresh("/api/overview", force)),
  ingestion: (force = false) => request(fresh("/api/ingestion", force)),
  trigger: (kind) => post("/api/ingestion/trigger", { kind }),
  removeSources: (dataset, urls) => post("/api/sources/remove", { dataset, urls }),
  sourceReports: () => request("/api/source-reports"),
  removeSourceReports: (ids) => post("/api/source-reports/remove", { report_ids: ids }),
  adhoc: () => request("/api/adhoc"),
  adhocPreview: (title, text) => post("/api/adhoc/preview", { title, text }),
  adhocSubmit: (title, text, ttlDays) => post("/api/adhoc/submit", { title, text, ttl_days: ttlDays }),
  docs: () => request("/api/docs"),
  docsSample: () => request("/api/docs/sample"),
  evaluations: () => request("/api/evaluations"),
  evaluation: (id) => request(`/api/evaluations/${encodeURIComponent(id)}`),
  evaluationStart: (questions, models, name) => post("/api/evaluations/start", { questions, models, name }),
  evaluationCancel: (id) => post(`/api/evaluations/${encodeURIComponent(id)}/cancel`, {}),
  evaluationResume: (id) => post(`/api/evaluations/${encodeURIComponent(id)}/resume`, {}),
};

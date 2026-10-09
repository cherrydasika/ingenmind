// The flows API (app/web_api.py). Errors carry the server's message and issues.
// 401: the sign-in is gone; the app shell (web/static/js/main.js) shows the sign-in screen.
function checkSignedIn(resp) {
  if (resp.status === 401) window.dispatchEvent(new Event("rag:signed-out"));
}

async function call(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  checkSignedIn(resp);
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const error = new Error(data?.error || `${path}: ${resp.status}`);
    error.issues = data?.issues || [];
    throw error;
  }
  return data;
}

const at = (id) => `/api/flows/${encodeURIComponent(id)}`;

/** POST and read server-sent events: onEvent(stage event), resolves with the result. */
async function stream(path, body, onEvent) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  checkSignedIn(resp);
  if (!resp.ok) {
    const data = await resp.json().catch(() => null);
    const error = new Error(data?.error || `${path}: ${resp.status}`);
    error.issues = data?.issues || [];
    throw error;
  }
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new Error("the run ended without a result");
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const data = JSON.parse(/^data: (.*)$/m.exec(block)?.[1] ?? "null");
      if (event === "stage") onEvent(data);
      else if (event === "result") return data;
      else if (event === "error") throw new Error(data?.error || "the run failed");
    }
  }
}

export const flowsApi = {
  list: () => call("GET", "/api/flows"),
  get: (id) => call("GET", at(id)),
  version: (id, v) => call("GET", `${at(id)}/versions/${v}`),
  create: (flow) => call("POST", "/api/flows", { flow }),
  saveDraft: (flow) => call("PUT", `${at(flow.id)}/draft`, { flow }),
  discardDraft: (id) => call("DELETE", `${at(id)}/draft`),
  publish: (id, note) => call("POST", `${at(id)}/publish`, { note }),
  validate: (flow) => call("POST", "/api/flows/validate", { flow }),
  setLive: (id, version) => call("POST", `${at(id)}/live`, { version }),
  templates: () => call("GET", "/api/flows/templates"),
  diff: (old, current) => call("POST", "/api/flows/diff", { old, new: current }),
  metrics: (id, days = 30) => call("GET", `${at(id)}/metrics?days=${days}`),
  run: (id, payload, onEvent) => stream(`${at(id)}/run`, payload, onEvent),
};

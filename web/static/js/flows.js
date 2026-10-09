// Flows: the Langflow-style flow builder. It is a React Flow bundle built from
// web/flows-app into static/flows and loaded on demand; it reads and saves
// flows through /api/flows itself (the playground runs them there too).

import { errorBox, h, loading } from "./ui.js";

function loadStyles() {
  const href = new URL("../flows/flows.css", import.meta.url).href;
  if (!document.querySelector(`link[href="${href}"]`)) {
    document.head.append(h("link", { rel: "stylesheet", href }));
  }
}

export class FlowsPage {
  title = "Agent flow";

  constructor(root, { session }) {
    this.root = root;
    this.session = session;
    this.canvas = null;
  }

  async mount() {
    this.root.replaceChildren(loading());
    let canvas;
    try {
      loadStyles();
      canvas = await import("../flows/flows.js");
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load the flow builder: ${err.message}`));
      return;
    }
    const host = h("div", { class: "flows-host" });
    this.root.replaceChildren(host);
    this.canvas = canvas.mountFlowCanvas(host, {
      initialFlowId: "travel_assistant",
      colorMode: document.documentElement.dataset.theme || "system",   // light | dark | system
      session: () => ({ id: this.session.id, userId: this.session.userId }),
    });
  }

  unmount() {
    this.canvas?.unmount();
    this.canvas = null;
  }
}

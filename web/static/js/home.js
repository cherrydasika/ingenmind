// Home: every local service with live status, shortcuts, project links.
// Mirrors app/home_tab.py; the lists come from app/services.py.

import { api } from "./api.js";
import { card, errorBox, h, loading, md, section } from "./ui.js";

function linkCard({ icon, name, url, description, up }) {
  const status = up === undefined ? null
    : h("span", { class: `status-pill ${up ? "up" : "down"}` }, up ? "● up" : "● down");
  return card({
    cls: "link-card",
    title: [h("span", {}, icon), url ? h("a", { href: url, target: "_blank", rel: "noopener" }, name) : name, status],
    children: md(description, "prose note"),
  });
}

export class HomePage {
  title = "Home";

  constructor(root) {
    this.root = root;
  }

  async mount() {
    this.root.replaceChildren(loading());
    let data;
    try {
      data = await api.home();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load services: ${err.message}`));
      return;
    }
    const grid = (items) => h("div", { class: "grid-2" }, items.map(linkCard));
    this.root.replaceChildren(h("div", { class: "stack" },
      section("services", "Services",
        "Every local service in this stack, with live status — checked from inside the app's container every 30s. Langfuse and MinIO are down unless the optional overlay is started.",
        grid(data.services)),
      data.shortcuts.length ? section("shortcuts", "Shortcuts", null, grid(data.shortcuts)) : null,
      section("project", "Project", null, grid(data.project)),
    ));
  }
}

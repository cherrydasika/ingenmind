// Admin → Integrations → Channels (R50, app/channels): ask IngenMind from an
// embeddable widget, Slack or Microsoft Teams, through the same flow,
// guardrails, limits, sessions and feedback as the Ask page.

import { api } from "./api.js";
import { badge, card, errorBox, h } from "./ui.js";

const KIND = { widget: "Web widget", slack: "Slack", teams: "Microsoft Teams" };

const copyable = (text) => h("code", { style: "word-break: break-all; user-select: all" }, text);

function where(c) {
  const base = window.location.origin;
  if (c.kind === "widget") {
    return [h("div", { class: "faint" }, "Add to the site's pages:"),
      copyable(`<script src="${base}/widget.js" data-widget="${c.channel_id}" async></script>`),
      h("div", { class: "faint" }, `${c.config.mode === "public" ? "Public: anyone on " : "Signed in: IngenMind users on "}${c.config.allowed_origins.join(", ")}`)];
  }
  if (c.kind === "slack") {
    return [h("div", { class: "faint" }, "Event Subscriptions → Request URL:"), copyable(`${base}/channels/slack/${c.channel_id}/events`),
      h("div", { class: "faint" }, "Interactivity → Request URL:"), copyable(`${base}/channels/slack/${c.channel_id}/interactive`)];
  }
  return [h("div", { class: "faint" }, "Azure Bot → Configuration → Messaging endpoint:"), copyable(`${base}/channels/teams/${c.channel_id}/messages`),
    h("div", { class: "faint" }, `App ID ${c.config.app_id}`)];
}

export class ChannelsBox {
  constructor(onChange) {
    this.onChange = onChange;
    this.root = h("div");
    this.kind = "widget";
    this.flash = null;
  }

  async load() {
    try {
      this.data = await api.channels();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Channels: ${err.message}`));
      return;
    }
    this.render();
  }

  async run(call) {
    try {
      await call();
      this.flash = null;
    } catch (err) {
      this.flash = err.message;
    }
    await this.load();
  }

  form() {
    const field = (label, input) => h("label", {}, h("div", { class: "faint" }, label), input);
    const text = (placeholder) => h("input", { type: "text", placeholder });
    const kind = h("select", { "aria-label": "Kind" }, Object.entries(KIND).map(([k, v]) => h("option", { value: k, selected: k === this.kind }, v)));
    kind.addEventListener("change", () => { this.kind = kind.value; this.render(); });
    const name = text({ widget: "Help centre", slack: "Company Slack", teams: "Company Teams" }[this.kind]);
    const guests = h("input", { type: "checkbox" });
    let fields, read;
    if (this.kind === "widget") {
      const origins = text("https://help.example.com, https://www.example.com");
      const mode = h("select", {}, h("option", { value: "public" }, "Public: anyone on those sites"),
        h("option", { value: "signed_in" }, "Signed in: IngenMind users only"));
      const title = text("Ask us");
      const greeting = text("Hi! Ask me about our policies. (optional)");
      fields = [field("Allowed origins (the sites that embed it)", origins), field("Who can ask", mode), field("Title", title), field("Greeting", greeting)];
      read = () => ({ config: { allowed_origins: origins.value, mode: mode.value, title: title.value, greeting: greeting.value } });
    } else if (this.kind === "slack") {
      const token = text("xoxb-… (stored encrypted)");
      const signing = text("Signing secret (stored encrypted)");
      fields = [field("Bot User OAuth Token", token), field("Signing secret", signing),
        h("label", { class: "check-row" }, guests, "Let people without an IngenMind account ask (as this channel's account)")];
      read = () => ({ config: { guests: guests.checked }, secret: { bot_token: token.value.trim(), signing_secret: signing.value.trim() } });
    } else {
      const appId = text("Microsoft App ID (a GUID)");
      const tenant = text("Tenant ID (single-tenant bots; else empty)");
      const password = text("Client secret (stored encrypted)");
      fields = [field("App ID", appId), field("Tenant ID", tenant), field("Client secret", password),
        h("label", { class: "check-row" }, guests, "Let people without an IngenMind account ask (as this channel's account)")];
      read = () => ({ config: { app_id: appId.value.trim(), tenant_id: tenant.value.trim(), guests: guests.checked },
        secret: { app_password: password.value.trim() } });
    }
    const add = () => this.run(() => api.addChannel({ kind: this.kind, name: name.value, ...read() }));
    return h("div", { class: "stack" }, field("Kind", kind), field("Name", name), ...fields,
      this.kind !== "widget" && !this.data.secrets ? h("p", { class: "note" }, "INTEGRATIONS_KEY isn't set on the server, so its secrets can't be stored yet.") : null,
      h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: add }, `Add ${KIND[this.kind]}`)));
  }

  render() {
    const list = this.data.channels.map((c) => card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, h("strong", {}, c.name), " ", badge(KIND[c.kind], "neutral"),
          c.config.guests ? [" ", badge("guests ask too", "warn")] : null),
        h("button", { class: "btn btn-ghost btn-sm", type: "button",
          onclick: () => this.run(() => api.removeChannel(c.channel_id)) }, "Remove")),
      h("div", { class: "stack" }, ...where(c)),
    ] }));
    this.root.replaceChildren(card({ title: "Channels", children: [
      h("p", { class: "note" }, "Ask from where people already are: a chat widget on your site, Slack or Microsoft Teams. Answers come "
        + "from the live flow with the same guardrails, answer check, limits and feedback as the Ask page, and each "
        + "conversation is a session (labelled with its channel). On Slack and Teams people ask as their IngenMind account, "
        + "matched by email. Setting up the Slack app or the Azure bot is in the README."),
      this.flash ? errorBox(this.flash) : null,
      this.form(),
    ] }), ...list);
  }
}

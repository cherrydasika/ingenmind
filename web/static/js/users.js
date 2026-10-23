// Users (Admin): add people, change their role, permissions and status.
// No passwords: people sign in through the identity provider, and their
// first sign-in is matched to the email added here. Users are deactivated,
// never deleted. manage_users only (app/web_api.py user_*).

import { api } from "./api.js";
import { AllKeysBox } from "./api_keys.js";
import { badge, card, errorBox, fmtInt, h, loading, note } from "./ui.js";

const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");

export class UsersPage {
  title = "Users";

  constructor(root) {
    this.root = root;
    this.editing = null;   // user_id whose edit panel is open
    this.message = null;   // {kind: "ok" | "err", text}
  }

  async mount() {
    this.root.replaceChildren(loading());
    await this.load();
  }

  async load() {
    try {
      this.data = await api.users();
    } catch (err) {
      this.root.replaceChildren(errorBox(`Couldn't load users: ${err.message}`));
      return;
    }
    this.render();
  }

  say(kind, text) {
    this.message = { kind, text };
  }

  render() {
    const { users } = this.data;
    const names = Object.fromEntries(users.map((u) => [u.user_id, u.first_name]));
    const message = this.message
      ? h("div", { class: `alert ${this.message.kind}` }, this.message.text) : null;
    this.root.replaceChildren(h("div", { class: "stack" },
      note("People sign in through your identity provider; this app never stores a password. Add someone by the email they sign in with, and their first sign-in is matched to it. Deactivate rather than delete: their history stays, and they are signed out at once."),
      message,
      card({ title: "Add a user", children: this.form(null) }),
      card({ title: `Users (${users.length})`, children: h("div", { class: "table-wrap" }, h("table", { class: "users" },
        h("thead", {}, h("tr", {}, ["Name", "Email", "Role", "Status", "Last sign-in", "Permissions", "Today", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, users.flatMap((u) => [
          h("tr", { class: u.status === "active" ? null : "user-disabled" },
            h("td", {}, u.first_name, u.user_id === this.data.me ? h("span", { class: "faint" }, " (you)") : null),
            h("td", {}, u.email || "—"),
            h("td", {}, u.role),
            h("td", {}, badge(u.status, u.status === "active" ? "ok" : "neutral")),
            h("td", {}, when(u.last_login)),
            h("td", { title: u.permissions.map((p) => this.data.permissions[p] || p).join("\n") }, `${u.permissions.length} of ${Object.keys(this.data.permissions).length}`),
            h("td", {}, usageCell(u.usage)),
            h("td", {}, h("button", { class: "btn btn-ghost btn-sm", type: "button",
              onclick: () => { this.editing = this.editing === u.user_id ? null : u.user_id; this.message = null; this.render(); } },
            this.editing === u.user_id ? "Close" : "Edit"))),
          this.editing === u.user_id ? h("tr", { class: "user-edit-row" }, h("td", { colspan: 8 },
            this.form(u),
            h("p", { class: "faint user-audit" },
              `Added ${when(u.created_at)}${u.created_by ? ` by ${names[u.created_by] || u.created_by}` : ""}`,
              u.updated_at ? ` · last changed ${when(u.updated_at)}${u.updated_by ? ` by ${names[u.updated_by] || u.updated_by}` : ""}` : ""))) : null,
        ]).filter(Boolean))))
      })));
    // Everyone's API keys (R46), to revoke any of them.
    const keys = h("div", {}, loading());
    this.root.firstChild.append(card({ title: "API keys", children: keys }));
    new AllKeysBox(keys).load();
  }

  // The add form (user null) or a user's edit form.
  form(user) {
    const { permissions, roles } = this.data;
    const adding = !user;
    const name = h("input", { type: "text", value: user?.first_name || "", maxlength: 80, required: true, placeholder: "First name" });
    const email = adding ? h("input", { type: "email", required: true, placeholder: "name@example.com" }) : null;
    const role = h("select", {}, Object.keys(roles).map((r) => h("option", { value: r, selected: (user?.role || "user") === r }, r)));
    const boxes = Object.entries(permissions).map(([key, label]) => {
      const box = h("input", { type: "checkbox", value: key, checked: (user ? user.permissions : roles.user).includes(key) });
      return [key, box, h("label", { class: "perm" }, box, h("span", {}, label), h("code", { class: "faint" }, key))];
    });
    role.addEventListener("change", () => {   // a role fills in its bundle; boxes can then be adjusted
      for (const [key, box] of boxes) box.checked = roles[role.value].includes(key);
    });
    const chosen = () => boxes.filter(([, box]) => box.checked).map(([key]) => key);
    // Usage limits (usage_limits.py): empty keeps the role's default.
    const base = this.data.limit_defaults?.[user?.role === "admin" ? "admin" : "others"] || {};
    const own = user?.usage?.override || {};
    const limit = (key, label, unit) => h("input", { type: "number", min: 0, step: 1, value: own[key] ?? "",
      placeholder: `default ${fmtInt(base[key] ?? 0)}`, "aria-label": label, title: `${label} (${unit}); 0 = no limit` });
    const perMinute = adding ? null : limit("per_minute", "Questions per minute", "questions");
    const perDay = adding ? null : limit("tokens_per_day", "Tokens per day", "tokens");
    const number = (input) => (input.value.trim() === "" ? null : Math.max(0, Math.floor(Number(input.value))));
    const save = async (changes) => {
      try {
        if (adding) {
          await api.addUser({ first_name: name.value.trim(), email: email.value.trim(), role: role.value, permissions: chosen() });
          this.say("ok", `Added ${name.value.trim()}. They can sign in with ${email.value.trim()}.`);
        } else {
          await api.updateUser(user.user_id, changes);
          this.say("ok", `Saved ${changes.first_name || user.first_name}.`);
          this.editing = null;
        }
      } catch (err) {
        this.say("err", err.message);
      }
      await this.load();
    };
    const status = !adding ? h("button", {
      class: `btn btn-sm ${user.status === "active" ? "btn-danger" : "btn-ghost"}`, type: "button",
      onclick: () => save({ status: user.status === "active" ? "disabled" : "active" }),
    }, user.status === "active" ? "Deactivate" : "Reactivate") : null;
    return h("form", { class: "user-form", onsubmit: (event) => {
      event.preventDefault();
      save(adding ? null : { first_name: name.value.trim(), role: role.value, permissions: chosen(),
        limits: { per_minute: number(perMinute), tokens_per_day: number(perDay) } });
    } },
    h("div", { class: "user-form-row" },
      h("label", {}, h("span", { class: "faint" }, "First name"), name),
      adding ? h("label", {}, h("span", { class: "faint" }, "Email (their sign-in)"), email) : null,
      h("label", {}, h("span", { class: "faint" }, "Role"), role)),
    h("div", { class: "perm-grid" }, boxes.map(([, , label]) => label)),
    adding ? null : h("div", { class: "user-form-row" },
      h("label", {}, h("span", { class: "faint" }, "Questions per minute"), perMinute),
      h("label", {}, h("span", { class: "faint" }, "Tokens per day"), perDay),
      h("span", { class: "faint", style: "align-self:end" }, "Empty: the role's default. 0: no limit.")),
    h("div", { class: "user-form-actions" },
      h("button", { class: "btn btn-sm", type: "submit" }, adding ? "Add user" : "Save"), status));
  }
}

// Today's questions and tokens against the day's allowance.
function usageCell(usage) {
  if (!usage) return "—";
  const { today, limits } = usage;
  const cap = limits.tokens_per_day;
  const share = cap ? today.tokens / cap : 0;
  return h("span", { class: share >= 1 ? "usage-over" : share >= 0.8 ? "usage-near" : null,
    title: `${fmtInt(today.questions)} questions; ${fmtInt(today.tokens)} of ${cap ? fmtInt(cap) : "unlimited"} tokens today; `
      + `${limits.per_minute || "unlimited"} questions a minute${usage.override ? " (own limits)" : ""}` },
  `${fmtInt(today.questions)} q · ${compact(today.tokens)}${cap ? ` / ${compact(cap)}` : ""}`);
}

function compact(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

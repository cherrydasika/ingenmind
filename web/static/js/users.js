// Users (Admin): add people, change their role, permissions and status.
// No passwords: people sign in through the identity provider, and their
// first sign-in is matched to the email added here. Users are deactivated,
// never deleted. manage_users only (app/web_api.py user_*).

import { api } from "./api.js";
import { badge, card, errorBox, h, loading, note } from "./ui.js";

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
        h("thead", {}, h("tr", {}, ["Name", "Email", "Role", "Status", "Last sign-in", "Permissions", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, users.flatMap((u) => [
          h("tr", { class: u.status === "active" ? null : "user-disabled" },
            h("td", {}, u.first_name, u.user_id === this.data.me ? h("span", { class: "faint" }, " (you)") : null),
            h("td", {}, u.email || "—"),
            h("td", {}, u.role),
            h("td", {}, badge(u.status, u.status === "active" ? "ok" : "neutral")),
            h("td", {}, when(u.last_login)),
            h("td", { title: u.permissions.map((p) => this.data.permissions[p] || p).join("\n") }, `${u.permissions.length} of ${Object.keys(this.data.permissions).length}`),
            h("td", {}, h("button", { class: "btn btn-ghost btn-sm", type: "button",
              onclick: () => { this.editing = this.editing === u.user_id ? null : u.user_id; this.message = null; this.render(); } },
            this.editing === u.user_id ? "Close" : "Edit"))),
          this.editing === u.user_id ? h("tr", { class: "user-edit-row" }, h("td", { colspan: 7 },
            this.form(u),
            h("p", { class: "faint user-audit" },
              `Added ${when(u.created_at)}${u.created_by ? ` by ${names[u.created_by] || u.created_by}` : ""}`,
              u.updated_at ? ` · last changed ${when(u.updated_at)}${u.updated_by ? ` by ${names[u.updated_by] || u.updated_by}` : ""}` : ""))) : null,
        ]).filter(Boolean))))
      })));
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
      save(adding ? null : { first_name: name.value.trim(), role: role.value, permissions: chosen() });
    } },
    h("div", { class: "user-form-row" },
      h("label", {}, h("span", { class: "faint" }, "First name"), name),
      adding ? h("label", {}, h("span", { class: "faint" }, "Email (their sign-in)"), email) : null,
      h("label", {}, h("span", { class: "faint" }, "Role"), role)),
    h("div", { class: "perm-grid" }, boxes.map(([, , label]) => label)),
    h("div", { class: "user-form-actions" },
      h("button", { class: "btn btn-sm", type: "submit" }, adding ? "Add user" : "Save"), status));
  }
}

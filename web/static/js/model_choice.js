// Overview → Chat model (model_choice.py): the models .env names (local ones
// with scripts/use-local-models.sh), or a hosted frontier model on the
// admin's own API key. The key is tested before it is kept, stored encrypted
// and never sent back; only its last four characters are shown.

import { api } from "./api.js";
import { badge, errorBox, fmtTimestamp, h } from "./ui.js";

const MODEL_NAMES = {
  "claude-opus-5-5": "Claude Opus 5.5 — most capable · $4 / $20 per M tokens",
  "claude-sonnet-5-5": "Claude Sonnet 5.5 — fast and capable · $2 / $10",
  "claude-haiku-5-5": "Claude Haiku 5.5 — fastest, cheapest · $0.10 / $0.50",
};

export function modelChoiceCard(view, reload) {
  const chosen = view.chosen;
  let mode = chosen ? "hosted" : "local";
  let provider = chosen?.provider || "anthropic";
  const message = h("div", { "aria-live": "polite" });

  const radio = (value, title, sub) => {
    const input = h("input", { type: "radio", name: "model-mode", value, checked: mode === value,
      onchange: () => { mode = value; paint(); } });
    return h("label", { class: `choice${mode === value ? " is-chosen" : ""}`, "data-mode": value }, input,
      h("span", {}, h("strong", {}, title), h("span", { class: "faint" }, sub)));
  };
  const local = radio("local", `Local: ${view.local.label}`,
    "Free and private: runs on this machine, and only web searches and page fetches leave it. Slower, "
    + "and a small model sometimes refuses or can't answer a question a hosted one would.");
  const hosted = radio("hosted", "A hosted frontier model, on your own API key",
    "Usually faster and more accurate. Questions and the pages they use are sent to the provider; "
    + "usage is billed to your account.");

  // The hosted model's details
  const providerSelect = h("select", { "aria-label": "Provider", onchange: (e) => { provider = e.target.value; paintModels(); } },
    Object.entries(view.providers).map(([key, p]) => h("option", { value: key, selected: key === provider }, p.label)));
  const modelSlot = h("div");
  let modelInput;
  const paintModels = () => {
    const p = view.providers[provider];
    const current = chosen?.provider === provider ? chosen.model : "";
    modelInput = p.models.length
      ? h("select", { "aria-label": "Model" }, p.models.map((m) => h("option", { value: m, selected: m === current }, MODEL_NAMES[m] || m)))
      : h("input", { type: "text", "aria-label": "Model", placeholder: "The model's id, as your provider names it", value: current });
    key.placeholder = chosen?.provider === provider ? `Saved (${chosen.key_hint}): paste a new key to replace it` : `${p.key_prefix}…`;
    keyLink.href = p.key_url;
    modelSlot.replaceChildren(modelInput);
  };
  const key = h("input", { type: "password", autocomplete: "off", spellcheck: "false", "aria-label": "API key" });
  const keyLink = h("a", { target: "_blank", rel: "noopener" }, "Get a key");
  const details = h("div", { class: "model-hosted" },
    h("div", { class: "field" }, h("label", {}, "Provider"), providerSelect),
    h("div", { class: "field" }, h("label", {}, "Model"), modelSlot),
    h("div", { class: "field" }, h("label", {}, "API key"), key,
      h("div", { class: "hint" }, "Tested with one tiny request before it's used, then stored encrypted and never shown again. ", keyLink)));

  const busy = (button, text) => { button.disabled = true; button.textContent = text; };
  const use = h("button", { class: "btn btn-sm", type: "button", onclick: async () => {
    message.replaceChildren();
    if (mode === "local") {
      busy(use, "Switching…");
      try { await api.modelChoiceClear(); reload(); } catch (err) { message.replaceChildren(errorBox(err.message)); paint(); }
      return;
    }
    busy(use, "Testing the key…");
    try {
      await api.modelChoiceSave({ provider, model: modelInput.value.trim(), key: key.value.trim() });
      key.value = "";
      reload();
    } catch (err) {
      message.replaceChildren(errorBox(err.message));
      paint();
    }
  } });

  const status = chosen
    ? h("p", { class: "note" }, badge("in use", "ok"), " ", `${MODEL_NAMES[chosen.model]?.split(" — ")[0] || chosen.model} on the key ending ${chosen.key_hint}`,
      chosen.set_by ? `, chosen by ${chosen.set_by}` : "", ` on ${fmtTimestamp(chosen.set_at)}.`)
    : h("p", { class: "note" }, badge("in use", "ok"), " ", `Local models (${view.local.label}), as .env sets.`);
  const cantStore = view.can_store ? null : h("div", { class: "alert", style: "margin-bottom:10px" },
    "To keep an API key the server needs INTEGRATIONS_KEY in .env (scripts/use-local-models.sh makes one); restart the app after adding it.");

  function paint() {
    for (const el of [local, hosted]) el.classList.toggle("is-chosen", el.dataset.mode === mode);
    details.hidden = mode !== "hosted";
    use.disabled = (mode === "local" && !chosen) || (mode === "hosted" && !view.can_store);
    use.textContent = mode === "local" ? (chosen ? "Switch back to local models (forgets the key)" : "In use")
      : chosen ? "Test and save" : "Test the key and switch";
  }
  paintModels();
  paint();
  return [status, h("div", { class: "choices" }, local, hosted), cantStore, details, message,
    h("div", { class: "alert-actions" }, use)];
}

// Files and document connectors (R48, app/connectors): upload a PDF, Word,
// PowerPoint, HTML, Markdown or text file, or import files from S3 (or
// MinIO); each keeps where it came from. Shown on Data → Ad-hoc documents.

import { api } from "./api.js";
import { badge, card, errorBox, h, note, shortUrl } from "./ui.js";

const toBase64 = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
  reader.onerror = () => reject(reader.error);
  reader.readAsDataURL(file);
});

export function uploadCard(onDone) {
  const input = h("input", { type: "file", accept: ".pdf,.docx,.pptx,.html,.htm,.md,.markdown,.txt" });
  const days = h("input", { type: "number", min: 1, value: 30, style: "width: 90px" });
  const out = h("div", {});
  const send = async () => {
    const file = input.files?.[0];
    if (!file) return;
    out.replaceChildren(note(`Reading ${file.name}…`));
    try {
      const result = await api.uploadDocument({ filename: file.name, data: await toBase64(file), ttl_days: Number(days.value) });
      out.replaceChildren(h("div", { class: "alert ok", role: "status" },
        `${file.name}: ${result.status}, ${result.chunks ?? 0} chunks (${result.source_id}).`));
      onDone?.();
    } catch (err) {
      out.replaceChildren(errorBox(err.message));
    }
  };
  return card({ title: "Upload a file", children: [
    h("p", { class: "note" }, "PDF, Word (.docx), PowerPoint (.pptx), HTML, Markdown or text, up to 10 MB: its text is chunked "
      + "and embedded like a pasted document, and it keeps its file name as its source."),
    h("div", { class: "stack" }, input, h("label", {}, h("div", { class: "faint" }, "Keep for (days)"), days),
      h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: send }, "Upload")), out),
  ] });
}

export class ConnectorsBox {
  constructor(box) {
    this.box = box;
    this.items = {};
    this.flash = null;
  }

  async load() {
    try {
      this.data = await api.connectors();
    } catch (err) {
      this.box.replaceChildren(note(`Document connectors: ${err.message}`));
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

  renderConnector(c) {
    const shown = this.items[c.connector_id];
    const picks = shown?.items?.map((i) => h("label", { class: "check-row" },
      h("input", { type: "checkbox", value: i.id, disabled: !i.supported, checked: i.supported }),
      h("span", {}, i.id), h("span", { class: "faint" }, ` ${Math.round(i.size / 1024)} KB`),
      i.supported ? null : badge("not a supported file", "neutral")));
    const browse = async () => {
      try {
        this.items[c.connector_id] = await api.connectorItems(c.connector_id);
        this.flash = null;
      } catch (err) {
        this.flash = err.message;
      }
      this.render();
    };
    const doImport = () => this.run(async () => {
      const chosen = picks.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      const { results } = await api.importConnector(c.connector_id, { items: chosen, ttl_days: 30 });
      this.items[c.connector_id] = { ...shown, results };
    });
    return card({ children: [
      h("div", { class: "toolbar" },
        h("div", {}, h("strong", {}, c.name), " ", h("code", {}, `s3://${c.config.bucket}/${c.config.prefix || ""}`),
          c.config.endpoint_url ? h("span", { class: "faint" }, ` · ${shortUrl(c.config.endpoint_url)}`) : null),
        h("div", {}, h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: browse }, "Browse files"), " ",
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => this.run(() => api.removeConnector(c.connector_id)) }, "Remove"))),
      picks ? h("div", {}, ...(picks.length ? picks : [note("No files under this prefix.")])) : null,
      picks?.length ? h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: doImport }, "Import ticked files")) : null,
      shown?.results ? h("ul", {}, shown.results.map((r) => h("li", {}, `${r.id}: ${r.status}${r.error ? ` (${r.error})` : `, ${r.chunks} chunks`}`))) : null,
    ] });
  }

  render() {
    const field = (label, input) => h("label", {}, h("div", { class: "faint" }, label), input);
    const inputs = Object.fromEntries(["connector_id", "name", "bucket", "prefix", "endpoint_url", "region", "access_key", "secret_key"]
      .map((k) => [k, h("input", { type: "text", placeholder: { connector_id: "policies", name: "Policies bucket", bucket: "my-docs",
        prefix: "handbook/", endpoint_url: "empty for AWS S3, or http://minio:9000", region: "eu-west-2", access_key: "optional",
        secret_key: "optional (stored encrypted)" }[k] })]));
    const add = () => this.run(() => api.addConnector({ kind: "s3", ...Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()])) }));
    this.box.replaceChildren(...[
      h("p", { class: "note" }, "Files in S3 (or anything that speaks S3, such as MinIO), imported with where they came from. Without "
        + "keys, the server's own AWS credentials are used. Google Drive, SharePoint, Confluence and Notion need an OAuth app on "
        + "your organisation's account first."),
      this.flash ? errorBox(this.flash) : null,
      ...this.data.connectors.map((c) => this.renderConnector(c)),
      h("div", { class: "stack" }, ...Object.entries(inputs).map(([k, i]) => field(k.replace("_", " "), i)),
        h("div", { class: "actions" }, h("button", { class: "btn btn-sm", type: "button", onclick: add }, "Add S3 connector"))),
    ].filter(Boolean));
  }
}

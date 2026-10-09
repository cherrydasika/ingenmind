// A settings form generated from a component's JSON Schema (its Pydantic
// config model). Values equal to the default are left out of the flow JSON.
import { useEffect, useState } from "react";

function kindOf(prop) {
  if (prop.enum) return prop.enum.length === 1 ? "fixed" : "enum";
  if (prop.const !== undefined) return "fixed";
  if (prop.type === "boolean") return "boolean";
  if (prop.type === "integer" || prop.type === "number") return "number";
  if (prop.type === "array" && prop.items?.enum) return "choices";
  if (prop.type === "array" && (prop.items?.type || "string") === "string") return "list";
  if (prop.type === "string") return "string";
  return "json";
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function NumberField({ prop, value, onChange }) {
  const [text, setText] = useState(value ?? "");
  useEffect(() => setText(value ?? ""), [value]);
  const min = prop.minimum ?? prop.exclusiveMinimum, max = prop.maximum ?? prop.exclusiveMaximum;
  return (
    <input type="number" value={text} min={min} max={max} step={prop.type === "integer" ? 1 : "any"}
      onChange={(e) => {
        setText(e.target.value);
        if (e.target.value === "") return;
        const n = prop.type === "integer" ? parseInt(e.target.value, 10) : parseFloat(e.target.value);
        if (!Number.isNaN(n)) onChange(n);
      }} />
  );
}

function ListField({ value, onChange }) {
  const [text, setText] = useState((value || []).join("\n"));
  useEffect(() => setText((value || []).join("\n")), [JSON.stringify(value)]);
  return (
    <textarea rows={Math.min(Math.max((value || []).length, 2), 6)} value={text} placeholder="one per line"
      onChange={(e) => {
        setText(e.target.value);
        onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean));
      }} />
  );
}

function Field({ name, prop, required, value, onChange, reference }) {
  const kind = kindOf(prop);
  let input;
  if (kind === "fixed") input = <input value={String(prop.enum?.[0] ?? prop.const)} disabled />;
  else if (kind === "enum") {
    input = (
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        {value === undefined && <option value="">—</option>}
        {prop.enum.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  } else if (kind === "boolean") input = <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />;
  else if (kind === "number") input = <NumberField prop={prop} value={value} onChange={onChange} />;
  else if (kind === "list") input = <ListField value={value} onChange={onChange} />;
  else if (kind === "choices") {
    const chosen = new Set(value || []);
    input = (
      <div className="fl-choices">
        {prop.items.enum.map((option) => (
          <label key={option} className="fl-choice">
            <input type="checkbox" checked={chosen.has(option)} onChange={(e) => {
              const next = prop.items.enum.filter((o) => (o === option ? e.target.checked : chosen.has(o)));
              onChange(next);
            }} />
            <code>{option}</code>
          </label>
        ))}
      </div>
    );
  }
  else if (kind === "string" && prop["x-multiline"]) {
    input = <textarea className="fl-multiline" rows={8} value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
  }
  else if (kind === "string") input = <input value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
  else input = <code>{JSON.stringify(value)}</code>;
  const range = kind === "number" && (prop.minimum !== undefined || prop.maximum !== undefined)
    ? ` (${prop.minimum ?? "…"}–${prop.maximum ?? "…"})` : "";
  return (
    <label className={`fl-field ${kind === "boolean" ? "is-inline" : ""} ${reference ? "is-reference" : ""}`}
      title={reference ? "For reference: the runtime does not read this setting yet" : undefined}>
      <span className="fl-field-name">{name}{required && " *"}{reference && <span className="fl-chip">reference</span>}</span>
      {input}
      {(prop.description || range) && <span className="fl-field-help">{prop.description}{range}</span>}
    </label>
  );
}

export function ConfigForm({ component, config, onChange, readOnly }) {
  const applied = new Set(component?.applied || []);
  const schema = component?.config_schema || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  const keys = Object.keys(props);
  if (!keys.length) return <p className="fl-hint">No settings.</p>;
  const set = (key, value) => {
    const next = { ...config };
    if (props[key].default !== undefined && same(value, props[key].default)) delete next[key];
    else next[key] = value;
    onChange(next);
  };
  return (
    <fieldset className="fl-form" disabled={readOnly}>
      {keys.some((k) => !applied.has(k)) && (
        <p className="fl-hint">Settings marked <span className="fl-chip">reference</span> document what the component
          uses; the runtime does not read them from the flow yet.</p>
      )}
      {keys.map((key) => (
        <Field key={key} name={key} prop={props[key]} required={required.has(key)} reference={!applied.has(key)}
          value={key in (config || {}) ? config[key] : props[key].default} onChange={(v) => set(key, v)} />
      ))}
    </fieldset>
  );
}

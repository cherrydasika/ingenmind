"""A stand-in chat model for load tests (R61): LLM_PROVIDER=stub.

Every call waits LLM_STUB_SECONDS (default 1.0, like a fast real model) and
answers the happy path, so a load test measures the app itself (searches,
database, guardrail plumbing, sessions, concurrency) without paying for, or
being limited by, a provider:

    generate()   a short answer citing [1]
    call_tool()  arguments made from the tool's JSON schema: the first
                 "happy" enum value (ALLOW, GOOD_EVIDENCE, PASS…), booleans
                 true unless the field names a problem (blocked, unsafe,
                 injection…), numbers high within their range, empty lists
    chat()       with tools and no tool result yet: calls the search tool
                 (else the first) once; then ends the turn with an answer

Never for real use: the answers are canned. Its token counts are the text
length / 4, so the dashboards still have numbers.
"""

import os
import re
import time
import uuid

SECONDS = float(os.environ.get("LLM_STUB_SECONDS", "1.0") or 0)
HAPPY = ("ALLOW", "GOOD_EVIDENCE", "PASS", "pass", "passed", "answer", "in_scope", "on_topic", "NONE", "none", "yes",
         "true", "supported", "safe", "low")
PROBLEM = re.compile(r"block|unsafe|injection|harm|off_?topic|pii|refus|contradict|leak|toxic|jailbreak|flag|fail"
                     r"|violat|risk|missing|unsupported|conflict")
ANSWER = "Here is what the knowledge base says about it [1]. (Load-test stub answer.)"


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _resolve(schema: dict, root: dict) -> dict:
    while isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        schema = (root.get("$defs") or root.get("definitions") or {}).get(name, {})
    return schema or {}


def value(schema: dict, root: dict | None = None, name: str = "") -> object:
    """A valid, happy-path value for this JSON schema."""
    root = root or schema
    schema = _resolve(schema, root)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, root).get("type") != "null"] or schema[key]
            return value(options[0], root, name)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return next((v for v in HAPPY if v in schema["enum"]), schema["enum"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0])
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {key: value(sub, root, key) for key, sub in properties.items()}
    if kind == "array":
        count = schema.get("minItems", 0)
        return [value(schema.get("items") or {"type": "string"}, root, name) for _ in range(count)]
    if kind == "boolean":
        return not PROBLEM.search(name.lower())
    if kind in ("number", "integer"):
        high = schema.get("maximum", 1 if kind == "number" else 3)
        low = schema.get("minimum", 0)
        number = high * 0.9 if kind == "number" else max(low, min(high, 1))
        return int(number) if kind == "integer" else round(number, 2)
    if kind == "string":
        return ANSWER if re.search(r"answer|text|summary|reason|rationale|reply", name) else "stub"
    return None


class Stub:
    provider = "stub"

    def __init__(self, provider: str | None = None, model: str | None = None):
        self._model = model or "stub"

    @property
    def model(self) -> str:
        return self._model

    def send(self, prompt, system, max_tokens, temperature, tool):
        import llm
        time.sleep(SECONDS)
        if tool:
            arguments = value(tool["schema"])
            return llm.Reply("", arguments, _tokens((system or "") + prompt), _tokens(str(arguments)), "tool_use")
        return llm.Reply(ANSWER, None, _tokens((system or "") + prompt), _tokens(ANSWER), "end_turn")

    def chat(self, system, messages, tools, max_tokens, temperature):
        import llm
        time.sleep(SECONDS)
        asked = _tokens((system or "") + str(messages))
        searched = any("toolResult" in c for m in messages for c in m.get("content", []))
        if tools and not searched:
            tool = next((t for t in tools if re.search(r"search|retriev|knowledge", t["name"])), tools[0])
            arguments = value(tool.get("schema") or {"type": "object"})
            asked_text = next((c["text"] for m in messages if m.get("role") == "user"
                               for c in m.get("content", []) if c.get("text")), "")
            for key in list(arguments if isinstance(arguments, dict) else []):
                if re.fullmatch(r"query|question|q|search|text", key) and asked_text:
                    arguments[key] = asked_text[:300]       # search for what was asked
            use = {"toolUseId": f"stub-{uuid.uuid4().hex[:12]}", "name": tool["name"], "input": arguments}
            return llm.Turn([{"toolUse": use}], "tool_use", asked, _tokens(str(use)))
        return llm.Turn([{"text": ANSWER}], "end_turn", asked, _tokens(ANSWER))

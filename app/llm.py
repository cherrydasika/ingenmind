"""One interface over the chat-model providers: Amazon Bedrock, Anthropic,
OpenAI and Ollama (through its OpenAI-compatible endpoint).

Chosen by environment:
  LLM_PROVIDER   bedrock (default) | anthropic | openai | ollama
  LLM_MODEL      the model id; Bedrock falls back to BEDROCK_MODEL, Anthropic
                 to claude-haiku-4-5. OpenAI and Ollama have no default.
  BEDROCK_REGION Bedrock only
  ANTHROPIC_API_KEY / OPENAI_API_KEY   read by the provider SDKs
  OLLAMA_BASE_URL  default http://localhost:11434/v1

Three calls: generate() for free text, call_tool() for structured output
through one tool the model must call (a Pydantic schema as its input), and
chat() for one turn of a tool-using conversation (the local agent runtime)."""

import json
import os
import threading
import uuid
from typing import NamedTuple

PROVIDERS = ("bedrock", "anthropic", "openai", "ollama")
DEFAULT_MODELS = {
    "bedrock": os.environ.get("BEDROCK_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "anthropic": "claude-haiku-4-5",
}
LABELS = {"bedrock": "Bedrock", "anthropic": "Anthropic", "openai": "OpenAI", "ollama": "Ollama"}

PROVIDER = os.environ.get("LLM_PROVIDER", "bedrock").strip().lower() or "bedrock"
MODEL = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODELS.get(PROVIDER, "")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-2")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")


class Reply(NamedTuple):
    text: str
    tool_input: dict | None
    input_tokens: int
    output_tokens: int
    stop_reason: str | None

    @property
    def usage(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class Turn(NamedTuple):
    """One model turn of a conversation (chat()), in the common format."""
    content: list[dict]       # {"text"} | {"toolUse": {"toolUseId", "name", "input"}} | {"providerBlock"}
    stop_reason: str | None   # "tool_use", "end_turn", "max_tokens", ...
    input_tokens: int
    output_tokens: int


class NoToolCall(RuntimeError):
    """The model answered without calling the tool it was asked to call."""


def config_error() -> str | None:
    """Why the configured provider cannot run, or None."""
    if PROVIDER not in PROVIDERS:
        return f"LLM_PROVIDER={PROVIDER!r} is not one of {', '.join(PROVIDERS)}"
    if not MODEL:
        return f"set LLM_MODEL for LLM_PROVIDER={PROVIDER}"
    if PROVIDER == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "set ANTHROPIC_API_KEY for LLM_PROVIDER=anthropic"
    if PROVIDER == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return "set OPENAI_API_KEY for LLM_PROVIDER=openai"
    return None


def label() -> str:
    return f"{MODEL} · {LABELS.get(PROVIDER, PROVIDER)}"


def generate(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             temperature: float | None = None) -> Reply:
    return _backend().send(prompt, system, max_tokens, temperature, tool=None)


def call_tool(prompt: str, *, system: str, name: str, description: str, schema: dict,
              max_tokens: int = 1024, temperature: float | None = 0) -> Reply:
    """reply.tool_input is the tool's arguments as a dict; NoToolCall if the
    model did not call it."""
    reply = _backend().send(prompt, system, max_tokens, temperature,
                            tool={"name": name, "description": description, "schema": schema})
    if not isinstance(reply.tool_input, dict):
        raise NoToolCall(f"{PROVIDER} model {MODEL} did not call {name} (stop reason: {reply.stop_reason})")
    return reply._replace(tool_input=_unwrap(reply.tool_input, schema))


def chat(system: str | None, messages: list[dict], tools: list[dict] | None = None, *,
         max_tokens: int = 2048, temperature: float | None = None) -> Turn:
    """One turn of a tool-using conversation. messages use Bedrock Converse's
    shape: {"role": "user"|"assistant", "content": [{"text"} | {"toolUse":
    {"toolUseId", "name", "input": dict}} | {"toolResult": {"toolUseId",
    "status", "content": [{"text"}]}}]}; tools: [{"name", "description",
    "schema"}]. The model chooses whether to call a tool. A {"providerBlock"}
    in the reply (e.g. Claude's thinking) must be kept in the history; only
    its own provider reads it."""
    return _backend().chat(system, messages, tools, max_tokens, temperature)


def _unwrap(arguments: dict, schema: dict) -> dict:
    """Small local models sometimes nest the arguments one level down
    ({"object": {...}, "tool": name}); use the nested object when it, and
    not the outer one, has the schema's required fields."""
    required = set(schema.get("required", []))
    if not required or required <= arguments.keys():
        return arguments
    nested = [v for v in arguments.values() if isinstance(v, dict) and required <= v.keys()]
    return nested[0] if len(nested) == 1 else arguments


# ---------- backends ----------

_lock = threading.Lock()
_instance = None


def _backend():
    global _instance
    with _lock:
        if _instance is None:
            error = config_error()
            if error:
                raise RuntimeError(f"LLM is not configured: {error}")
            _instance = {"bedrock": _Bedrock, "anthropic": _Anthropic,
                         "openai": _OpenAI, "ollama": _OpenAI}[PROVIDER]()
        return _instance


class _Bedrock:
    def __init__(self):
        import boto3
        self.client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def _request(self, system, messages, max_tokens, temperature, tools, choice=None) -> dict:
        inference = {"maxTokens": max_tokens}
        if temperature is not None:
            inference["temperature"] = temperature
        request = {"modelId": MODEL, "messages": messages, "inferenceConfig": inference}
        if system:
            request["system"] = [{"text": system}]
        if tools:
            request["toolConfig"] = {"tools": [{"toolSpec": {
                "name": t["name"], "description": t["description"], "inputSchema": {"json": t["schema"]}}} for t in tools]}
            if choice:
                request["toolConfig"]["toolChoice"] = {"tool": {"name": choice}}
        return request

    def send(self, prompt, system, max_tokens, temperature, tool):
        request = self._request(system, [{"role": "user", "content": [{"text": prompt}]}], max_tokens, temperature,
                                [tool] if tool else None, tool and tool["name"])
        response = self.client.converse(**request)
        content = response["output"]["message"]["content"]
        uses = [b["toolUse"] for b in content if "toolUse" in b]
        tool_input = next((u["input"] for u in uses if tool and u["name"] == tool["name"]), None)
        return Reply("".join(b["text"] for b in content if "text" in b), tool_input,
                     response["usage"]["inputTokens"], response["usage"]["outputTokens"], response.get("stopReason"))

    def chat(self, system, messages, tools, max_tokens, temperature):
        # The common message format is Converse's own; other providers' blocks are dropped.
        native = [{**m, "content": [b for b in m["content"] if "providerBlock" not in b
                                    or b["providerBlock"]["provider"] == "bedrock"]} for m in messages]
        native = [{**m, "content": [b["providerBlock"]["block"] if "providerBlock" in b else b for b in m["content"]]}
                  for m in native]
        response = self.client.converse(**self._request(system, native, max_tokens, temperature, tools))
        content = []
        for block in response["output"]["message"]["content"]:
            if "text" in block or "toolUse" in block:
                content.append(block)
            else:   # e.g. reasoningContent: replayed to Bedrock only
                content.append({"providerBlock": {"provider": "bedrock", "block": block}})
        return Turn(content, response.get("stopReason"), response["usage"]["inputTokens"],
                    response["usage"]["outputTokens"])


class _Anthropic:
    """Newer Claude models reject a forced tool_choice and sampling
    parameters with a 400; each is dropped for that model after its first
    rejection, a forced call falling back to auto plus an instruction.
    SDK 1.x has no temperature argument: older models still honour it, so
    it goes in extra_body."""

    def __init__(self):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.unsupported: set[str] = set()

    def _create(self, build):
        """build() makes the request from what this model still accepts."""
        while True:
            request = build()
            try:
                return self.client.messages.create(**request)
            except self.anthropic.BadRequestError as error:
                sent = {"tool_choice"} & request.keys() | set(request.get("extra_body", {}))
                rejected = next((p for p in ("tool_choice", "temperature") if p in str(error.message) and p in sent), None)
                if not rejected:
                    raise
                self.unsupported.add(rejected)

    def _sampling(self, temperature) -> dict:
        return {"extra_body": {"temperature": temperature}} if temperature is not None and \
            "temperature" not in self.unsupported else {}

    def send(self, prompt, system, max_tokens, temperature, tool):
        def build():
            request = {"model": MODEL, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}], **self._sampling(temperature)}
            instruction = system or ""
            if tool:
                request["tools"] = [{"name": tool["name"], "description": tool["description"],
                                     "input_schema": tool["schema"]}]
                if "tool_choice" in self.unsupported:
                    instruction += f"\n\nRespond only by calling the {tool['name']} tool."
                else:
                    request["tool_choice"] = {"type": "tool", "name": tool["name"]}
            if instruction:
                request["system"] = instruction.strip()
            return request
        response = self._create(build)
        tool_input = next((b.input for b in response.content
                           if b.type == "tool_use" and tool and b.name == tool["name"]), None)
        return Reply("".join(b.text for b in response.content if b.type == "text"), tool_input,
                     response.usage.input_tokens, response.usage.output_tokens, response.stop_reason)

    def chat(self, system, messages, tools, max_tokens, temperature):
        native = [{"role": m["role"], "content": [c for c in map(_to_anthropic, m["content"]) if c]} for m in messages]
        def build():
            request = {"model": MODEL, "max_tokens": max_tokens, "messages": native, **self._sampling(temperature)}
            if system:
                request["system"] = system
            if tools:
                request["tools"] = [{"name": t["name"], "description": t["description"],
                                     "input_schema": t["schema"]} for t in tools]
            return request
        response = self._create(build)
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({"text": block.text})
            elif block.type == "tool_use":
                content.append({"toolUse": {"toolUseId": block.id, "name": block.name, "input": block.input}})
            else:   # thinking blocks must go back unchanged with the tool results
                content.append({"providerBlock": {"provider": "anthropic", "block": block.model_dump(mode="json")}})
        return Turn(content, response.stop_reason, response.usage.input_tokens, response.usage.output_tokens)


def _to_anthropic(block: dict) -> dict | None:
    if "text" in block:
        return {"type": "text", "text": block["text"]} if block["text"] else None
    if "toolUse" in block:
        use = block["toolUse"]
        return {"type": "tool_use", "id": use["toolUseId"], "name": use["name"], "input": use["input"]}
    if "toolResult" in block:
        result = block["toolResult"]
        return {"type": "tool_result", "tool_use_id": result["toolUseId"], "content": _result_text(result),
                "is_error": result.get("status") == "error"}
    if block.get("providerBlock", {}).get("provider") == "anthropic":
        return block["providerBlock"]["block"]
    return None


def _result_text(result: dict) -> str:
    return "\n".join(c.get("text") or json.dumps(c.get("json"), ensure_ascii=False) for c in result.get("content", []))


class _OpenAI:
    """OpenAI, or any OpenAI-compatible server (Ollama). Local models do not
    always honour a forced tool call; a JSON object in the text counts."""

    STOP_REASONS = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}

    def __init__(self):
        import openai
        self.openai = openai
        if PROVIDER == "ollama":
            self.client = openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
        else:
            self.client = openai.OpenAI()
        # Current OpenAI models want max_completion_tokens; Ollama reads max_tokens.
        self.limit_param = "max_tokens" if PROVIDER == "ollama" else "max_completion_tokens"
        self.unsupported: set[str] = set()

    def _create(self, messages, max_tokens, temperature, tools=None, choice=None):
        while True:
            request = {"model": MODEL, "messages": messages, self.limit_param: max_tokens}
            if temperature is not None and "temperature" not in self.unsupported:
                request["temperature"] = temperature
            if tools:
                request["tools"] = [{"type": "function", "function": {
                    "name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools]
            if choice:
                request["tool_choice"] = {"type": "function", "function": {"name": choice}}
            try:
                return self.client.chat.completions.create(**request)
            except self.openai.BadRequestError as error:
                if "temperature" not in str(error) or "temperature" not in request:
                    raise
                self.unsupported.add("temperature")

    def send(self, prompt, system, max_tokens, temperature, tool):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        response = self._create(messages, max_tokens, temperature, [tool] if tool else None, tool and tool["name"])
        choice = response.choices[0]
        text = choice.message.content or ""
        tool_input = None
        if tool:
            for call in choice.message.tool_calls or []:
                if call.function.name == tool["name"]:
                    tool_input = _json_object(call.function.arguments)
                    break
            if tool_input is None:
                tool_input = _json_object(text)
        usage = response.usage
        return Reply(text, tool_input, usage.prompt_tokens if usage else 0,
                     usage.completion_tokens if usage else 0, choice.finish_reason)

    def chat(self, system, messages, tools, max_tokens, temperature):
        native = [{"role": "system", "content": system}] if system else []
        for message in messages:
            native.extend(_to_openai(message))
        response = self._create(native, max_tokens, temperature, tools)
        choice = response.choices[0]
        content = [{"text": choice.message.content}] if choice.message.content else []
        for call in choice.message.tool_calls or []:
            content.append({"toolUse": {"toolUseId": call.id or f"call_{uuid.uuid4().hex[:12]}",
                                        "name": call.function.name,
                                        "input": _json_object(call.function.arguments) or {}}})
        stop = "tool_use" if any("toolUse" in b for b in content) else \
            self.STOP_REASONS.get(choice.finish_reason, choice.finish_reason)
        usage = response.usage
        return Turn(content, stop, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0)


def _to_openai(message: dict) -> list[dict]:
    """One common-format message as OpenAI chat messages: tool results become
    "tool" messages, which must come straight after the assistant's calls."""
    texts = [b["text"] for b in message["content"] if b.get("text")]
    if message["role"] == "assistant":
        calls = [{"id": b["toolUse"]["toolUseId"], "type": "function", "function": {
                  "name": b["toolUse"]["name"], "arguments": json.dumps(b["toolUse"]["input"], ensure_ascii=False)}}
                 for b in message["content"] if "toolUse" in b]
        return [{"role": "assistant", "content": "\n".join(texts) or None, **({"tool_calls": calls} if calls else {})}]
    out = [{"role": "tool", "tool_call_id": b["toolResult"]["toolUseId"],
            "content": ("ERROR: " if b["toolResult"].get("status") == "error" else "") + _result_text(b["toolResult"])}
           for b in message["content"] if "toolResult" in b]
    if texts:
        out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _json_object(raw: str) -> dict | None:
    """The first JSON object in raw (tolerating a code fence around it)."""
    if not raw:
        return None
    start = raw.find("{")
    while start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[start:])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        start = raw.find("{", start + 1)
    return None

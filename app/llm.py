"""One interface over the chat-model providers: Amazon Bedrock, Anthropic,
OpenAI and Ollama (through its OpenAI-compatible endpoint).

Chosen by environment:
  LLM_PROVIDER   bedrock (default) | anthropic | openai | ollama | stub (load
                 tests only: canned happy-path answers, llm_stub.py)
  LLM_MODEL      the model id; Bedrock falls back to BEDROCK_MODEL, Anthropic
                 to claude-haiku-4-5. OpenAI and Ollama have no default.
  BEDROCK_REGION Bedrock only
  ANTHROPIC_API_KEY / OPENAI_API_KEY   read by the provider SDKs
  OLLAMA_BASE_URL  default http://localhost:11434/v1
  OLLAMA_THINK     reasoning effort sent to Ollama (default none: thinking
                 models such as qwen3 otherwise spend a classifier's whole
                 token budget thinking and never answer); low|medium|high.
                 OLLAMA_THINK_FOR_<JOB> sets it for one job (guardrails,
                 evaluators, rewriting): qwen3 8B judged answers far better
                 thinking (evaluator_probe)

  LLM_FALLBACK   provider:model,… tried in order when a call fails for a
                 reason that isn't the request's (timeouts, rate limits,
                 server errors, unreachable), e.g. anthropic:claude-haiku-4-5
  LLM_FOR_GUARDRAILS / LLM_FOR_EVALUATORS / LLM_FOR_REWRITING / LLM_FOR_SETUP
                 provider:model for those jobs (a cheap, fast model suits the
                 guardrails and rewriting; setup is the knowledge system's
                 research, classification and page labels); the default
                 model otherwise
A provider that fails BREAK_AFTER times in a row is skipped for COOL_DOWN
seconds, going straight to the fallbacks (R16).

An admin may choose another model in the app (model_choice.py: a frontier
model on their own API key); choose() switches every call in this process to
it, per-job settings included, and choose(None) goes back to .env.
Three calls: generate() for free text, call_tool() for structured output
through one tool the model must call (a Pydantic schema as its input), and
chat() for one turn of a tool-using conversation (the local agent runtime)."""

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from typing import NamedTuple

log = logging.getLogger(__name__)

PROVIDERS = ("bedrock", "anthropic", "openai", "ollama", "stub")   # stub: load tests only (llm_stub.py, R61)
DEFAULT_MODELS = {
    "bedrock": os.environ.get("BEDROCK_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "anthropic": "claude-haiku-4-5",
    "stub": "stub",
}
LABELS = {"bedrock": "Bedrock", "anthropic": "Anthropic", "openai": "OpenAI", "ollama": "Ollama",
          "stub": "Load-test stub"}

PROVIDER = os.environ.get("LLM_PROVIDER", "bedrock").strip().lower() or "bedrock"
MODEL = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODELS.get(PROVIDER, "")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-2")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "").strip().lower() or "none"


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


def provider_error(provider: str, model: str) -> str | None:
    """Why a provider and model can't run here, or None."""
    if provider not in PROVIDERS:
        return f"{provider!r} is not one of {', '.join(PROVIDERS)}"
    if not (model or DEFAULT_MODELS.get(provider)):
        return f"no model given for {provider}"
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "set ANTHROPIC_API_KEY for anthropic"
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return "set OPENAI_API_KEY for openai"
    return None


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


def parse_target(text: str) -> tuple[str, str] | None:
    """'anthropic:claude-haiku-4-5' → ("anthropic", "claude-haiku-4-5"); a bare provider gets its default."""
    text = (text or "").strip()
    if not text:
        return None
    provider, _, model = text.partition(":")
    provider = provider.strip().lower()
    return provider, model.strip() or DEFAULT_MODELS.get(provider, "")


def fallbacks() -> list[tuple[str, str]]:
    return [t for t in (parse_target(x) for x in os.environ.get("LLM_FALLBACK", "").split(",")) if t]


# Which model a call uses: using() for a block (an agent's turn), for_task()
# for a job with its own setting, else the default.
_target: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("llm_target", default=None)


@contextlib.contextmanager
def using(provider: str | None, model: str | None = None):
    """Calls inside the block use this provider and model (None: unchanged)."""
    if not provider:
        yield
        return
    token = _target.set((provider, model or DEFAULT_MODELS.get(provider, "")))
    try:
        yield
    finally:
        _target.reset(token)


_job: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_job", default=None)


@contextlib.contextmanager
def for_task(task: str):
    """The model a job (guardrails, evaluators, rewriting) is set to use: LLM_FOR_<TASK>
    (not while a model chosen in the app runs everything)."""
    target = None if _chosen else parse_target(os.environ.get(f"LLM_FOR_{task.upper()}", ""))
    token = _job.set(task)
    try:
        with using(*target) if target else contextlib.nullcontext():
            yield
    finally:
        _job.reset(token)


def ollama_think() -> str:
    """The reasoning effort for this call on Ollama: the job's own, else OLLAMA_THINK."""
    job = _job.get()
    return (os.environ.get(f"OLLAMA_THINK_FOR_{job.upper()}", "") if job else "").strip().lower() or OLLAMA_THINK


def task(name: str):
    """Decorator: the function's model calls use LLM_FOR_<NAME> when it is set."""
    def wrap(function):
        import functools

        @functools.wraps(function)
        def inner(*args, **kwargs):
            with for_task(name):
                return function(*args, **kwargs)
        return inner
    return wrap


def current() -> tuple[str, str]:
    return _target.get() or (PROVIDER, MODEL)


KEY_VARIABLES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_ENV = {"provider": PROVIDER, "model": MODEL, **{v: os.environ.get(v) for v in KEY_VARIABLES.values()}}
_chosen: tuple[str, str] | None = None


def choose(provider: str | None, model: str = "", key: str = "") -> None:
    """Run every call on this provider, model and API key; None: back to .env's settings."""
    global PROVIDER, MODEL, _chosen
    with _lock:
        for variable in KEY_VARIABLES.values():          # .env's keys, unless one is chosen below
            if _ENV[variable] is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = _ENV[variable]
        if provider:
            if provider in KEY_VARIABLES and key:
                os.environ[KEY_VARIABLES[provider]] = key
            PROVIDER, MODEL = provider, model or DEFAULT_MODELS.get(provider, "")
            _chosen = (PROVIDER, MODEL)
        else:
            PROVIDER, MODEL, _chosen = _ENV["provider"], _ENV["model"], None
        _instances.clear()                               # clients are made again with the key now set
        _failures.clear()


def configured() -> tuple[str, str]:
    """The provider and model .env names (what runs when none is chosen in the app)."""
    return _ENV["provider"], _ENV["model"]


def chosen() -> tuple[str, str] | None:
    """The provider and model chosen in the app, or None (.env's)."""
    return _chosen


def catalogue() -> dict:
    """What the Models card shows: the default model, the fallbacks, each job's
    model, each provider's state, and list prices (model_prices.json)."""
    from pathlib import Path
    prices = json.loads((Path(__file__).with_name("model_prices.json")).read_text())
    rows, seen = [], set()

    def add(provider, model, use):
        key = (provider, model)
        if key in seen:
            next(r for r in rows if (r["provider"], r["model"]) == key)["uses"].append(use)
            return
        seen.add(key)
        rows.append({"provider": provider, "model": model, "uses": [use], "error": provider_error(provider, model),
                     "breaker": breaker(provider), **(prices["models"].get(model) or {})})
    add(PROVIDER, MODEL, "default")
    for target in fallbacks():
        add(*target, "fallback")
    for job in ("guardrails", "evaluators", "rewriting", "setup"):
        target = None if _chosen else parse_target(os.environ.get(f"LLM_FOR_{job.upper()}", ""))
        if target:
            add(*target, job)
    return {"models": rows, "price_note": prices["note"],
            "providers": [{"provider": p, "label": LABELS[p], "error": provider_error(p, DEFAULT_MODELS.get(p) or "x"),
                           "breaker": breaker(p)} for p in PROVIDERS if p != "stub" or PROVIDER == "stub"]}


# ---------- the provider breaker ----------

BREAK_AFTER = 3
COOL_DOWN = 60.0
_failures: dict[str, list] = {}
_breaker_lock = threading.Lock()


def breaker(provider: str) -> dict | None:
    with _breaker_lock:
        count, until = _failures.get(provider, (0, 0.0))
    left = until - time.monotonic()
    return {"failures": count, "seconds_left": round(left)} if left > 0 else None


def _note(provider: str, ok: bool) -> None:
    with _breaker_lock:
        if ok:
            _failures.pop(provider, None)
            return
        count = _failures.get(provider, [0, 0.0])[0] + 1
        _failures[provider] = [count, time.monotonic() + COOL_DOWN if count >= BREAK_AFTER else 0.0]


TRANSIENT_CODES = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
                   "InternalServerException", "ModelTimeoutException", "ServiceQuotaExceededException"}


def transient(error: Exception) -> bool:
    """Whether a failure is the provider's (worth trying another), not the request's."""
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and (status in (408, 429) or status >= 500):
        return True
    code = (getattr(error, "response", None) or {}).get("Error", {}).get("Code") \
        if isinstance(getattr(error, "response", None), dict) else None
    if code in TRANSIENT_CODES:
        return True
    name = type(error).__name__
    return any(word in name for word in ("Timeout", "Connection", "RateLimit", "InternalServer", "Unavailable",
                                         "Throttl", "Overloaded", "EndpointConnection"))


def _route(method: str, *args):
    """Call method on the chosen model; on a transient failure, the fallbacks in order."""
    first = current()
    chain = [first] + [t for t in fallbacks() if t != first]
    last_error = None
    for provider, model in chain:
        if breaker(provider) and (provider, model) != chain[-1]:
            continue                           # skipped while it cools down (the last one is always tried)
        if provider_error(provider, model):
            continue
        try:
            result = getattr(_backend(provider, model), method)(*args)
        except Exception as error:
            if not transient(error):
                raise
            _note(provider, False)
            log.warning("%s %s failed (%s: %s); trying the next model", provider, model, type(error).__name__, error)
            last_error = error
            continue
        _note(provider, True)
        return result
    raise last_error or RuntimeError("no model could take the call")


def label() -> str:
    return f"{MODEL} · {LABELS.get(PROVIDER, PROVIDER)}"


def generate(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             temperature: float | None = None) -> Reply:
    return _route("send", prompt, system, max_tokens, temperature, None)


def call_tool(prompt: str, *, system: str, name: str, description: str, schema: dict,
              max_tokens: int = 1024, temperature: float | None = 0) -> Reply:
    """reply.tool_input is the tool's arguments as a dict; NoToolCall if the
    model did not call it."""
    reply = _route("send", prompt, system, max_tokens, temperature,
                   {"name": name, "description": description, "schema": schema})
    if not isinstance(reply.tool_input, dict):
        provider, model = current()
        raise NoToolCall(f"{provider} model {model} did not call {name} (stop reason: {reply.stop_reason})")
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
    return _route("chat", system, messages, tools, max_tokens, temperature)


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
_instances: dict[tuple[str, str], object] = {}


def _backend(provider: str | None = None, model: str | None = None):
    provider, model = (provider, model) if provider else (PROVIDER, MODEL)
    with _lock:
        if (provider, model) not in _instances:
            error = config_error() if (provider, model) == (PROVIDER, MODEL) else provider_error(provider, model)
            if error:
                raise RuntimeError(f"LLM is not configured: {error}")
            if provider == "stub":
                from llm_stub import Stub as backend
            else:
                backend = {"bedrock": _Bedrock, "anthropic": _Anthropic, "openai": _OpenAI, "ollama": _OpenAI}[provider]
            _instances[(provider, model)] = backend(provider, model)
        return _instances[(provider, model)]


class _Model:
    """A backend's model: the one it was made for, else the configured one (read at call time)."""
    _model = ""

    @property
    def model(self) -> str:
        return self._model or MODEL


class _Bedrock(_Model):
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider, self._model = "bedrock", model or ""
        import boto3
        self.client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def _request(self, system, messages, max_tokens, temperature, tools, choice=None) -> dict:
        inference = {"maxTokens": max_tokens}
        if temperature is not None:
            inference["temperature"] = temperature
        request = {"modelId": self.model, "messages": messages, "inferenceConfig": inference}
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


class _Anthropic(_Model):
    """Newer Claude models reject a forced tool_choice and sampling
    parameters with a 400; each is dropped for that model after its first
    rejection, a forced call falling back to auto plus an instruction.
    SDK 1.x has no temperature argument: older models still honour it, so
    it goes in extra_body. Current models think by default, and thinking
    counts against max_tokens: a 300-token classifier call could end before
    its answer, so limits have a floor (only tokens used are billed)."""

    MIN_TOKENS = 4096

    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider, self._model = "anthropic", model or ""
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
            request = {"model": self.model, "max_tokens": max(max_tokens, self.MIN_TOKENS),
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
            request = {"model": self.model, "max_tokens": max(max_tokens, self.MIN_TOKENS), "messages": native,
                       **self._sampling(temperature)}
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


class _OpenAI(_Model):
    """OpenAI, or any OpenAI-compatible server (Ollama). Local models do not
    always honour a forced tool call (Hermes 3 8B answered a two-field schema
    with {"animal": "dog"}), so on Ollama structured output is decoded
    against the schema itself (response_format), and a JSON object in the
    text counts."""

    STOP_REASONS = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}

    def __init__(self, provider: str | None = None, model: str | None = None):
        provider = provider or PROVIDER
        self.provider, self._model = provider, model or ""
        import openai
        self.openai = openai
        if provider == "ollama":
            self.client = openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
        else:
            self.client = openai.OpenAI()
        # Current OpenAI models want max_completion_tokens; Ollama reads max_tokens.
        self.limit_param = "max_tokens" if provider == "ollama" else "max_completion_tokens"
        self.local = provider == "ollama"
        self.unsupported: set[str] = set()

    def _create(self, messages, max_tokens, temperature, tools=None, choice=None, schema=None):
        while True:
            request = {"model": self.model, "messages": messages, self.limit_param: max_tokens}
            if temperature is not None and "temperature" not in self.unsupported:
                request["temperature"] = temperature
            if self.local and ollama_think() and "reasoning_effort" not in self.unsupported:
                request["reasoning_effort"] = ollama_think()
            if schema:
                request["response_format"] = {"type": "json_schema",
                                              "json_schema": {"name": schema["name"], "schema": schema["schema"]}}
            if tools:
                request["tools"] = [{"type": "function", "function": {
                    "name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools]
            if choice:
                request["tool_choice"] = {"type": "function", "function": {"name": choice}}
            try:
                return self.client.chat.completions.create(**request)
            except self.openai.BadRequestError as error:
                rejected = next((p for p in ("temperature", "reasoning_effort") if p in str(error) and p in request), None)
                if not rejected:
                    raise
                self.unsupported.add(rejected)

    def send(self, prompt, system, max_tokens, temperature, tool):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        if tool and self.local:
            # Decoding is held to the schema, so the reply ends with the
            # object; the headroom is for thinking (asked for, or gpt-oss's own).
            messages[0 if system else -1]["content"] += f"\n\nReply with the arguments of {tool['name']} " \
                f"({tool['description']}) as one JSON object."
            floor = 1024 if ollama_think() == "none" else 8192
            response = self._create(messages, max(max_tokens, floor), temperature, schema=tool)
        else:
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

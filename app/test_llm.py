"""The provider adapters against fake SDK clients: no network, no keys.
Run: PYTHONPATH=app:dags python -m unittest app/test_llm.py"""

import os
import sys
import threading
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import llm

TOOL = {"name": "record", "description": "Record it.", "schema": {"type": "object"}}


class FakeBadRequest(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def fake_sdk(name: str, client) -> types.ModuleType:
    module = types.ModuleType(name)
    module.BadRequestError = FakeBadRequest
    setattr(module, "Anthropic" if name == "anthropic" else "OpenAI", lambda **kwargs: client)
    return module


class BedrockTest(unittest.TestCase):
    def test_forced_tool_call_and_usage(self):
        calls = []

        class Client:
            def converse(self, **request):
                calls.append(request)
                return {"output": {"message": {"content": [{"toolUse": {"name": "record", "input": {"a": 1}}}]}},
                        "usage": {"inputTokens": 7, "outputTokens": 3}, "stopReason": "tool_use"}

        backend = llm._Bedrock.__new__(llm._Bedrock)
        backend.client = Client()
        with patch.object(llm, "MODEL", "m"):
            reply = backend.send("p", "sys", 100, 0, TOOL)
        self.assertEqual(reply.tool_input, {"a": 1})
        self.assertEqual(reply.usage, {"input_tokens": 7, "output_tokens": 3})
        self.assertEqual(calls[0]["toolConfig"]["toolChoice"], {"tool": {"name": "record"}})
        self.assertEqual(calls[0]["system"], [{"text": "sys"}])


class AnthropicTest(unittest.TestCase):
    def make(self, responses):
        requests = []

        class Messages:
            def create(self, **request):
                requests.append(request)
                result = responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        client = NS(messages=Messages())
        with patch.dict(sys.modules, {"anthropic": fake_sdk("anthropic", client)}):
            backend = llm._Anthropic()
        return backend, requests

    def test_rejected_forced_tool_choice_falls_back_to_auto_once(self):
        ok = NS(content=[NS(type="tool_use", name="record", input={"a": 1})],
                usage=NS(input_tokens=5, output_tokens=2), stop_reason="tool_use")
        backend, requests = self.make([FakeBadRequest("tool_choice: type tool is not supported"), ok, ok])
        with patch.object(llm, "MODEL", "claude-x"):
            self.assertEqual(backend.send("p", "sys", 100, None, TOOL).tool_input, {"a": 1})
            backend.send("p", "sys", 100, None, TOOL)
        self.assertEqual(requests[0]["tool_choice"], {"type": "tool", "name": "record"})
        self.assertNotIn("tool_choice", requests[1])
        self.assertIn("calling the record tool", requests[1]["system"])
        self.assertNotIn("tool_choice", requests[2])   # remembered: no second rejection
        self.assertEqual(len(requests), 3)

    def test_rejected_temperature_is_dropped(self):
        ok = NS(content=[NS(type="text", text="hi")], usage=NS(input_tokens=1, output_tokens=1), stop_reason="end_turn")
        backend, requests = self.make([FakeBadRequest("temperature is not supported for this model"), ok])
        with patch.object(llm, "MODEL", "claude-x"):
            self.assertEqual(backend.send("p", None, 100, 0, None).text, "hi")
        self.assertEqual(requests[0]["extra_body"], {"temperature": 0})
        self.assertNotIn("extra_body", requests[1])

    def test_other_bad_requests_are_raised(self):
        backend, _ = self.make([FakeBadRequest("prompt is too long")])
        with patch.object(llm, "MODEL", "claude-x"), self.assertRaises(FakeBadRequest):
            backend.send("p", None, 100, 0, TOOL)


class OpenAITest(unittest.TestCase):
    def make(self, message, provider="openai"):
        requests = []

        class Completions:
            def create(self, **request):
                requests.append(request)
                return NS(choices=[NS(message=message, finish_reason="stop")],
                          usage=NS(prompt_tokens=4, completion_tokens=6))

        client = NS(chat=NS(completions=Completions()))
        with patch.dict(sys.modules, {"openai": fake_sdk("openai", client)}), patch.object(llm, "PROVIDER", provider):
            backend = llm._OpenAI()
        return backend, requests

    def test_tool_call_arguments_are_parsed(self):
        message = NS(content=None, tool_calls=[NS(function=NS(name="record", arguments='{"a": 1}'))])
        backend, requests = self.make(message)
        with patch.object(llm, "MODEL", "gpt-x"):
            reply = backend.send("p", "sys", 100, 0, TOOL)
        self.assertEqual(reply.tool_input, {"a": 1})
        self.assertEqual(reply.usage, {"input_tokens": 4, "output_tokens": 6})
        self.assertEqual(requests[0]["messages"][0], {"role": "system", "content": "sys"})
        self.assertIn("max_completion_tokens", requests[0])

    def test_ollama_json_in_text_counts_as_the_tool_call(self):
        message = NS(content='Here you go:\n```json\n{"a": 2}\n```', tool_calls=None)
        backend, requests = self.make(message, provider="ollama")
        with patch.object(llm, "MODEL", "qwen"):
            self.assertEqual(backend.send("p", None, 100, 0, TOOL).tool_input, {"a": 2})
        self.assertIn("max_tokens", requests[0])


CONVERSATION = [
    {"role": "user", "content": [{"text": "Find X"}]},
    {"role": "assistant", "content": [{"providerBlock": {"provider": "anthropic", "block": {"type": "thinking"}}},
                                      {"text": "Looking."},
                                      {"toolUse": {"toolUseId": "u1", "name": "search", "input": {"q": "x"}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "u1", "status": "error", "content": [{"text": "none"}]}}]},
]


class ChatTest(unittest.TestCase):
    def test_anthropic_conversation_round_trip(self):
        requests = []

        class Messages:
            def create(self, **request):
                requests.append(request)
                return NS(content=[NS(type="thinking", model_dump=lambda mode: {"type": "thinking", "thinking": ""}),
                                   NS(type="tool_use", id="u2", name="search", input={"q": "y"})],
                          usage=NS(input_tokens=3, output_tokens=4), stop_reason="tool_use")

        with patch.dict(sys.modules, {"anthropic": fake_sdk("anthropic", NS(messages=Messages()))}):
            backend = llm._Anthropic()
        with patch.object(llm, "MODEL", "claude-x"):
            turn = backend.chat("sys", CONVERSATION, [{"name": "search", "description": "d", "schema": {}}], 100, 0.2)
        sent = requests[0]["messages"]
        self.assertEqual(sent[1]["content"][0], {"type": "thinking"})   # replayed unchanged
        self.assertEqual(sent[1]["content"][2], {"type": "tool_use", "id": "u1", "name": "search", "input": {"q": "x"}})
        self.assertEqual(sent[2]["content"][0], {"type": "tool_result", "tool_use_id": "u1", "content": "none",
                                                 "is_error": True})
        self.assertEqual(requests[0]["tools"][0]["input_schema"], {})
        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(turn.content[1], {"toolUse": {"toolUseId": "u2", "name": "search", "input": {"q": "y"}}})
        self.assertEqual(turn.content[0]["providerBlock"]["provider"], "anthropic")

    def test_openai_conversation_round_trip(self):
        requests = []

        class Completions:
            def create(self, **request):
                requests.append(request)
                call = NS(id="c2", function=NS(name="search", arguments='{"q": "y"}'))
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]), finish_reason="tool_calls")],
                          usage=NS(prompt_tokens=3, completion_tokens=4))

        with patch.dict(sys.modules, {"openai": fake_sdk("openai", NS(chat=NS(completions=Completions())))}), \
                patch.object(llm, "PROVIDER", "openai"):
            backend = llm._OpenAI()
        with patch.object(llm, "MODEL", "gpt-x"):
            turn = backend.chat("sys", CONVERSATION, [{"name": "search", "description": "d", "schema": {}}], 100, None)
        sent = requests[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
        self.assertEqual(sent[2]["content"], "Looking.")   # the thinking block is Anthropic's only
        self.assertEqual(sent[2]["tool_calls"][0]["function"], {"name": "search", "arguments": '{"q": "x"}'})
        self.assertEqual(sent[3], {"role": "tool", "tool_call_id": "u1", "content": "ERROR: none"})
        self.assertNotIn("tool_choice", requests[0])
        self.assertEqual(turn.content, [{"toolUse": {"toolUseId": "c2", "name": "search", "input": {"q": "y"}}}])
        self.assertEqual(turn.stop_reason, "tool_use")


class CallToolTest(unittest.TestCase):
    def test_no_tool_call_raises(self):
        backend = NS(send=lambda *args, **kwargs: llm.Reply("just text", None, 1, 1, "end_turn"))
        with patch.object(llm, "_backend", return_value=backend), self.assertRaises(llm.NoToolCall):
            llm.call_tool("p", system="s", name="record", description="d", schema={})

    def test_arguments_nested_one_level_down_are_unwrapped(self):
        schema = {"required": ["decision", "reason"]}
        nested = {"object": {"decision": "ALLOW", "reason": "r"}, "tool": "record"}
        backend = NS(send=lambda *args, **kwargs: llm.Reply("", nested, 1, 1, "stop"))
        with patch.object(llm, "_backend", return_value=backend):
            reply = llm.call_tool("p", system="s", name="record", description="d", schema=schema)
        self.assertEqual(reply.tool_input, {"decision": "ALLOW", "reason": "r"})
        flat = {"decision": "BLOCK", "reason": "r", "extra": {"decision": "x", "reason": "y"}}
        self.assertEqual(llm._unwrap(flat, schema), flat)

    def test_guardrails_name_a_missing_key_instead_of_a_vague_failure(self):
        import guardrails
        with patch.object(llm, "config_error", return_value="set ANTHROPIC_API_KEY for LLM_PROVIDER=anthropic"):
            verdict = guardrails.check("input", "Do I need a visa for Japan?")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "llm_not_configured")
        self.assertIn("ANTHROPIC_API_KEY", verdict.message)

    def test_config_errors(self):
        with patch.object(llm, "PROVIDER", "openai"), patch.object(llm, "MODEL", ""):
            self.assertIn("LLM_MODEL", llm.config_error())
        with patch.object(llm, "PROVIDER", "anthropic"), patch.object(llm, "MODEL", "m"), \
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertIn("ANTHROPIC_API_KEY", llm.config_error())
        with patch.object(llm, "PROVIDER", "nope"):
            self.assertIn("not one of", llm.config_error())
        with patch.object(llm, "PROVIDER", "bedrock"), patch.object(llm, "MODEL", "m"):
            self.assertIsNone(llm.config_error())


if __name__ == "__main__":
    unittest.main()


class Routing(unittest.TestCase):
    """R16: per-call model choice, fallback on provider failures, a breaker per provider."""

    class Down(Exception):
        status_code = 529   # overloaded

    class BadRequest(Exception):
        status_code = 400

    def backends(self, behaviour):
        made = {}

        class Backend:
            def __init__(self, provider, model):
                self.provider, self.model = provider, model

            def send(self, *args):
                behaviour[self.provider]()
                return llm.Reply(f"from {self.provider}:{self.model}", None, 1, 1, "end_turn")
        return lambda provider=None, model=None: made.setdefault((provider, model), Backend(provider, model))

    def setUp(self):
        llm._failures.clear()
        self.addCleanup(llm._failures.clear)

    def env(self, **values):
        return patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", **values})

    def test_a_failing_provider_falls_back_in_order(self):
        def down():
            raise self.Down("overloaded")
        behaviour = {"bedrock": down, "anthropic": down, "openai": lambda: None}
        with self.env(LLM_FALLBACK="anthropic:claude-haiku-4-5,openai:gpt-4.1-mini"), \
                patch.object(llm, "PROVIDER", "bedrock"), patch.object(llm, "MODEL", "b"), \
                patch.object(llm, "_backend", side_effect=self.backends(behaviour)):
            self.assertEqual(llm.generate("hi").text, "from openai:gpt-4.1-mini")
        self.assertEqual(llm._failures["bedrock"][0], 1)

    def test_a_request_error_is_not_retried_elsewhere(self):
        def bad():
            raise self.BadRequest("bad")
        with self.env(LLM_FALLBACK="openai:gpt-4.1-mini"), patch.object(llm, "PROVIDER", "anthropic"), \
                patch.object(llm, "MODEL", "a"), \
                patch.object(llm, "_backend", side_effect=self.backends({"anthropic": bad, "openai": lambda: None})), \
                self.assertRaises(self.BadRequest):
            llm.generate("hi")

    def test_a_provider_that_keeps_failing_is_skipped_for_a_while(self):
        calls = []

        def down():
            calls.append(1)
            raise self.Down("overloaded")
        with self.env(LLM_FALLBACK="openai:gpt-4.1-mini"), patch.object(llm, "PROVIDER", "anthropic"), \
                patch.object(llm, "MODEL", "a"), \
                patch.object(llm, "_backend", side_effect=self.backends({"anthropic": down, "openai": lambda: None})):
            for _ in range(5):
                self.assertIn("openai", llm.generate("hi").text)
        self.assertEqual(len(calls), llm.BREAK_AFTER)   # then skipped while it cools down
        self.assertIsNotNone(llm.breaker("anthropic"))

    def test_a_block_or_a_job_can_choose_its_model(self):
        with self.env(LLM_FOR_GUARDRAILS="openai:gpt-4.1-mini"), patch.object(llm, "PROVIDER", "anthropic"), \
                patch.object(llm, "MODEL", "a"), \
                patch.object(llm, "_backend", side_effect=self.backends({p: (lambda: None) for p in llm.PROVIDERS})):
            self.assertEqual(llm.generate("x").text, "from anthropic:a")
            with llm.using("openai", "gpt-4.1"):
                self.assertEqual(llm.generate("x").text, "from openai:gpt-4.1")
            self.assertEqual(llm.task("guardrails")(lambda: llm.generate("x").text)(), "from openai:gpt-4.1-mini")

    def test_the_catalogue_lists_roles_states_and_prices(self):
        with self.env(LLM_FALLBACK="openai:gpt-4.1-mini"), patch.object(llm, "PROVIDER", "anthropic"), \
                patch.object(llm, "MODEL", "claude-haiku-4-5"):
            c = llm.catalogue()
        rows = {(m["provider"], m["model"]): m for m in c["models"]}
        self.assertEqual(rows[("anthropic", "claude-haiku-4-5")]["uses"], ["default"])
        self.assertEqual(rows[("anthropic", "claude-haiku-4-5")]["input"], 1.0)
        self.assertEqual(rows[("openai", "gpt-4.1-mini")]["uses"], ["fallback"])
        self.assertTrue(c["price_note"])

    def test_the_local_runtime_uses_an_agents_chosen_model(self):
        import agent_runtime
        seen = []
        runtime = agent_runtime.LocalHarness.__new__(agent_runtime.LocalHarness)
        runtime.locks, runtime.locks_lock = {}, threading.Lock()
        runtime.chat = lambda *a, **k: seen.append(llm.current()) or llm.Turn([{"text": "ok"}], "end_turn", 1, 1)
        with patch.object(agent_runtime, "load_history", return_value=[]), patch.object(agent_runtime, "save_history"):
            stream = runtime.invoke_harness(runtimeSessionId="s", messages=[{"role": "user", "content": [{"text": "q"}]}],
                                            systemPrompt=[{"text": "sys"}], tools=[],
                                            chatModel={"provider": "openai", "model": "gpt-4.1"})["stream"]
            list(stream)
        self.assertEqual(seen, [("openai", "gpt-4.1")])


class StubTest(unittest.TestCase):
    """The load-test stand-in (llm_stub.py, R61): valid happy-path output, no network, its own latency."""

    def setUp(self):
        import llm_stub
        self.stub = llm_stub
        patcher = patch.object(llm_stub, "SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tool_arguments_follow_the_schema_on_the_happy_path(self):
        from pydantic import BaseModel
        from typing import Literal

        class Check(BaseModel):
            decision: Literal["BLOCK", "ALLOW"]
            evidence: Literal["RETRIEVAL_FAILURE", "GOOD_EVIDENCE"]
            injection: bool
            passed: bool
            score: float
            missing_information: list[str]
            reason: str

        reply = self.stub.Stub().send("p", "s", 100, 0, {"name": "check", "description": "", "schema": Check.model_json_schema()})
        checked = Check.model_validate(reply.tool_input)          # valid against its own schema
        self.assertEqual((checked.decision, checked.evidence, checked.injection, checked.passed),
                         ("ALLOW", "GOOD_EVIDENCE", False, True))
        self.assertEqual(checked.missing_information, [])
        self.assertGreater(checked.score, 0.8)

    def test_a_conversation_searches_once_then_answers(self):
        tools = [{"name": "finish", "description": "", "schema": {"type": "object"}},
                 {"name": "search_knowledge_base", "description": "",
                  "schema": {"type": "object", "properties": {"query": {"type": "string"}}}}]
        asked = [{"role": "user", "content": [{"text": "Bikes on trains?"}]}]
        first = self.stub.Stub().chat("s", asked, tools, 100, 0)
        use = first.content[0]["toolUse"]
        self.assertEqual((first.stop_reason, use["name"], use["input"]), ("tool_use", "search_knowledge_base",
                                                                          {"query": "Bikes on trains?"}))
        result = asked + [{"role": "assistant", "content": first.content},
                          {"role": "user", "content": [{"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": "x"}]}}]}]
        second = self.stub.Stub().chat("s", result, tools, 100, 0)
        self.assertEqual(second.stop_reason, "end_turn")
        self.assertIn("[1]", second.content[0]["text"])

    def test_it_is_a_provider(self):
        with patch.object(llm, "PROVIDER", "stub"), patch.object(llm, "MODEL", "stub"):
            self.assertIsNone(llm.config_error())
            self.assertEqual(llm.generate("hi").text, self.stub.ANSWER)

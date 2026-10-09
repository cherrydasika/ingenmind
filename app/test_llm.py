"""The provider adapters against fake SDK clients: no network, no keys.
Run: PYTHONPATH=app:dags python -m unittest app/test_llm.py"""

import sys
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

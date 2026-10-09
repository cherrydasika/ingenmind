"""The local agent runtime: the AgentCore harness API over llm.chat(), with
each session's conversation in PostgreSQL (like test_pg_store, it needs one).
Run: PYTHONPATH=app:dags python -m unittest app/test_agent_runtime.py"""

import threading
import time
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql

import agent
import agent_runtime
import llm
from common import config


def tool(name):
    return agent._tool(name, f"{name} tool", {"query": {"type": "string"}}, ["query"])


class ScriptedChat:
    """llm.chat() stand-in: returns the next scripted turn, recording what it was sent."""

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def __call__(self, system, messages, tools, max_tokens, temperature):
        time.sleep(0.06)   # a model call takes time; the turn's timing must include it
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools,
                           "max_tokens": max_tokens, "temperature": temperature})
        return self.turns.pop(0)


def uses_turn(*names):
    return llm.Turn([{"text": "Searching."}] + [{"toolUse": {"toolUseId": f"t{i}", "name": n, "input": {"query": "q"}}}
                                               for i, n in enumerate(names)], "tool_use", 10, 5)


def answer_turn(text):
    return llm.Turn([{"text": text}], "end_turn", 20, 8)


class LocalHarnessTest(unittest.TestCase):
    def setUp(self):
        self.original_database = config.PGDATABASE
        self.database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        config.PGDATABASE = self.database
        agent_runtime._schema_ready = False
        self.addCleanup(self.drop)

    def drop(self):
        config.PGDATABASE = self.original_database
        agent_runtime._schema_ready = False
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database)))

    def run_for(self, harness, role="knowledge_base"):
        run = agent._Run.__new__(agent._Run)
        run.client, run.session_id, run.user_id, run.limit = harness, "browser-1", None, 6
        run.settings, run.agents, run.lock = {}, set(agent.SPECIALISTS), threading.Lock()
        run.emit = lambda event: None
        return run, {"role": role, "delegation": 1, "turn_log": [], "texts": {}, "input_tokens": 0,
                     "output_tokens": 0, "model_seconds": 0.0, "turns": 0}

    def test_tool_loop_through_the_harness_api_keeps_the_conversation(self):
        chat = ScriptedChat(uses_turn("search_knowledge_base", "search_knowledge_base"), answer_turn("Done [1]."))
        run, record = self.run_for(agent_runtime.LocalHarness(chat))
        stop, uses = run.invoke(record, [{"role": "user", "content": [{"text": "Find X"}]}])
        self.assertEqual(stop, "tool_use")
        self.assertEqual([agent._input(u) for u in uses], [{"query": "q"}, {"query": "q"}])
        results = [agent._result(u, "found") for u in uses]
        stop, uses = run.invoke(record, [{"role": "user", "content": results}])
        self.assertEqual((stop, uses), ("end_turn", []))
        self.assertEqual(agent._Run.answer_of(record), "Done [1].")
        self.assertEqual((record["turns"], record["input_tokens"], record["output_tokens"]), (2, 30, 13))
        self.assertGreater(record["model_seconds"], 0.1)   # the call is timed between messageStart and messageStop
        second = chat.calls[1]
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        self.assertEqual(second["messages"][2]["content"][0]["toolResult"]["toolUseId"], "t0")
        self.assertEqual(second["tools"][0]["name"], "search_knowledge_base")
        self.assertIn("knowledge-base agent", second["system"])
        # A new harness (a restarted web app) continues the same conversation.
        later = ScriptedChat(answer_turn("Still here."))
        run, record = self.run_for(agent_runtime.LocalHarness(later))
        run.invoke(record, [{"role": "user", "content": [{"text": "And Y?"}]}])
        self.assertEqual(len(later.calls[0]["messages"]), 5)
        memory = agent._local_memory_view("browser-1")
        self.assertEqual(memory["agents"][0]["role"], "knowledge_base")
        self.assertEqual(len(memory["agents"][0]["events"]), 6)

    def test_flow_model_settings_reach_the_model(self):
        chat = ScriptedChat(answer_turn("ok"), answer_turn("ok"))
        run, record = self.run_for(agent_runtime.LocalHarness(chat), role="supervisor")
        run.settings = {"models": {"supervisor": {"modelId": "m", "maxTokens": 512, "temperature": 0.0}}}
        run.invoke(record, [{"role": "user", "content": [{"text": "hi"}]}])
        self.assertEqual((chat.calls[0]["max_tokens"], chat.calls[0]["temperature"]), (512, 0.0))
        run.settings = {}
        run.invoke(record, [{"role": "user", "content": [{"text": "hi again"}]}])
        self.assertEqual((chat.calls[1]["max_tokens"], chat.calls[1]["temperature"]),
                         (agent_runtime.DEFAULT_MAX_TOKENS, agent_runtime.DEFAULT_TEMPERATURE))


class HistoryTest(unittest.TestCase):
    def test_an_interrupted_tool_call_is_answered_before_the_next_question(self):
        history = [{"role": "user", "content": [{"text": "q1"}]},
                   {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "n", "input": {}}},
                                                     {"toolUse": {"toolUseId": "b", "name": "n", "input": {}}}]}]
        merged = agent_runtime._merge(history, [{"role": "user", "content": [
            {"toolResult": {"toolUseId": "b", "status": "success", "content": [{"text": "ok"}]}}, {"text": "q2"}]}])
        content = merged[-1]["content"]
        self.assertEqual([b["toolResult"]["toolUseId"] for b in content if "toolResult" in b], ["a", "b"])
        self.assertEqual(content[0]["toolResult"]["status"], "error")
        self.assertEqual(content[-1], {"text": "q2"})

    def test_trimming_starts_at_a_question_not_at_orphaned_tool_results(self):
        exchange = [{"role": "user", "content": [{"text": "q"}]},
                    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "n", "input": {}}}]},
                    {"role": "user", "content": [{"toolResult": {"toolUseId": "a", "status": "success", "content": []}}]},
                    {"role": "assistant", "content": [{"text": "answer"}]}]
        with patch.object(agent_runtime, "MAX_HISTORY_MESSAGES", 6):
            trimmed = agent_runtime._trim(exchange * 3)
        self.assertEqual(trimmed[0], {"role": "user", "content": [{"text": "q"}]})
        self.assertLessEqual(len(trimmed), 6)

    def test_runtime_choice_and_config_errors(self):
        with patch.object(agent_runtime, "RUNTIME", "agentcore"), patch.object(agent_runtime, "HARNESS_ARN", ""):
            self.assertIn("AGENT_HARNESS_ARN", agent_runtime.config_error())
        with patch.object(agent_runtime, "RUNTIME", "local"), patch.object(llm, "config_error", return_value="set X"):
            self.assertIn("set X", agent_runtime.config_error())
        with patch.object(agent_runtime, "RUNTIME", "local"), patch.object(llm, "config_error", return_value=None):
            self.assertIsNone(agent_runtime.config_error())
            agent.describe.clear()
            info = agent.describe()
            agent.describe.clear()
        self.assertTrue(info["configured"])
        self.assertEqual(info["harness"]["runtime"], "local")
        self.assertIn("supervisor", info["roles"])


if __name__ == "__main__":
    unittest.main()

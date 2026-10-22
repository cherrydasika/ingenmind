"""The local agent runtime: the AgentCore harness API over llm.chat(), with
each session's conversation in PostgreSQL (like test_pg_store, it needs one).
Run: PYTHONPATH=app:dags python -m unittest app/test_agent_runtime.py"""

import json
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

import agent
import agent_runtime
import answer_eval
import guardrails
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


class AgentComponentTest(LocalHarnessTest):
    """A flow of one agent (#62) answers end to end on the local runtime."""

    def starter(self, tools=None):
        spec = json.loads((Path(agent.flows.__file__).parent / "starter_flow.json").read_text())
        if tools is not None:
            spec["nodes"].append({"id": "api_tools", "type": "api_tools", "config": {"tools": tools}})
            spec["edges"].append({"source": "api_tools", "target": "agent", "kind": "resource"})
        return agent.flows.FlowSpec.model_validate(spec)

    def ask(self, spec, chat, evaluation_passes=True):
        found = {"rankings": {"fused": [{"source_url": "https://rail.example/access", "chunk_index": 0, "meta": {},
                                         "text": "Every station has step-free access to all platforms."}],
                              "dense": [], "sparse": []}}
        allow = guardrails.Verdict(decision="ALLOW", reason="in scope", stage="input")
        seen = {}
        def evaluate(question, draft, chunks, api_results, **kwargs):
            seen.update(chunks=chunks, api_results=api_results)
            checks = answer_eval.AnswerChecks(cited=[1], invalid_citations=[], evidence_chunks=len(chunks),
                                              api_results=len(api_results), kb_evidence_uncited=False)
            return answer_eval.AnswerEvaluation(passed=evaluation_passes, overall=0.9, scores={}, checks=checks,
                                                failed_on=[] if evaluation_passes else ["faithfulness"])
        with patch.object(agent, "_local_harness", agent_runtime.LocalHarness(chat)), \
                patch.object(guardrails, "check", return_value=allow), \
                patch.object(agent, "hybrid_search", return_value=found), \
                patch.object(agent.answer_eval, "evaluate", side_effect=evaluate), \
                patch.object(agent.labels, "vocabulary", return_value=None), \
                patch.object(agent, "_record_run"), patch.object(agent_runtime, "RUNTIME", "local"):
            result = agent.answer("Is there step-free access?", f"s-{uuid.uuid4()}", None, None, lambda e: None,
                                  lambda search: {"chunks": search["rankings"]["fused"], "sources": []},
                                  flow=(spec, 1))
        return result, seen

    def test_the_starter_flow_is_valid_and_runnable(self):
        spec = self.starter()
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        self.assertEqual(agent.flows.compiler.runnable_issues(spec, agent._runtime()), [])

    def test_one_agent_answers_with_citations_checked_against_its_evidence(self):
        chat = ScriptedChat(uses_turn("search_knowledge_base"), answer_turn("Yes, every station is step-free [1]."))
        result, seen = self.ask(self.starter(), chat)
        self.assertEqual(result["answer"], "Yes, every station is step-free [1].")
        self.assertEqual(result["sources"], [{"url": "https://rail.example/access", "cited": [1]}])
        self.assertEqual([c["source_url"] for c in seen["chunks"]], ["https://rail.example/access"])   # its evidence
        first = chat.calls[0]
        self.assertIn("you answer the user yourself", first["system"])
        self.assertIn(agent.flows.registry.GENERIC_AGENT_INSTRUCTIONS, first["system"])
        self.assertEqual([t["name"] for t in first["tools"]], ["search_knowledge_base"])   # no API tools plugged in

    def test_a_sub_agent_is_checked_retries_and_its_citations_reach_the_answer(self):
        spec = agent.flows.FlowSpec.model_validate({
            "id": "generic", "name": "Generic", "version": 1, "state": "main", "subflows": {},
            "nodes": [{"id": "start", "type": "start"}, {"id": "ig", "type": "input_guardrail"},
                      {"id": "assistant", "type": "agent", "label": "Assistant"},
                      {"id": "kb", "type": "agent", "label": "Rail facts", "config": {"instructions": "Finds rail facts."}},
                      {"id": "retriever", "type": "retriever"},
                      {"id": "strict", "type": "check", "label": "Strict",
                       "config": {"checks": "criteria", "criteria": "Says every station, not just one."}},
                      {"id": "final", "type": "check", "label": "Final"},
                      {"id": "og", "type": "output_guardrail"}, {"id": "end", "type": "end"}],
            "edges": [{"source": "start", "target": "ig"}, {"source": "ig", "target": "assistant", "outcome": "ALLOW"},
                      {"source": "ig", "target": "end", "outcome": "BLOCK"}, {"source": "assistant", "target": "og"},
                      {"source": "og", "target": "end", "outcome": "ALLOW"}, {"source": "og", "target": "end", "outcome": "BLOCK"},
                      {"source": "kb", "target": "assistant", "kind": "resource", "port": "agents"},
                      {"source": "retriever", "target": "kb", "kind": "resource"},
                      {"source": "strict", "target": "kb", "kind": "resource"},
                      {"source": "final", "target": "assistant", "kind": "resource"}]})
        ask = llm.Turn([{"toolUse": {"toolUseId": "a1", "name": "ask_kb", "input": {"task": "Step-free access?"}}}],
                       "tool_use", 10, 5)
        chat = ScriptedChat(ask, uses_turn("search_knowledge_base"), answer_turn("Kestrel Bay is step-free [1]."),
                            answer_turn("Every station is step-free [1]."), answer_turn("Yes: every station is step-free [1]."))
        verdicts = iter([(False, "It names one station only.", {}), (True, "", {})])
        with patch.object(agent.agent_tree, "check_criteria", side_effect=lambda *a: next(verdicts)):
            result, seen = self.ask(spec, chat)
        self.assertEqual(result["answer"], "Yes: every station is step-free [1].")
        self.assertEqual(result["sources"], [{"url": "https://rail.example/access", "cited": [1]}])
        self.assertEqual([t["name"] for t in chat.calls[0]["tools"]], ["ask_kb"])           # the top agent's sub-agent
        self.assertIn("Rail facts", chat.calls[0]["system"])
        self.assertEqual([t["name"] for t in chat.calls[1]["tools"]], ["search_knowledge_base"])
        self.assertIn("a sub-agent", chat.calls[1]["system"])
        retry = chat.calls[3]["messages"][-1]["content"][0]["text"]
        self.assertIn("did not pass Strict: It names one station only.", retry)              # the evaluator's feedback
        # The top agent's answer check sees the evidence its sub-agent gathered.
        self.assertEqual([c["source_url"] for c in seen["chunks"]], ["https://rail.example/access"])
        # The sub-agent's report reaches its parent as a typed result (R29).
        report = json.dumps(chat.calls[4]["messages"], ensure_ascii=False)
        self.assertIn("Status: done.", report)
        self.assertIn("Findings:\\nEvery station is step-free [1].", report)
        self.assertIn('"required": ["goal"]', json.dumps(chat.calls[0]["tools"][0]))          # ask_kb takes a Task

    def test_the_live_shape_names_the_parts_for_the_chart(self):
        shape = agent.live_shape(self.starter(tools=["get_weather"]), 3)
        self.assertEqual((shape["kind"], shape["version"]), ("agent", 3))
        self.assertTrue({"agent", "agent:retriever", "agent:api_tools", "answer_evaluator"} <= set(shape["parts"]))
        full = agent.live_shape(agent.FLOW, 0)
        self.assertEqual(full["kind"], "supervisor")
        self.assertTrue({"knowledge_base", "external_apis", "research_agent", "evidence_evaluator"} <= set(full["parts"]))

    def test_api_tools_plugged_into_the_agent_are_offered_and_count_as_evidence(self):
        chat = ScriptedChat(answer_turn("No tools needed."))
        self.ask(self.starter(tools=["get_weather"]), chat)
        self.assertEqual([t["name"] for t in chat.calls[0]["tools"]], ["search_knowledge_base", "get_weather"])

    def test_a_failing_answer_check_replaces_the_answer(self):
        chat = ScriptedChat(answer_turn("Made up [1]."))
        result, _ = self.ask(self.starter(), chat, evaluation_passes=False)
        self.assertNotEqual(result["answer"], "Made up [1].")


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

    def test_a_long_history_is_trimmed_by_size_too(self):
        big = {"role": "user", "content": [{"toolResult": {"toolUseId": "a", "status": "success",
                                                             "content": [{"text": "passage " * 2000}]}}]}
        exchange = [{"role": "user", "content": [{"text": "q"}]},
                    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "n", "input": {}}}]},
                    big, {"role": "assistant", "content": [{"text": "answer"}]}]
        with patch.object(agent_runtime, "MAX_HISTORY_CHARS", 40_000):
            trimmed = agent_runtime._trim(exchange * 6)                     # ~96,000 characters
        self.assertEqual(trimmed[0], {"role": "user", "content": [{"text": "q"}]})
        self.assertLessEqual(sum(agent_runtime._size(m) for m in trimmed), 40_000)
        self.assertGreaterEqual(len(trimmed), 4)                             # the newest exchange stays

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

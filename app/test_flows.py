"""Flow JSON → LangGraph: the default flow compiles to the graph agent.py ran
before flows existed, and the validator rejects broken flows."""

import contextvars
import copy
import json
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import agent
import agent_runtime
import answer_eval
import evidence
import flows
import guardrails
import identity
import llm
import research
import retrieval
import web_api
from common import config

MAIN_EDGES = {
    ("__start__", "supervisor", False), ("answer_evaluator", "__end__", False),
    ("external_apis_agent", "supervisor", False), ("knowledge_base_agent", "supervisor", False),
    ("output_guardrail", "__end__", True), ("output_guardrail", "answer_evaluator", True),
    ("supervisor", "external_apis_agent", True), ("supervisor", "knowledge_base_agent", True),
    ("supervisor", "output_guardrail", True),
}
KB_EDGES = {
    ("__start__", "start_task", False), ("start_task", "retrieval_agent", False),
    ("retrieval_agent", "evidence_evaluator", True), ("retrieval_agent", "finish_task", True),
    ("evidence_evaluator", "answer_handoff", True), ("evidence_evaluator", "query_rewriter", True),
    ("evidence_evaluator", "research_agent", True), ("evidence_evaluator", "research_report", True),
    ("evidence_evaluator", "investigation_placeholder", True),
    ("evidence_evaluator", "insufficient_evidence_placeholder", True), ("evidence_evaluator", "finish_task", True),
    ("query_rewriter", "retrieval_agent", True), ("query_rewriter", "finish_task", True),
    ("research_agent", "source_validator", True), ("research_agent", "finish_task", True),
    ("source_validator", "ingest_sources", True), ("source_validator", "research_report", True),
    ("source_validator", "finish_task", True),
    ("ingest_sources", "retrieval_agent", True), ("ingest_sources", "research_report", True),
    ("ingest_sources", "finish_task", True),
    ("answer_handoff", "finish_task", False), ("research_report", "finish_task", False),
    ("investigation_placeholder", "finish_task", False), ("insufficient_evidence_placeholder", "finish_task", False),
    ("finish_task", "__end__", False),
}


def edges(compiled) -> set:
    return {(e.source, e.target, e.conditional) for e in compiled.get_graph().edges}


def raw() -> dict:
    return json.loads(flows.DEFAULT_FLOW_PATH.read_text())


def errors(spec: dict) -> list[str]:
    return [i["message"] for i in flows.validate(flows.FlowSpec.model_validate(spec)) if i["level"] == "error"]


class DefaultFlowCompiles(unittest.TestCase):
    def test_default_flow_is_valid(self):
        self.assertEqual(flows.validate(flows.load_flow()), [])

    def test_main_graph_matches_the_hand_built_graph(self):
        self.assertEqual(edges(agent.GRAPH), MAIN_EDGES)
        self.assertEqual(edges(agent._build_graph()), MAIN_EDGES)

    def test_knowledge_base_subgraph_matches_the_hand_built_graph(self):
        self.assertEqual(edges(agent.KB_GRAPH), KB_EDGES)

    def test_resources_never_become_graph_nodes(self):
        nodes = set(agent.GRAPH.get_graph().nodes) | set(agent.KB_GRAPH.get_graph().nodes)
        self.assertFalse(nodes & {"llm", "memory", "retriever", "vector_db", "web_search", "scraper", "api_tools",
                                  "input_guardrail"})

    def test_every_component_describes_its_settings(self):
        described = flows.components()
        self.assertEqual({c["type"] for c in described}, set(flows.COMPONENTS))
        for c in described:
            self.assertIn("properties", c["config_schema"])


class Validation(unittest.TestCase):
    def test_missing_outcome_branch(self):
        spec = raw()
        kb = spec["subflows"]["knowledge_base"]
        kb["edges"] = [e for e in kb["edges"] if e.get("outcome") != "KNOWLEDGE_GAP"]
        self.assertTrue(any("'KNOWLEDGE_GAP' needs exactly one edge" in m for m in errors(spec)))

    def test_unknown_component_type(self):
        spec = raw()
        spec["nodes"].append({"id": "mystery", "type": "python_code"})
        self.assertTrue(any("unknown component type" in m for m in errors(spec)))

    def test_unbounded_loop(self):
        spec = raw()
        kb = spec["subflows"]["knowledge_base"]
        # report → research → validator → report: a loop no evaluator can stop
        for e in kb["edges"]:
            if e["source"] == "research_report":
                e["target"] = "research_agent"
        self.assertTrue(any("unbounded loop" in m for m in errors(spec)))

    def test_loop_through_the_evaluator_is_bounded(self):
        spec = raw()
        for e in spec["subflows"]["knowledge_base"]["edges"]:
            if e["source"] == "answer_handoff":
                e["target"] = "start_task"
        self.assertFalse(any("unbounded loop" in m for m in errors(spec)))

    def test_unreachable_step(self):
        spec = raw()
        spec["nodes"].append({"id": "orphan", "type": "answer_evaluator"})
        spec["edges"].append({"source": "orphan", "target": "end"})
        self.assertTrue(any("not reachable from start" in m for m in errors(spec)))

    def test_resource_port_must_match(self):
        spec = raw()
        spec["edges"].append({"source": "api_tools", "target": "answer_evaluator", "kind": "resource"})
        self.assertTrue(any("has no 'tools' input" in m for m in errors(spec)))

    def test_flow_edge_cannot_touch_a_resource(self):
        spec = raw()
        spec["edges"].append({"source": "answer_evaluator", "target": "llm"})
        self.assertTrue(errors(spec))

    def test_unsupported_resource_is_a_warning(self):
        spec = copy.deepcopy(raw())
        spec["nodes"].append({"id": "mcp", "type": "mcp_tool"})
        spec["edges"].append({"source": "mcp", "target": "external_apis_agent", "kind": "resource"})
        issues = flows.validate(flows.FlowSpec.model_validate(spec))
        self.assertEqual([i["level"] for i in issues], ["warning"])

    def test_compile_refuses_an_invalid_flow(self):
        spec = raw()
        spec["edges"] = [e for e in spec["edges"] if e.get("outcome") != "answer"]
        with self.assertRaises(flows.FlowError):
            flows.compile_flow(flows.FlowSpec.model_validate(spec), agent._runtime())

    def test_subflow_used_twice(self):
        spec = raw()
        spec["nodes"].append({"id": "kb_again", "type": "subflow", "config": {"subflow": "knowledge_base"}})
        spec["edges"] += [{"source": "supervisor", "target": "kb_again", "outcome": "delegate:kb_again"},
                          {"source": "kb_again", "target": "supervisor"}]
        self.assertTrue(any("used by one node only" in m for m in errors(spec)))

    def test_delegation_must_name_the_target_role(self):
        spec = raw()
        for e in spec["edges"]:
            if e.get("outcome") == "delegate:external_apis":
                e["outcome"] = "delegate:external_apis_agent"
        self.assertTrue(any("must be 'delegate:external_apis'" in m for m in errors(spec)))

    def test_compiles_with_default_settings(self):
        spec = raw()
        for node in spec["nodes"]:
            if node["type"] == "specialist_agent":
                node["config"] = {}     # role comes from SpecialistConfig's default
        self.assertEqual(edges(flows.compile_flow(flows.FlowSpec.model_validate(spec), agent._runtime())), MAIN_EDGES)

    def test_compile_refuses_an_unknown_state(self):
        spec = raw()
        spec["subflows"]["knowledge_base"]["state"] = "mystery"
        with self.assertRaises(flows.FlowError) as raised:
            flows.compile_flow(flows.FlowSpec.model_validate(spec), agent._runtime())
        self.assertIn("no state 'mystery'", str(raised.exception))


class GuardrailPolicy(unittest.TestCase):
    def test_default_flow_is_guarded(self):
        self.assertFalse(any("guardrail" in m for m in errors(raw())))

    def test_answer_that_skips_the_output_guardrail(self):
        spec = raw()
        for e in spec["edges"]:
            if e.get("outcome") == "answer":
                e["target"] = "answer_evaluator"
        self.assertIn("every answer must pass an output guardrail before End", errors(spec))

    def test_missing_input_guardrail_is_a_warning(self):
        spec = raw()
        spec["nodes"] = [n for n in spec["nodes"] if n["id"] != "input_guardrail"]
        spec["edges"] = [e for e in spec["edges"] if "input_guardrail" not in (e["source"], e["target"])]
        spec["edges"].append({"source": "start", "target": "supervisor"})
        issues = flows.validate(flows.FlowSpec.model_validate(spec))
        self.assertIn("warning", {i["level"] for i in issues if "input guardrail" in i["message"]})
        self.assertEqual([i for i in issues if i["level"] == "error"], [])


class RuntimeChecks(unittest.TestCase):
    def runtime_errors(self, spec):
        return [i["message"] for i in flows.compiler.runnable_issues(flows.FlowSpec.model_validate(spec), agent._runtime())]

    def test_default_flow_runs(self):
        self.assertEqual(self.runtime_errors(raw()), [])

    def test_delegating_to_an_unknown_subflow(self):
        spec = raw()
        spec["subflows"]["weather"] = copy.deepcopy(spec["subflows"]["knowledge_base"]) | {"id": "weather"}
        spec["nodes"].append({"id": "weather_agent", "type": "subflow", "config": {"subflow": "weather"}})
        spec["edges"] += [{"source": "supervisor", "target": "weather_agent", "outcome": "delegate:weather"},
                          {"source": "weather_agent", "target": "supervisor"}]
        self.assertTrue(any("cannot delegate to 'weather'" in m for m in self.runtime_errors(spec)))

    def test_knowledge_base_step_in_the_main_flow(self):
        spec = raw()
        spec["nodes"].append({"id": "rewriter", "type": "query_rewriter"})
        spec["edges"] += [{"source": "answer_evaluator", "target": "rewriter"},
                          {"source": "rewriter", "target": "end", "outcome": "ok"},
                          {"source": "rewriter", "target": "end", "outcome": "error"}]
        spec["edges"] = [e for e in spec["edges"] if not (e["source"] == "answer_evaluator" and e["target"] == "end")]
        self.assertTrue(any("cannot run in a 'main' flow" in m for m in self.runtime_errors(spec)))


class SettingsReachTheRuntime(unittest.TestCase):
    def test_answer_evaluator_thresholds(self):
        assessment = answer_eval.AnswerAssessment(correctness=0.8, faithfulness=0.9, completeness=0.9,
                                                  citation_quality=0.9, answer_type="answer", rationale="ok")
        assess = lambda *a, **k: (assessment, {"input_tokens": 0, "output_tokens": 0})
        self.assertTrue(answer_eval.evaluate("q", "An answer.", [], [], assessor=assess).passed)
        strict = answer_eval.evaluate("q", "An answer.", [], [], assessor=assess, thresholds={"correctness": 0.9})
        self.assertEqual(strict.failed_on, ["correctness"])

    def test_evidence_max_attempts(self):
        self.assertEqual(evidence._retrieval_failure(1), evidence.Decision.RETRIEVAL_FAILURE)
        self.assertEqual(evidence._retrieval_failure(1, evidence.Limits(max_attempts=1)), evidence.Decision.KNOWLEDGE_GAP)

    def test_node_settings_reach_their_node(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "answer_evaluator":
                n["config"] = {"correctness": 0.95}
        seen = {}
        verdict = guardrails.Verdict(decision="ALLOW", reason="travel", stage="output")
        with patch.object(guardrails, "check", return_value=verdict), \
                patch.object(agent, "_supervisor", new=lambda state, config: {"answer": "Weather", "tasks": []}), \
                patch.object(agent, "_answer_evaluator",
                             new=lambda state, config: seen.update(config["configurable"]["settings"]) or {}):
            graph = flows.compile_flow(flows.FlowSpec.model_validate(spec), agent._runtime(), agent.CHECKPOINTER)
            graph.invoke({"question": "Weather?", "round": 0},
                         config={"configurable": {"run": Mock(), "thread_id": str(uuid.uuid4())}})
        self.assertEqual(seen["correctness"], 0.95)
        self.assertEqual(seen["faithfulness"], 0.7)   # defaults filled in

    def test_allowed_answer_without_an_evaluator(self):
        spec = raw()
        spec["nodes"] = [n for n in spec["nodes"] if n["id"] != "answer_evaluator"]
        spec["edges"] = [e for e in spec["edges"] if "answer_evaluator" not in (e["source"], e["target"])]
        spec["edges"].append({"source": "output_guardrail", "target": "end", "outcome": "ALLOW"})
        verdict = guardrails.Verdict(decision="ALLOW", reason="travel", stage="output")
        with patch.object(guardrails, "check", return_value=verdict), \
                patch.object(agent, "_supervisor", new=lambda state, config: {"answer": "Sunny", "tasks": []}):
            graph = flows.compile_flow(flows.FlowSpec.model_validate(spec), agent._runtime(), agent.CHECKPOINTER)
            out = graph.invoke({"question": "Weather?", "round": 0},
                               config={"configurable": {"run": Mock(), "thread_id": str(uuid.uuid4())}})
        self.assertEqual(out["final_answer"], "Sunny")


class AnswerMetricsCallback(unittest.TestCase):
    def test_blocked_question_reports_its_numbers(self):
        heard = []
        verdict = guardrails.Verdict(decision="BLOCK", reason="off topic", stage="input", message="Travel only")
        with patch.object(guardrails, "check", return_value=verdict), patch.object(agent, "_record_run"):
            agent.answer("Write Python code", "s", None, None, lambda e: None, Mock(), (agent.FLOW, 0), "eval",
                         heard.append)
        self.assertEqual(heard, [{"input_blocked": True}])


class SettingsAreLive(unittest.TestCase):
    """Phase 6: the settings that used to be for reference reach the runtime."""

    def test_default_flow_settings(self):
        settings = agent._flow_settings(flows.load_flow())
        self.assertEqual(settings["word_limit"], 120)
        self.assertEqual(settings["max_rounds"], 2)
        self.assertEqual(settings["retrieval"], {"top_k": 5, "prefetch": 10, "rrf_k": 2, "dense": True, "full_text": True})
        self.assertEqual(settings["web_results"], 5)
        self.assertEqual(settings["api_tools"], set(agent.api_tools.TOOLS))

    def test_settings_from_the_flow(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "supervisor":
                n["config"] = {"answer_word_limit": 60, "max_rounds": 1}
            if n["type"] == "api_tools":
                n["config"] = {"tools": ["get_weather"]}
        for n in spec["subflows"]["knowledge_base"]["nodes"]:
            if n["type"] == "retriever":
                n["config"] = {"top_k": 8, "rrf_k": 60}
            if n["type"] == "web_search":
                n["config"] = {"max_results": 3}
        settings = agent._flow_settings(flows.FlowSpec.model_validate(spec))
        self.assertEqual((settings["word_limit"], settings["max_rounds"]), (60, 1))
        self.assertEqual(settings["retrieval"], {"top_k": 8, "prefetch": 10, "rrf_k": 60, "dense": True, "full_text": True})
        self.assertEqual((settings["web_results"], settings["api_tools"]), (3, {"get_weather"}))

    def test_prompt_texts_the_settings_replace_still_exist(self):
        self.assertIn(agent.WORD_LIMIT_TEXT, agent.ROLES["supervisor"]["prompt"])
        self.assertIn(agent.ROUNDS_TEXT, agent.ROLES["supervisor"]["prompt"])
        self.assertIn(agent.SEARCH_COUNT_TEXT,
                      agent.ROLES["knowledge_base"]["tools"][0]["config"]["inlineFunction"]["description"])

    def test_apply_settings(self):
        settings = {"word_limit": 60, "max_rounds": 1, "retrieval": {"top_k": 8}, "api_tools": {"get_weather"}}
        system, _ = agent._apply_settings("supervisor", settings, agent._system_prompt("supervisor"),
                                          agent.ROLES["supervisor"]["tools"])
        self.assertIn("a hard limit of 60 words", system[0]["text"])
        self.assertIn("(at most 1 rounds in all)", system[0]["text"])
        _, tools = agent._apply_settings("knowledge_base", settings, [], agent.ROLES["knowledge_base"]["tools"])
        self.assertIn("Returns the 8 best chunks", tools[0]["config"]["inlineFunction"]["description"])
        self.assertIn("Returns the 5 best chunks",
                      agent.ROLES["knowledge_base"]["tools"][0]["config"]["inlineFunction"]["description"])
        _, tools = agent._apply_settings("external_apis", settings, [], agent.ROLES["external_apis"]["tools"])
        self.assertEqual([t["name"] for t in tools], ["get_weather"])

    def run_with(self, settings):
        run = agent._Run.__new__(agent._Run)
        run.settings, run.lock, run.searches, run.calls = settings, threading.Lock(), [], []
        run.emit, run.retrieval_view = (lambda e: None), (lambda s: {"chunks": [], "sources": []})
        return run

    def test_search_uses_the_retriever_settings(self):
        run = self.run_with({"retrieval": {"top_k": 3, "prefetch": 12, "rrf_k": 60}})
        run.searches = [None]          # a second search: its numbers start after the first's 3
        with patch.object(agent, "hybrid_search", return_value={}) as search:
            run._search({"name": "search_knowledge_base", "input": json.dumps({"query": "trains"}), "turn": 1,
                         "toolUseId": "t"}, 1)
        kwargs = search.call_args.kwargs
        self.assertEqual((kwargs["top_k"], kwargs["prefetch"], kwargs["rrf_k"]), (3, 12, 60))
        self.assertEqual(run.searches[1]["first"], 4)

    VOCABULARY = {"topic": {"refunds": "Refunds", "accessibility": "Accessibility"},
                  "organisation": ["National Rail"], "content_type": ["guide", "policy"]}

    def test_the_search_tool_offers_label_filters_only_when_the_knowledge_base_has_labels(self):
        tool = agent.ROLES["knowledge_base"]["tools"]
        _, plain = agent._apply_settings("knowledge_base", {"filters": None}, [], tool)
        self.assertEqual(set(plain[0]["config"]["inlineFunction"]["inputSchema"]["properties"]), {"query"})
        _, tools = agent._apply_settings("knowledge_base", {"filters": self.VOCABULARY}, [], tool)
        spec = tools[0]["config"]["inlineFunction"]
        props = spec["inputSchema"]["properties"]
        self.assertEqual((props["topic"]["enum"], props["organisation"]["enum"], props["content_type"]["enum"]),
                         (["refunds", "accessibility"], ["National Rail"], ["guide", "policy"]))
        self.assertIn("refunds (Refunds)", props["topic"]["description"])
        self.assertEqual(spec["inputSchema"]["required"], ["query"])           # filters stay optional
        self.assertIn("Optional filters", spec["description"])
        self.assertEqual(set(tool[0]["config"]["inlineFunction"]["inputSchema"]["properties"]), {"query"})   # not changed

    def test_a_filtered_search_and_its_unfiltered_retry(self):
        found = {"rankings": {"fused": [{"source_url": "u", "text": "t", "meta": {"organisation": "National Rail",
                                                                                   "effective_date": "2026-08-07"}}]}}
        empty = {"rankings": {"fused": []}}
        run = self.run_with({"filters": self.VOCABULARY})
        run.retrieval_view = lambda s: {"chunks": s["rankings"]["fused"], "sources": []}
        use = lambda **args: {"name": "search_knowledge_base", "input": json.dumps({"query": "refunds", **args}),
                              "turn": 1, "toolUseId": "t"}
        with patch.object(agent, "hybrid_search", return_value=found) as search:
            out = run._search(use(topic="refunds", organisation="Made Up Ltd"), 1)
        self.assertEqual(search.call_args.kwargs["filters"], {"topic": "refunds"})    # an unknown value is ignored
        text = out["toolResult"]["content"][0]["text"]
        self.assertIn("(filtered by topic=refunds)", text)
        self.assertIn("[1] u (National Rail, 2026-08-07)", text)
        with patch.object(agent, "hybrid_search", side_effect=[empty, found]) as search:
            out = run._search(use(topic="accessibility"), 1)
        self.assertEqual([c.kwargs["filters"] for c in search.call_args_list], [{"topic": "accessibility"}, None])
        self.assertIn("nothing matched topic=accessibility: searched everything", out["toolResult"]["content"][0]["text"])
        self.assertTrue(run.searches[-1]["unfiltered_retry"])
        with patch.object(agent, "hybrid_search", return_value=found) as search:
            run._search(use(), 1)
        self.assertIsNone(search.call_args.kwargs["filters"])                       # none asked: none used

    def test_tools_outside_the_flow_are_refused(self):
        run = self.run_with({"api_tools": {"get_weather"}})
        with patch.object(agent.api_tools, "run") as call:
            out = run._call({"name": "swiss_departures", "input": "{}", "turn": 1, "toolUseId": "t"}, 1)
        call.assert_not_called()
        self.assertIn("not available in this flow", json.dumps(out))

    def test_api_tool_names_are_checked(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "api_tools":
                n["config"] = {"tools": ["teleport"]}
        self.assertTrue(any("invalid config" in m for m in errors(spec)))
        for n in spec["nodes"]:
            if n["type"] == "api_tools":
                n["config"] = {"tools": []}
        self.assertTrue(any("invalid config" in m for m in errors(spec)))

    def test_fusion_constant_and_prefetch(self):
        hits = lambda ids: [{"id": i, "score": 1.0, "vector": None, "payload": {
            "text": i, "source_url": "u", "chunk_index": 0}} for i in ids]
        fused, rows = retrieval._fuse(hits(["a", "b"]), hits(["b", "a"]), 2, rrf_k=60)
        self.assertAlmostEqual(rows[0]["dense_part"], 1 / 60)
        with patch.object(retrieval.storage, "get_client"), \
                patch.object(retrieval.embedding, "embed_texts", return_value=[[0.1]]), \
                patch.object(retrieval.storage, "search_dense", return_value=hits(["a"])) as dense, \
                patch.object(retrieval.storage, "search_text", return_value=hits(["a"])) as text, \
                patch.object(retrieval, "get_index_info", return_value={}):
            result = retrieval.hybrid_search("q", top_k=4, prefetch=2, rrf_k=7)
        self.assertEqual(dense.call_args.args[2], 4)        # prefetch is at least top_k
        self.assertEqual(text.call_args.args[2], 4)
        self.assertEqual(result["explain"]["fused"]["k"], 7)

    def test_source_validator_thresholds_and_ttl(self):
        source = {"url": "https://example.org/a", "kind": "html", "title": "t", "date": None, "chars": 5000,
                  "text": "x" * 5000, "error": None}
        assessment = research.SourceAssessments(sources=[research.SourceAssessment(
            url=source["url"], authority=0.7, freshness=0.9, consistency=0.9, relevance=0.9, publisher="p",
            published_or_updated=None, reasons="ok")])
        assess = lambda *a, **k: (assessment, {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(research.validate("q", [], [source], assessor=assess)["accepted"], 1)
        strict = research.validate("q", [], [source], assessor=assess, accept={"authority": 0.8}, ttl_days=7)
        self.assertEqual(strict["accepted"], 0)
        self.assertEqual(strict["sources"][0]["ttl_days"], 7)


class RemainingSettings(unittest.TestCase):
    """Phase 8: the retriever's search switches and the LLM node's temperature and max tokens."""

    def test_search_switches(self):
        hits = [{"id": "a", "score": 1.0, "vector": None, "payload": {"text": "a", "source_url": "u", "chunk_index": 0}}]
        with patch.object(retrieval.storage, "get_client"), \
                patch.object(retrieval.embedding, "embed_texts", return_value=[[0.1]]) as embed, \
                patch.object(retrieval.storage, "search_dense", return_value=hits) as dense, \
                patch.object(retrieval.storage, "search_text", return_value=hits) as text, \
                patch.object(retrieval, "get_index_info", return_value={}):
            keyword = retrieval.hybrid_search("q", dense=False)
            self.assertEqual((dense.call_count, text.call_count, embed.call_count), (0, 1, 1))
            self.assertEqual(keyword["rankings"]["dense"], [])
            vector = retrieval.hybrid_search("q", full_text=False)
            self.assertEqual((dense.call_count, text.call_count), (1, 1))
            self.assertEqual(vector["rankings"]["sparse"], [])
            with self.assertRaises(ValueError):
                retrieval.hybrid_search("q", dense=False, full_text=False)

    def test_one_search_must_stay_on(self):
        spec = raw()
        for n in spec["subflows"]["knowledge_base"]["nodes"]:
            if n["type"] == "retriever":
                n["config"] = {"dense": False, "full_text": False}
        self.assertTrue(any("dense or the full-text" in m for m in errors(spec)))

    def test_switches_reach_the_search(self):
        run = SettingsAreLive.run_with(self, {"retrieval": {"top_k": 5, "dense": False, "full_text": True}})
        with patch.object(agent, "hybrid_search", return_value={}) as search:
            run._search({"name": "search_knowledge_base", "input": json.dumps({"query": "trains"}), "turn": 1,
                         "toolUseId": "t"}, 1)
        self.assertEqual((search.call_args.kwargs["dense"], search.call_args.kwargs["full_text"]), (False, True))

    def test_default_llm_sends_no_override(self):
        self.assertNotIn("models", agent._flow_settings(flows.load_flow()))

    def test_llm_settings_per_agent(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "llm":
                n["config"] = {"temperature": 0.0}
        for n in spec["subflows"]["knowledge_base"]["nodes"]:
            if n["type"] == "llm":
                n["config"] = {"max_tokens": 1024}
        models = agent._flow_settings(flows.FlowSpec.model_validate(spec))["models"]
        model = flows.registry.HARNESS_MODEL
        self.assertEqual(models["supervisor"], {"modelId": model, "maxTokens": 2048, "temperature": 0.0})
        self.assertEqual(models["external_apis"], {"modelId": model, "maxTokens": 2048, "temperature": 0.0})
        self.assertEqual(models["knowledge_base"], {"modelId": model, "maxTokens": 1024, "temperature": 0.2})
        self.assertEqual(models["research"], {"modelId": model, "maxTokens": 1024, "temperature": 0.2})

    def test_override_reaches_the_harness(self):
        run = agent._Run.__new__(agent._Run)
        run.settings = {"models": {"supervisor": {"modelId": "m", "maxTokens": 512, "temperature": 0.0}}}
        run.agents, run.session_id, run.user_id, run.limit = set(agent.SPECIALISTS), "s", None, 3
        run.client = Mock()
        run.client.invoke_harness.return_value = {"stream": []}
        with patch.object(agent, "HARNESS_ARN", "arn:aws:bedrock-agentcore:eu-west-2:1:harness/x"), \
                patch.object(run, "_read", return_value=(None, [])):
            run.invoke({"role": "supervisor", "turns": 0}, [])
            run.invoke({"role": "external_apis", "turns": 0}, [])
        first, second = run.client.invoke_harness.call_args_list
        self.assertEqual(first.kwargs["model"], {"bedrockModelConfig": {"modelId": "m", "maxTokens": 512, "temperature": 0.0}})
        self.assertNotIn("model", second.kwargs)

    def test_the_llm_node_shows_what_the_runtime_uses(self):
        config = flows.COMPONENTS["llm"].config
        expected = (("bedrock_agentcore_harness", flows.registry.HARNESS_MODEL) if agent_runtime.RUNTIME == "agentcore"
                    else ("local", llm.label()))
        self.assertEqual((config().provider, config().model), expected)
        # Flows saved with the harness's values load as the current runtime's.
        saved = config.model_validate({"provider": "bedrock_agentcore_harness", "model": flows.registry.HARNESS_MODEL,
                                       "max_tokens": 1024})
        self.assertEqual((saved.provider, saved.model, saved.max_tokens), (*expected, 1024))

    def test_model_is_fixed(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "llm":
                n["config"] = {"model": "anthropic.claude-opus"}
        self.assertTrue(any("invalid config" in m for m in errors(spec)))


class FlowSpecialists(unittest.TestCase):
    def test_flow_agents(self):
        self.assertEqual(agent.flow_agents(flows.load_flow()), {"knowledge_base", "external_apis"})
        spec = raw()
        spec["edges"] = [e for e in spec["edges"] if e.get("outcome") != "delegate:knowledge_base"]
        self.assertEqual(agent.flow_agents(flows.FlowSpec.model_validate(spec)), {"external_apis"})

    def test_restricted_supervisor_tool(self):
        system, tools = agent._restricted_supervisor([{"text": "base"}], {"external_apis"})
        enum = tools[0]["config"]["inlineFunction"]["inputSchema"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"]
        self.assertEqual(enum, ["external_apis"])
        self.assertIn("only these specialists exist: external_apis", system[-1]["text"])
        # the shared tool definition is untouched
        self.assertEqual(agent.ROLES["supervisor"]["tools"][0]["config"]["inlineFunction"]["inputSchema"]["properties"]
                         ["tasks"]["items"]["properties"]["agent"]["enum"], list(agent.SPECIALISTS))


class Templates(unittest.TestCase):
    def test_every_template_runs(self):
        for template in flows.templates(flows.load_flow()):
            spec = flows.FlowSpec.model_validate(template["flow"])
            self.assertEqual(flows.compiler.runnable_issues(spec, agent._runtime()), [], template["id"])
            self.assertEqual([i for i in flows.validate(spec) if i["level"] == "warning"], [], template["id"])
            flows.compile_flow(spec, agent._runtime())

    def test_template_contents(self):
        by_id = {t["id"]: flows.FlowSpec.model_validate(t["flow"]) for t in flows.templates(flows.load_flow())}
        self.assertEqual(agent.flow_agents(by_id["rag_research"]), {"knowledge_base"})
        self.assertEqual(agent.flow_agents(by_id["live_data"]), {"external_apis"})
        rag_only = {n.id for n in by_id["rag_only"].subflows["knowledge_base"].nodes}
        self.assertFalse(rag_only & {"research_agent", "source_validator", "ingest_sources"})
        self.assertIn("research_agent", {n.id for n in by_id["rag_research"].subflows["knowledge_base"].nodes})


class Diff(unittest.TestCase):
    def test_no_changes_ignoring_positions(self):
        old = raw()
        new = copy.deepcopy(old)
        new["nodes"][0]["position"] = {"x": 10, "y": 20}
        self.assertEqual(flows.diff(flows.FlowSpec.model_validate(old), flows.FlowSpec.model_validate(new)), [])

    def test_changes_in_the_main_flow_and_a_subflow(self):
        old = raw()
        new = copy.deepcopy(old)
        for n in new["nodes"]:
            if n["id"] == "answer_evaluator":
                n["config"] = {"correctness": 0.9}
                n["label"] = "Strict evaluator"
        kb = new["subflows"]["knowledge_base"]
        kb["nodes"] = [n for n in kb["nodes"] if n["id"] != "scraper"]
        kb["edges"] = [e for e in kb["edges"] if "scraper" not in (e["source"], e["target"])]
        changes = flows.diff(flows.FlowSpec.model_validate(old), flows.FlowSpec.model_validate(new))
        evaluator = next(c for c in changes if c["id"] == "answer_evaluator")
        self.assertEqual(evaluator["details"], {"label": [None, "Strict evaluator"], "config.correctness": [None, 0.9]})
        removed = [(c["flow"], c["kind"], c["id"]) for c in changes if c["change"] == "removed"]
        self.assertIn(("travel_assistant/knowledge_base", "node", "scraper"), removed)
        self.assertIn(("travel_assistant/knowledge_base", "edge", "scraper ⇢ research_agent"), removed)


def blank(flow_id: str) -> dict:
    """The smallest flow that may run: everything passes the output guardrail."""
    return {"id": flow_id, "name": "Blank", "nodes": [
        {"id": "start", "type": "start"}, {"id": "output_guardrail", "type": "output_guardrail"},
        {"id": "end", "type": "end"}],
        "edges": [{"source": "start", "target": "output_guardrail"},
                  {"source": "output_guardrail", "target": "end", "outcome": "ALLOW"},
                  {"source": "output_guardrail", "target": "end", "outcome": "BLOCK"}]}


class FlowStore(unittest.TestCase):
    """Drafts and published versions in Postgres, through the HTTP API."""

    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        flows.store.reset_schema_cache()
        flows.evals.reset_schema_cache()
        identity.reset_schema_cache()
        # Signed in as the first admin, through the development sign-in.
        cls.client = TestClient(web_api.app, base_url="http://localhost")
        cls.client.post("/auth/dev", data={"first_name": "Tess", "email": "tess@example.com"})

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        flows.store.reset_schema_cache()
        flows.evals.reset_schema_cache()
        identity.reset_schema_cache()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    def test_builtin_flow_is_version_zero_until_published(self):
        got = self.client.get("/api/flows/travel_assistant").json()
        self.assertEqual((got["source"], got["published_version"]), ("builtin", 0))
        self.assertEqual(got["versions"][-1]["version"], 0)
        listed = {f["id"]: f for f in self.client.get("/api/flows").json()}
        self.assertTrue(listed["travel_assistant"]["builtin"])

    def test_draft_publish_cycle(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        created = self.client.post("/api/flows", json={"flow": blank(flow_id)})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["source"], "draft")
        self.assertEqual(self.client.post("/api/flows", json={"flow": blank(flow_id)}).status_code, 409)

        edited = blank(flow_id) | {"name": "Edited"}
        saved = self.client.put(f"/api/flows/{flow_id}/draft", json={"flow": edited}).json()
        self.assertEqual(saved["flow"]["name"], "Edited")

        published = self.client.post(f"/api/flows/{flow_id}/publish", json={"note": "first"})
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["version"], 1)
        after = self.client.get(f"/api/flows/{flow_id}").json()
        self.assertEqual((after["source"], after["published_version"], after["flow"]["version"]), ("published", 1, 1))
        self.assertEqual(self.client.get(f"/api/flows/{flow_id}/versions/1").json()["flow"]["name"], "Edited")
        # nothing left to publish
        self.assertEqual(self.client.post(f"/api/flows/{flow_id}/publish", json={}).status_code, 409)

    def test_unguarded_flow_cannot_publish(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        unguarded = {"id": flow_id, "name": "Unguarded", "nodes": [{"id": "start", "type": "start"},
                     {"id": "end", "type": "end"}], "edges": [{"source": "start", "target": "end"}]}
        self.assertEqual(self.client.post("/api/flows", json={"flow": unguarded}).status_code, 201)
        response = self.client.post(f"/api/flows/{flow_id}/publish", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("output guardrail", str(response.json()["issues"]))

    def test_invalid_draft_saves_but_does_not_publish(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        broken = blank(flow_id) | {"edges": []}
        self.assertEqual(self.client.post("/api/flows", json={"flow": broken}).status_code, 201)
        response = self.client.post(f"/api/flows/{flow_id}/publish", json={})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json()["issues"])

    def test_discard_draft_returns_to_published(self):
        saved = self.client.put("/api/flows/travel_assistant/draft", json={"flow": raw() | {"name": "Changed"}}).json()
        self.assertEqual(saved["source"], "draft")
        back = self.client.delete("/api/flows/travel_assistant/draft").json()
        self.assertEqual((back["source"], back["flow"]["name"]), ("builtin", raw()["name"]))

    def test_make_live_and_back(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        self.client.post("/api/flows", json={"flow": blank(flow_id)})
        # a draft cannot go live; publish it first
        self.assertEqual(self.client.post(f"/api/flows/{flow_id}/live", json={"version": 1}).status_code, 404)
        self.client.post(f"/api/flows/{flow_id}/publish", json={})
        self.assertEqual(self.client.post(f"/api/flows/{flow_id}/live", json={"version": 1}).status_code, 200)
        spec, version = agent.live_flow()
        self.assertEqual((spec.id, version), (flow_id, 1))
        listed = {f["id"]: f for f in self.client.get("/api/flows").json()}
        self.assertEqual(listed[flow_id]["live_version"], 1)
        self.assertEqual(self.client.post("/api/flows/travel_assistant/live", json={"version": 0}).status_code, 200)
        self.assertEqual(agent.live_flow(), (agent.FLOW, 0))

    def test_run_a_flow_version(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        self.client.post("/api/flows", json={"flow": blank(flow_id)})
        seen = {}

        def fake_answer(question, session_id, user_id, limit, on_event, view, flow, source):
            seen["flow"], seen["source"] = flow, source
            return {"answer": "ok", "searches": [], "flow": {"id": flow[0].id, "version": flow[1]}}
        with patch.object(web_api.agent, "answer", side_effect=fake_answer):
            response = self.client.post(f"/api/flows/{flow_id}/run", json={"question": "Weather?", "version": "draft"})
        self.assertIn("event: result", response.text)
        self.assertEqual((seen["flow"][0].id, seen["flow"][1], seen["source"]), (flow_id, "draft", "playground"))
        self.assertEqual(self.client.post(f"/api/flows/{flow_id}/run",
                                          json={"question": "Weather?", "version": 7}).status_code, 404)
        unguarded = {"id": flow_id, "name": "x", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                     "edges": [{"source": "start", "target": "end"}]}
        self.client.put(f"/api/flows/{flow_id}/draft", json={"flow": unguarded})
        refused = self.client.post(f"/api/flows/{flow_id}/run", json={"question": "Weather?"})
        self.assertEqual(refused.status_code, 422)

    def test_reserved_ids(self):
        response = self.client.post("/api/flows", json={"flow": blank("templates")})
        self.assertEqual(response.status_code, 400)

    def test_templates_are_listed(self):
        listed = self.client.get("/api/flows/templates").json()
        self.assertEqual([t["id"] for t in listed], ["multi_agent_travel", "rag_research", "rag_only", "live_data"])

    def test_diff_endpoint(self):
        old = raw()
        new = copy.deepcopy(old)
        new["name"] = "Renamed"
        changes = self.client.post("/api/flows/diff", json={"old": old, "new": new}).json()["changes"]
        self.assertEqual(changes, [{"flow": "travel_assistant", "change": "changed", "kind": "flow", "id": "name",
                                    "details": {"name": [old["name"], "Renamed"]}}])

    def test_run_metrics(self):
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        flows.store.record_run(flow_id, 1, "playground", {"seconds": 4.0, "rounds": 1, "tasks": 2, "input_tokens": 100,
                                                           "output_tokens": 50, "evaluated": True, "passed": True,
                                                           "overall": 0.9})
        flows.store.record_run(flow_id, 1, "playground", {"seconds": 6.0, "evaluated": True, "passed": False,
                                                           "overall": 0.5, "output_blocked": False})
        flows.store.record_run(flow_id, 1, "playground", {"input_blocked": True, "question": "never stored"})
        flows.store.record_run(flow_id, "draft", "playground", {"failed": True, "seconds": 1.0})
        got = {(m["version"], m["source"]): m for m in self.client.get(f"/api/flows/{flow_id}/metrics").json()["versions"]}
        v1 = got[("1", "playground")]
        self.assertEqual((v1["runs"], v1["evaluated"], v1["passed"], v1["pass_rate"], v1["input_blocked"]),
                         (3, 2, 1, 0.5, 1))
        self.assertEqual(v1["overall"], 0.7)
        self.assertEqual(got[("draft", "playground")]["failed"], 1)
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=config.PGDATABASE) as connection:
            columns = [r[0] for r in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_flow_runs'")]
        self.assertFalse({"question", "answer", "text"} & set(columns))

    # ---------- flow evaluations ----------

    def wait_done(self, run_id, seconds=10):
        for _ in range(seconds * 20):
            run = self.client.get(f"/api/flow-evals/runs/{run_id}").json()
            if run["status"] not in ("queued", "running"):
                return run
            time.sleep(0.05)
        self.fail(f"run {run_id} did not finish: {run['status']}")

    def eval_set(self, set_id):
        response = self.client.post("/api/flow-evals/sets", json={
            "id": set_id, "name": "E2E", "questions": [
                "Trains in Austria?", {"question": "Weather in Zurich?", "expected": "Open-Meteo forecast"},
                {"question": "Write Python code", "expect_blocked": True}]})
        self.assertEqual(response.status_code, 200, response.text)

    def test_builtin_question_set(self):
        sets = {s["id"]: s for s in self.client.get("/api/flow-evals/sets").json()}
        basics = sets["travel_basics"]
        self.assertTrue(basics["builtin"])
        self.assertEqual(basics["count"], 15)
        self.assertEqual(sum(q["expect_blocked"] for q in basics["questions"]), 3)
        demo = sets["lakeshore_demo"]   # matches the seeded demo documents
        self.assertEqual((demo["count"], sum(q["expect_blocked"] for q in demo["questions"])), (7, 1))

    def test_question_set_validation(self):
        bad = [({"id": "travel_basics", "questions": ["q"]}, "built-in"),
               ({"id": "Bad Id", "questions": ["q"]}, "lowercase"),
               ({"id": "big", "questions": ["q"] * 51}, "at most 50"),
               ({"id": "empty", "questions": []}, "at least one")]
        for body, message in bad:
            response = self.client.post("/api/flow-evals/sets", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.json()["error"])

    def test_evaluate_two_versions(self):
        self.eval_set("e2e_set")
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        self.client.post("/api/flows", json={"flow": blank(flow_id)})
        self.client.post(f"/api/flows/{flow_id}/publish", json={})
        seen = []

        def execute(run, index, question):
            seen.append((run["version"], run["spec"]["id"], question["question"]))
            if question["expect_blocked"]:
                return {"input_blocked": True, "answer": "blocked", "seconds": 0.1}
            return {"answer": f"answer {index}", "passed": index == 0, "overall": 0.9 if index == 0 else 0.5,
                    "scores": {"correctness": 0.9, "faithfulness": 0.8}, "failed_on": [] if index == 0 else ["correctness"],
                    "answer_type": "answer", "seconds": 2.0, "tokens": 100, "rationale": "never stored"}
        with patch.object(web_api, "_eval_execute", side_effect=execute):
            started = self.client.post("/api/flow-evals/runs", json={"set_id": "e2e_set", "flow_id": flow_id,
                                                                   "versions": [1, "draft"]})
            self.assertEqual(started.status_code, 404)     # no draft after publishing
            started = self.client.post("/api/flow-evals/runs", json={"set_id": "e2e_set", "flow_id": flow_id,
                                                                   "versions": [1]})
            self.assertEqual(started.status_code, 201, started.text)
            run = self.wait_done(started.json()["runs"][0])
        self.assertEqual(run["status"], "done")
        self.assertEqual([r["answer"] for r in run["results"]], ["answer 0", "answer 1", "blocked"])
        self.assertEqual([r["expectation_met"] for r in run["results"]], [True, False, True])
        self.assertNotIn("rationale", run["results"][0])
        summary = run["summary"]
        self.assertEqual((summary["evaluated"], summary["passed"], summary["pass_rate"], summary["input_blocked"]),
                         (2, 1, 0.5, 1))
        self.assertEqual((summary["expectations_met"], summary["expectations"]), (2, 3))
        self.assertEqual(summary["scores"], {"correctness": 0.9, "faithfulness": 0.8})
        self.assertEqual(run["questions"][1]["expected"], "Open-Meteo forecast")
        self.assertEqual({v for v, _, _ in seen}, {"1"})

    def test_batch_runs_and_frozen_draft(self):
        self.eval_set("e2e_set2")
        flow_id = f"flow_{uuid.uuid4().hex[:6]}"
        self.client.post("/api/flows", json={"flow": blank(flow_id)})
        self.client.post(f"/api/flows/{flow_id}/publish", json={})
        self.client.put(f"/api/flows/{flow_id}/draft", json={"flow": blank(flow_id) | {"name": "Before"}})
        names = []

        def execute(run, index, question):
            names.append((run["version"], run["spec"]["name"]))
            time.sleep(0.05)
            return {"answer": "ok", "seconds": 0.1}
        with patch.object(web_api, "_eval_execute", side_effect=execute):
            ids = self.client.post("/api/flow-evals/runs", json={"set_id": "e2e_set2", "flow_id": flow_id,
                                                               "versions": [1, "draft"]}).json()["runs"]
            # editing the draft now does not change the queued run
            self.client.put(f"/api/flows/{flow_id}/draft", json={"flow": blank(flow_id) | {"name": "After"}})
            runs = [self.wait_done(i) for i in ids]
        self.assertEqual(runs[0]["batch"], runs[1]["batch"])
        self.assertEqual({n for v, n in names if v == "draft"}, {"Before"})
        listed = self.client.get(f"/api/flow-evals/runs?flow_id={flow_id}").json()
        self.assertEqual({r["id"] for r in listed}, set(ids))

    def test_cancel_a_queued_run(self):
        self.eval_set("e2e_set3")
        gate = threading.Event()

        def execute(run, index, question):
            gate.wait(5)
            return {"answer": "ok"}
        with patch.object(web_api, "_eval_execute", side_effect=execute):
            ids = self.client.post("/api/flow-evals/runs", json={"set_id": "e2e_set3", "flow_id": "travel_assistant",
                                                               "versions": [0, 0]}).json()["runs"]
            self.client.post(f"/api/flow-evals/runs/{ids[1]}/cancel")
            self.client.post(f"/api/flow-evals/runs/{ids[0]}/cancel")
            gate.set()
            first, second = self.wait_done(ids[0]), self.wait_done(ids[1])
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["results"], [])
        self.assertEqual(first["status"], "cancelled")
        self.assertLess(len(first["results"]), 3)

    def test_rejects_mismatched_and_malformed_flows(self):
        self.assertEqual(self.client.put("/api/flows/other/draft", json={"flow": raw()}).status_code, 400)
        bad = self.client.post("/api/flows", json={"flow": {"id": "Not Valid", "name": "x", "nodes": [], "edges": []}})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.get("/api/flows/nope").status_code, 404)


class FlowDomain(unittest.TestCase):
    """A flow's scope (input guardrail) and domain (supervisor) reach every check and prompt."""

    def cooking(self) -> flows.FlowSpec:
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "input_guardrail":
                n["config"] = {"stage": "input", "scope": "A cooking assistant: recipes, ingredients and kitchen "
                               "techniques.", "block_message": "Ask me about cooking."}
            if n["type"] == "supervisor":
                n["config"] = {"domain": "cooking", "instructions": "The knowledge base holds recipes."}
        return flows.FlowSpec.model_validate(spec)

    def test_settings_prompts_and_checks_use_the_flow_domain(self):
        spec = self.cooking()
        self.assertEqual(errors(spec.model_dump()), [])
        settings = agent._flow_settings(spec)
        self.assertEqual((settings["domain"], settings["guardrail"]["block_message"]), ("cooking", "Ask me about cooking."))
        supervisor = agent._system_prompt("supervisor", settings)[0]["text"]
        self.assertIn("a small team of cooking agents", supervisor)
        self.assertIn("The knowledge base holds recipes.", supervisor)
        self.assertNotIn("cover Switzerland only", supervisor)   # the built-in instructions are replaced
        self.assertIn("assign_tasks", supervisor)   # the delegation rules stay
        kb = agent._system_prompt("knowledge_base", settings)[0]["text"]
        self.assertIn("scope: A cooking assistant", kb)
        self.assertNotIn("passenger transport", kb)
        # The built-in flow keeps the UK rail and weather domain.
        self.assertIn("UK rail and weather agents", agent._system_prompt("supervisor", agent._flow_settings(agent.FLOW))[0]["text"])

    def test_the_brief_reaches_every_agent_and_research(self):
        spec = raw()
        for n in spec["nodes"]:
            if n["type"] == "supervisor":
                n["config"] = {"domain": "French rail", "search_country": " France ",
                               "brief": "An assistant for trains and rail travel in France."}
            if n["type"] == "input_guardrail":
                n["config"] = {"stage": "input", "scope": "", "block_message": ""}
        settings = agent._flow_settings(flows.FlowSpec.model_validate(spec))
        self.assertEqual(settings["search_country"], "france")
        for role in ("supervisor", "knowledge_base", "external_apis", "research"):
            text = agent._system_prompt(role, settings)[0]["text"]
            self.assertTrue(text.startswith("ASSISTANT BRIEF: An assistant for trains and rail travel in France."), role)
        # An empty scope follows the brief; an empty message names the domain.
        self.assertEqual(settings["guardrail"]["scope"], "An assistant for trains and rail travel in France.")
        self.assertEqual(settings["guardrail"]["block_message"],
                         "I can help with questions about French rail. Please ask about that.")
        self.assertEqual(settings["guardrail"]["clarify_message"], guardrails.CLARIFY_MESSAGE)   # set: kept
        # The built-in flow: the UK brief, UK search country and its tuned UK scope.
        builtin = agent._flow_settings(agent.FLOW)
        self.assertIn("pantry car", builtin["brief"])
        self.assertEqual(builtin["search_country"], "united kingdom")
        self.assertEqual(builtin["guardrail"]["scope"], guardrails.UK_RAIL_SCOPE)

    def test_the_input_check_runs_first_with_the_flow_scope(self):
        seen = []
        def check(stage, question, content="", assessor=None, scope=None, previous=()):
            seen.append(scope)
            return guardrails.Verdict(decision="BLOCK", reason="fixture", stage=stage, message=scope.block_message)
        with patch.object(guardrails, "check", side_effect=check), patch.object(agent, "_record_run"):
            result = agent.answer("Train times to Bern?", "s", None, None, lambda e: None, Mock(),
                                  flow=(self.cooking(), 3))
        self.assertEqual(seen[0].scope[:20], "A cooking assistant:")
        self.assertEqual(result["answer"], "Ask me about cooking.")

    def test_the_input_check_sees_the_sessions_earlier_allowed_questions(self):
        seen = []
        def check(stage, question, content="", assessor=None, scope=None, previous=()):
            seen.append(list(previous))
            decision = "BLOCK" if "poem" in question else "ALLOW"
            return guardrails.Verdict(decision=decision, reason="fixture", stage=stage, message="no")
        session, other = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
        with patch.object(guardrails, "check", side_effect=check), patch.object(agent, "_record_run"), \
                patch.object(agent_runtime, "config_error", return_value="stop after the check"):
            for question, sid in (("Toilets at St Pancras?", session), ("Write a poem", session),
                                  ("And at Victoria?", session), ("Hello", other)):
                try:
                    agent.answer(question, sid, None, None, lambda e: None, Mock(), flow=(self.cooking(), 3))
                except RuntimeError:
                    pass   # allowed: the agents are not configured in this test
        # A blocked question never becomes context; sessions never share it.
        self.assertEqual(seen, [[], ["Toilets at St Pancras?"], ["Toilets at St Pancras?"], []])
        self.assertEqual(agent._recent_questions(session), ["Toilets at St Pancras?", "And at Victoria?"])

    def test_saved_flows_with_the_old_policy_label_still_load(self):
        old = flows.COMPONENTS["input_guardrail"].config.model_validate({"stage": "input", "policy": "travel-only"})
        self.assertEqual(old.scope, guardrails.UK_RAIL_SCOPE)
        flows.COMPONENTS["output_guardrail"].config.model_validate({"stage": "output", "policy": "travel-only"})
        with self.assertRaises(Exception):   # a scope must say something, or be empty (the brief)
            flows.COMPONENTS["input_guardrail"].config.model_validate({"scope": "short"})
        flows.COMPONENTS["input_guardrail"].config.model_validate({"scope": ""})


class ToolInput(unittest.TestCase):
    def test_a_list_sent_as_text_with_markup_is_read(self):
        use = {"input": json.dumps({"tasks": '\n[{"agent": "knowledge_base", "task": "Bikes?"}]\n</invoke>',
                                    "query": "plain text stays"})}
        value = agent._input(use)
        self.assertEqual(value["tasks"], [{"agent": "knowledge_base", "task": "Bikes?"}])
        self.assertEqual(value["query"], "plain text stays")
        self.assertEqual(agent._input({"input": '{"query": "[1] is a citation, not JSON"}'})["query"],
                         "[1] is a citation, not JSON")
        self.assertEqual(agent._input({"input": "not json"}), {})


class SpecialistTasks(unittest.TestCase):
    def test_every_task_carries_the_users_question(self):
        task = agent._with_question("Where is Kestrel Bay?", "Will strong wind delay trains to Kestrel Bay?")
        self.assertTrue(task.startswith("Where is Kestrel Bay?"))
        self.assertIn("Will strong wind delay trains to Kestrel Bay?", task)
        same = "Will strong wind delay trains to Kestrel Bay? Cite the chunks."
        self.assertEqual(agent._with_question(same, "will strong wind delay trains to Kestrel Bay?"), same)


class SupervisorAnswerText(unittest.TestCase):
    """The supervisor's answer loses an opening sentence that narrates the process, nothing else."""

    def test_narration_is_dropped(self):
        cases = {
            "Now I can answer: Kestrel Bay is served by Lakeshore Rail [1].": "Kestrel Bay is served by Lakeshore Rail [1].",
            "Great! Now I have all the information. Dogs travel free [5].": "Dogs travel free [5].",
            "Based on the findings, here is what I found:\n- Bikes need a reservation [1].": "- Bikes need a reservation [1].",
            "The specialists found the following: Lifts are at every station [1].": "Lifts are at every station [1].",
        }
        for given, want in cases.items():
            self.assertEqual(agent._without_preamble(given), want)

    def test_answers_refusals_and_questions_are_kept(self):
        for text in ("Now, trains run hourly [1].",
                     "Yes, you can bring your dog [5]. Great news for pet owners.",
                     "I can help with visas. Please ask a travel question.",
                     "I found no information about Highfold. Try another station.",
                     "I have no information on that. Ask about Lakeshore Rail.",
                     "Let me know which station you mean: Merrow or Highfold?",
                     "Now that the line has reopened, trains run every 30 minutes.",
                     "Okay."):
            self.assertEqual(agent._without_preamble(text), text)


class Recorder:
    """Stands in for tracing.observation: records each observation's arguments and updates."""
    def __init__(self):
        self.calls = []

    @contextmanager
    def __call__(self, **kwargs):
        obs = Mock()
        self.calls.append((kwargs, obs))
        yield obs

    def updates(self, i):
        merged = {}
        for call in self.calls[i][1].update.call_args_list:
            merged.update(call.kwargs)
        return merged


class Tracing(unittest.TestCase):
    def test_usage_uses_langfuses_key_names(self):
        import tracing
        inner = Mock()
        tracing._Observation(inner).update(output="x", usage_details={"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(inner.update.call_args.kwargs, {"output": "x", "usage_details": {"input": 12, "output": 3}})

    def test_an_agent_turn_is_a_generation_with_its_reply_tools_and_tokens(self):
        run = agent._Run.__new__(agent._Run)
        run.settings, run.agents, run.session_id, run.user_id, run.limit = {}, set(agent.SPECIALISTS), "s", None, 3
        run.emit = lambda event: None
        run.client = Mock()
        run.client.invoke_harness.return_value = {"stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Searching."}}},
            {"contentBlockStart": {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "t1", "name": "search_knowledge_base"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"query": "York lifts"}'}}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 120, "outputTokens": 15}}},
        ]}
        record = run.new_run.__func__(Mock(lock=threading.Lock(), runs=[]), "knowledge_base", 2)
        recorder = Recorder()
        with patch.object(agent, "observation", recorder), patch.object(agent, "HARNESS_ARN", "arn:x:harness/h"):
            stop, uses = run.invoke(record, [{"role": "user", "content": [{"text": "Find York lifts"}]}])
        (kwargs, _), = recorder.calls
        self.assertEqual((kwargs["name"], kwargs["as_type"]), ("knowledge_base_agent", "generation"))
        self.assertEqual(kwargs["input"]["messages"][0]["content"][0]["text"], "Find York lifts")
        self.assertTrue(kwargs["input"]["system"])
        self.assertEqual(kwargs["metadata"]["delegation"], 2)
        out = recorder.updates(0)
        self.assertEqual(out["output"]["stop_reason"], "tool_use")
        self.assertEqual(out["output"]["text"], "Searching.")
        self.assertEqual(out["output"]["tool_calls"], [{"name": "search_knowledge_base", "input": '{"query": "York lifts"}'}])
        self.assertEqual(out["usage_details"], {"input": 120, "output": 15})

    def test_parallel_tool_calls_stay_in_the_callers_trace(self):
        marker = contextvars.ContextVar("marker", default="lost")
        run = agent._Run.__new__(agent._Run)
        run.role_locks, run.lock, run.runs = {"external_apis": threading.Lock()}, threading.Lock(), []
        run.limit, run.session_id, run.emit = 5, "s", (lambda event: None)
        uses = [{"toolUseId": "a", "name": "get_weather", "input": '{"location": "York"}', "turn": 1},
                {"toolUseId": "b", "name": "get_weather", "input": '{"location": "Leeds"}', "turn": 1}]
        replies = iter([("tool_use", uses), ("end_turn", [])])
        seen = []
        def call(use, n):
            seen.append(marker.get())
            return agent._result(use, "no such place", error=use["toolUseId"] == "b")
        recorder = Recorder()
        with patch.object(run, "invoke", side_effect=lambda record, messages: next(replies)), \
                patch.object(run, "_call", side_effect=call), patch.object(agent, "observation", recorder):
            marker.set("caller")
            run.specialist("external_apis", "Weather in York and Leeds", 1)
        self.assertEqual(seen, ["caller", "caller"])
        tools = {c[0]["input"]["location"]: i for i, c in enumerate(recorder.calls) if c[0]["as_type"] == "tool"}
        self.assertEqual(set(tools), {"York", "Leeds"})
        self.assertNotIn("level", recorder.updates(tools["York"]))
        self.assertEqual(recorder.updates(tools["Leeds"])["level"], "ERROR")
        self.assertEqual(recorder.updates(tools["Leeds"])["output"], "no such place")

    def test_a_run_reports_the_pages_it_found_and_cited_and_its_live_tools(self):
        run = agent._Run("s", "u", 6, lambda event: None, lambda search: search)
        run.searches = [{"n": 1, "first": 1, "seconds": 0.1, "query": "q", "retrieval": {"chunks": [
            {"source_url": "https://a.example/one", "text": "x"}, {"source_url": "https://a.example/two", "text": "y"}]}}]
        run.calls = [{"n": 1, "tool": "get_weather", "seconds": 0.2}]
        graph = Mock()
        graph.invoke.return_value = {"final_answer": "Yes [2].", "answer": "Yes [2].",
                                     "output_guardrail": {"decision": "ALLOW"}, "findings": [], "round": 1}
        with patch.object(agent, "observation", Recorder()), patch.object(agent.agent_memory, "learn"):
            agent._drive(str(uuid.uuid4()), run, {}, "q", {}, time.time(), graph=graph)
        self.assertEqual((run.metrics["retrieved"], run.metrics["cited"], run.metrics["live_tools"]),
                         (["https://a.example/one", "https://a.example/two"], ["https://a.example/two"],
                          ["get_weather"]))

    def test_the_verdicts_become_trace_scores(self):
        scored = []
        final = {"output_guardrail": {"decision": "ALLOW"},
                 "answer_evaluation": {"passed": False, "overall": 0.62, "failed_on": ["faithfulness"],
                                       "answer_type": "answer",
                                       "scores": {"correctness": 0.8, "faithfulness": 0.6}}}
        internal = {"evaluations": [{"delegation": 1, "attempt": 1, "decision": "RETRIEVAL_FAILURE", "overall_confidence": 0.41}]}
        with patch.object(agent, "score", side_effect=lambda *a, **k: scored.append(a)):
            agent._score_run("t1", final, internal)
        got = {(name, value) for _, name, value, *rest in scored}
        self.assertTrue({("output_guardrail", "ALLOW"), ("answer_check", 0), ("answer_overall", 0.62),
                         ("answer_faithfulness", 0.6), ("evidence_decision", "RETRIEVAL_FAILURE"),
                         ("evidence_confidence", 0.41)} <= got)
        self.assertTrue(all(trace == "t1" for trace, *_ in scored))

    def test_a_question_is_one_trace_with_its_input_check_scored(self):
        recorder, scored, tagged = Recorder(), [], []
        verdict = guardrails.Verdict(decision="BLOCK", reason="out of scope", stage="input", message="no")
        with patch.object(agent, "observation", recorder), patch.object(guardrails, "check", return_value=verdict), \
                patch.object(agent, "current_trace_id", return_value="t9"), patch.object(agent, "_record_run"), \
                patch.object(agent, "tag_current_trace", side_effect=lambda **k: tagged.append(k)), \
                patch.object(agent, "score", side_effect=lambda *a, **k: scored.append(a)):
            result = agent.answer("Write a poem", "s", "u", None, lambda e: None, Mock(), source="playground")
        self.assertEqual(result["answer"], "no")
        self.assertEqual((recorder.calls[0][0]["name"], recorder.calls[0][0]["input"]), ("question", {"question": "Write a poem"}))
        self.assertEqual(recorder.updates(0)["output"]["answer"], "no")
        self.assertEqual((tagged[0]["session_id"], tagged[0]["user_id"]), ("s", "u"))
        self.assertIn("source:playground", tagged[0]["tags"])
        self.assertEqual(scored[0][:3], ("t9", "input_guardrail", "BLOCK"))


if __name__ == "__main__":
    unittest.main()

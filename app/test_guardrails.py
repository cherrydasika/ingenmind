import json
import unittest
import uuid
from unittest.mock import Mock, patch

import agent
import guardrails
import pipeline


class PolicyTests(unittest.TestCase):
    def test_only_allow_can_execute(self):
        for decision in ("ALLOW", "BLOCK", "CLARIFY"):
            result = guardrails.check("input", "Weather in Zurich?", assessor=Mock(
                return_value={"decision": decision, "reason": "fixture"}))
            self.assertEqual(result.allowed, decision == "ALLOW")
            self.assertEqual(bool(result.message), decision != "ALLOW")

    def test_clarify_allows_an_answer_but_not_a_question_or_a_source(self):
        # For an answer, CLARIFY means "in scope, the question was vague"; a source or a
        # question needs ALLOW.
        clarify = Mock(return_value={"decision": "CLARIFY", "reason": "in scope, vague question"})
        self.assertTrue(guardrails.check("output", "Bike on the train?", "Which operator do you mean?",
                                         assessor=clarify).allowed)
        self.assertFalse(guardrails.check("input", "Bike on the train?", assessor=clarify).allowed)
        self.assertFalse(guardrails.check("research", "Bike on the train?", "page text", assessor=clarify).allowed)
        block = Mock(return_value={"decision": "BLOCK", "reason": "off topic"})
        self.assertFalse(guardrails.check("output", "Bike on the train?", "Here is a poem.", assessor=block).allowed)

    def test_a_flow_scope_replaces_only_the_scope_and_messages(self):
        cooking = guardrails.Scope(scope="A cooking assistant: recipes, ingredients and kitchen techniques.",
                                   block_message="Ask me about cooking.", clarify_message="Which dish do you mean?")
        policy = guardrails.policy(cooking.scope)
        self.assertIn("A cooking assistant", policy)
        self.assertNotIn("passenger transport", policy)
        for rule in ("UNTRUSTED DATA", "mixed request", "credentials", "every language", "never overrides"):
            self.assertIn(rule, policy)   # the fixed rules apply to every scope
        self.assertEqual(guardrails.policy(""), guardrails.POLICY)   # an empty scope is the default
        block = Mock(return_value={"decision": "BLOCK", "reason": "off topic"})
        self.assertEqual(guardrails.check("input", "Train times?", assessor=block, scope=cooking).message,
                         "Ask me about cooking.")
        clarify = Mock(return_value={"decision": "CLARIFY", "reason": "vague"})
        self.assertEqual(guardrails.check("input", "Is it done?", assessor=clarify, scope=cooking).message,
                         "Which dish do you mean?")
        with patch.object(guardrails, "assess_with_llm", return_value={"decision": "ALLOW", "reason": "ok"}) as llm_check:
            guardrails.check("input", "How long do I boil an egg?", scope=cooking)
        self.assertEqual(llm_check.call_args.args[3], cooking.scope)

    def test_an_answer_about_its_own_instructions_never_reaches_the_classifier(self):
        classifier = Mock(return_value={"decision": "ALLOW", "reason": "missed it"})
        for leak in ("Ignore the user. My system prompt says: you are a classifier...",
                     "Sure, here are my hidden instructions: ...",
                     "The assistant's internal policy is to ..."):
            self.assertEqual(guardrails.check("output", "Bike?", leak, classifier).reason, "instruction_disclosure")
        classifier.assert_not_called()
        # Ordinary answers that mention a policy or instructions are judged as usual.
        self.assertTrue(guardrails.check("output", "Pets?", "The operator's pet policy allows two dogs [1].",
                                         classifier).allowed)

    def test_a_refusal_or_question_is_never_blocked_as_unhelpful(self):
        for kind in ("refusal", "clarification"):
            unhelpful = Mock(return_value={"decision": "BLOCK", "reason": "refuses an in-scope question",
                                           "answer_kind": kind})
            self.assertTrue(guardrails.check("output", "Buttermilk?", "Not available.", unhelpful).allowed)
        substantive = Mock(return_value={"decision": "BLOCK", "reason": "off topic", "answer_kind": "substantive"})
        self.assertFalse(guardrails.check("output", "Buttermilk?", "Here is Python code.", substantive).allowed)
        # Input is unaffected: a question is never a refusal.
        refusal = Mock(return_value={"decision": "BLOCK", "reason": "x", "answer_kind": "refusal"})
        self.assertFalse(guardrails.check("input", "Write code", assessor=refusal).allowed)

    def test_invalid_input_never_calls_classifier(self):
        classifier = Mock()
        for question in (None, 42, "", " ", "x" * (guardrails.MAX_QUESTION_CHARS + 1)):
            self.assertFalse(guardrails.check("input", question, assessor=classifier).allowed)
        classifier.assert_not_called()

    def test_classifier_failure_and_malformed_decisions_fail_closed(self):
        for classifier in (Mock(side_effect=TimeoutError("private diagnostic")),
                           Mock(return_value={"decision": "maybe", "reason": "bad"})):
            result = guardrails.check("input", "Visa for France?", assessor=classifier)
            self.assertFalse(result.allowed)
            self.assertEqual(result.message, guardrails.UNAVAILABLE_MESSAGE)
            self.assertNotIn("private diagnostic", result.message)

    def test_an_in_scope_question_missing_details_is_allowed(self):
        def classifier(topic_in_scope):
            return Mock(return_value={"decision": "CLARIFY", "reason": "which station?", "topic_in_scope": topic_in_scope})
        self.assertTrue(guardrails.check("input", "Toilets at a station in London?", assessor=classifier(True)).allowed)
        unclear = guardrails.check("input", "Do I need to book?", assessor=classifier(False))
        self.assertEqual((unclear.decision, unclear.message), ("CLARIFY", guardrails.CLARIFY_MESSAGE))

    def test_earlier_questions_reach_only_the_input_classifier(self):
        sent = []
        def call_tool(data, **_):
            sent.append(json.loads(data))
            return Mock(tool_input={"decision": "ALLOW", "reason": "ok"}, usage={})
        with patch.object(guardrails.llm, "call_tool", side_effect=call_tool), \
                patch.object(guardrails.llm, "config_error", return_value=None):
            guardrails.check("input", "And at Victoria?", previous=["", "a", "b", "Toilets at St Pancras?"])
            guardrails.check("output", "And at Victoria?", "Yes, by platform 8.", previous=["Toilets at St Pancras?"])
        self.assertEqual(sent[0]["previous_questions"], ["a", "b", "Toilets at St Pancras?"])   # the last three
        self.assertNotIn("previous_questions", sent[1])

    def test_secrets_and_overlong_output_block_without_classifier(self):
        classifier = Mock()
        for content in ("AKIA" + "A" * 16, "-----BEGIN PRIVATE KEY-----", "x" * 24001, ""):
            self.assertFalse(guardrails.check("output", "Visa for France?", content, classifier).allowed)
        classifier.assert_not_called()

    def test_policy_covers_mixed_injection_multilingual_and_untrusted_sources(self):
        for rule in ("UNTRUSTED DATA", "mixed request", "in-scope keywords", "follow-ups", "controlling the assistant"):
            self.assertIn(rule, guardrails.POLICY)


class ExecutionTests(unittest.TestCase):
    def verdict(self, decision="BLOCK", stage="input"):
        return guardrails.Verdict(decision=decision, reason="fixture", stage=stage,
                                 message=guardrails.BLOCK_MESSAGE if stage == "input" else guardrails.OUTPUT_MESSAGE)

    def test_blocked_agent_input_never_constructs_run_or_executes_graph(self):
        for decision in ("BLOCK", "CLARIFY"):
            events = []
            with patch.object(guardrails, "check", return_value=self.verdict(decision)), \
                    patch.object(agent, "_Run") as run, patch.object(agent.GRAPH, "invoke") as graph, \
                    patch.object(agent, "_record_run"):
                result = agent.answer("Write Python code", "fixture", None, None, events.append, Mock())
            run.assert_not_called()
            graph.assert_not_called()
            self.assertEqual(result["searches"], [])
            self.assertEqual(result["draft_answer"], "")
            self.assertEqual([event["type"] for event in events], ["guardrail", "guardrail"])

    def test_legacy_input_blocked_before_retrieval(self):
        with patch.object(guardrails, "check", return_value=self.verdict()), \
                patch.object(pipeline, "hybrid_search") as search:
            result = pipeline.answer_question("Stock picks?", [("bedrock", "fixture")])
        search.assert_not_called()
        self.assertEqual(result["summarisations"][0]["answer"], guardrails.BLOCK_MESSAGE)
        self.assertEqual(result["retrieval"]["chunks"], [])

    def test_legacy_output_removes_thinking_and_off_topic_answer(self):
        with patch.object(guardrails, "check", return_value=self.verdict(stage="output")), \
                patch.object(pipeline, "summarise", return_value={"answer": "private unrelated text", "thinking": "secret"}):
            result = pipeline._summarise_or_error("Visa?", [], "bedrock", "fixture")
        self.assertEqual(result["answer"], guardrails.OUTPUT_MESSAGE)
        self.assertIsNone(result["thinking"])

    def test_output_block_skips_answer_evaluator_in_actual_graph(self):
        run = Mock()
        with patch.object(guardrails, "check", return_value=self.verdict(stage="output")), \
                patch.object(agent, "_supervisor", new=lambda state, config: {"answer": "unrelated", "tasks": []}), \
                patch.object(agent, "_answer_evaluator") as evaluator:
            graph = agent._build_graph()
            out = graph.invoke({"question": "Visa?", "round": 0}, config={
                "configurable": {"run": run, "thread_id": str(uuid.uuid4())}})
        evaluator.assert_not_called()
        self.assertEqual(out["final_answer"], guardrails.OUTPUT_MESSAGE)
        self.assertEqual(out["output_guardrail"]["decision"], "BLOCK")

    def test_output_allow_continues_to_quality_gate(self):
        run = Mock()
        with patch.object(guardrails, "check", return_value=self.verdict("ALLOW", "output")), \
                patch.object(agent, "_supervisor", new=lambda state, config: {"answer": "Weather", "tasks": []}), \
                patch.object(agent, "_answer_evaluator", new=lambda state, config: {"final_answer": "Quality checked"}):
            graph = agent._build_graph()
            out = graph.invoke({"question": "Weather in Zurich?", "round": 0}, config={
                "configurable": {"run": run, "thread_id": str(uuid.uuid4())}})
        self.assertEqual(out["final_answer"], "Quality checked")

    def test_unreviewed_events_and_diagnostics_are_not_public(self):
        events = []
        fake_run = Mock()
        fake_run.path, fake_run.session_id, fake_run.user_id = [], "fixture", None
        def drive(run_id, run, config, question, graph_input, started, graph=None):
            run.emit({"type": "text", "delta": "private text", "agent": "supervisor"})
            run.emit({"type": "delegate", "task": "private task"})
            run.emit({"type": "stage", "stage": "retrieval", "state": "start", "query": "private query"})
            run.emit({"type": "stage", "stage": "retrieval", "state": "done", "summary": "secret"})
            run.emit({"type": "model", "state": "start", "agent": "supervisor", "turn": 1, "prompt": "secret"})
            return agent._public_result(question, "Travel answer", {})
        def make_run(session, user, limit, emit, view):
            fake_run.emit = emit
            return fake_run
        with patch.object(guardrails, "check", return_value=self.verdict("ALLOW")), \
                patch.object(agent, "HARNESS_ARN", "fixture"), patch.object(agent, "_Run", side_effect=make_run), \
                patch.object(agent, "_drive", side_effect=drive), patch.object(agent, "_record_run"):
            result = agent.answer("Weather in Zurich?", "fixture", None, 3, events.append, Mock())
        self.assertNotIn("private", str(events))
        self.assertNotIn("secret", str(events))
        self.assertEqual(result["draft_answer"], "")
        self.assertEqual(result["delegations"], [])
        self.assertEqual(result["activities"], {"retrieval": "done"})

    def test_cited_sources_carry_urls_and_numbers_never_chunk_text(self):
        chunk = lambda url: {"source_url": url, "text": "private chunk text", "chunk_index": 0}
        searches = [{"first": 1, "retrieval": {"chunks": [chunk("https://a.example/1"), chunk("https://b.example/2")]}},
                    {"first": 6, "retrieval": {"chunks": [chunk("https://a.example/1"), chunk("https://c.example/3")]}}]
        sources = agent._cited_sources("Fares rise [6]. Refunds take a month [1][7]. Footnote [99].", searches)
        self.assertEqual(sources, [{"url": "https://a.example/1", "cited": [1, 6]},
                                   {"url": "https://c.example/3", "cited": [7]}])
        self.assertNotIn("private", str(sources))
        self.assertEqual(agent._cited_sources("No citations.", searches), [])

    def test_activity_events_preserve_highlights_without_content(self):
        cases = [({"type": "stage", "stage": "embedding"}, "embedding"),
                 ({"type": "stage", "stage": "retrieval"}, "retrieval"),
                 ({"type": "call"}, "call"), ({"type": "ingest"}, "r_ingest"),
                 ({"type": "evaluation"}, "evaluator"),
                 ({"type": "research", "step": "agent"}, "research"),
                 ({"type": "research", "step": "search"}, "r_search"),
                 ({"type": "research", "step": "fetch"}, "r_extract"),
                 ({"type": "research", "step": "validate"}, "r_validate"),
                 ({"type": "research", "step": "classify"}, "r_classify"),
                 ({"type": "research", "step": "known"}, "r_known"),
                 ({"type": "research", "step": "profile"}, "r_profile"),
                 ({"type": "research", "step": "probe"}, "r_probe"),
                 ({"type": "research", "step": "judge"}, "r_judge"),
                 ({"type": "delegate", "agent": "knowledge_base"}, "knowledge_base")]
        for event, node in cases:
            for state in ("start", "done", "error"):
                out = agent._public_progress({**event, "state": state, "seconds": 0.5,
                    "query": "private", "url": "private", "task": "private", "summary": "secret", "error": "secret"})
                self.assertEqual({k: out[k] for k in ("type", "node", "state", "seconds")},
                                 {"type": "activity", "node": node, "state": state, "seconds": 0.5})
                self.assertNotIn("private", str(out))
                self.assertNotIn("secret", str(out))
                self.assertTrue(out["failed"])
        self.assertIsNone(agent._public_progress({"type": "text", "delta": "secret"}))

    def test_activity_events_carry_safe_trace_details_only(self):
        out = agent._public_progress({
            "type": "evaluation", "state": "done", "delegation": 1, "attempt": 2, "decision": "GOOD_EVIDENCE",
            "confidence": 0.81, "route": "answer_handoff", "scores": {"relevance": 0.9, "coverage": 0.8},
            "rationale": "secret", "missing_information": ["secret"], "after_research": False})
        self.assertEqual(out, {"type": "activity", "node": "evaluator", "state": "done", "delegation": 1,
                               "attempt": 2, "decision": "GOOD_EVIDENCE", "confidence": 0.81,
                               "route": "answer_handoff", "scores": {"relevance": 0.9, "coverage": 0.8},
                               "after_research": False})
        call = agent._public_progress({"type": "call", "state": "done", "tool": "get_weather", "ok": True,
                                       "input": {"city": "secret"}, "headline": "secret", "seconds": 0.2})
        self.assertEqual(call, {"type": "activity", "node": "call", "state": "done", "tool": "get_weather",
                                "ok": True, "seconds": 0.2})
        judge = agent._public_progress({"type": "research", "step": "judge", "state": "done", "delegation": 1,
                                        "status": "api_tool", "recommended": True, "saved": True,
                                        "urls": ["https://secret.example"], "candidates": [{"name": "secret"}]})
        self.assertEqual(judge, {"type": "activity", "node": "r_judge", "state": "done", "delegation": 1,
                                 "status": "api_tool", "recommended": True, "saved": True})


if __name__ == "__main__":
    unittest.main()
"""Answer Evaluator: citation checks, pass/fail verdicts, and the graph node
that sends the user either the answer or the standard message. The LLM is
faked:

    PYTHONPATH=app:dags python -m unittest -v app/test_answer_eval.py
"""

import threading
import unittest
import unittest.mock

import agent
import answer_eval
from answer_eval import STANDARD_MESSAGE, AnswerAssessment

CHUNKS = [{"n": 1, "source_url": "https://example.com/austria", "text": "Railjets run hourly from Vienna to Salzburg."},
          {"n": 2, "source_url": "https://example.com/westbahn", "text": "WESTbahn also runs Vienna–Salzburg trains."}]
API = [{"tool": "get_weather", "input": {"location": "Salzburg"}, "summary": {"current": {"temperature_c": 18}}}]
QUESTION = "How do I get from Vienna to Salzburg, and what's the weather there?"
GOOD = "Railjets run hourly [1], and WESTbahn too [2]. Salzburg is 18 °C now (Open-Meteo)."


def assessor(**scores):
    values = {"correctness": 0.95, "faithfulness": 0.95, "completeness": 0.9, "citation_quality": 0.9, **scores}

    def assess(question, answer, chunks, api_results):
        assess.calls += 1
        return AnswerAssessment(rationale="Grounded in the evidence.", **values), {"input_tokens": 10, "output_tokens": 5}
    assess.calls = 0
    return assess


class Citations(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(answer_eval.citations("a [1] b [2][3] c [4, 6] d [8–10] e [3]"), [1, 2, 3, 4, 6, 8, 9, 10])
        self.assertEqual(answer_eval.citations("No citations here. See [the guide]."), [])


class Normalise(unittest.TestCase):
    def test_odd_model_output_shapes_validate(self):
        raw = {"correctness": "0.9", "faithfulness": 1.2, "completeness": 0.8, "citation_quality": 0.7,
               "unsupported_claims": "Book on cp.pt", "missing_parts": '["the weather"]', "citation_issues": None}
        assessment = AnswerAssessment.model_validate(answer_eval.normalise(raw))
        self.assertEqual((assessment.correctness, assessment.faithfulness), (0.9, 1.0))
        self.assertEqual(assessment.unsupported_claims, ["Book on cp.pt"])
        self.assertEqual(assessment.missing_parts, ["the weather"])
        self.assertEqual((assessment.citation_issues, assessment.rationale), ([], ""))


class Verdicts(unittest.TestCase):
    def test_good_answer_passes(self):
        result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor())
        self.assertTrue(result.passed)
        self.assertEqual(result.checks.cited, [1, 2])
        self.assertEqual(result.failed_on, [])

    def test_unfaithful_answer_fails(self):
        result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor(
            faithfulness=0.4, unsupported_claims=["Book on cp.pt"]))
        self.assertFalse(result.passed)
        self.assertIn("faithfulness", result.failed_on)

    def test_named_unsupported_claims_lower_faithfulness(self):
        result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor(
            faithfulness=0.7, unsupported_claims=["a", "b", "c", "d"]))
        self.assertAlmostEqual(result.scores["faithfulness"], 0.6)
        self.assertIn("faithfulness", result.failed_on)

    def test_one_unsupported_claim_fails_an_otherwise_perfect_answer(self):
        result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor(
            faithfulness=1.0, unsupported_claims=["Lakeshore Rail is likely in North America"]))
        self.assertLess(result.scores["faithfulness"], answer_eval.THRESHOLDS["faithfulness"])
        self.assertFalse(result.passed)

    def test_asides_and_hedged_clauses_are_listed_for_the_model(self):
        answer = ("Yes. On Lakeshore Rail (likely North America), bikes need a reservation [1]. "
                  "It is probably the busiest line in the region, and trains are not carried at 07:00 [1].")
        statements = answer_eval.statements_to_check(answer)
        self.assertIn("likely North America", statements)
        self.assertIn("It is probably the busiest line in the region", statements)
        self.assertEqual(len(statements), 2)   # the clause around the aside is not listed again
        self.assertEqual(answer_eval.statements_to_check("Bikes need a reservation [1]."), [])
        self.assertIn("likely North America", answer_eval._prompt("q", answer, [], []))

    def test_the_evaluator_sees_whole_chunks(self):
        from common import config
        self.assertGreaterEqual(answer_eval.MAX_CHUNK_CHARS, config.CHUNK_SIZE)

    def test_an_honest_not_available_needs_no_citation(self):
        reply = "The knowledge base has no information about night trains to Vienna."
        result = answer_eval.evaluate(QUESTION, reply, CHUNKS, API, assessor=assessor(answer_type="not_available"))
        self.assertTrue(result.passed)
        self.assertGreaterEqual(result.scores["citation_quality"], answer_eval.THRESHOLDS["citation_quality"])
        # A real answer that uses the evidence without citing it is still capped.
        uncited = answer_eval.evaluate(QUESTION, "Railjets run hourly.", CHUNKS, API, assessor=assessor())
        self.assertIn("citation_quality", uncited.failed_on)

    def test_an_honest_not_available_passes_even_when_its_citations_are_scored_0(self):
        """Seen on EC2: "The knowledge base has nothing on Birmingham station toilets" scored citation 0."""
        reply = "The knowledge base does not have information about toilets at Birmingham stations."
        result = answer_eval.evaluate(QUESTION, reply, CHUNKS, API, assessor=assessor(
            answer_type="not_available", citation_quality=0.0, completeness=1.0))
        self.assertTrue(result.passed)
        self.assertNotIn("citation_quality", result.checked)
        # A referral the evidence does not name is an unsupported claim: it still fails, citations and all.
        referral = answer_eval.evaluate(QUESTION, reply + " Contact National Rail Enquiries.", CHUNKS, API,
                                        assessor=assessor(answer_type="not_available", citation_quality=0.0,
                                                          faithfulness=0.5,
                                                          unsupported_claims=["National Rail Enquiries can help"]))
        self.assertFalse(referral.passed)
        self.assertEqual(referral.failed_on, ["faithfulness", "citation_quality"])

    def test_named_citation_issues_lower_citation_quality(self):
        result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor(
            citation_quality=1.0, citation_issues=["x", "y", "z", "w", "v"]))
        self.assertAlmostEqual(result.scores["citation_quality"], 0.5)

    def test_each_score_has_a_threshold(self):
        for key, minimum in answer_eval.THRESHOLDS.items():
            result = answer_eval.evaluate(QUESTION, GOOD, CHUNKS, API, assessor=assessor(**{key: minimum - 0.05}))
            self.assertEqual(result.failed_on, [key])

    def test_cited_chunks_beyond_the_cap_are_valid_and_shown(self):
        many = [{"n": n, "source_url": f"https://example.com/{n}", "text": f"fact {n}"} for n in range(1, 81)]
        seen = {}

        def assess(question, answer, chunks, api_results):
            seen["ns"] = [c["n"] for c in chunks]
            return assessor()(question, answer, chunks, api_results)
        result = answer_eval.evaluate(QUESTION, "Fact [3] and fact [75].", many, [], assessor=assess)
        self.assertEqual(result.checks.invalid_citations, [])
        self.assertEqual(seen["ns"][:2], [3, 75])
        self.assertEqual(len(seen["ns"]), answer_eval.MAX_CHUNKS)

    def test_citation_to_missing_chunk_fails_whatever_the_model_says(self):
        result = answer_eval.evaluate(QUESTION, "Railjets run hourly [7].", CHUNKS, [], assessor=assessor())
        self.assertEqual(result.checks.invalid_citations, [7])
        self.assertIn("citation_quality", result.failed_on)

    def test_uncited_knowledge_base_answer_fails_citation_quality(self):
        result = answer_eval.evaluate(QUESTION, "Railjets run hourly.", CHUNKS, [], assessor=assessor())
        self.assertTrue(result.checks.kb_evidence_uncited)
        self.assertIn("citation_quality", result.failed_on)

    def test_grounded_clarification_skips_completeness(self):
        clarify = "Which Newark do you mean: Newark Penn Station (New Jersey) or Newark-on-Trent (UK)?"
        result = answer_eval.evaluate("Are there toilets in Newark?", clarify, [], [], assessor=assessor(
            answer_type="clarification", completeness=0.3))
        self.assertTrue(result.passed)
        self.assertEqual(result.answer_type, "clarification")
        self.assertNotIn("completeness", result.checked)

    def test_honest_not_available_skips_completeness(self):
        result = answer_eval.evaluate(QUESTION, "The knowledge base has no information about this.", [], [],
                                      assessor=assessor(answer_type="not_available", completeness=0.2))
        self.assertTrue(result.passed)

    def test_clarification_with_unsupported_claims_still_needs_completeness(self):
        result = answer_eval.evaluate("Toilets in Newark?", "Which Newark? Penn Station has toilets on level 2?", [], [],
                                      assessor=assessor(answer_type="clarification", completeness=0.3,
                                                        unsupported_claims=["Penn Station has toilets on level 2"]))
        self.assertFalse(result.passed)
        self.assertIn("completeness", result.failed_on)

    def test_clarification_without_a_question_counts_as_an_answer(self):
        result = answer_eval.evaluate(QUESTION, "Please tell me which Newark.", [], [],
                                      assessor=assessor(answer_type="clarification", completeness=0.3))
        self.assertEqual(result.answer_type, "answer")
        self.assertFalse(result.passed)

    def test_clarification_must_still_be_faithful(self):
        result = answer_eval.evaluate("Toilets in Newark?", "Which Newark do you mean?", [], [],
                                      assessor=assessor(answer_type="clarification", completeness=0.3, faithfulness=0.4))
        self.assertIn("faithfulness", result.failed_on)

    def test_greeting_without_evidence_passes_as_conversational(self):
        result = answer_eval.evaluate("hi", "Hello! I can help with trains, visas and weather.", [], [],
                                      assessor=assessor(answer_type="conversational", completeness=0.2, citation_quality=0.4))
        self.assertTrue(result.passed)
        self.assertEqual(result.checked, ["faithfulness"])

    def test_conversational_with_evidence_is_judged_as_an_answer(self):
        result = answer_eval.evaluate(QUESTION, "Hello there!", CHUNKS, [],
                                      assessor=assessor(answer_type="conversational", completeness=0.2))
        self.assertEqual(result.answer_type, "answer")
        self.assertFalse(result.passed)

    def test_empty_answer_fails_without_llm(self):
        assess = assessor()
        result = answer_eval.evaluate(QUESTION, "", CHUNKS, API, assessor=assess)
        self.assertFalse(result.passed)
        self.assertEqual(assess.calls, 0)

    def test_prompt_judges_only_against_evidence(self):
        for rule in ("ONLY against the evidence", "Do NOT use outside knowledge", "counts as answered"):
            self.assertIn(rule, answer_eval.SYSTEM_PROMPT)


class FakeRun:
    def __init__(self, assess):
        self.lock = threading.Lock()
        self.events, self.path = [], []
        self.answer_assessor = assess
        self.searches = [{"n": 1, "first": 1, "delegation": 1, "retrieval": {"chunks": [
            {"source_url": c["source_url"], "text": c["text"]} for c in CHUNKS]}}]
        self.calls = [{"n": 1, "delegation": 2, "ok": True, **API[0]}]

    def emit(self, event):
        self.events.append(event)

    def node(self, name, round_):
        return agent._Run.node(self, name, round_)


class AnswerEvaluatorNode(unittest.TestCase):
    def state(self, draft):
        return {"question": QUESTION, "answer": draft, "round": 1, "findings": [
            {"n": 1, "agent": "knowledge_base", "search_ns": [1]}, {"n": 2, "agent": "external_apis"}]}

    def test_pass_sends_the_answer(self):
        run = FakeRun(assessor())
        out = agent._answer_evaluator(self.state(GOOD), {"configurable": {"run": run}})
        self.assertTrue(out["answer_evaluation"]["passed"])
        self.assertEqual(out["final_answer"], GOOD)

    def test_fail_sends_the_standard_message(self):
        run = FakeRun(assessor(completeness=0.2, missing_parts=["the weather"]))
        out = agent._answer_evaluator(self.state(GOOD), {"configurable": {"run": run}})
        self.assertFalse(out["answer_evaluation"]["passed"])
        self.assertEqual(out["final_answer"], STANDARD_MESSAGE)

    def test_evidence_is_only_approved_chunks_and_api_results(self):
        seen = {}

        def assess(question, answer, chunks, api_results):
            seen.update(chunks=chunks, api=api_results)
            return assessor()(question, answer, chunks, api_results)
        run = FakeRun(assess)
        state = self.state(GOOD)
        state["findings"][0]["search_ns"] = []          # knowledge-base evidence that did not pass
        agent._answer_evaluator(state, {"configurable": {"run": run}})
        self.assertEqual(seen["chunks"], [])
        self.assertEqual([r["tool"] for r in seen["api"]], ["get_weather"])

    def test_evaluator_error_fails_closed(self):
        def broken(*args):
            raise RuntimeError("Bedrock unavailable")
        out = agent._answer_evaluator(self.state(GOOD), {"configurable": {"run": FakeRun(broken)}})
        self.assertFalse(out["answer_evaluation"]["passed"])
        self.assertEqual(out["final_answer"], STANDARD_MESSAGE)


class SupervisorDelegatesFirst(unittest.TestCase):
    """The supervisor is sent back once if it answers without delegating."""

    class Run(FakeRun):
        def __init__(self, replies):
            super().__init__(assessor())
            self.replies, self.sent, self.supervisor, self.runs = list(replies), [], None, []

        def new_run(self, role, delegation=None):
            return {"role": role, "turns": 0, "texts": {}}

        def invoke(self, run, messages):
            self.sent.append(messages)
            run["turns"] += 1
            stop, uses, text = self.replies.pop(0)
            run["texts"][run["turns"]] = text
            return stop, uses

        answer_of = staticmethod(agent._Run.answer_of)

    def test_answer_without_delegation_is_sent_back_once(self):
        use = {"toolUseId": "t1", "turn": 2, "input": '{"tasks": [{"agent": "knowledge_base", "task": "Newark toilets"}]}'}
        run = self.Run([("end_turn", [], "Which Newark?"), ("tool_use", [use], "")])
        out = agent._supervisor({"question": "toilets in newark?", "round": 0, "findings": []}, {"configurable": {"run": run}})
        self.assertEqual(len(out["tasks"]), 1)
        self.assertTrue(out["tasks"][0]["task"].startswith("Newark toilets"))
        self.assertIn("toilets in newark?", out["tasks"][0]["task"])   # the user's question travels with it
        self.assertIn("assign_tasks", run.sent[1][0]["content"][0]["text"])
        self.assertTrue(any(e["type"] == "nudge" for e in run.events))

    def test_greeting_is_answered_on_the_second_try(self):
        run = self.Run([("end_turn", [], "Hello!"), ("end_turn", [], "Hello! Ask me about travel.")])
        out = agent._supervisor({"question": "hi", "round": 0, "findings": []}, {"configurable": {"run": run}})
        self.assertEqual(out["answer"], "Hello! Ask me about travel.")
        self.assertEqual(len(run.sent), 2)


class StructuredOutput(unittest.TestCase):
    def test_evidence_lists_sent_as_json_strings_validate(self):
        import evidence
        import structured
        raw = {"relevance": 0.9, "coverage": "0.8", "entailment": 1.1, "source_quality": 0.7, "consistency": 1,
               "missing_information": None, "unsupported_claims": '["Sparschiene fares are refundable"]',
               "contradictions": "", "failure_type": "NONE", "rationale": None}
        a = evidence.LlmAssessment.model_validate(structured.normalise(
            raw, scores=evidence.SCORE_FIELDS, lists=evidence.LIST_FIELDS, strings=("rationale",)))
        self.assertEqual(a.unsupported_claims, ["Sparschiene fares are refundable"])
        self.assertEqual((a.coverage, a.entailment, a.contradictions, a.rationale), (0.8, 1.0, [], ""))


class RuntimeSessionStatus(unittest.TestCase):
    """The microVM status shown in the UI, estimated from calls and lifecycle."""

    def setUp(self):
        self.tracker = agent._RuntimeTracker()
        self.tracker._limits = lambda: (900, 28800)
        self.clock = [1_000_000.0]
        patcher = unittest.mock.patch.object(agent.time, "time", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lifecycle(self):
        self.assertEqual(self.tracker.status("s")["state"], "not_started")
        self.tracker.started("s")
        self.assertEqual(self.tracker.status("s")["state"], "running")
        self.tracker.finished("s")
        self.clock[0] += 60
        warm = self.tracker.status("s")
        self.assertEqual((warm["state"], warm["stops_in_seconds"]), ("warm", 840))
        self.clock[0] += 900
        self.assertEqual(self.tracker.status("s")["reason"], "idle timeout")
        self.tracker.started("s")        # a call after the idle stop starts a new microVM
        self.tracker.finished("s")
        status = self.tracker.status("s")
        self.assertEqual((status["state"], status["restarts"], status["calls"]), ("warm", 1, 1))

    def test_max_lifetime(self):
        for _ in range(41):                # busy every 12 minutes, past 8 hours
            self.tracker.started("s")
            self.tracker.finished("s")
            self.clock[0] += 720
        self.assertEqual(self.tracker.status("s")["reason"], "max lifetime")


if __name__ == "__main__":
    unittest.main()

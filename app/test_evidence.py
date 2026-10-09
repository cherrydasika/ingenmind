"""Evidence Evaluator: the knowledge-base subgraph routes each decision to the
right node, and the deterministic checks hold. The retrieval agent and the
LLM assessor are faked, so no AWS call or database is needed:

    PYTHONPATH=app:dags python -m unittest -v app/test_evidence.py
"""

import threading
import unittest
from datetime import datetime, timedelta, timezone

import agent
import evidence
from evidence import Decision, LlmAssessment

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
TASK = {"n": 1, "agent": "knowledge_base", "task": "How do I travel by train from Vienna to Salzburg?",
        "round": 1, "turn": 1}


def chunk(n: int, text: str, source: str, score: float = 0.75, age_days: float = 2) -> dict:
    return {"id": f"c{n}", "text": text, "source_url": source, "chunk_index": n, "score": score,
            "ingested_at": (NOW - timedelta(days=age_days)).isoformat(), "ttl_days": 30}


GOOD_CHUNKS = [
    chunk(1, "ÖBB Railjet trains run hourly from Vienna to Salzburg in about 2h 22m.", "https://example.com/austria"),
    chunk(2, "WESTbahn also runs Vienna–Salzburg trains; tickets can be bought on board.", "https://example.com/westbahn"),
]


def assessment(**overrides) -> LlmAssessment:
    values = dict(relevance=0.95, coverage=0.9, entailment=0.95, source_quality=0.85, consistency=1.0,
                  missing_information=[], unsupported_claims=[], contradictions=[], failure_type="NONE",
                  rationale="The chunks describe Vienna–Salzburg trains.")
    values.update(overrides)
    return LlmAssessment(**values)


class FakeRun:
    """Stands in for agent._Run: a retrieval agent that "finds" the next set
    of chunks on each attempt, an assessor that returns the next
    LlmAssessment, and a query rewriter."""

    def __init__(self, chunks: list[list[dict]], results: list[LlmAssessment]):
        self.lock = threading.Lock()
        self.events, self.evaluations, self.searches, self.rewrites = [], [], [], []
        self.path, self.research, self.web_searches = [], [], []
        self.candidates, self.fetched = {}, {}
        self.validator = None  # set by research tests
        self.chunks, self.results = list(chunks), list(results)
        self.assessor_calls = self.retrieval_calls = self.rewriter_calls = 0
        self.tasks = []

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def specialist(self, role: str, task: str, n: int) -> dict:
        if role == "research":
            # Submits whatever the test set up as fetched candidates.
            self.candidates[n] = [{"url": url, "reason": "covers the gap"} for url in self.fetched]
            return {"answer": "Submitted candidates.", "turns": 3}
        found = self.chunks[min(self.retrieval_calls, len(self.chunks) - 1)]
        self.retrieval_calls += 1
        self.tasks.append(task)
        self.searches.append({"n": len(self.searches) + 1, "delegation": n, "turn": 1, "query": "Vienna Salzburg train",
                              "first": 1, "seconds": 0.1, "retrieval": {"chunks": found, "dense_ranking": found}})
        return {"answer": "Railjets run hourly [1]; WESTbahn too [2].", "turns": 2}

    def assessor(self, task: str, searches: list[dict], draft: str):
        result = self.results[min(self.assessor_calls, len(self.results) - 1)]
        self.assessor_calls += 1
        return result, {"input_tokens": 100, "output_tokens": 20}

    def rewriter(self, task: str, searches: list[dict], evaluation):
        self.rewriter_calls += 1
        return (evidence.QueryRewrite(diagnosis="The query mixed two cities and a vague topic.",
                                      queries=["Vienna Salzburg Railjet", "ÖBB Vienna to Salzburg"]),
                {"input_tokens": 50, "output_tokens": 10})


def run_kb(chunks, results) -> tuple[dict, FakeRun]:
    """chunks: one list per retrieval attempt (or a single list); results:
    one assessment per evaluation (or a single one)."""
    chunks = chunks if chunks and isinstance(chunks[0], list) else [chunks]
    results = results if isinstance(results, list) else [results]
    fake = FakeRun(chunks, results)
    out = agent.KB_GRAPH.invoke({"task": TASK}, config={"configurable": {"run": fake}})
    return {**out, "finding": out["findings"][0]}, fake



class GapType(unittest.TestCase):
    def test_the_gap_type_comes_from_the_assessment(self):
        fake = FakeRun([GOOD_CHUNKS], [assessment(coverage=0.2, failure_type="KNOWLEDGE_GAP", gap_type="data_source")])
        result = evidence.evaluate(TASK["task"], [{"retrieval": {"chunks": GOOD_CHUNKS, "dense_ranking": GOOD_CHUNKS},
                                                   "query": "q", "first": 1}], "", assessor=fake.assessor, now=NOW)
        self.assertEqual(result.gap_type, "data_source")
        self.assertEqual(assessment(gap_type="live feed").gap_type, "static_content")   # unknown → today's path

    def test_no_evidence_leaves_the_gap_type_unjudged(self):
        result = evidence.evaluate(TASK["task"], [], "", assessor=None, now=NOW, attempt=2)
        self.assertEqual((result.decision, result.gap_type), (evidence.Decision.KNOWLEDGE_GAP, None))

class KnowledgeBaseGraphRouting(unittest.TestCase):
    def test_good_evidence_goes_to_answer(self):
        out, _ = run_kb(GOOD_CHUNKS, assessment())
        self.assertEqual(out["evaluation"]["decision"], "GOOD_EVIDENCE")
        self.assertEqual(out["evaluation"]["route"], "answer_handoff")
        self.assertEqual(out["evaluation"]["recommended_action"], "ANSWER")
        self.assertEqual(out["finding"]["answer"], "Railjets run hourly [1]; WESTbahn too [2].")
        self.assertGreaterEqual(out["evaluation"]["overall_confidence"], evidence.GOOD_CONFIDENCE)

    def test_insufficient_evidence_goes_to_placeholder(self):
        out, _ = run_kb(GOOD_CHUNKS, assessment(coverage=0.45, missing_information=["ticket prices"]))
        self.assertEqual(out["evaluation"]["decision"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(out["evaluation"]["route"], "insufficient_evidence_placeholder")
        self.assertEqual(out["finding"]["status"], "Retry / clarification not implemented")
        self.assertIn("ticket prices", out["finding"]["answer"])
        self.assertNotIn("Railjets", out["finding"]["answer"])  # the draft is withheld

    def test_knowledge_gap_goes_to_research(self):
        out, fake = run_kb(GOOD_CHUNKS, assessment(coverage=0.3, failure_type="KNOWLEDGE_GAP",
                                                   missing_information=["night trains"]))
        self.assertEqual(out["evaluation"]["decision"], "KNOWLEDGE_GAP")
        self.assertEqual(out["evaluation"]["route"], "research_agent")
        self.assertEqual(out["evaluation"]["recommended_action"], "RESEARCH")
        # The fake research agent submits nothing: the gap is reported, nothing is ingested.
        self.assertEqual(out["finding"]["status"], "Research found no source that passed validation")
        self.assertIn("night trains", out["finding"]["answer"])
        # What the knowledge base does cover still reaches the supervisor, with the
        # searches the answer evaluator needs to check its citations.
        self.assertIn("Partial findings", out["finding"]["answer"])
        self.assertIn("Railjets run hourly [1]", out["finding"]["answer"])
        self.assertTrue(out["finding"]["search_ns"])

    def test_conflicting_evidence_goes_to_investigation_placeholder(self):
        out, _ = run_kb(GOOD_CHUNKS, assessment(consistency=0.2, contradictions=["[1] says hourly, [2] says every 2 hours"]))
        self.assertEqual(out["evaluation"]["decision"], "CONFLICTING_EVIDENCE")
        self.assertEqual(out["evaluation"]["route"], "investigation_placeholder")
        self.assertIn("[1] says hourly", out["finding"]["answer"])
        self.assertTrue(out["finding"]["status"].startswith("Investigation"))

    def test_empty_retrieval_is_rewritten_and_retried_once(self):
        out, fake = run_kb([[], GOOD_CHUNKS], assessment())
        decisions = [e["decision"] for e in fake.events if e["type"] == "evaluation" and e["state"] == "done"]
        self.assertEqual(decisions, ["RETRIEVAL_FAILURE", "GOOD_EVIDENCE"])
        self.assertEqual((fake.retrieval_calls, fake.rewriter_calls), (2, 1))
        self.assertEqual(fake.assessor_calls, 1)  # the empty first attempt needs no model call
        self.assertIn("Vienna Salzburg Railjet", fake.tasks[1])  # the retry gets the rewritten queries
        self.assertEqual(out["evaluation"]["attempt"], 2)
        self.assertEqual(out["evaluation"]["route"], "answer_handoff")
        self.assertEqual(out["finding"]["answer"], "Railjets run hourly [1]; WESTbahn too [2].")

    def test_retrieval_failure_retries_only_once_then_knowledge_gap(self):
        off_topic = [chunk(1, "Ferry times in Greece.", "https://example.com/greece", score=0.2)]
        miss = assessment(relevance=0.1, coverage=0.1, failure_type="RETRIEVAL_FAILURE")
        out, fake = run_kb([off_topic, off_topic], [miss, miss])
        decisions = [e["decision"] for e in fake.events if e["type"] == "evaluation" and e["state"] == "done"]
        self.assertEqual(decisions, ["RETRIEVAL_FAILURE", "KNOWLEDGE_GAP"])
        self.assertEqual((fake.retrieval_calls, fake.rewriter_calls), (2, 1))
        self.assertEqual(out["evaluation"]["route"], "research_agent")
        self.assertEqual(out["evaluation"]["attempt"], 2)


class EvaluatorChecks(unittest.TestCase):
    def searches(self, chunks):
        return [{"n": 1, "query": "q", "first": 1, "retrieval": {"chunks": chunks, "dense_ranking": chunks}}]

    def test_low_similarity_caps_relevance_to_retrieval_failure(self):
        weak = [chunk(1, "Ferry times in Greece.", "https://example.com/greece", score=0.12)]
        result = evidence.evaluate(TASK["task"], self.searches(weak), "",
                                   assessor=lambda *a: (assessment(), {"input_tokens": 0, "output_tokens": 0}), now=NOW)
        self.assertLess(result.scores.relevance, evidence.LOW)
        self.assertEqual(result.decision, Decision.RETRIEVAL_FAILURE)

    def test_retrieval_failure_on_second_attempt_is_a_knowledge_gap(self):
        weak = [chunk(1, "Belgian train tickets.", "https://example.com/belgium", score=0.45)]
        assessor = lambda *a: (assessment(relevance=0.05, coverage=0.1, failure_type="RETRIEVAL_FAILURE"),
                               {"input_tokens": 0, "output_tokens": 0})
        first = evidence.evaluate("How do I buy train tickets in Portugal?", self.searches(weak), "", assessor=assessor, now=NOW)
        second = evidence.evaluate("How do I buy train tickets in Portugal?", self.searches(weak), "", assessor=assessor,
                                   now=NOW, attempt=2)
        self.assertEqual((first.decision, second.decision), (Decision.RETRIEVAL_FAILURE, Decision.KNOWLEDGE_GAP))

    def test_malformed_tool_output_is_detected(self):
        self.assertTrue(evidence._malformed({"rationale": 'ok</rationale>\n<parameter name="missing_information">["x"]'}))
        self.assertFalse(evidence._malformed({"rationale": "The chunks cover it."}))

    def test_deterministic_checks(self):
        checks = evidence.deterministic_checks(self.searches(
            [chunk(1, "a", "https://a.example", age_days=3), chunk(2, "b", "https://b.example", age_days=27)]), NOW)
        self.assertEqual((checks.chunks, checks.distinct_sources, checks.best_similarity), (2, 2, 0.75))
        self.assertEqual(checks.oldest_ingest_days, 27.0)
        self.assertAlmostEqual(checks.freshness, (0.9 + 0.1) / 2, places=3)

    def test_prompt_forbids_answering_and_outside_knowledge(self):
        for rule in ("Do NOT answer the question", "Do NOT use outside knowledge", "Do NOT guess",
                     "Do NOT treat missing evidence as evidence of absence", "RETRIEVAL_FAILURE", "KNOWLEDGE_GAP"):
            self.assertIn(rule, evidence.SYSTEM_PROMPT)

    def test_assessment_schema_is_bounded(self):
        with self.assertRaises(Exception):
            LlmAssessment(**{**assessment().model_dump(), "coverage": 1.5})


if __name__ == "__main__":
    unittest.main()

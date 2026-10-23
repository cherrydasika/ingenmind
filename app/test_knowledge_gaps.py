"""Knowledge gaps (R54, knowledge_gaps.py): kept per turn for admins, grouped into topics, against the test database."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import identity
import knowledge_gaps
import sessions
import web_api
from common import embedding
from test_auth import AuthDatabase

TOILETS, LIFTS, SEASON = [1.0, 0.0, 0.0], [0.95, 0.3, 0.0], [0.0, 0.0, 1.0]
VECTORS = {"toilets at Victoria": TOILETS, "lifts at Victoria": LIFTS, "monthly season ticket price": SEASON,
           "toilets at Victoria station": TOILETS}


def fake_embed(texts, query=False):
    return [VECTORS[t] for t in texts]


class KnowledgeGaps(AuthDatabase):
    def setUp(self):
        for module in (knowledge_gaps, sessions):
            module.reset_schema_cache()
            self.addCleanup(module.reset_schema_cache)
        knowledge_gaps.ensure_schema()
        with knowledge_gaps._connect() as connection:       # the class shares one database
            connection.execute("DELETE FROM app_session_turns")
        self.user = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        self.session = sessions.current(self.user["user_id"], None)

    def asked(self, question, gaps, answered=False, blocked=False):
        result = {"answer": "a", "guardrails": {"input": {"decision": "BLOCK" if blocked else "ALLOW"}}}
        turn = sessions.record_turn(self.session, self.user["user_id"], question, result)
        knowledge_gaps.record(turn["turn_id"], question, [{"missing": m} for m in gaps], answered)
        return turn

    def period(self):
        now = datetime.now(timezone.utc)
        return now - timedelta(days=1), now + timedelta(days=1)

    def test_the_gaps_of_a_run(self):
        evaluations = [{"decision": "GOOD_EVIDENCE", "missing_information": ["ignored"]},
                       {"decision": "KNOWLEDGE_GAP", "missing_information": ["Toilets at Victoria", "toilets at victoria "]},
                       {"decision": "KNOWLEDGE_GAP", "missing_information": [], "after_research": True}]
        self.assertEqual(knowledge_gaps.of_run(evaluations), [{"missing": "Toilets at Victoria", "after_research": False},
                                                              {"missing": "", "after_research": True}])

    def test_topics_counts_and_examples(self):
        self.asked("Are there toilets at Victoria?", ["toilets at Victoria"])
        self.asked("Is Victoria step-free?", ["lifts at Victoria"], answered=True)
        self.asked("Where are the loos at Victoria station?", ["toilets at Victoria station"])
        self.asked("How much is a monthly ticket to Leeds?", ["monthly season ticket price"])
        self.asked("Toilets at Victoria?", ["toilets at Victoria"], blocked=True)        # off-topic: not a gap
        with patch.object(embedding, "embed_texts", side_effect=fake_embed) as embed:
            report = knowledge_gaps.report(*self.period())
            knowledge_gaps.report(*self.period())
        self.assertEqual(embed.call_count, 1)                      # embeddings are kept once made
        self.assertEqual((report["questions"], report["grouped_by"]), (4, "meaning"))
        [stations, season] = report["topics"]
        self.assertEqual((stations["questions"], stations["unanswered"]), (3, 2))
        self.assertEqual(len(stations["examples"]), 3)
        self.assertIn("toilets", stations["topic"])
        self.assertEqual((season["topic"], season["examples"]), ("monthly season ticket price",
                                                                 ["How much is a monthly ticket to Leeds?"]))
        self.assertEqual(knowledge_gaps.report(datetime(2020, 1, 1, tzinfo=timezone.utc),
                                               datetime(2020, 2, 1, tzinfo=timezone.utc))["topics"], [])

    def test_without_embeddings_identical_items_still_group(self):
        self.asked("Toilets?", ["toilets at Victoria"])
        self.asked("Loos?", ["Toilets at Victoria"])
        self.asked("Lifts?", ["lifts at Victoria"])
        with patch.object(embedding, "embed_texts", side_effect=RuntimeError("no model")):
            report = knowledge_gaps.report(*self.period())
        self.assertEqual((report["grouped_by"], [t["questions"] for t in report["topics"]]), ("text", [2, 1]))

    def test_gaps_go_with_their_session(self):
        self.asked("Toilets?", ["toilets at Victoria"])
        with knowledge_gaps._connect() as connection:
            connection.execute("DELETE FROM app_sessions WHERE session_id = %s", (self.session,))
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM knowledge_gaps").fetchone()["n"], 0)

    def test_kept_from_the_ask_path_but_never_sent_to_the_browser(self):
        result = {"answer": "Not in the knowledge base.", "searches": [], "guardrails": {"output": {"decision": "ALLOW"}},
                  "answer_check": {"passed": False}, "gaps": [{"missing": "toilets at Victoria", "after_research": True}],
                  "question": "Toilets at Victoria?"}
        with patch.object(web_api.agent, "answer", return_value=result):
            shaped = web_api._agent_payload("Toilets at Victoria?", self.session,
                                            {"user_id": self.user["user_id"]}, None, lambda *a: None, record=True)
        self.assertNotIn("gaps", shaped)
        with knowledge_gaps._connect() as connection:
            row = connection.execute("SELECT missing, after_research, answered FROM knowledge_gaps").fetchone()
        self.assertEqual(row, {"missing": "toilets at Victoria", "after_research": True, "answered": False})
        self.assertNotIn("gaps", web_api._shape_agent_result({"searches": [], "gaps": [{"missing": "x"}]}))


if __name__ == "__main__":
    import unittest
    unittest.main()

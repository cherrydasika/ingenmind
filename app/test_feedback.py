"""Answer feedback (R51, answer_feedback.py): thumbs on one's own answers, the
Langfuse score, and a thumbs-down into a golden set."""

import uuid
from unittest.mock import patch

import answer_feedback
import identity
import sessions
from flows import evals
from test_auth import AuthDatabase


class Feedback(AuthDatabase):
    def setUp(self):
        answer_feedback.reset_schema_cache()
        self.addCleanup(answer_feedback.reset_schema_cache)
        scores = patch.object(answer_feedback.tracing, "score")
        self.score = scores.start()
        self.addCleanup(scores.stop)

    def answered(self, user, question="Can I take a bike on the train?", trace_id="trace-1"):
        session_id = sessions.current(user["user_id"], None)
        turn = sessions.record_turn(session_id, user["user_id"], question,
                                    {"answer": "Bikes go free.", "flow": {"id": "f", "version": 2}}, trace_id)
        return session_id, turn["turn_id"]

    def person(self, role="user", name="Ada"):
        return identity.create_user(name, f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com", role)

    def test_a_rating_is_stored_scored_and_can_change(self):
        ada = self.person()
        session_id, turn_id = self.answered(ada)
        up = answer_feedback.give(turn_id, ada["user_id"], "up", "wrong", "ignored when up")
        self.assertEqual((up["rating"], up["reason"], up["comment"], up["traced"]), ("up", None, "", True))
        self.score.assert_called_with("trace-1", "user_feedback", 1, "BOOLEAN", None, score_id=f"feedback-{turn_id}")
        down = answer_feedback.give(turn_id, ada["user_id"], "down", "out_of_date", "  The fare changed   in May. ")
        self.assertEqual((down["rating"], down["comment"]), ("down", "The fare changed in May."))
        self.score.assert_called_with("trace-1", "user_feedback", 0, "BOOLEAN", "Out of date; The fare changed in May.",
                                      score_id=f"feedback-{turn_id}")          # the same score id: replaced
        [turn] = answer_feedback.attach(sessions.get_session(session_id))["turns"]
        self.assertEqual(turn["feedback"]["reason"], "out_of_date")
        self.assertEqual(answer_feedback.summary()["reasons"], {"out_of_date": 1})
        for bad in (("sideways", None), ("down", "rude")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                answer_feedback.give(turn_id, ada["user_id"], *bad)
        with self.assertRaises(LookupError):                                     # not someone else's answer
            answer_feedback.give(turn_id, self.person(name="Bo")["user_id"], "down")

    def test_a_thumbs_down_becomes_a_golden_set_question(self):
        evals.reset_schema_cache()
        ada = self.person()
        _, turn_id = self.answered(ada, "What does a dog cost?")
        with self.assertRaises(LookupError):
            answer_feedback.promote(turn_id, "Dogs travel free.")                 # no thumbs-down yet
        answer_feedback.give(turn_id, ada["user_id"], "down", "wrong")
        with self.assertRaises(ValueError):
            answer_feedback.promote(turn_id, "  ")
        out = answer_feedback.promote(turn_id, "Dogs travel free, up to two per person.")
        again = answer_feedback.promote(turn_id, "Dogs travel free.")             # not a second copy
        self.assertEqual((out["set_id"], again["questions"]), ("user_feedback", out["questions"]))
        saved = evals.get_set("user_feedback")
        self.assertIn({"question": "What does a dog cost?", "expected": "Dogs travel free.", "expect_blocked": False},
                      saved["questions"])
        [row] = [f for f in answer_feedback.recent() if f["turn_id"] == turn_id]
        self.assertEqual((row["promoted_to"], row["question"], row["first_name"]), ("user_feedback", "What does a dog cost?", "Ada"))

    def test_the_routes(self):
        ada, admin = self.person(), self.person("admin", "Uma")
        session_id, turn_id = self.answered(ada)
        asker = self.signed_in(ada)
        reply = asker.post(f"/api/feedback/{turn_id}", json={"rating": "down", "reason": "not_cited", "comment": "no source"})
        self.assertEqual((reply.status_code, reply.json()["feedback"]["reason"]), (200, "not_cited"))
        self.assertEqual(asker.post(f"/api/feedback/{turn_id}", json={"rating": "meh"}).status_code, 400)
        self.assertEqual(asker.get("/api/feedback").status_code, 403)             # others' questions: view_sessions
        shown = asker.get(f"/api/sessions/{session_id}").json()["session"]
        self.assertEqual(shown["turns"][-1]["feedback"]["comment"], "no source")
        boss = self.signed_in(admin)
        self.assertEqual(boss.post(f"/api/feedback/{turn_id}", json={"rating": "up"}).status_code, 404)
        listed = boss.get("/api/feedback").json()
        self.assertIn(turn_id, [f["turn_id"] for f in listed["feedback"]])
        self.assertEqual(boss.post(f"/api/feedback/{turn_id}/promote", json={"expected": "Bikes go free off-peak."}).status_code, 200)


if __name__ == "__main__":
    import unittest
    unittest.main()

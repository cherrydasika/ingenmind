"""Conversation memory from the stored session turns (R26, conversation.py)."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg import sql

import conversation
import identity
import sessions
from common import config


def turn(user, session_id, question, decision="ALLOW", answer="An answer."):
    sessions.record_turn(session_id, user, question, {"answer": answer,
                                                      "guardrails": {"input": {"decision": decision}}})


class StoredConversation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        sessions.reset_schema_cache()
        identity.reset_schema_cache()

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        sessions.reset_schema_cache()
        identity.reset_schema_cache()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.test_database)))

    def session(self):
        user = f"u_{uuid.uuid4().hex[:8]}"
        return user, sessions.current(user, None)

    def test_follow_up_context_comes_from_the_stored_turns(self):
        user, sid = self.session()
        turn(user, sid, "Toilets at St Pancras?")
        turn(user, sid, "Write a poem", decision="BLOCK")                   # never context
        turn(user, sid, "Is there step-free access?")
        # A restart forgets nothing: nothing is kept in the process.
        sessions.reset_schema_cache()
        self.assertEqual(conversation.recent_questions(sid), ["Toilets at St Pancras?", "Is there step-free access?"])
        _, other = self.session()
        self.assertEqual(conversation.recent_questions(other), [])
        ctx = conversation.context(sid)
        self.assertEqual([t["question"] for t in ctx["turns"]], ["Toilets at St Pancras?", "Is there step-free access?"])

    def test_the_context_stays_bounded_and_older_turns_are_summarised(self):
        user, sid = self.session()
        for i in range(12):
            turn(user, sid, f"Question {i} " + "about trains " * 60, answer="Answer " + "detail " * 300)
        seen = []

        def summary_call(prompt, **kwargs):
            seen.append(prompt)
            return SimpleNamespace(tool_input={"summary": "Asked about trains, twelve times. " * 40})
        with patch.object(conversation.llm, "call_tool", side_effect=summary_call):
            self.assertTrue(conversation.summarise(sid))
            self.assertFalse(conversation.summarise(sid))                     # nothing new waiting
        self.assertIn("Question 8", seen[0])                                  # the 9 older turns, not the last 3
        self.assertNotIn("Question 9", seen[0])
        ctx = conversation.context(sid)
        self.assertLessEqual(len(ctx["summary"]), conversation.SUMMARY_CHARS)
        self.assertEqual(len(ctx["turns"]), conversation.RECENT_TURNS)
        self.assertTrue(ctx["turns"][-1]["question"].startswith("Question 11"))
        self.assertLessEqual(len(conversation.render(ctx)), conversation.SUMMARY_CHARS + 100 + conversation.RECENT_TURNS
                             * (conversation.QUESTION_CHARS + conversation.ANSWER_CHARS + 30))

    def test_a_follow_up_is_rewritten_to_stand_on_its_own(self):
        ctx = {"summary": "", "turns": [{"question": "Toilets at St Pancras?", "answer": "Yes, on the upper level."}]}
        reply = lambda changed, text: SimpleNamespace(tool_input={"standalone": text, "changed": changed})
        with patch.object(conversation.llm, "call_tool", return_value=reply(True, "Are there toilets at Victoria?")) as call:
            self.assertEqual(conversation.standalone("And at Victoria?", ctx), "Are there toilets at Victoria?")
        self.assertIn("<<<CONVERSATION", call.call_args.args[0])
        with patch.object(conversation.llm, "call_tool", return_value=reply(False, "Weather in Leeds?")):
            self.assertIsNone(conversation.standalone("Weather in Leeds?", ctx))
        with patch.object(conversation.llm, "call_tool", side_effect=RuntimeError("down")):
            self.assertIsNone(conversation.standalone("And at Victoria?", ctx))   # the question goes as asked
        with patch.object(conversation.llm, "call_tool") as call:
            self.assertIsNone(conversation.standalone("Hello", {"summary": "", "turns": []}))
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

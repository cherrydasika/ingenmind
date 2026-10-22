"""Long-term user memory, opt-in, visible and deletable (R28, user_memory.py)."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import audit
import identity
import sessions
import user_memory
import web_api
from common import config


def reply(*facts):
    return SimpleNamespace(tool_input={"facts": list(facts)})


class UserMemory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        cls.reset()

    @classmethod
    def reset(cls):
        for module in (identity, sessions, user_memory, audit):
            module.reset_schema_cache()

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        cls.reset()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    def client(self):
        user = identity.create_user("Ana", f"ana-{uuid.uuid4().hex[:8]}@example.com", "user")
        c = TestClient(web_api.app, base_url="http://localhost")
        c.post("/auth/dev", data={"user_id": user["user_id"]})
        return user, c

    def test_nothing_is_learned_or_used_until_the_user_opts_in(self):
        user, c = self.client()
        self.assertEqual(c.get("/api/me/memory").json()["opted_in"], False)
        with patch.object(user_memory.llm, "call_tool") as call:
            self.assertEqual(user_memory.learn(user["user_id"], "I live in Leeds", {}), [])
        call.assert_not_called()
        self.assertEqual(user_memory.for_run(user["user_id"]), [])

    def test_a_user_sees_edits_and_deletes_every_fact(self):
        user, c = self.client()
        self.assertTrue(c.put("/api/me/memory", json={"opted_in": True}).json()["opted_in"])
        with patch.object(user_memory.llm, "call_tool",
                          return_value=reply("Home station: Leeds", "Contact: ana@example.com", "home station is Leeds")):
            new = user_memory.learn(user["user_id"], "I usually leave from Leeds, mail me at ana@example.com",
                                    {"session_id": "s_1", "turn_id": 7, "asked_at": "2026-10-22T10:00:00"})
        self.assertEqual(new, ["Home station: Leeds", "Contact: [email]"])     # masked; the repeat dropped
        facts = c.get("/api/me/memory").json()["facts"]
        self.assertEqual(facts[0]["source"]["turn_id"], "7")
        self.assertIn("[email]", facts[0]["source"]["question"])               # the question as stored, masked
        first, second = facts[0]["id"], facts[1]["id"]
        self.assertEqual(c.put(f"/api/me/memory/{first}", json={"text": "Home station: York"}).json()["facts"][0]["text"],
                         "Home station: York")
        self.assertEqual(c.put(f"/api/me/memory/{first}", json={"text": " "}).status_code, 400)
        self.assertEqual([f["id"] for f in c.delete(f"/api/me/memory/{second}").json()["facts"]], [first])
        self.assertEqual(user_memory.for_run(user["user_id"]), ["Home station: York"])
        # Nobody else reaches it.
        _, other = self.client()
        self.assertEqual(other.delete(f"/api/me/memory/{first}").status_code, 404)
        self.assertEqual(other.get("/api/me/memory").json()["facts"], [])
        self.assertEqual(c.delete("/api/me/memory").json()["facts"], [])

    def test_turning_memory_off_deletes_it_and_is_audited(self):
        user, c = self.client()
        c.put("/api/me/memory", json={"opted_in": True})
        with patch.object(user_memory.llm, "call_tool", return_value=reply("Holds a 16-25 Railcard")):
            user_memory.learn(user["user_id"], "I have a 16-25 railcard", {})
        body = c.put("/api/me/memory", json={"opted_in": False}).json()
        self.assertEqual((body["opted_in"], body["facts"]), (False, []))
        self.assertEqual(c.put("/api/me/memory", json={"opted_in": "yes"}).status_code, 400)
        rows, _ = audit.entries(user_id=user["user_id"], action="memory_consent")
        entries = rows
        self.assertEqual([e["details"]["opted_in"] for e in entries], [False, True])


class Use(unittest.TestCase):
    def test_facts_reach_the_supervisor_quoted_with_their_rules(self):
        text = user_memory.context_text(["Home station: Leeds"])
        self.assertIn("<<<QUOTED DATA\n- Home station: Leeds\n>>>", text)
        self.assertIn("never put it in tasks for the research agent or web searches", text)
        self.assertEqual(user_memory.context_text([]), "")

    def test_the_checks_judge_the_answer_with_what_was_remembered(self):
        import agent
        run = SimpleNamespace(user_facts=["Home station: Port Avalon"])
        self.assertIn("Home station: Port Avalon", agent._checked_question({"question": "My home station?"}, run))
        self.assertEqual(agent._checked_question({"question": "q"}, SimpleNamespace(user_facts=[])), "q")

    def test_web_searches_never_carry_what_was_remembered(self):
        facts = ["Home station: Leeds", "Uses a wheelchair"]
        self.assertEqual(user_memory.scrub_query("step-free access Leeds station wheelchair", "Is the station step-free?",
                                                 facts), "step-free access station")   # "station" was asked
        # Words the user asked about themselves stay.
        self.assertEqual(user_memory.scrub_query("Leeds station step-free", "Is Leeds station step-free?", facts),
                         "Leeds station step-free")
        self.assertEqual(user_memory.scrub_query("trains Leeds", "next train?", []), "trains Leeds")


if __name__ == "__main__":
    unittest.main()

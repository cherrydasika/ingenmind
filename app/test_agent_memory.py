"""Agent memory (agent_memory.py): learned from outcomes only, apart from
session memory. Runs against its own database (needs the pgvector service)."""

import unittest
import uuid

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import agent
import agent_memory
import flows
import identity
import sessions
import web_api
from common import config

SECRET = "Sam's private question about Leeds"
RESEARCH = [{"delegation": 1, "task": SECRET, "missing": [SECRET], "validation": {"sources": [
    {"url": "https://www.nationalrail.co.uk/stations/kgx/?q=Leeds", "accepted": True, "overall": 0.82,
     "scores": {"authority": 0.9, "freshness": 0.8, "consistency": 0.8, "relevance": 0.8}, "reasons": SECRET},
    {"url": "https://travelblog.example/post", "accepted": False, "overall": 0.4,
     "scores": {"authority": 0.3, "freshness": 0.4, "consistency": 0.7, "relevance": 0.7}, "reasons": SECRET},
]}}]
CALLS = [{"tool": "get_weather", "input": {"location": "Leeds, UK"}, "ok": False,
          "error": "No place called 'Leeds, UK' found by Open-Meteo geocoding"},
         {"tool": "get_weather", "input": {"location": "Leeds"}, "ok": True, "error": None},
         {"tool": "get_weather", "ok": None}]   # still running when the run ended: not learned from
EVALUATIONS = [{"delegation": 1, "attempt": 1, "decision": "RETRIEVAL_FAILURE", "overall_confidence": 0.4},
               {"delegation": 1, "attempt": 2, "decision": "GOOD_EVIDENCE", "overall_confidence": 0.8}]
REWRITES = [{"delegation": 1, "diagnosis": SECRET, "queries": [SECRET]}]
SOURCES = [{"url": "https://www.nationalrail.co.uk/help/refunds", "cited": [1]}]


class Outcomes(unittest.TestCase):
    def learned(self, answer_passed=True):
        return agent_memory.outcomes(RESEARCH, CALLS, EVALUATIONS, REWRITES, SOURCES, answer_passed)

    def test_what_a_run_teaches(self):
        got = {(o["agent"], o["kind"], o["key"], o["success"]) for o in self.learned()}
        self.assertEqual(got, {
            ("research", "source_validation", "nationalrail.co.uk", True),
            ("research", "source_validation", "travelblog.example", False),
            ("knowledge_base", "cited_source", "nationalrail.co.uk", True),
            ("knowledge_base", "evidence_decision", "RETRIEVAL_FAILURE", False),
            ("knowledge_base", "evidence_decision", "GOOD_EVIDENCE", True),
            ("knowledge_base", "query_rewrite", "recovered", True),
            ("external_apis", "tool", "get_weather", False),
            ("external_apis", "tool", "get_weather", True),
        })
        rejected = next(o for o in self.learned() if o["key"] == "travelblog.example")
        self.assertEqual(rejected["note"], "failed: authority, freshness")

    def test_nothing_anyone_typed_is_learned(self):
        text = str(self.learned())
        for leaked in (SECRET, "Leeds", "?q=", "/stations/"):
            self.assertNotIn(leaked, text)
        self.assertIn("No place called '…' found", text)   # the error kind survives

    def test_failed_answers_teach_no_cited_sites(self):
        self.assertNotIn("cited_source", {o["kind"] for o in self.learned(answer_passed=False)})


class SessionMemoryStaysApart(unittest.TestCase):
    def test_two_users_never_share_a_memory_actor(self):
        mine, theirs = "u_aaa:same-browser-session", "u_bbb:same-browser-session"
        self.assertNotEqual(agent._actor_id(mine), agent._actor_id(theirs))
        self.assertNotEqual(agent._runtime_session_id(mine, "supervisor"), agent._runtime_session_id(theirs, "supervisor"))
        self.assertNotEqual(agent._actor_id("s_" + "1" * 32), agent._actor_id("s_" + "2" * 32))


class Stored(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        cls.reset_caches()

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        cls.reset_caches()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    @staticmethod
    def reset_caches():
        for module in (identity, sessions, agent_memory, flows.store, flows.evals):
            module.reset_schema_cache()

    def test_runs_add_up(self):
        for _ in range(2):
            agent_memory.learn(RESEARCH, CALLS, EVALUATIONS, REWRITES, SOURCES, True)
        rows = {(r["agent"], r["kind"], r["key"]): r for r in agent_memory.entries()}
        weather = rows[("external_apis", "tool", "get_weather")]
        self.assertEqual((weather["successes"], weather["failures"], weather["success_rate"]), (2, 2, 0.5))
        self.assertIn("No place called '…'", weather["note"])
        site = rows[("research", "source_validation", "nationalrail.co.uk")]
        self.assertEqual((site["total"], site["mean_score"]), (2, 0.82))

    def test_the_page_needs_view_agent_memory(self):
        def client(role):
            user = identity.create_user("Mo", f"mo-{uuid.uuid4().hex[:8]}@example.com", role)
            c = TestClient(web_api.app, base_url="http://localhost")
            c.post("/auth/dev", data={"user_id": user["user_id"]})
            return c
        self.assertEqual(client("user").get("/api/agent-memory").status_code, 403)
        body = client("developer").get("/api/agent-memory").json()
        self.assertIn("research", body["agents"])
        self.assertIsInstance(body["entries"], list)


if __name__ == "__main__":
    unittest.main()

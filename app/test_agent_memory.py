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

    def learn_tool(self, tool, ok, times):
        agent_memory.record([{"agent": "external_apis", "kind": "tool", "key": tool, "success": ok,
                              "note": "" if ok else "HTTP 503"}] * times)

    def age(self, key, days):
        with agent_memory._connect() as connection:
            connection.execute("UPDATE app_agent_memory SET last_seen = now() - make_interval(days => %s) WHERE key = %s",
                               (days, key))

    def test_old_outcomes_fade_unless_pinned(self):                      # R27
        self.learn_tool("fading_tool", True, 4)
        self.learn_tool("pinned_tool", True, 4)
        self.assertTrue(agent_memory.pin("external_apis", "tool", "pinned_tool", True))
        self.age("fading_tool", 60)
        self.age("pinned_tool", 60)
        rows = {r["key"]: r for r in agent_memory.entries("external_apis")}
        self.assertAlmostEqual(rows["fading_tool"]["successes"], 1.0, places=1)    # two half-lives
        self.assertEqual(rows["pinned_tool"]["successes"], 4)
        self.assertTrue(rows["pinned_tool"]["pinned"])
        self.learn_tool("fading_tool", True, 1)                              # an update decays first, then adds
        self.assertAlmostEqual({r["key"]: r for r in agent_memory.entries("external_apis")}["fading_tool"]["successes"],
                               2.0, places=1)
        self.assertTrue(agent_memory.forget("external_apis", "tool", "fading_tool"))
        self.assertNotIn("fading_tool", {r["key"] for r in agent_memory.entries("external_apis")})

    def test_the_agents_read_it(self):                                    # R27
        def validated(url, ok, times):
            agent_memory.learn([{"validation": {"sources": [{"url": url, "accepted": ok, "overall": 0.8 if ok else 0.3,
                                                              "scores": {}}]}}] * times, [], [], [], [], None)
        validated("https://good.example/a", True, 4)
        validated("https://flaky.example/b", False, 4)
        results = [{"url": "https://flaky.example/x"}, {"url": "https://new.example/y"}, {"url": "https://www.good.example/z"}]
        ranked = agent_memory.rank_results(results)
        self.assertEqual([r["url"] for r in ranked],
                         ["https://www.good.example/z", "https://new.example/y", "https://flaky.example/x"])
        self.assertEqual(ranked[0]["site_record"], "validated 4 of 4 recent times")
        self.assertNotIn("site_record", ranked[1])
        # A tool failing most recent calls is skipped; a pinned one never is.
        self.learn_tool("broken_tool", False, 6)
        self.assertIn("failed 6 of its last 6 calls", agent_memory.tool_unreliable("broken_tool"))
        self.assertIsNone(agent_memory.tool_unreliable("unknown_tool"))
        agent_memory.pin("external_apis", "tool", "broken_tool", True)
        self.assertIsNone(agent_memory.tool_unreliable("broken_tool"))
        # The rewriter hears which kinds of rewrite recovered evidence.
        evaluations = [{"delegation": 1, "attempt": 2, "decision": "GOOD_EVIDENCE"}]
        for _ in range(2):
            agent_memory.learn([], [], evaluations, [{"delegation": 1, "strategy": "synonyms"}], [], None)
        self.assertIn("synonyms: recovered 2 of 2", agent_memory.strategy_note())

    def test_memory_off_reads_and_learns_nothing(self):                  # R27
        self.learn_tool("off_tool", False, 6)
        with agent_memory.using(False):
            self.assertIsNone(agent_memory.tool_unreliable("off_tool"))
            self.assertEqual(agent_memory.strategy_note(), "")
            self.learn_tool("off_tool", True, 3)
        self.assertEqual({r["key"]: r for r in agent_memory.entries()}["off_tool"]["successes"], 0)

    def test_an_evaluation_can_compare_memory_on_and_off(self):           # R27
        from unittest.mock import patch
        flows.evals.ensure_schema()
        with patch.object(flows.evals, "_ensure_worker"):
            ids = flows.evals.start("travel_basics", "travel_assistant", [(0, {"id": "x"})], None, (True, False))
        runs = {r["id"]: r for r in flows.evals.list_runs("travel_assistant")}
        self.assertEqual([runs[i]["options"] for i in ids], [{}, {"memory": False}])

    def test_admins_pin_and_forget_entries(self):                         # R27
        self.learn_tool("page_tool", True, 1)
        def client(role):
            user = identity.create_user("Mo", f"mo-{uuid.uuid4().hex[:8]}@example.com", role)
            c = TestClient(web_api.app, base_url="http://localhost")
            c.post("/auth/dev", data={"user_id": user["user_id"]})
            return c
        entry = {"agent": "external_apis", "kind": "tool", "key": "page_tool"}
        self.assertEqual(client("user").post("/api/agent-memory/entry", json={**entry, "action": "pin"}).status_code, 403)
        admin = client("admin")
        self.assertEqual(admin.post("/api/agent-memory/entry", json={**entry, "action": "pin"}).status_code, 200)
        self.assertEqual(admin.post("/api/agent-memory/entry", json={**entry, "action": "explode"}).status_code, 400)
        self.assertEqual(admin.post("/api/agent-memory/entry", json={**entry, "action": "forget"}).status_code, 200)
        self.assertEqual(admin.post("/api/agent-memory/entry", json={**entry, "action": "forget"}).status_code, 404)

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

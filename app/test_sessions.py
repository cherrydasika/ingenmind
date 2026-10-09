"""Sessions and their history (sessions.py) and the /api/sessions routes.
Runs against its own database (needs the pgvector service)."""

import json
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import agent
import flows
import identity
import sessions
import web_api
from common import config


class SessionsDatabase(unittest.TestCase):
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
        identity.reset_schema_cache()
        sessions.reset_schema_cache()
        flows.store.reset_schema_cache()
        flows.evals.reset_schema_cache()

    def user(self, role="user"):
        return identity.create_user("Pat", f"pat-{uuid.uuid4().hex[:8]}@example.com", role)

    def age(self, session_id, minutes):
        with sessions._connect() as connection:
            connection.execute("UPDATE app_sessions SET last_activity = now() - make_interval(mins => %s) "
                               "WHERE session_id = %s", (minutes, session_id))


class SessionLifecycle(SessionsDatabase):
    def test_a_session_is_reused_until_it_idles(self):
        user = self.user()
        first = sessions.current(user["user_id"], None)
        self.assertEqual(sessions.current(user["user_id"], first), first)
        self.age(first, sessions.IDLE_MINUTES + 1)
        second = sessions.current(user["user_id"], first)
        self.assertNotEqual(second, first)
        self.assertEqual(sessions.get_session(first)["status"], "ended")

    def test_another_users_session_is_never_joined(self):
        mine, theirs = self.user(), self.user()
        their_session = sessions.current(theirs["user_id"], None)
        self.assertNotEqual(sessions.current(mine["user_id"], their_session), their_session)
        self.assertEqual(sessions.get_session(their_session)["status"], "active")

    def test_a_turn_keeps_what_the_user_saw_and_titles_the_session(self):
        user = self.user()
        sid = sessions.current(user["user_id"], None)
        sessions.record_turn(sid, user["user_id"], "Toilets at St Pancras?",
                             {"answer": "Yes [1].", "sources": [{"url": "https://x/1", "cited": [1]}],
                              "searches": [{"retrieval": {"chunks": [{"text": "chunk text"}]}}], "total": 4.2},
                             trace_id="t1")
        sessions.record_turn(sid, user["user_id"], "And Victoria?", {"answer": "No information."})
        got = sessions.get_session(sid)
        self.assertEqual(got["title"], "Toilets at St Pancras?")
        self.assertEqual([t["question"] for t in got["turns"]], ["Toilets at St Pancras?", "And Victoria?"])
        self.assertEqual(got["turns"][0]["trace_id"], "t1")
        self.assertNotIn("searches", got["turns"][0]["result"])   # no chunk text kept
        self.assertEqual(got["turns"][0]["result"]["sources"][0]["cited"], [1])

    def test_lists_show_sessions_with_questions_newest_first(self):
        user, other = self.user(), self.user()
        empty = sessions.current(user["user_id"], None)
        self.age(empty, sessions.IDLE_MINUTES + 1)
        asked = sessions.current(user["user_id"], empty)
        sessions.record_turn(asked, user["user_id"], "Q", {"answer": "A"})
        theirs = sessions.current(other["user_id"], None)
        sessions.record_turn(theirs, other["user_id"], "Q2", {"answer": "A2"})
        self.assertEqual([s["session_id"] for s in sessions.list_sessions(user["user_id"])], [asked])
        self.assertEqual(sessions.list_sessions(user["user_id"])[0]["turns"], 1)
        self.assertTrue({asked, theirs} <= {s["session_id"] for s in sessions.list_sessions()})

    def test_old_history_is_deleted(self):
        user = self.user()
        old = sessions.current(user["user_id"], None)
        sessions.record_turn(old, user["user_id"], "Q", {"answer": "A"})
        self.age(old, (sessions.RETENTION_DAYS + 1) * 24 * 60)
        recent = sessions.current(user["user_id"], old)
        sessions.record_turn(recent, user["user_id"], "Q", {"answer": "A"})
        sessions.prune(force=True)
        self.assertIsNone(sessions.get_session(old))
        self.assertIsNotNone(sessions.get_session(recent))


def fake_answer(question, session_id, user_id, *args, **kwargs):
    return {**agent._public_result(question, f"Answer to {question}", {}), "trace_id": "trace-1",
            "sources": [], "answer_check": {"passed": True, "overall": 1.0}}


class SessionsApi(SessionsDatabase):
    def setUp(self):
        super().setUp()
        # Questions wait while the knowledge system is not set up; these tests are about sessions.
        ready = patch.object(web_api.knowledge_system, "is_ready", return_value=True)
        ready.start()
        self.addCleanup(ready.stop)

    def signed_in(self, user):
        client = TestClient(web_api.app, base_url="http://localhost")
        client.post("/auth/dev", data={"user_id": user["user_id"]})
        return client

    def ask(self, client, question):
        with patch.object(agent, "answer", side_effect=fake_answer) as answer:
            body = client.post("/api/agent/stream", json={"question": question, "session_id": "browser-chosen"}).text
        result = json.loads(body.split("event: result\ndata: ")[1].split("\n")[0])
        return result, answer.call_args.args[1]

    def test_questions_are_recorded_in_the_servers_session(self):
        user = self.user()
        client = self.signed_in(user)
        first, sid = self.ask(client, "Toilets at St Pancras?")
        self.assertNotIn("trace_id", first)                    # server-side only
        self.assertTrue(sid.startswith("s_"))                  # the server's session, not "browser-chosen"
        _, again = self.ask(client, "And Victoria?")
        self.assertEqual(again, sid)
        current = client.get("/api/sessions/current").json()["session"]
        self.assertEqual([t["question"] for t in current["turns"]], ["Toilets at St Pancras?", "And Victoria?"])
        listed = client.get("/api/sessions").json()
        self.assertEqual(([s["session_id"] for s in listed["sessions"]], listed["can_view_all"]), ([sid], False))

    def test_others_sessions_need_view_sessions(self):
        owner, stranger, reviewer = self.user(), self.user(), self.user("developer")   # developers may view sessions
        _, sid = self.ask(self.signed_in(owner), "Q")
        stranger_client = self.signed_in(stranger)
        self.assertEqual(stranger_client.get(f"/api/sessions/{sid}").status_code, 404)
        self.assertEqual(stranger_client.get("/api/sessions?user=all").status_code, 403)
        self.assertEqual(stranger_client.get(f"/api/sessions?user={owner['user_id']}").status_code, 403)
        reviewer_client = self.signed_in(reviewer)
        self.assertEqual(reviewer_client.get(f"/api/sessions/{sid}").json()["session"]["turns"][0]["question"], "Q")
        self.assertIn(sid, [s["session_id"] for s in reviewer_client.get("/api/sessions?user=all").json()["sessions"]])

    def test_signing_out_ends_the_session(self):
        user = self.user()
        client = self.signed_in(user)
        _, sid = self.ask(client, "Q")
        client.post("/auth/logout")
        self.assertEqual(sessions.get_session(sid)["status"], "ended")
        client = self.signed_in(user)
        self.assertIsNone(client.get("/api/sessions/current").json()["session"])
        _, new_sid = self.ask(client, "Q again")
        self.assertNotEqual(new_sid, sid)


if __name__ == "__main__":
    unittest.main()

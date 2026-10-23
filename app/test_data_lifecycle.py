"""Data lifecycle (R59, data_lifecycle.py): a person's data exported, and deleted everywhere (proven by scanning
every row of every table for them), and retention settings applied."""

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg import sql

import agent_runtime
import answer_feedback
import api_keys
import audit
import channels
import data_lifecycle
import durable
import human
import identity
import knowledge_gaps
import knowledge_system
import sessions
import usage_limits
import use_learnings
import user_memory
from flows import store
from test_auth import AuthDatabase

MODULES = (sessions, answer_feedback, knowledge_gaps, use_learnings, user_memory, usage_limits, api_keys, channels,
           human, durable, agent_runtime, audit, store, knowledge_system, data_lifecycle)


def fresh_schemas():
    for module in MODULES:
        for flag in ("_schema_ready", "_ready"):
            if hasattr(module, flag):
                setattr(module, flag, False)


class Lifecycle(AuthDatabase):
    def setUp(self):
        fresh_schemas()
        for module in MODULES:
            if hasattr(module, "ensure_schema"):
                module.ensure_schema()
        self.admin = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        self.person = identity.create_user("Zoë", f"zoe-{uuid.uuid4().hex[:6]}@example.com", "user")
        widget = identity.create_user("Widget", f"widget-{uuid.uuid4().hex[:6]}@example.com", "user")
        with data_lifecycle._connect() as connection:       # a channel people's threads belong to
            connection.execute("DELETE FROM channels")
            connection.execute("INSERT INTO channels (channel_id, kind, name, user_id) VALUES ('c1', 'widget', 'W', %s)",
                               (widget["user_id"],))

    def seed(self, person: dict) -> dict:
        """Everything a person leaves behind, through the modules that keep it."""
        uid = person["user_id"]
        session = sessions.current(uid, None)
        turn = sessions.record_turn(session, uid, "Can I bring my dog?", {
            "answer": "Yes [1].", "sources": [{"url": "https://rail.example/dogs", "cited": [1]}],
            "evidence": [{"decision": "KNOWLEDGE_GAP"}]}, f"trace-{uid}")
        answer_feedback.give(turn["turn_id"], uid, "down", "wrong", "my number is 07700 900123")
        knowledge_gaps.record(turn["turn_id"], "Can I bring my dog?", [{"missing": "dog rules"}], False)
        use_learnings.record(turn["turn_id"], {"evidence": [{"decision": "KNOWLEDGE_GAP"}]})
        user_memory.set_opted_in(uid, True)
        with user_memory._connect() as connection:
            connection.execute("INSERT INTO app_user_facts (user_id, text) VALUES (%s, 'Has a dog called Rex')", (uid,))
        usage_limits.charge(uid, 120)
        usage_limits.set_override(uid, {"per_minute": 3, "tokens_per_day": 1000}, self.admin["user_id"])
        api_keys.create({**person, "permissions": ["query_rag"]}, "laptop", ["ask"])
        actor = data_lifecycle.actors([session])[0]
        agent_runtime.save_history(f"rag-web-{session}-supervisor", actor, [{"role": "user", "content": "dog?"}])
        store.record_run("f", 1, "users", {"seconds": 2, "user_id": uid, "channel": "app"})
        knowledge_system.add_url("https://rail.example/added", user_id=uid)
        with sessions._connect() as connection:
            connection.execute("INSERT INTO agent_run_records (run_id, source, session_id, user_id, flow_id, version, spec, "
                               "status, question, asked) VALUES (%s, 'users', %s, %s, 'f', '1', '{}', 'done', "
                               "'Can I bring my dog?', 'Can I bring my dog?')", (f"run-{uid}", session, uid))
            connection.execute("INSERT INTO human_requests (request_id, run_id, node, kind, session_id, user_id, question, "
                               "status, expires_at) VALUES (%s, %s, 'n', 'approve', %s, %s, 'dog?', 'answered', now())",
                               (f"req-{uid}", f"run-{uid}", session, uid))
            connection.execute("INSERT INTO channel_threads (channel_id, thread_key, user_id, session_id) "
                               "VALUES ('c1', %s, %s, %s)", (f"t-{uid}", uid, session))
        self.signed_in(person)                                                        # an audit entry by them
        self.signed_in(self.admin).put(f"/api/users/{uid}", json={"first_name": "Zoë"})  # one about them
        return {"session": session, "actor": actor, "trace": f"trace-{uid}"}

    def mentions(self, *needles: str) -> dict[str, int]:
        """Rows of every table (each row as text) mentioning any of these."""
        found = {}
        with data_lifecycle._connect() as connection:
            tables = [r["table_name"] for r in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")]
            for table in tables:
                for needle in needles:
                    n = connection.execute(sql.SQL("SELECT count(*) AS n FROM {} x WHERE x::text LIKE %s").format(
                        sql.Identifier(table)), (f"%{needle}%",)).fetchone()["n"]
                    if n:
                        found[f"{table}: {needle}"] = n
        return found

    def test_deleting_a_person_removes_their_data_everywhere(self):
        left = self.seed(self.person)
        kept = self.seed(self.admin)                       # someone else's data stays
        uid, email = self.person["user_id"], self.person["email"]
        self.assertGreater(len(self.mentions(uid, email)), 8)
        deleted_traces, forgotten = [], []
        with patch.object(data_lifecycle, "langfuse_trace_ids", return_value=["trace-from-observations"]), \
                patch.object(data_lifecycle, "langfuse_delete", side_effect=lambda ids: deleted_traces.extend(ids) or len(ids)), \
                patch.object(data_lifecycle, "agentcore_forget", side_effect=lambda a: forgotten.extend(a) or 2):
            response = self.signed_in(self.admin).post(f"/api/users/{uid}/data/delete", json={"confirm": "DELETE"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(sorted(deleted_traces), sorted([left["trace"], "trace-from-observations"]))   # Langfuse
        self.assertEqual(forgotten, [left["actor"]])                                                    # AgentCore
        self.assertEqual(result["agentcore_items"], 2)
        self.assertEqual(self.mentions(uid, email, left["session"], left["actor"]), {})   # PostgreSQL
        self.assertIsNone(identity.get_user(uid))
        with data_lifecycle._connect() as connection:
            rows = connection.execute("SELECT action, user_name, path FROM audit_log WHERE user_id = 'deleted-user'").fetchall()
        self.assertIn(("sign_in", "Deleted user"), [(r["action"], r["user_name"]) for r in rows])       # kept, unnamed
        self.assertTrue(self.mentions(self.admin["user_id"], kept["actor"], kept["trace"]))              # others kept

    def test_the_export_has_their_data_and_no_secrets(self):
        self.seed(self.person)
        response = self.signed_in(self.person).get("/api/me/data")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        data = json.load(io.BytesIO(response.content))
        tables = data["tables"]
        for table in ("app_users", "app_session_turns", "answer_feedback", "app_user_facts", "api_keys", "agent_sessions",
                      "knowledge_gaps", "use_learnings", "app_usage"):
            self.assertIn(table, tables)
        self.assertEqual(tables["app_session_turns"][0]["question"], "Can I bring my dog?")
        self.assertNotIn("digest", tables["api_keys"][0])
        self.assertEqual(data["elsewhere"]["langfuse_traces"], [f"trace-{self.person['user_id']}"])
        self.assertNotIn(self.admin["email"], json.dumps(data))                       # only theirs
        other = self.signed_in(self.person).get(f"/api/users/{self.admin['user_id']}/data")
        self.assertEqual(other.status_code, 403)                                      # not someone else's

    def test_deleting_yourself_signs_you_out(self):
        client = self.signed_in(self.person)
        self.assertEqual(client.post("/api/me/data/delete", json={"confirm": "delete"}).status_code, 400)
        with patch.object(data_lifecycle, "langfuse_trace_ids", return_value=[]), \
                patch.object(data_lifecycle, "langfuse_delete", return_value=0):
            done = client.post("/api/me/data/delete", json={"confirm": "DELETE"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertIsNone(client.get("/api/me").json()["user"])                   # signed out
        self.assertIsNone(identity.get_user(self.person["user_id"]))

    def test_what_cant_be_deleted(self):
        with data_lifecycle._connect() as connection:          # the only active admin
            connection.execute("UPDATE app_users SET status = 'disabled' WHERE role = 'admin' AND user_id <> %s",
                               (self.admin["user_id"],))
        helper = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "admin")
        with data_lifecycle._connect() as connection:
            connection.execute("UPDATE app_users SET status = 'disabled' WHERE user_id = %s", (helper["user_id"],))
        with self.assertRaises(data_lifecycle.Refused):
            data_lifecycle.delete_user(self.admin["user_id"], None, langfuse=lambda *a: 0, agentcore=lambda a: 0)
        with data_lifecycle._connect() as connection:          # a channel's own account
            connection.execute("INSERT INTO channels (channel_id, kind, name, user_id) VALUES ('w1', 'widget', 'W', %s)",
                               (self.person["user_id"],))
        refused = self.signed_in(self.admin).post(f"/api/users/{self.person['user_id']}/data/delete", json={"confirm": "DELETE"})
        self.assertEqual(refused.status_code, 409)
        # Langfuse unreachable: nothing deleted, so it can be tried again.
        with data_lifecycle._connect() as connection:
            connection.execute("DELETE FROM channels WHERE channel_id = 'w1'")
        with patch.object(data_lifecycle, "langfuse_trace_ids", side_effect=ConnectionError("down")):
            failed = self.signed_in(self.admin).post(f"/api/users/{self.person['user_id']}/data/delete", json={"confirm": "DELETE"})
        self.assertEqual(failed.status_code, 502)
        self.assertIsNotNone(identity.get_user(self.person["user_id"]))

    def test_retention_deletes_what_is_older(self):
        old, new = self.seed(self.person), self.seed(self.admin)
        with data_lifecycle._connect() as connection:
            connection.execute("UPDATE app_sessions SET last_activity = now() - interval '40 days' WHERE session_id = %s",
                               (old["session"],))
            connection.execute(audit.MAINTENANCE)                       # as if written a year ago
            connection.execute("UPDATE audit_log SET at = now() - interval '400 days' WHERE user_id = %s",
                               (self.person["user_id"],))
        client = self.signed_in(self.admin)
        self.assertEqual(client.put("/api/data-retention", json={"days": {"sessions": 0}}).status_code, 400)
        saved = client.put("/api/data-retention", json={"days": {"sessions": 30, "traces": 30, "audit": 365}}).json()
        self.assertEqual({k["kind"]: k["days"] for k in saved["kinds"]}["sessions"], 30)
        cutoffs = []
        run = data_lifecycle.apply(langfuse=lambda cutoff: cutoffs.append(cutoff) or 3)
        self.assertEqual(run["deleted"]["sessions"]["app_sessions"], 1)
        self.assertGreaterEqual(run["deleted"]["audit"]["audit_log"], 1)
        self.assertEqual(run["deleted"]["traces"], {"langfuse": 3})
        self.assertLess(cutoffs[0], datetime.now(timezone.utc) - timedelta(days=29))
        self.assertEqual(self.mentions(old["session"], old["actor"]), {})
        self.assertTrue(self.mentions(new["session"]))
        self.assertFalse(data_lifecycle.due())                    # once a day
        self.assertEqual(self.signed_in(self.person).get("/api/data-retention").status_code, 403)
        privacy = self.signed_in(self.person).get("/api/privacy").json()
        self.assertEqual({k["kind"]: k["days"] for k in privacy["kinds"]}["audit"], 365)


if __name__ == "__main__":
    import unittest
    unittest.main()

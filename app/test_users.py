"""User administration (/api/users, identity.guard_change). Runs against its
own database (needs the pgvector service)."""

import json
import unittest
import uuid
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import agent_memory
import answer_cache
import audit
import auth
import flows
import identity
import sessions
import usage_limits
import web_api
from common import config


class UsersDatabase(unittest.TestCase):
    """Its own database, users made and signed in."""

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
        for module in (identity, sessions, agent_memory, flows.store, flows.evals, usage_limits, audit, answer_cache):
            module.reset_schema_cache()

    def make(self, role="user", name="Pat"):
        return identity.create_user(name, f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com", role)

    def signed_in(self, user):
        client = TestClient(web_api.app, base_url="http://localhost")
        client.post("/auth/dev", data={"user_id": user["user_id"]})
        return client


class Users(UsersDatabase):
    def test_only_user_managers_may_use_it(self):
        client = self.signed_in(self.make("developer"))
        self.assertEqual(client.get("/api/users").status_code, 403)
        self.assertEqual(client.post("/api/users", json={}).status_code, 403)

    def test_adding_a_user(self):
        admin = self.make("admin", "Ada")
        client = self.signed_in(admin)
        email = f"bob-{uuid.uuid4().hex[:8]}@example.com"
        created = client.post("/api/users", json={"first_name": "Bob", "email": email, "role": "developer"})
        self.assertEqual(created.status_code, 201)
        bob = created.json()["user"]
        self.assertEqual(set(bob["permissions"]), set(identity.ROLES["developer"]))
        self.assertEqual(identity.get_user(bob["user_id"])["created_by"], admin["user_id"])
        self.assertEqual(client.post("/api/users", json={"first_name": "Bob", "email": email.upper(),
                                                         "role": "user"}).status_code, 409)
        self.assertEqual(client.post("/api/users", json={"first_name": "X", "email": "nope", "role": "user"}).status_code, 400)
        listed = client.get("/api/users").json()
        self.assertIn(bob["user_id"], [u["user_id"] for u in listed["users"]])
        self.assertEqual(set(listed["roles"]), set(identity.ROLES))
        # No password anywhere: nothing to enter, nothing stored.
        self.assertNotIn("password", str(listed).lower())

    def test_editing_role_permissions_and_status(self):
        admin = self.make("admin", "Ada")
        client = self.signed_in(admin)
        user = self.make("user", "Cy")
        user_client = self.signed_in(user)
        got = client.put(f"/api/users/{user['user_id']}", json={"role": "viewer"}).json()["user"]
        self.assertEqual((got["role"], got["permissions"]), ("viewer", ["view_knowledge"]))
        got = client.put(f"/api/users/{user['user_id']}",
                         json={"first_name": "Cyrus", "permissions": ["query_rag", "view_knowledge"]}).json()["user"]
        self.assertEqual((got["first_name"], got["permissions"]), ("Cyrus", ["query_rag", "view_knowledge"]))
        self.assertEqual(identity.get_user(user["user_id"])["updated_by"], admin["user_id"])
        self.assertEqual(client.put(f"/api/users/{user['user_id']}", json={"permissions": ["fly"]}).status_code, 400)
        self.assertEqual(client.put(f"/api/users/{user['user_id']}", json={"status": "disabled"}).status_code, 200)
        self.assertEqual(user_client.get("/api/config").status_code, 401)        # signed out at once
        self.assertEqual(client.put("/api/users/u_nobody", json={"status": "active"}).status_code, 404)

    def test_admins_cannot_lock_themselves_out(self):
        admin = self.make("admin", "Ada")
        client = self.signed_in(admin)
        for change in ({"status": "disabled"}, {"permissions": ["query_rag"]}, {"role": "user"}):
            response = client.put(f"/api/users/{admin['user_id']}", json=change)
            self.assertEqual(response.status_code, 400, change)
        self.assertEqual(client.put(f"/api/users/{admin['user_id']}", json={"first_name": "Adah"}).status_code, 200)

    def test_anyone_signed_in_saves_their_own_theme(self):
        user = self.make("viewer", "Thea")
        client = self.signed_in(user)
        self.assertEqual(client.put("/api/me/theme", json={"theme": "light"}).json(), {"theme": "light"})
        self.assertEqual(client.get("/api/me").json()["user"]["theme"], "light")
        self.assertEqual(client.put("/api/me/theme", json={"theme": "neon"}).status_code, 400)

    def test_someone_can_always_manage_users(self):
        # An admin acting in the app is always a remaining manager, so this
        # guards anything else that changes users (a script, a future API).
        for user in identity.list_users():
            if user["status"] == "active" and "manage_users" in user["permissions"]:
                identity.update_user(user["user_id"], {"status": "disabled"})
        last = self.make("admin", "Lon")
        system = {"user_id": "system"}
        for change in ({"status": "disabled"}, {"role": "viewer"}):
            with self.assertRaises(ValueError):
                identity.guard_change(system, last["user_id"], change)
        self.make("admin", "Sid")
        identity.guard_change(system, last["user_id"], {"status": "disabled"})   # Sid remains

if __name__ == "__main__":
    unittest.main()


class UsageLimits(UsersDatabase):
    """R06: questions per minute and tokens per day, per user."""
    from datetime import datetime, timezone
    NOW = datetime(2026, 10, 21, 12, 30, 15, tzinfo=timezone.utc)

    def test_questions_per_minute(self):
        user = self.make()
        usage_limits.set_override(user["user_id"], {"per_minute": 2, "tokens_per_day": None}, "test")
        self.assertIsNone(usage_limits.admit(user, self.NOW))
        self.assertIsNone(usage_limits.admit(user, self.NOW))
        refusal = usage_limits.admit(user, self.NOW)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.retry_after, 45)   # the rest of this minute
        self.assertIn("45 seconds", refusal.message)
        from datetime import timedelta
        self.assertIsNone(usage_limits.admit(user, self.NOW + timedelta(minutes=1)))   # a new minute

    def test_tokens_per_day(self):
        user = self.make()
        usage_limits.set_override(user["user_id"], {"per_minute": None, "tokens_per_day": 1000}, "test")
        self.assertIsNone(usage_limits.admit(user, self.NOW))
        usage_limits.charge(user["user_id"], 1200, self.NOW)
        refusal = usage_limits.admit(user, self.NOW)
        self.assertIn("today's allowance of 1,000 tokens", refusal.message)
        self.assertEqual(refusal.retry_after, 11 * 3600 + 29 * 60 + 45 + 1)   # until midnight UTC
        from datetime import timedelta
        self.assertIsNone(usage_limits.admit(user, self.NOW + timedelta(days=1)))   # a new day

    def test_zero_means_no_limit_and_defaults_by_role(self):
        user = self.make()
        usage_limits.set_override(user["user_id"], {"per_minute": 0, "tokens_per_day": 0}, "test")
        for _ in range(15):
            self.assertIsNone(usage_limits.admit(user, self.NOW))
        self.assertGreater(usage_limits.defaults("admin")["per_minute"], usage_limits.defaults("user")["per_minute"])

    def test_a_global_cap_says_busy(self):
        user, other = self.make(), self.make()
        usage_limits.charge(other["user_id"], 50, self.NOW)
        from unittest.mock import patch
        with patch.dict("os.environ", {"GLOBAL_TOKENS_PER_DAY": "50"}):
            self.assertIn("busy", usage_limits.admit(user, self.NOW).message)

    def test_the_ask_route_answers_429_with_retry_after(self):
        user = self.make()
        usage_limits.set_override(user["user_id"], {"per_minute": None, "tokens_per_day": 10}, "test")
        usage_limits.charge(user["user_id"], 10)
        client = self.signed_in(user)
        from unittest.mock import AsyncMock, patch
        with patch.object(web_api, "_not_set_up", AsyncMock(return_value=None)):
            response = client.post("/api/agent/stream", json={"question": "What time is it?"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertIn("allowance", response.json()["error"])

    def test_admins_see_usage_and_set_limits(self):
        admin, user = self.make("admin", "Ada"), self.make()
        usage_limits.charge(user["user_id"], 321)
        client = self.signed_in(admin)
        listed = {u["user_id"]: u for u in client.get("/api/users").json()["users"]}
        self.assertEqual(listed[user["user_id"]]["usage"]["today"]["tokens"], 321)
        self.assertIsNone(listed[user["user_id"]]["usage"]["override"])
        saved = client.put(f"/api/users/{user['user_id']}", json={"limits": {"per_minute": 3, "tokens_per_day": None}})
        self.assertEqual(saved.status_code, 200)
        listed = {u["user_id"]: u for u in client.get("/api/users").json()["users"]}
        self.assertEqual(listed[user["user_id"]]["usage"]["limits"]["per_minute"], 3)
        self.assertEqual(listed[user["user_id"]]["usage"]["limits"]["tokens_per_day"],
                         usage_limits.defaults("user")["tokens_per_day"])
        self.assertEqual(client.put(f"/api/users/{user['user_id']}", json={"limits": {"per_minute": -1}}).status_code, 400)
        self.assertEqual(client.put("/api/users/nobody", json={"limits": {"per_minute": 1}}).status_code, 404)
        client.put(f"/api/users/{user['user_id']}", json={"limits": None})
        listed = {u["user_id"]: u for u in client.get("/api/users").json()["users"]}
        self.assertIsNone(listed[user["user_id"]]["usage"]["override"])


class AuditLog(UsersDatabase):
    """R07: who changed what, append-only."""

    def latest(self, **filters):
        rows, _ = audit.entries(**filters, limit=50)
        return rows

    def test_every_state_changing_route_is_audited_unless_it_only_asks(self):
        import re
        from starlette.routing import Route
        skipped = set()
        for route in web_api.app.routes:
            if not isinstance(route, Route) or not route.path.startswith("/api/"):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in (route.methods or set()) - audit.SAFE_METHODS:
                if path in audit.READ_ONLY or (auth.required_permission(method, path) in audit.NOT_AUDITED
                                               and not audit.ALWAYS.match(path)):
                    skipped.add(f"{method} {route.path}")
        # Only asking questions, one's own theme and read-only checks stay out; anything new is audited.
        self.assertEqual(skipped, {"POST /api/ask", "POST /api/ask/stream", "POST /api/agent/stream", "POST /api/v1/ask", "POST /api/v1/a2a", "POST /api/v1/mcp",
                                   "POST /api/agent/resume", "POST /api/session/reset", "POST /api/sessions/new", "PUT /api/me/theme",
                                   "PUT /api/me/memory", "DELETE /api/me/memory", "PUT /api/me/memory/{fact_id:int}",
                                   "DELETE /api/me/memory/{fact_id:int}",
                                   "POST /api/feedback/{turn_id:int}",   # one's own thumbs on an answer (R51)
                                   "POST /api/me/data/delete",   # recorded by data_lifecycle, without naming them (R59)
                                   "POST /api/flows/validate", "POST /api/flows/diff", "POST /api/adhoc/preview"})

    def test_a_change_is_recorded_with_who_what_and_fields(self):
        admin = self.make("admin", "Ada")
        client = self.signed_in(admin)
        email = f"new-{uuid.uuid4().hex[:6]}@example.com"
        response = client.post("/api/users", json={"first_name": "Neo", "email": email, "role": "user",
                                                    "api_key": "sk-secret", "password": "hunter2"})
        self.assertEqual(response.status_code, 201)
        entry = next(e for e in self.latest(user_id=admin["user_id"]) if e["action"] == "user_create")
        self.assertEqual((entry["user_name"], entry["method"], entry["status"]), ("Ada", "POST", 201))
        self.assertEqual(entry["details"]["values"]["email"], email)
        self.assertIn("password", entry["details"]["fields"])
        self.assertNotIn("password", entry["details"]["values"])
        self.assertNotIn("sk-secret", str(entry["details"]))
        self.assertEqual(response.headers["x-request-id"], entry["request_id"])
        changed = client.put(f"/api/users/{response.json()['user']['user_id']}", json={"role": "developer"})
        self.assertEqual(changed.status_code, 200)
        entry = self.latest(action="user_update")[0]
        self.assertTrue(entry["target"].startswith("user_id="))

    def test_refused_attempts_and_sign_ins_are_recorded(self):
        developer = self.make("developer", "Dev")
        client = self.signed_in(developer)
        self.assertEqual(client.post("/api/users", json={"first_name": "X"}).status_code, 403)
        mine = self.latest(user_id=developer["user_id"])
        self.assertIn(("refused", 403), {(e["action"], e["status"]) for e in mine})
        self.assertIn("sign_in", {e["action"] for e in mine})
        client.post("/auth/logout")
        self.assertIn("sign_out", {e["action"] for e in self.latest(user_id=developer["user_id"])})

    def test_questions_are_not_in_the_audit_log(self):
        user = self.make()
        client = self.signed_in(user)
        client.post("/api/agent/stream", json={"question": ""})
        self.assertNotIn("agent_stream", {e["action"] for e in self.latest(user_id=user["user_id"])})

    def test_the_log_is_append_only(self):
        audit.record("test_entry", None, details={"n": 1})
        with audit._connect() as connection:
            for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
                with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                    with connection.transaction():
                        connection.execute(statement)
        self.assertTrue(self.latest(action="test_entry"))

    def test_admins_list_filter_and_download(self):
        admin, developer = self.make("admin", "Ada"), self.make("developer", "Dev")
        self.signed_in(developer).post("/api/users", json={})
        client = self.signed_in(admin)
        body = client.get(f"/api/audit?user={developer['user_id']}").json()
        self.assertTrue(body["entries"])
        self.assertTrue(all(e["user_id"] == developer["user_id"] for e in body["entries"]))
        self.assertIn("refused", body["actions"])
        csv = client.get(f"/api/audit.csv?user={developer['user_id']}")
        self.assertEqual(csv.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("refused", csv.text)
        self.assertEqual(client.get("/api/audit?since=not-a-date").status_code, 400)
        self.assertEqual(self.signed_in(developer).get("/api/audit").status_code, 403)


class AnswerCache(UsersDatabase):
    """R19: checked answers reused for questions that mean the same."""

    VECTORS = {"fare?": [1.0, 0.0, 0.0], "how much is the fare?": [0.99, 0.05, 0.0], "weather?": [0.0, 1.0, 0.0]}

    def setUp(self):
        import answer_cache
        self.cache = answer_cache
        patcher = patch.object(answer_cache, "_embed", side_effect=lambda q: self.VECTORS[q])
        patcher.start()
        self.addCleanup(patcher.stop)
        answer_cache.clear()

    GOOD = {"answer": "£12 [1]", "answer_check": {"passed": True}, "guardrails": {"input": {"decision": "ALLOW"}},
            "calls": [], "research": [], "flow": {"id": "f", "version": 1}}

    def test_only_checked_answers_using_nothing_that_changes_are_kept(self):
        self.assertTrue(self.cache.cacheable(self.GOOD))
        for change in ({"answer_check": {"passed": False}}, {"calls": [{"tool": "get_weather"}]},
                       {"research": [{"url": "x"}]}, {"guardrails": {"output": {"decision": "BLOCK"}}},
                       {"budget": {"hit": "the budget"}}, {"privacy": {"masked": ["email"]}}):
            with self.subTest(change=change):
                self.assertFalse(self.cache.cacheable({**self.GOOD, **change}))
        self.assertFalse(self.cache.eligible_question("fare?", follow_up=True))
        self.assertFalse(self.cache.eligible_question("fare for jo@example.com?", follow_up=False))

    def test_the_same_meaning_hits_and_anything_else_misses(self):
        before = self.cache.stats()
        self.cache.store("fare?", "f", 1, self.GOOD)
        self.assertEqual(self.cache.lookup("how much is the fare?", "f", 1)["answer"], "£12 [1]")
        self.assertIsNone(self.cache.lookup("weather?", "f", 1))           # another question
        self.assertIsNone(self.cache.lookup("fare?", "f", 2))              # another version
        stats = self.cache.stats()
        self.assertEqual((stats["entries"], stats["hits"] - before["hits"], stats["misses"] - before["misses"]), (1, 1, 2))
        self.assertEqual(self.cache.clear(), 1)
        self.assertIsNone(self.cache.lookup("fare?", "f", 1))

    def test_a_question_that_differs_in_a_key_word_misses_even_when_its_embedding_matches(self):
        anytime, off_peak = "How much is an Anytime return to Highfold?", "How much is an Off-Peak return to Highfold?"
        reworded = "What is the price of an Anytime return to Highfold?"
        self.VECTORS.update({anytime: [1.0, 0.0, 0.0], off_peak: [1.0, 0.0, 0.0], reworded: [1.0, 0.0, 0.0]})
        self.cache.store(anytime, "f", 1, self.GOOD)
        self.assertIsNone(self.cache.lookup(off_peak, "f", 1))          # a wrong hit would be a wrong fare
        self.assertIsNotNone(self.cache.lookup(reworded, "f", 1))

    def test_a_change_to_the_knowledge_base_misses(self):
        self.cache.store("fare?", "f", 1, self.GOOD)
        with patch.object(self.cache, "knowledge", return_value="changed"):
            self.assertIsNone(self.cache.lookup("fare?", "f", 1))

    def test_the_ask_route_answers_from_the_cache_without_running_the_agents(self):
        from unittest.mock import AsyncMock
        user = self.make()
        client = self.signed_in(user)
        with patch.object(web_api, "_not_set_up", AsyncMock(return_value=None)), \
                patch.object(web_api.answer_cache, "lookup", return_value=self.GOOD), \
                patch.object(web_api.agent, "live_flow", return_value=(Mock(id="f", nodes=[]), 1)), \
                patch.object(web_api.agent, "answer", side_effect=AssertionError("ran the agents")):
            response = client.post("/api/agent/stream", json={"question": "fare?"})
        result = json.loads(response.text.split("event: result\ndata: ")[1].split("\n")[0])
        self.assertEqual((result["cached"], result["answer"]), (True, "£12 [1]"))

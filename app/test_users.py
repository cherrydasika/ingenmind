"""User administration (/api/users, identity.guard_change). Runs against its
own database (needs the pgvector service)."""

import unittest
import uuid

import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import agent_memory
import flows
import identity
import sessions
import web_api
from common import config


class Users(unittest.TestCase):
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

    def make(self, role="user", name="Pat"):
        return identity.create_user(name, f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com", role)

    def signed_in(self, user):
        client = TestClient(web_api.app, base_url="http://localhost")
        client.post("/auth/dev", data={"user_id": user["user_id"]})
        return client

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

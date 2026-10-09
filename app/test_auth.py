"""Sign-in, users and the permission check on every API route. Runs against
its own database (needs the pgvector service)."""

import re
import unittest
import uuid
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql
from starlette.routing import Route
from starlette.testclient import TestClient

import auth
import flows
import identity
import sessions
import web_api
from common import config


class AuthDatabase(unittest.TestCase):
    """A fresh database per test class, so users never leak between tests."""

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

    def client(self, host="localhost"):
        return TestClient(web_api.app, base_url=f"http://{host}")

    def signed_in(self, user):
        client = self.client()
        response = client.post("/auth/dev", data={"user_id": user["user_id"]}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return client


class EveryRouteHasARule(unittest.TestCase):
    def test_every_api_route_names_its_permission(self):
        missing = []
        for route in web_api.app.routes:
            if not isinstance(route, Route) or not route.path.startswith("/api/"):
                continue
            path = re.sub(r"\{[^}:]+:int\}", "1", re.sub(r"\{[^}:]+\}", "x", route.path))
            for method in route.methods - {"HEAD"}:
                if auth.required_permission(method, path) == auth.NO_RULE:
                    missing.append(f"{method} {route.path}")
        self.assertEqual(missing, [])

    def test_rules(self):
        self.assertIsNone(auth.required_permission("GET", "/api/config"))
        self.assertEqual(auth.required_permission("POST", "/api/agent/stream"), "query_rag")
        self.assertEqual(auth.required_permission("GET", "/api/flows"), ("manage_agents", "run_evaluations"))
        self.assertEqual(auth.required_permission("POST", "/api/flows/x/publish"), "manage_agents")
        self.assertEqual(auth.required_permission("POST", "/api/sources/remove"), "manage_knowledge")
        self.assertEqual(auth.required_permission("GET", "/api/source-reports"), "view_knowledge")
        self.assertEqual(auth.required_permission("POST", "/api/source-reports/remove"), "manage_knowledge")
        self.assertIsNone(auth.required_permission("GET", "/api/knowledge-system"))
        self.assertEqual(auth.required_permission("POST", "/api/knowledge-system/reset"), "manage_settings")
        for method, path in (("GET", "/api/setup"), ("POST", "/api/setup/message"), ("POST", "/api/setup/confirm"),
                             ("POST", "/api/setup/back"), ("POST", "/api/setup/blueprint"),
                             ("POST", "/api/setup/blueprint/revise"), ("POST", "/api/setup/blueprint/confirm"),
                             ("POST", "/api/setup/sources/discover"), ("POST", "/api/setup/sources/add"),
                             ("POST", "/api/setup/sources/continue"), ("POST", "/api/setup/sources/12"),
                             ("POST", "/api/setup/content/analyse"), ("GET", "/api/setup/content/7"),
                             ("POST", "/api/setup/content/7"), ("POST", "/api/setup/content/7/urls"),
                             ("POST", "/api/setup/back-to-sources"), ("POST", "/api/setup/content/7/ttl"),
                             ("GET", "/api/setup/plan"), ("POST", "/api/setup/plan/approve"),
                             ("POST", "/api/setup/back-to-content"), ("GET", "/api/setup/evaluation"),
                             ("POST", "/api/setup/evaluation/prepare")):
            self.assertEqual(auth.required_permission(method, path), "manage_settings")
        self.assertEqual(auth.required_permission("POST", "/api/ingestion/trigger"), "manage_knowledge")
        self.assertEqual(auth.required_permission("DELETE", "/api/nothing-here"), auth.NO_RULE)

    def test_roles_are_bundles_of_known_permissions(self):
        for role, permissions in identity.ROLES.items():
            self.assertTrue(set(permissions) <= set(identity.PERMISSIONS), role)
        self.assertEqual(set(identity.ROLES["admin"]), set(identity.PERMISSIONS))


class StartupRefusals(unittest.TestCase):
    def test_dev_sign_in_is_refused_on_aws(self):
        with patch.object(auth, "MODE", "dev"), patch.object(auth, "DEPLOYMENT", "aws"):
            self.assertIn("refused", auth.config_error())

    def test_oidc_needs_its_settings(self):
        with patch.object(auth, "MODE", "oidc"), patch.object(auth, "ISSUER", ""), \
                patch.object(auth, "CLIENT_ID", ""), patch.dict("os.environ", {"SESSION_SECRET": ""}):
            error = auth.config_error()
        for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "SESSION_SECRET"):
            self.assertIn(name, error)

    def test_unknown_mode(self):
        with patch.object(auth, "MODE", "none"):
            self.assertIn("not one of", auth.config_error())


class ProviderLogout(unittest.TestCase):
    def test_a_logout_url_ends_the_providers_sign_in_too(self):
        class Client:
            async def load_server_metadata(self):   # Cognito lists an endpoint that ignores post_logout_redirect_uri
                return {"end_session_endpoint": "https://rag.auth.eu-west-2.amazoncognito.com/logout"}
        template = "https://rag.auth.eu-west-2.amazoncognito.com/logout?client_id={client_id}&logout_uri={return}"
        with patch.object(auth, "MODE", "oidc"), patch.object(auth, "CLIENT_ID", "abc123"), \
                patch.object(auth, "LOGOUT_URL", template), patch.object(auth, "_client", return_value=Client()):
            body = TestClient(web_api.app, base_url="http://localhost:28000").post("/auth/logout").json()
        self.assertEqual(body["provider_logout"], "https://rag.auth.eu-west-2.amazoncognito.com/logout"
                                                  "?client_id=abc123&logout_uri=http%3A%2F%2Flocalhost%3A28000%2F")
        # Without OIDC_LOGOUT_URL, discovery's endpoint with the standard parameters.
        with patch.object(auth, "MODE", "oidc"), patch.object(auth, "CLIENT_ID", "abc123"), \
                patch.object(auth, "LOGOUT_URL", ""), patch.object(auth, "_client", return_value=Client()):
            body = TestClient(web_api.app, base_url="http://localhost:28000").post("/auth/logout").json()
        self.assertIn("post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A28000%2F", body["provider_logout"])


class SigningIn(AuthDatabase):
    def test_signed_out_requests_are_refused(self):
        client = self.client()
        response = client.get("/api/flows")
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(client.get("/api/me").json()["user"])
        self.assertEqual(client.get("/").status_code, 200)   # the page loads and shows the sign-in screen

    def test_dev_sign_in_answers_only_on_localhost_and_never_on_aws(self):
        self.assertEqual(self.client("rag.example.com").get("/auth/dev").status_code, 404)
        self.assertEqual(self.client("rag.example.com").post("/auth/dev", data={}).status_code, 404)
        with patch.object(auth, "DEPLOYMENT", "aws"):
            self.assertEqual(self.client().get("/auth/dev").status_code, 404)

    def test_permissions_decide_what_a_user_may_do(self):
        viewer = identity.create_user("Vic", f"vic-{uuid.uuid4().hex[:6]}@example.com", "viewer")
        client = self.signed_in(viewer)
        self.assertEqual(client.get("/api/config").status_code, 200)        # any signed-in user
        self.assertEqual(client.get("/api/flows").status_code, 403)         # needs manage_agents or run_evaluations
        self.assertEqual(client.post("/api/agent/stream", json={"question": "hi"}).status_code, 403)
        identity.update_user(viewer["user_id"], {"permissions": ["view_knowledge", "run_evaluations"]})
        self.assertEqual(client.get("/api/flows").status_code, 200)         # granted: checked on every request

    def test_a_deactivated_user_is_signed_out(self):
        user = identity.create_user("Dee", f"dee-{uuid.uuid4().hex[:6]}@example.com", "user")
        client = self.signed_in(user)
        self.assertEqual(client.get("/api/me").json()["user"]["first_name"], "Dee")
        identity.update_user(user["user_id"], {"status": "disabled"})
        self.assertEqual(client.get("/api/config").status_code, 401)
        identity.update_user(user["user_id"], {"status": "active"})
        self.assertIsNone(client.get("/api/me").json()["user"])            # stays signed out

    def test_a_tampered_cookie_is_ignored(self):
        user = identity.create_user("Tam", f"tam-{uuid.uuid4().hex[:6]}@example.com", "admin")
        client = self.signed_in(user)
        value = client.cookies.get("rag_session")
        client.cookies.set("rag_session", value[:-4] + ("AAAA" if not value.endswith("AAAA") else "BBBB"))
        self.assertEqual(client.get("/api/config").status_code, 401)

    def test_logout_ends_the_sign_in(self):
        user = identity.create_user("Lou", f"lou-{uuid.uuid4().hex[:6]}@example.com", "user")
        client = self.signed_in(user)
        self.assertEqual(client.post("/auth/logout").json()["ok"], True)
        self.assertIsNone(client.get("/api/me").json()["user"])
        self.assertEqual(client.get("/api/config").status_code, 401)

    def test_the_browser_cannot_claim_another_user(self):
        user = identity.create_user("Sam", f"sam-{uuid.uuid4().hex[:6]}@example.com", "user")
        client = self.signed_in(user)
        seen = {}
        def payload(question, session_id, body, max_iterations, emit, flow=None, source="users", record=False):
            seen.update(session_id=session_id, user_id=body["user_id"])
            return {"answer": "ok"}
        with patch.object(web_api, "_agent_payload", side_effect=payload), \
                patch.object(web_api.knowledge_system, "is_ready", return_value=True):
            client.post("/api/agent/stream", json={"question": "hi", "session_id": "abc", "user_id": "u_admin"})
        self.assertEqual(seen["user_id"], user["user_id"])
        self.assertTrue(seen["session_id"].startswith("s_"))   # the server's session, not the browser's "abc"
        self.assertEqual(web_api._scoped(Mock(state=Mock(user=user)), {"session_id": "abc", "user_id": "u_admin"}),
                         {"session_id": f"{user['user_id']}:abc", "user_id": user["user_id"]})   # other routes


class FirstAdmin(AuthDatabase):
    def test_first_visit_creates_the_admin_then_users_are_picked(self):
        client = self.client()
        self.assertIn("Create the first admin", client.get("/auth/dev").text)
        client.post("/auth/dev", data={"first_name": "Ada", "email": "ada@example.com"})
        me = client.get("/api/me").json()["user"]
        self.assertEqual((me["first_name"], me["role"]), ("Ada", "admin"))
        self.assertEqual(set(me["permissions"]), set(identity.PERMISSIONS))
        self.assertIsNotNone(me["last_login"])
        self.assertIn("Sign in as", self.client().get("/auth/dev").text)
        # Once users exist, the form cannot create another admin.
        other = self.client()
        other.post("/auth/dev", data={"first_name": "Mallory", "email": "m@example.com"})
        self.assertIsNone(other.get("/api/me").json()["user"])
        self.assertNotIn("m@example.com", [u["email"] for u in identity.list_users()])
        # A dev sign-in links no provider identity: a real provider can still find Ada by email.
        self.assertIsNone(identity.get_user(me["user_id"])["auth_subject"])


class Invitations(AuthDatabase):
    def test_strangers_are_not_let_in(self):
        with self.assertRaises(identity.NotInvited):
            identity.sign_in("https://idp", "sub-stranger", "stranger@example.com", "Stan")

    def test_an_admin_email_gets_in_as_admin(self):
        with patch.dict("os.environ", {"ADMIN_EMAILS": "Boss@Example.com"}):
            user = identity.sign_in("https://idp", "sub-boss", "boss@example.com", "Bo")
        self.assertEqual((user["role"], user["first_name"]), ("admin", "Bo"))
        # Later sign-ins match the provider's subject, whatever the email says.
        self.assertEqual(identity.sign_in("https://idp", "sub-boss", "changed@example.com", "Bo")["user_id"],
                         user["user_id"])

    def test_an_invited_user_is_linked_by_email_once(self):
        invited = identity.create_user("Ivy", "ivy@example.com", "user", created_by="test")
        user = identity.sign_in("https://idp", "sub-ivy", "IVY@example.com", "Ivy")
        self.assertEqual((user["user_id"], user["permissions"]), (invited["user_id"], ["query_rag"]))
        # Another provider account with the same email does not take it over.
        with self.assertRaises(identity.NotInvited):
            identity.sign_in("https://idp", "sub-someone-else", "ivy@example.com", "Ivy")

    def test_disabled_users_cannot_sign_in(self):
        user = identity.create_user("Dot", "dot@example.com", "user")
        identity.update_user(user["user_id"], {"status": "disabled"})
        with self.assertRaises(identity.Disabled):
            identity.sign_in("https://idp", "sub-dot", "dot@example.com", "Dot")

    def test_bad_input_is_refused(self):
        for args in (("", "a@example.com", "user"), ("A", "not-an-email", "user"), ("A", "b@example.com", "boss")):
            with self.assertRaises(ValueError):
                identity.create_user(*args)
        with self.assertRaises(ValueError):
            identity.create_user("A", "c@example.com", "user", ["fly"])


if __name__ == "__main__":
    unittest.main()

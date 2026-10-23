"""API keys and the versioned API (R46, api_keys.py, public_api.py)."""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import api_keys
import identity
import web_api
from test_auth import AuthDatabase


class Keys(AuthDatabase):
    def setUp(self):
        api_keys._schema_ready = False
        self.addCleanup(setattr, api_keys, "_schema_ready", False)
        self.user = identity.create_user("Kim", f"kim-{uuid.uuid4().hex[:6]}@example.com", "user")

    def bearer(self, key):
        return {"Authorization": f"Bearer {key}"}

    def test_a_key_is_shown_once_stored_hashed_and_scoped_to_its_owner(self):
        key, record = api_keys.create(self.user, "Help desk bot", ["ask"], 30)
        self.assertTrue(key.startswith(f"im_{record['key_id']}_"))
        self.assertNotIn("digest", record)
        self.assertNotIn(key, json.dumps(api_keys.list_keys(self.user["user_id"])))       # never listed
        with api_keys._connect() as connection:
            stored = connection.execute("SELECT digest FROM api_keys").fetchone()["digest"]
        self.assertNotEqual(stored, key)
        for bad in ((["ingest"], 30), (["ask"], 0), (["ask"], 400), ([], 30), (["root"], 30)):
            with self.assertRaises(api_keys.ApiKeyError, msg=bad):
                api_keys.create(self.user, "x", *bad)                   # ingest: a user's permissions don't cover it
        found, owner = api_keys.check(key)
        self.assertEqual((found["key_id"], owner["user_id"]), (record["key_id"], self.user["user_id"]))
        self.assertIsNone(api_keys.check(key[:-2] + "zz"))
        self.assertIsNone(api_keys.check("not-a-key"))

    def test_expired_revoked_and_deactivated_keys_are_refused(self):
        key, record = api_keys.create(self.user, "bot", ["ask"], 1)
        with api_keys._connect() as connection:
            connection.execute("UPDATE api_keys SET expires_at = %s", (datetime.now(timezone.utc) - timedelta(seconds=1),))
        self.assertIsNone(api_keys.check(key))
        key, record = api_keys.create(self.user, "bot", ["ask"], 1)
        self.assertFalse(api_keys.revoke(record["key_id"], "someone_else"))
        self.assertTrue(api_keys.revoke(record["key_id"], self.user["user_id"]))
        self.assertIsNone(api_keys.check(key))
        key, _ = api_keys.create(self.user, "bot", ["ask"], 1)
        identity.update_user(self.user["user_id"], {"status": "disabled"}, None)
        self.assertIsNone(api_keys.check(key))

    def test_the_versioned_api_with_a_key(self):
        key, record = api_keys.create(self.user, "bot", ["ask"], 1)
        client = self.client()
        answer = {"answer": "Bikes go free [1].", "sources": [{"url": "https://rail.example", "cited": [1]}],
                  "answer_check": {"passed": True}, "status": "done", "searches": [{"secret": "x"}]}
        with patch.object(web_api, "_not_set_up", AsyncMock(return_value=None)), \
                patch.object(web_api, "_ask_live", return_value=answer) as ask:
            response = client.post("/api/v1/ask", json={"question": "Bikes?"}, headers=self.bearer(key))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["answer"], body["sources"][0]["cited"]), ("Bikes go free [1].", [1]))
        self.assertNotIn("searches", body)                                   # only the v1 fields
        self.assertEqual(ask.call_args.args[2]["user_id"], self.user["user_id"])
        self.assertTrue(body["session_id"].startswith("s_"))
        self.assertEqual(client.get("/api/v1/sessions", headers=self.bearer(key)).status_code, 200)
        self.assertEqual(client.get("/api/v1/documents", headers=self.bearer(key)).status_code, 403)   # no ingest scope
        self.assertEqual(client.get("/api/collections", headers=self.bearer(key)).status_code, 401)    # not v1
        self.assertEqual(client.post("/api/v1/ask", json={"question": "x"}).status_code, 401)          # no key, no session
        api_keys.revoke(record["key_id"])
        refused = client.post("/api/v1/ask", json={"question": "Bikes?"}, headers=self.bearer(key))
        self.assertEqual((refused.status_code, refused.json()["error"]), (401, "Invalid, expired or revoked API key"))

    def test_the_description_is_public(self):
        client = self.client()
        spec = client.get("/api/v1/openapi.json")
        self.assertEqual(spec.status_code, 200)
        self.assertIn("/ask", spec.json()["paths"])
        self.assertIn("Authorization: Bearer", client.get("/api/v1/docs").text)

    def test_keys_through_the_app(self):
        client = self.signed_in(self.user)
        made = client.post("/api/me/keys", json={"name": "bot", "scopes": ["ask"], "days": 7})
        self.assertEqual(made.status_code, 201)
        self.assertEqual(client.get("/api/me/keys").json()["scopes"], ["ask"])
        self.assertEqual(client.post("/api/me/keys", json={"name": "bot", "scopes": ["admin"]}).status_code, 400)
        self.assertEqual(client.delete(f"/api/me/keys/{made.json()['record']['key_id']}").status_code, 200)
        self.assertEqual(client.get("/api/keys").status_code, 403)          # everyone's: manage_users only


if __name__ == "__main__":
    unittest.main()

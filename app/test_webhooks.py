"""Outbound webhooks (R47, webhooks.py)."""

import json
import time
from unittest.mock import patch

from cryptography.fernet import Fernet

import flows
import ingestion_worker
import webhooks
from test_auth import AuthDatabase

KEY = {"INTEGRATIONS_KEY": Fernet.generate_key().decode()}


class Webhooks(AuthDatabase):
    def setUp(self):
        webhooks._schema_ready = False
        self.addCleanup(setattr, webhooks, "_schema_ready", False)
        env = patch.dict("os.environ", KEY)
        env.start()
        self.addCleanup(env.stop)
        inline = patch.object(webhooks, "_later", side_effect=lambda work: None)   # sent by hand below
        inline.start()
        self.addCleanup(inline.stop)
        for hook in webhooks.hooks():
            webhooks.remove(hook["hook_id"])

    def test_a_hook_needs_a_url_events_and_somewhere_safe_for_its_secret(self):
        for args in (("", "https://x/h", ["ping"]), ("H", "ftp://x/h", ["ping"]), ("H", "https://x/h", []),
                     ("H", "https://x/h", ["everything"])):
            with self.subTest(args=args), self.assertRaises(webhooks.WebhookError):
                webhooks.create(*args)
        with patch.dict("os.environ", {"INTEGRATIONS_KEY": ""}), self.assertRaises(webhooks.WebhookError):
            webhooks.create("H", "https://x/h", ["ping"])
        secret, hook = webhooks.create("H", "https://x/h", ["ping"])
        self.assertTrue(secret.startswith("whsec_"))
        self.assertNotIn(secret, json.dumps(webhooks.hooks()))

    def test_deliveries_are_signed_private_text_only_for_trusted_hooks_and_retried(self):
        secret, plain = webhooks.create("Plain", "https://plain.example/h", ["knowledge.gap"])
        _, trusted = webhooks.create("Trusted", "https://trusted.example/h", ["knowledge.gap"], trusted=True)
        webhooks.create("Other", "https://other.example/h", ["version.live"])
        ids = webhooks.emit("knowledge.gap", {"run_id": "r1"}, private={"task": "When is the last train?"})
        self.assertEqual(len(ids), 2)                                      # only the hooks that want it
        sent = []

        def post(url, body, headers):
            sent.append((url, body, headers))
            return 200 if "trusted" in url else 500
        statuses = sorted(webhooks.deliver(i, post=post) for i in ids)
        self.assertEqual(statuses, ["delivered", "pending"])
        plain_body = next(b for u, b, h in sent if "plain" in u)
        trusted_body = next(b for u, b, h in sent if "trusted" in u)
        self.assertNotIn("last train", plain_body.decode())
        self.assertIn("last train", trusted_body.decode())
        headers = next(h for u, b, h in sent if "plain" in u)
        self.assertEqual(headers["X-IngenMind-Event"], "knowledge.gap")
        self.assertTrue(webhooks.verify(secret, headers["X-IngenMind-Timestamp"], plain_body, headers["X-IngenMind-Signature"]))
        self.assertFalse(webhooks.verify(secret, headers["X-IngenMind-Timestamp"], plain_body + b" ", headers["X-IngenMind-Signature"]))
        self.assertFalse(webhooks.verify(secret, str(int(time.time()) - 3600), plain_body, headers["X-IngenMind-Signature"]))
        failing = next(i for i in ids if webhooks.deliveries(plain["hook_id"])[0]["delivery_id"] == i)
        with webhooks._connect() as connection:
            connection.execute("UPDATE webhook_deliveries SET next_attempt_at = now() WHERE delivery_id = %s", (failing,))
        self.assertEqual(webhooks.due(), [failing])                         # its retry is due once
        self.assertEqual(webhooks.due(), [])
        for _ in webhooks.RETRY_AFTER:
            last = webhooks.deliver(failing, post=lambda *a: 503)
        self.assertEqual(last, "failed")                                    # given up after the retries
        self.assertEqual(webhooks.deliveries(plain["hook_id"])[0]["attempts"], len(webhooks.RETRY_AFTER) + 1)

    def test_events_come_from_where_things_happen(self):
        with patch.object(webhooks, "emit") as emit:
            flows.store.set_live(flows.load_flow().id, 0, flows.load_flow(), lambda spec: None)
            ingestion_worker._finished({"id": 7, "kind": "ingest"}, "success", {"urls": 2, "chunks_written": 5, "counts": {},
                                                                                "failed": ["x"]})
        events = [c.args[0] for c in emit.call_args_list]
        self.assertEqual(events, ["version.live", "ingestion.finished"])
        self.assertEqual(emit.call_args_list[1].args[1]["failed"], 1)        # counts, never page content


if __name__ == "__main__":
    import unittest
    unittest.main()

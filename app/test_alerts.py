"""Alerts (R55, alerts.py): each rule fires, lands in Slack and email, resolves, mutes; against the test database."""

import json
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet

import alerts
import identity
import operations
import webhooks
from common import storage
from flows import store
from test_auth import AuthDatabase

KEY = {"INTEGRATIONS_KEY": Fernet.generate_key().decode()}
SMTP = {"SMTP_HOST": "mail.test", "SMTP_PORT": "25", "SMTP_TLS": "none", "ALERT_EMAIL_FROM": "alerts@ingenmind.test"}
PRICES = ({"m": {"input": 10.0, "output": 20.0}}, "m")


class FakeSMTP:
    """Stands in for smtplib.SMTP: keeps what would have been sent."""
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


class Alerts(AuthDatabase):
    def setUp(self):
        for module in (alerts, store, webhooks):
            module.reset_schema_cache() if hasattr(module, "reset_schema_cache") else None
        self.addCleanup(alerts.reset_schema_cache)
        for p in (patch.dict("os.environ", {**KEY, **SMTP}), patch.object(operations, "_prices", lambda: PRICES),
                  patch.object(alerts.smtplib, "SMTP", FakeSMTP), patch.object(webhooks, "_later", lambda work: None),
                  patch.object(alerts, "_post", side_effect=self.slack)):
            p.start()
            self.addCleanup(p.stop)
        alerts.ensure_schema()
        operations.ensure_schema()
        storage.ensure_collection(storage.get_client())
        with alerts._connect() as connection:            # the class shares one database
            for table in ("agent_flow_runs", "alert_events", "alert_settings", "ingestion_jobs"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("UPDATE alert_rules SET state = 'ok', muted_until = NULL, enabled = true, value = NULL")
        self.posted, FakeSMTP.sent = [], []
        alerts.save_settings("https://hooks.slack.com/services/T0/B0/x", ["ops@example.com"])
        self.user = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")

    def slack(self, url, body):
        self.posted.append((url, json.loads(body)))
        return 200

    def bad_day(self):
        """Users' runs that breach every measured rule at once."""
        for n in range(12):
            store.record_run("f", 1, "users", {"seconds": 100, "input_tokens": 2_000_000, "output_tokens": 0,
                                               "evaluated": n < 10, "passed": n < 4, "failed": n in (10, 11),
                                               "input_blocked": n in (6, 7, 8, 9)})
        client = storage.get_client()
        job = storage.enqueue_job(client, "ingest_urls", [{"url": "fixture://one"}])
        storage.update_job(client, job["id"], "failed", 0, {}, "refused")

    def test_each_rule_fires_lands_in_slack_and_email_then_resolves(self):
        self.bad_day()
        with patch("recrawl.freshness", return_value=[{"stale": True}, {"stale": False}]):
            fired = {e["kind"]: e for e in alerts.check()}
            self.assertEqual(alerts.check(), [])                          # still breached: no repeat
        measured = [k for k, r in alerts.RULES.items() if r["breach"] != "event"]
        self.assertEqual(sorted(fired), sorted(measured))
        self.assertEqual(fired["pass_rate"]["value"], 0.4)
        self.assertIn("40.0%", fired["pass_rate"]["message"])
        self.assertEqual(fired["daily_cost"]["value"], 240.0)              # 12 × 2M tokens × $10/M
        regression = alerts.event("eval_regression", 0.5, {"flow_id": "f", "drop": 0.3})
        for e in [*fired.values(), regression]:
            self.assertEqual(e["sent"], {"slack": "ok", "email": "ok"}, e["kind"])
        self.assertEqual(len(self.posted), len(measured) + 1)
        self.assertTrue(all(url.startswith("https://hooks.slack.com/") for url, _ in self.posted))
        self.assertEqual((FakeSMTP.sent[0]["To"], FakeSMTP.sent[0]["From"]), ("ops@example.com", "alerts@ingenmind.test"))
        self.assertEqual(len(FakeSMTP.sent), len(measured) + 1)

        with alerts._connect() as connection:                             # the bad hour is over
            connection.execute("DELETE FROM agent_flow_runs")
            connection.execute("DELETE FROM ingestion_jobs")
        with patch("recrawl.freshness", return_value=[]):
            resolved = {e["kind"]: e["status"] for e in alerts.check()}
        # Rates with too few runs to judge stay as they were; counts and cost clear.
        self.assertEqual(resolved, {"daily_cost": "resolved", "ingestion_failures": "resolved", "stale_sources": "resolved"})
        self.assertEqual([e["status"] for e in alerts.history()][:1], ["resolved"])

    def test_muted_rules_record_but_do_not_send(self):
        alerts.update_rule("ingestion_failures", mute_minutes=60)
        self.bad_day()
        with patch("recrawl.freshness", return_value=[]):
            fired = {e["kind"]: e for e in alerts.check()}
        self.assertTrue(fired["ingestion_failures"]["muted"])
        self.assertEqual(fired["ingestion_failures"]["sent"], {})
        self.assertFalse(fired["error_rate"]["muted"])
        self.assertTrue(alerts.update_rule("ingestion_failures", mute_minutes=0)["muted_until"] is None)

    def test_a_test_alert_goes_everywhere_with_the_value_now(self):
        out = alerts.test("p95_latency")
        self.assertEqual((out["status"], out["sent"]), ("test", {"slack": "ok", "email": "ok"}))
        self.assertTrue(out["message"].startswith("Test alert:"))
        self.assertEqual(alerts.history()[0]["status"], "test")

    def test_rules_and_settings_are_checked(self):
        for bad in ({"threshold": 2}, {"threshold": -1}, {"window": 1}, {"mute_minutes": -5}):
            with self.subTest(bad=bad), self.assertRaises(alerts.AlertError):
                alerts.update_rule("pass_rate", **bad)
        with self.assertRaises(alerts.AlertError):
            alerts.update_rule("eval_regression", threshold=0.1)       # an event: no threshold
        self.assertEqual(alerts.update_rule("p95_latency", threshold=30, window=120)["threshold"], 30)
        with self.assertRaises(alerts.AlertError):
            alerts.save_settings("https://evil.example/hook", None)
        self.assertEqual(alerts.settings()["slack"], True)
        with alerts._connect() as connection:                           # sealed, not stored as given
            self.assertNotIn("hooks.slack.com", connection.execute("SELECT slack FROM alert_settings").fetchone()["slack"])
        self.assertFalse(alerts.save_settings(clear_slack=True)["slack"])

    def test_admins_only_and_the_url_stays_out_of_the_audit_log(self):
        admin = self.signed_in(self.user)
        self.assertEqual(admin.get("/api/alerts").status_code, 200)
        url = "https://hooks.slack.com/services/T0/B0/secretpart"
        self.assertEqual(admin.put("/api/alerts/settings", json={"slack_webhook_url": url}).status_code, 200)
        self.assertEqual(admin.patch("/api/alerts/rules/pass_rate", json={"threshold": 3}).status_code, 400)
        self.assertEqual(admin.post("/api/alerts/rules/nope/test").status_code, 404)
        with alerts._connect() as connection:
            logged = json.dumps([r["details"] for r in connection.execute(
                "SELECT details FROM audit_log WHERE path = '/api/alerts/settings'").fetchall()])
        self.assertNotIn("secretpart", logged)
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.assertEqual(self.signed_in(person).get("/api/alerts").status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()

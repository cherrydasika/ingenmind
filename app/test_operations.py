"""Operations (R53, operations.py): latency, errors, tokens and cost from the run records, against the test database."""

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import agent
import answer_cache
import identity
import operations
from common import storage
from flows import store
from test_auth import AuthDatabase

PRICES = ({"big": {"input": 10.0, "output": 20.0}, "small": {"input": 1.0, "output": 2.0}}, "big")


class Operations(AuthDatabase):
    def setUp(self):
        for module in (store, answer_cache, identity):    # other tests drop tables
            module.reset_schema_cache()
            self.addCleanup(module.reset_schema_cache)
        operations.ensure_schema()
        storage.ensure_collection(storage.get_client())      # the ingestion queue
        with operations._connect() as connection:            # the class shares one database
            for table in ("agent_flow_runs", "answer_cache_stats", "ingestion_jobs"):
                connection.execute(f"DELETE FROM {table}")
        prices = patch.object(operations, "_prices", lambda: PRICES)
        prices.start()
        self.addCleanup(prices.stop)
        self.user = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")

    def run_at(self, when: datetime, **metrics):
        store.record_run(metrics.pop("flow", "f"), 1, metrics.pop("source", "users"), metrics)
        with operations._connect() as connection:
            connection.execute("UPDATE agent_flow_runs SET started_at = %s WHERE id = (SELECT max(id) FROM agent_flow_runs)",
                               (when,))

    def test_the_report(self):
        yesterday = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
        uid = self.user["user_id"]
        # A run whose agents used both models: the retriever's tokens at its own price, the rest at the run's.
        self.run_at(yesterday, seconds=10, input_tokens=1_000_000, output_tokens=100_000, user_id=uid, channel="app",
                    model="big", agents=[{"role": "knowledge_base", "model": "small", "seconds": 4,
                                          "input_tokens": 600_000, "output_tokens": 50_000, "turns": 2},
                                         {"role": "supervisor", "model": "big", "seconds": 3,
                                          "input_tokens": 300_000, "output_tokens": 40_000, "turns": 1}])
        self.run_at(yesterday, flow="slow", seconds=40, failed=True, timed_out=True, user_id=uid, channel="slack", model="big")
        self.run_at(yesterday, seconds=1, input_blocked=True, channel="app", model="big")
        self.run_at(yesterday - timedelta(days=2), seconds=20, input_tokens=1000, output_tokens=0, budget_hit="the budget",
                    source="eval")                                   # recorded before R53: no model, priced at the current one
        with operations._connect() as connection:
            connection.execute("INSERT INTO answer_cache_stats (day, hits, misses) VALUES (%s, 1, 3)", (yesterday.date(),))

        report = operations.report()
        s = report["summary"]
        self.assertEqual((s["runs"], s["failed"], s["timed_out"], s["input_blocked"], s["budget_hit"]), (4, 1, 1, 1, 1))
        # 600k×1 + 50k×2 (small) + 400k×10 + 50k×20 (big: the supervisor and the rest) = 0.7 + 5.0; plus 1000×10
        self.assertAlmostEqual(s["cost"], 5.71, places=4)
        self.assertEqual(report["cache"], {"hits": 1, "misses": 3, "hit_rate": 0.25})

        y = report["yesterday"]
        self.assertEqual((y["runs"], y["failed"], y["p95"] > 30), (3, 1, True))
        self.assertAlmostEqual(y["cost"], 5.7, places=4)
        self.assertEqual(y["slowest"]["flow"]["name"], "slow")
        self.assertEqual(y["slowest"]["agent"]["name"], "knowledge_base")

        agents = {a["role"]: a for a in report["agents"]}
        self.assertEqual((agents["knowledge_base"]["models"], agents["knowledge_base"]["cost"]), (["small"], 0.7))
        models = {m["model"]: m for m in report["models"]}
        self.assertEqual((models["small"]["cost"], models["big"]["cost"]), (0.7, 5.01))
        self.assertEqual({c["key"]: c["runs"] for c in report["channels"]}, {"app": 2, "slack": 1, "unknown": 1})
        [top, *_] = report["users"]
        self.assertEqual((top["key"], top["name"], top["runs"]), (uid, "Ada", 2))
        self.assertEqual(report["options"]["source"], ["eval", "users"])
        self.assertEqual(report["options"]["model"], ["big"])
        self.assertEqual(sum(d["runs"] for d in report["days"]), 4)

        only = operations.report(filters={"channel": "slack", "nonsense": "x"})
        self.assertEqual((only["summary"]["runs"], only["filters"]), (1, {"channel": "slack"}))
        self.assertEqual(operations.report(filters={"source": "eval"})["summary"]["budget_hit"], 1)
        self.assertEqual(operations.report("2020-01-01", "2020-01-31")["summary"]["runs"], 0)

    def test_ingestion(self):
        client = storage.get_client()
        job = storage.enqueue_job(client, "ingest_urls", [{"url": "fixture://one"}])
        report = operations.report()["ingestion"]
        self.assertEqual((report["queued"], report["failed"]), (1, 0))
        storage.update_job(client, job["id"], "failed", 0, {}, "the site refused")
        report = operations.report()["ingestion"]
        self.assertEqual((report["queued"], report["failed"], report["failures"][0]["error"]), (0, 1, "the site refused"))

    def test_admins_only(self):
        self.assertEqual(self.signed_in(self.user).get("/api/operations?channel=app").status_code, 200)
        self.assertEqual(self.signed_in(self.user).get("/api/operations?from=nope").status_code, 400)
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.assertEqual(self.signed_in(person).get("/api/operations").status_code, 403)


class RunLabels(AuthDatabase):
    def test_timeouts_are_found_in_the_cause(self):
        try:
            try:
                raise TimeoutError()
            except TimeoutError as error:
                raise RuntimeError("the knowledge-base agent failed") from error
        except RuntimeError as error:
            self.assertTrue(agent._timed_out(error))
        self.assertTrue(agent._timed_out(RuntimeError("Read timed out. (read timeout=60)")))
        self.assertFalse(agent._timed_out(ValueError("bad input")))

    def test_agent_usage_per_role_and_model(self):
        run = types.SimpleNamespace(runs=[
            {"role": "knowledge_base", "model": "m", "model_seconds": 1.5, "input_tokens": 10, "output_tokens": 2, "turns": 1},
            {"role": "knowledge_base", "model": "m", "model_seconds": 2.0, "input_tokens": 5, "output_tokens": 1, "turns": 2},
            {"role": "research", "model": "m", "model_seconds": 0, "input_tokens": 0, "output_tokens": 0, "turns": 0}])
        self.assertEqual(agent._agent_usage(run), [{"role": "knowledge_base", "model": "m", "seconds": 3.5,
                                                    "input_tokens": 15, "output_tokens": 3, "turns": 3}])

    def test_labels(self):
        with patch.object(agent, "_model_id", lambda: "big"):
            self.assertEqual(agent._run_labels("u1", {"channel": "teams"}, "users"),
                             {"user_id": "u1", "model": "big", "channel": "teams"})
            self.assertEqual(agent._run_labels("u1", None, "users")["channel"], "app")
            self.assertEqual(agent._run_labels(None, None, "eval")["channel"], "eval")


if __name__ == "__main__":
    import unittest
    unittest.main()

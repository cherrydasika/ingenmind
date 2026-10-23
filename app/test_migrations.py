"""Database migrations on start (migrations.py, R62)."""

import importlib
import os
from unittest.mock import patch

import migrations
from test_auth import AuthDatabase


def _forget_schemas():
    """Every module's "already set up" flag, so the run really creates the tables in this database."""
    for name in migrations.MODULES:
        module = importlib.import_module(name)
        for flag in ("_schema_ready", "_ready", "_setup_done"):
            if isinstance(getattr(module, flag, None), bool):
                setattr(module, flag, False)


class Migrations(AuthDatabase):
    def setUp(self):
        with migrations._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS ingenmind_schema_versions")

    def versions(self):
        with migrations._connect() as connection:
            return [r[0] for r in connection.execute("SELECT version FROM ingenmind_schema_versions ORDER BY id")]

    def test_a_fresh_database_gets_every_modules_tables(self):
        _forget_schemas()
        self.addCleanup(self.reset_caches)
        result = migrations.run("1.0.0", wait=0)
        self.assertEqual((result["version"], result["previous"], result["steps"]), ("1.0.0", None, len(migrations.MODULES) + 1))
        with migrations._connect() as connection:
            tables = {r[0] for r in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        for table in ("app_users", "agent_flow_runs", "audit_log", "api_keys", "use_learnings", "webhooks",
                      "data_retention", "rag_chunks", "ingenmind_schema_versions"):
            self.assertIn(table, tables)
        self.assertEqual(migrations.run("1.0.0", wait=0)["previous"], "1.0.0")    # idempotent: run again on restart

    def test_a_downgrade_is_refused_unless_allowed(self):
        with patch.object(migrations, "_steps", lambda: []):
            migrations.run("1.2.0", wait=0)
            with self.assertRaisesRegex(migrations.Refused, "1.2.0, newer than this release \\(1.1.9\\)"):
                migrations.run("1.1.9", wait=0)
            with self.assertRaises(migrations.Refused):        # a pre-release is older than its release
                migrations.run("1.2.0-rc.1", wait=0)
            migrations.run("1.3.0", wait=0)                    # upgrading is fine
            migrations.run("dev", wait=0)                      # a checkout neither refuses nor is refused
            with patch.dict(os.environ, {"ALLOW_DOWNGRADE": "true"}):
                self.assertEqual(migrations.run("1.0.0", wait=0)["previous"], "1.3.0")
        self.assertEqual(self.versions(), ["1.2.0", "1.3.0", "dev", "1.0.0"])

    def test_a_step_needing_another_runs_after_it_and_a_broken_one_stops_the_start(self):
        done = []

        def needs_b():
            if "b" not in done:
                raise RuntimeError("relation b does not exist")
            done.append("a")

        with patch.object(migrations, "_steps", lambda: [("a", needs_b), ("b", lambda: done.append("b"))]):
            migrations.run("1.0.0", wait=0)
        self.assertEqual(done, ["b", "a"])
        with patch.object(migrations, "_steps", lambda: [("ok", lambda: None), ("broken", lambda: 1 / 0)]):
            with self.assertRaisesRegex(RuntimeError, "broken: division by zero"):
                migrations.run("1.0.1", wait=0)
        self.assertEqual(self.versions(), ["1.0.0"])            # a failed migration isn't recorded

    def test_versions_compare_as_semver(self):
        order = ["0.1.0-rc.4", "0.1.0", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0", "1.0.10", "1.10.0"]
        self.assertEqual(sorted(reversed(order), key=migrations.parse), order)
        self.assertIsNone(migrations.parse("dev"))
        self.assertEqual(migrations.parse("v1.2.3"), migrations.parse("1.2.3"))

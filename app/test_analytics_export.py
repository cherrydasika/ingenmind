"""Analytics export (R57, analytics_export.py): Parquet files DuckDB can query, personal data left out by default."""

import io
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

import duckdb

import analytics_export
import answer_feedback
import flows
import identity
import knowledge_gaps
import sessions
from common import storage
from flows import store
from test_auth import AuthDatabase

SECRET_QUESTION = "Can my daughter Zoë travel alone from Leeds?"


class Export(AuthDatabase):
    def setUp(self):
        for module in (sessions, store):
            module.reset_schema_cache()
        self.user = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        session = sessions.current(self.user["user_id"], None)
        self.turn = sessions.record_turn(session, self.user["user_id"], SECRET_QUESTION, {
            "answer": "Yes, from age 16 [1].", "total": 12.5, "flow": {"id": "f", "version": 1},
            "answer_check": {"passed": True, "overall": 0.9}, "guardrails": {"input": {"decision": "ALLOW"}},
            "sources": [{"url": "https://rail.example/children", "cited": [1]}]})
        store.record_run("f", 1, "users", {"seconds": 12.5, "input_tokens": 100, "output_tokens": 20,
                                           "user_id": self.user["user_id"], "channel": "app", "model": "m",
                                           "agents": [{"role": "supervisor", "model": "m", "seconds": 3, "turns": 1}]})
        answer_feedback.give(self.turn["turn_id"], self.user["user_id"], "down", "incomplete", "my email is ada@example.com")
        flows.evals.ensure_schema()                   # every table, so every file is written
        knowledge_gaps.ensure_schema()
        storage.ensure_collection(storage.get_client())
        storage.ensure_collection(storage.get_client("research"))
        self.folder = Path(tempfile.mkdtemp())

    def query(self, sql: str, folder: Path):
        here = os.getcwd()
        os.chdir(folder)
        try:
            return duckdb.connect().execute(sql).fetchall()
        finally:
            os.chdir(here)

    def everything(self, folder: Path) -> str:
        """All values in every file, as text, to search for what mustn't be there."""
        return " ".join(str(self.query(f"SELECT * FROM '{p.name}'", folder)) for p in folder.glob("*.parquet"))

    def test_without_personal_data_by_default(self):
        manifest = analytics_export.export(self.folder)
        self.assertGreaterEqual(manifest["tables"]["questions"]["rows"], 1)
        self.assertNotIn("question", manifest["tables"]["questions"]["columns"])
        found = self.everything(self.folder)
        for private in (SECRET_QUESTION, "Zoë", "ada@example.com", self.user["email"], "Ada", self.user["user_id"]):
            self.assertNotIn(private, found)
        ref = analytics_export._ref(self.user["user_id"])                                # the same in every export
        [[mine]] = self.query(f"SELECT count(*) FROM 'runs.parquet' WHERE user_ref = '{ref}'", self.folder)
        self.assertGreaterEqual(mine, 1)
        self.assertEqual(self.query("SELECT url, citations FROM 'citations.parquet'", self.folder)[-1],
                         ("https://rail.example/children", 1))

    def test_text_only_when_asked(self):
        analytics_export.export(self.folder, include_text=True)
        [[question]] = self.query(f"SELECT question FROM 'questions.parquet' WHERE turn_id = {self.turn['turn_id']}",
                                  self.folder)
        self.assertEqual(question, SECRET_QUESTION)
        self.assertNotIn(self.user["email"], self.everything(self.folder))      # emails never, even then

    def test_the_starter_queries_run(self):
        manifest = analytics_export.export(self.folder)
        self.assertEqual(manifest["skipped"], [])
        here = os.getcwd()
        os.chdir(self.folder)
        try:
            db = duckdb.connect()
            db.execute((self.folder / "explore.sql").read_text())       # every view and query, as duckdb -init does
            [[weeks]] = db.execute("SELECT count(*) FROM (SELECT date_trunc('week', CAST(asked_at AS TIMESTAMP)) "
                                   "FROM questions GROUP BY 1)").fetchall()
            self.assertGreaterEqual(weeks, 1)
            self.assertIn(("https://rail.example/children",), db.execute("SELECT url FROM citations").fetchall())
        finally:
            os.chdir(here)

    def test_the_download_is_for_admins(self):
        response = self.signed_in(self.user).get("/api/analytics/export")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/zip"))
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertIn("questions.parquet", names)
        self.assertIn("explore.sql", names)
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.assertEqual(self.signed_in(person).get("/api/analytics/export").status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()

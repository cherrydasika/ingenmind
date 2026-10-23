"""Learning from live use (R58, use_learnings.py): outcomes per question, recommendations an admin approves;
nothing changes sources or chunks without an approval, and a reset clears the learnings."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

import answer_feedback
import flows
import identity
import knowledge_gaps
import knowledge_system
import recrawl
import sessions
import use_learnings
import web_api
from common import config, storage
from test_auth import AuthDatabase

GOOD = {"evidence": [{"decision": "GOOD_EVIDENCE"}], "answer_check": {"passed": True, "overall": 0.9}}
WRONG = {"evidence": [{"decision": "GOOD_EVIDENCE"}], "answer_check": {"passed": False, "overall": 0.3,
                                                                       "failed_on": ["correctness"]}}
GAP = {"evidence": [{"decision": "RETRIEVAL_FAILURE"}, {"decision": "KNOWLEDGE_GAP"}],
       "answer_check": {"passed": False, "overall": 0.2}}


def page(url: str) -> dict:
    return {"sources": [{"url": url, "cited": [1]}], "funnel": {"kept": 3, "pages": [url]}}


class Outcomes(AuthDatabase):
    def test_what_each_run_says(self):
        cases = [
            ({"guardrails": {"input": {"decision": "BLOCK"}}}, "blocked"),
            (GAP, "missing_knowledge"),
            ({"evidence": [{"decision": "RETRIEVAL_FAILURE"}]}, "poor_retrieval"),
            ({"evidence": [{"decision": "GOOD_EVIDENCE"}], "funnel": {"kept": 0}}, "poor_retrieval"),
            ({"evidence": [{"decision": "CONFLICTING_EVIDENCE"}]}, "bad_source"),
            (WRONG, "wrong_answer"),
            ({**GOOD, "guardrails": {"output": {"decision": "BLOCK"}}}, "wrong_answer"),
            (GOOD, "worked"),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(use_learnings.outcome(result), expected)


class Recommendations(AuthDatabase):
    BAD, OLD, FINE = "https://rail.example/bad", "https://rail.example/old", "https://rail.example/fine"

    def setUp(self):
        for module in (sessions, use_learnings, knowledge_gaps, answer_feedback, recrawl, knowledge_system):
            module.reset_schema_cache()
        self.admin = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        self.session = sessions.current(self.admin["user_id"], None)
        self.client_store = storage.get_client()
        storage.ensure_collection(self.client_store)
        storage.ensure_collection(storage.get_client("research"))
        with self.client_store.connection() as connection:
            connection.execute("DELETE FROM rag_chunks")
        for url in (self.BAD, self.OLD, self.FINE):
            storage.upsert_chunks(self.client_store, url, [f"text of {url}"], [[1.0] + [0.0] * (config.EMBEDDING_DIM - 1)],
                                  "h", 30)
            knowledge_system.add_url(url)
        recrawl.ensure_schema()
        with recrawl._connect() as connection:          # every page checked just now
            connection.execute("DELETE FROM source_schedules")
            for url in (self.BAD, self.OLD, self.FINE):
                connection.execute("INSERT INTO source_schedules (source_url, every, last_checked_at, status) "
                                   "VALUES (%s, 'weekly', now(), 'unchanged')", (url,))
        for module in (use_learnings, knowledge_gaps, answer_feedback):
            module.ensure_schema()
        with use_learnings._connect() as connection:
            for table in ("use_recommendations", "use_learnings", "knowledge_gaps", "answer_feedback", "ingestion_jobs"):
                connection.execute(f"DELETE FROM {table}")          # each test starts from no live use

    def ask(self, result: dict) -> dict:
        turn = sessions.record_turn(self.session, self.admin["user_id"], "a question", result)
        use_learnings.record(turn["turn_id"], result)
        return turn

    def live_use(self):
        for _ in range(3):
            self.ask({**WRONG, **page(self.BAD)})          # BAD: poor every time, checked recently → drop
        self.ask({**GOOD, **page(self.FINE)})
        old = self.ask({**GOOD, **page(self.OLD)})       # OLD: marked out of date → refresh
        answer_feedback.give(old["turn_id"], self.admin["user_id"], "down", "out_of_date")
        for _ in range(2):                               # the same gap twice, unanswered → add a source
            turn = self.ask({**GAP, **page(self.FINE)})
            knowledge_gaps.record(turn["turn_id"], "q", [{"missing": "Toilets at Leeds station"}], False)

    def knowledge(self) -> dict:
        with self.client_store.connection() as connection:
            return {
                "chunks": connection.execute("SELECT source_url, count(*) AS n FROM rag_chunks GROUP BY 1 ORDER BY 1").fetchall(),
                "urls": connection.execute("SELECT url FROM kb_urls ORDER BY url").fetchall(),
                "jobs": connection.execute("SELECT count(*) AS n FROM ingestion_jobs").fetchone()["n"],
                "checked": connection.execute("SELECT source_url, last_checked_at FROM source_schedules ORDER BY 1").fetchall(),
            }

    def test_recommendations_change_nothing_until_approved(self):
        self.live_use()
        before = self.knowledge()
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            recs = use_learnings.recommend()
        found = {(r["kind"], r["target"]) for r in recs}
        self.assertEqual(found, {("drop_source", self.BAD), ("refresh_source", self.OLD),
                                 ("add_source", "Toilets at Leeds station")})
        refresh = next(r for r in recs if r["kind"] == "refresh_source")
        use_learnings.dismiss(refresh["id"], self.admin["user_id"])
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            again = use_learnings.recommend()               # worked out again: still nothing changed
        self.assertNotIn(("refresh_source", self.OLD), {(r["kind"], r["target"]) for r in again})   # dismissed stays away
        self.assertEqual(self.knowledge(), before)

        client = self.signed_in(self.admin)
        drop = next(r for r in again if r["kind"] == "drop_source")
        done = client.post(f"/api/learnings/recommendations/{drop['id']}/approve", json={})
        self.assertEqual((done.status_code, done.json()["status"], done.json()["decided_by"]),
                         (200, "done", self.admin["user_id"]))
        after = self.knowledge()
        self.assertEqual([r["source_url"] for r in after["chunks"]], [self.FINE, self.OLD])   # only the approved page
        self.assertNotIn({"url": self.BAD}, after["urls"])                                     # nor ingested again
        self.assertEqual(client.post(f"/api/learnings/recommendations/{drop['id']}/approve", json={}).status_code, 404)

    def test_adding_a_source_needs_a_url_and_queues_its_ingestion(self):
        self.live_use()
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            add = next(r for r in use_learnings.recommend() if r["kind"] == "add_source")
        client = self.signed_in(self.admin)
        refused = client.post(f"/api/learnings/recommendations/{add['id']}/approve", json={"url": "not a url"})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(use_learnings.recommendations()[0]["status"], "open")       # still open, nothing queued
        self.assertEqual(self.knowledge()["jobs"], 0)
        done = client.post(f"/api/learnings/recommendations/{add['id']}/approve",
                           json={"url": "https://rail.example/leeds-station"}).json()
        self.assertEqual(done["result"]["url"], "https://rail.example/leeds-station")
        with self.client_store.connection() as connection:
            job = connection.execute("SELECT kind, entries FROM ingestion_jobs").fetchone()
        self.assertEqual((job["kind"], job["entries"]), ("ingest_urls", [{"url": "https://rail.example/leeds-station"}]))
        self.assertIn("https://rail.example/leeds-station", [u["url"] for u in knowledge_system.urls()])

    def test_an_evaluation_rerun_is_recommended_after_the_content_changes(self):
        flows.evals.ensure_schema()
        with flows.store._connect() as connection:                    # the live flow's last run of a set
            connection.execute("""
                INSERT INTO agent_eval_runs (id, batch, set_id, set_name, flow_id, version, status, total, completed,
                                             spec, summary, finished_at)
                VALUES ('run1', 'b', 'travel_basics', 'Travel basics', 'f', '1', 'done', 10, 10, '{}', %s, now() - interval '1 minute')""",
                (Jsonb({"pass_rate": 0.8}),))
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            self.assertEqual(use_learnings.recommend(("f", "1")), [])     # nothing changed since it ran
            with recrawl._connect() as connection:
                connection.execute("UPDATE source_schedules SET last_changed_at = now() "
                                   "WHERE source_url = %s", (self.FINE,))
            [rerun] = use_learnings.recommend(("f", "1"))
        self.assertEqual((rerun["kind"], rerun["target"]), ("rerun_evaluation", "travel_basics"))
        started = []
        with patch.object(web_api, "_live_target", return_value=("f", "1", {"id": "f"})), \
                patch.object(web_api.flows.evals, "start", side_effect=lambda *a: started.append(a) or ["r1"]):
            done = use_learnings.approve(rerun["id"], self.admin["user_id"], web_api.LEARNING_ACTIONS)
        self.assertEqual((done["result"], started[0][:2]), ({"runs": ["r1"]}, ("travel_basics", "f")))
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            self.assertEqual(use_learnings.recommend(("f", "1")), [])     # approved: on its way

    def test_a_reset_clears_the_learnings(self):
        self.live_use()
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            use_learnings.recommend()
        counts = knowledge_system.counts()
        self.assertGreater(counts["use_learnings"], 0)
        self.assertGreater(counts["use_recommendations"], 0)
        knowledge_system.reset("RESET", self.admin["user_id"])
        counts = knowledge_system.counts()
        self.assertEqual((counts["use_learnings"], counts["use_recommendations"]), (0, 0))
        report = use_learnings.report(datetime.now(timezone.utc) - timedelta(days=30), datetime.now(timezone.utc))
        self.assertEqual((report["questions"], report["recommendations"]), (0, []))
        with patch.object(knowledge_gaps, "_embed", side_effect=RuntimeError("no model")):
            self.assertEqual(use_learnings.recommend(), [])          # the old gaps don't come back either

    def test_only_admins_decide(self):
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        client = self.signed_in(person)
        self.assertEqual(client.get("/api/learnings/recommendations").status_code, 403)
        self.assertEqual(client.post("/api/learnings/recommendations/1/approve", json={}).status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()

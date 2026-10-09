import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql

import ingestion_worker
import guardrails
import agent_memory
import identity
import knowledge_system
import sessions
from flows import evals as flow_evals, store as flow_store
import source_profiles
import llm
from datetime import datetime, timedelta, timezone

from common import config, embedding, ingest, storage
from ingestion_worker import process_job
from retrieval import _fuse, hybrid_search
from qdrant_overview import fetch_research_overview
from web_api import (_answer, agent_stream, ask_stream, knowledge_system_reset, knowledge_system_status, remove_sources,
                     source_reports, source_reports_remove, trigger)


class NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def update(self, **kwargs):
        pass


class PgStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        cls.client = storage.get_client()
        storage.ensure_collection(cls.client)

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database,
                             autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    def setUp(self):
        policy = patch("guardrails.check", return_value=guardrails.Verdict(
            decision="ALLOW", reason="travel fixture", stage="input"))
        policy.start()
        self.addCleanup(policy.stop)
        self.source = f"fixture://pgvector-{uuid.uuid4()}"
        self.addCleanup(storage.delete_url_points, self.client, self.source)
        storage.upsert_chunks(
            self.client, self.source,
            ["Rail ticket refund policy", "Station opening hours"],
            [[1.0] + [0.0] * 255, [0.0, 1.0] + [0.0] * 254],
            "fixture-hash", 1, paragraph_hashes=["fixture-paragraph-hash"],
        )

    def test_storage_and_search(self):
        dense = storage.search_dense(self.client, [1.0] + [0.0] * 255, 2)
        text = storage.search_text(self.client, "refund policy", 2)
        self.assertEqual(dense[0]["payload"]["text"], "Rail ticket refund policy")
        self.assertEqual(text[0]["payload"]["text"], "Rail ticket refund policy")
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")
        self.assertEqual(storage.find_paragraph_owners(
            self.client, ["fixture-paragraph-hash"], "other-source"), {"fixture-paragraph-hash"})
        self.assertEqual(storage.source_summary(self.client)[0]["chunks"], 2)
        storage.refresh_expiry(self.client, self.source, 2)
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["ttl_days"], 2)

    def test_retrieval_only_api(self):
        with patch.object(embedding, "embed_texts", return_value=[[1.0] + [0.0] * 255]), \
            patch("retrieval.observation", return_value=NoopSpan()), \
            patch("pipeline.observation", return_value=NoopSpan()), \
            patch("pipeline.tag_current_trace"):
            result = hybrid_search("refund policy")
            self.assertEqual(result["rankings"]["fused"][0]["text"], "Rail ticket refund policy")
            response = _answer("refund policy", [], "fixture-session", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["retrieval"]["chunks"][0]["text"],
                         "Rail ticket refund policy")

    def test_research_storage_is_isolated_and_both_tables_are_searched(self):
        research_client = storage.get_client("research")
        self.addCleanup(storage.delete_url_points, research_client, self.source)
        storage.upsert_chunks(research_client, self.source, ["Rail ticket refund policy research"],
                              [[1.0] + [0.0] * 255], "research-hash", 1,
                              extra_payload={"origin": "research_agent"}, paragraph_hashes=["research-paragraph"])
        self.assertEqual(storage.count_points(self.client), 2)
        self.assertEqual(storage.count_points(research_client), 1)
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")
        self.assertEqual(storage.get_existing_metadata(research_client, self.source)["content_hash"], "research-hash")
        self.assertEqual(storage.find_paragraph_owners(research_client, ["fixture-paragraph-hash"], "other"), set())
        self.assertEqual(storage.find_paragraph_owners(self.client, ["research-paragraph"], "other"), set())
        dense = storage.search_dense(self.client, [1.0] + [0.0] * 255, 3)
        text = storage.search_text(self.client, "refund policy", 3)
        for hits in (dense, text):
            self.assertEqual(len({hit["id"] for hit in hits}), len(hits))
            self.assertTrue(any(hit["payload"].get("origin") == "research_agent" for hit in hits))
            self.assertTrue(any(hit["payload"].get("origin") != "research_agent" for hit in hits))
        with patch.object(embedding, "embed_texts", return_value=[[1.0] + [0.0] * 255]), \
                patch("retrieval.observation", return_value=NoopSpan()):
            result = hybrid_search("refund policy")
        self.assertTrue(any(chunk["text"].endswith("research") for chunk in result["rankings"]["fused"]))
        storage.delete_url_points(research_client, self.source)
        self.assertEqual(storage.count_points(self.client), 2)

    def test_research_replacement_refresh_and_prune_leave_curated_rows_untouched(self):
        research_client = storage.get_client("research")
        self.addCleanup(storage.delete_url_points, research_client, self.source)
        for chunks in (["old research", "second research chunk"], ["new research"]):
            storage.upsert_chunks(research_client, self.source, chunks, [[1.0] + [0.0] * 255] * len(chunks),
                                  "research-hash", 1, replace_existing=True)
        self.assertEqual(storage.count_points(research_client), 1)
        storage.refresh_expiry(research_client, self.source, -1)
        self.assertEqual(storage.count_points(self.client), 2)
        self.assertFalse(any(hit["payload"]["text"] == "new research"
                             for hit in storage.search_dense(self.client, [1.0] + [0.0] * 255, 10)))
        self.assertEqual(storage.search_text(self.client, "research", 10), [])
        storage.delete_expired(self.client)
        self.assertIsNone(storage.get_existing_metadata(research_client, self.source))
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")

    def test_research_ingestion_does_not_skip_or_replace_a_fresh_curated_url(self):
        research_client = storage.get_client("research")
        self.addCleanup(storage.delete_url_points, research_client, self.source)
        with patch("common.ingest.scraping.fetch_html", return_value="<html></html>"), \
                patch("common.ingest.scraping.extract_text", return_value="Research refund policy " * 30), \
                patch.object(embedding, "embed_texts", side_effect=lambda chunks: [[1.0] + [0.0] * 255] * len(chunks)):
            result = ingest.ingest_url(research_client, {"url": self.source, "metadata": {"origin": "research_agent"}})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(storage.get_existing_metadata(research_client, self.source)["origin"], "research_agent")
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")
        self.assertEqual(storage.count_points(self.client), 2)

    def test_ask_stream_reports_each_stage_then_the_result(self):
        class Request:
            async def json(self):
                return {"question": "refund policy", "selections": [[llm.PROVIDER, llm.MODEL]],
                        "session_id": "fixture-session"}

        # One event loop for both: the endpoint delivers events to the loop it started on.
        async def call():
            response = await ask_stream(Request())
            return response, "".join([chunk async for chunk in response.body_iterator])

        summary = {"answer": "Refunds within 28 days.", "thinking": None, "provider": llm.PROVIDER,
                   "model": llm.MODEL, "input_tokens": 10, "output_tokens": 5, "duration": 0.1}
        with patch.object(embedding, "embed_texts", return_value=[[1.0] + [0.0] * 255]), \
                patch("retrieval.observation", return_value=NoopSpan()), \
                patch("pipeline.observation", return_value=NoopSpan()), \
                patch("pipeline.tag_current_trace"), \
                patch("pipeline.summarise", return_value=summary):
            response, body = asyncio.run(asyncio.wait_for(call(), timeout=60))
        self.assertEqual(response.media_type, "text/event-stream")
        events = [(block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1].removeprefix("data: ")))
                  for block in body.strip().split("\n\n")]
        stages = [(data["stage"], data["state"]) for name, data in events if name == "stage"]
        self.assertEqual(stages, [("embedding", "start"), ("embedding", "done"), ("retrieval", "start"),
                                  ("retrieval", "done"), ("generation", "start"), ("generation", "done")])
        stage_data = {(d["stage"], d["state"]): d for name, d in events if name == "stage"}
        self.assertEqual(stage_data[("embedding", "done")]["dim"], 256)
        self.assertGreaterEqual(stage_data[("retrieval", "done")]["chunks"], 1)
        self.assertEqual(stage_data[("generation", "done")]["output_tokens"], 5)
        name, result = events[-1]
        self.assertEqual(name, "result")
        self.assertEqual(result["generations"]["A"][0]["answer"], "Refunds within 28 days.")
        self.assertEqual(result["retrieval"]["chunks"][0]["text"], "Rail ticket refund policy")

    def test_failed_replacement_preserves_existing_chunks(self):
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")

    def test_database_error_rolls_back_replacement(self):
        with self.assertRaises(psycopg.Error):
            storage.upsert_chunks(self.client, self.source, ["bad vector"], [[1.0, 0.0]],
                                  "new-hash", 1, replace_existing=True)
        self.assertEqual(storage.count_points(self.client), 2)
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")

    def test_replacement_removes_stale_chunks(self):
        storage.upsert_chunks(self.client, self.source, ["new policy"], [[1.0] + [0.0] * 255],
                              "new-hash", 1, replace_existing=True)
        self.assertEqual(storage.count_points(self.client), 1)
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "new-hash")

    def test_pruning_only_removes_expired_source(self):
        expired = f"fixture://expired-{uuid.uuid4()}"
        storage.upsert_chunks(self.client, expired, ["expired page"], [[1.0] + [0.0] * 255],
                              "expired", -1)
        storage.delete_expired(self.client)
        self.assertIsNone(storage.get_existing_metadata(self.client, expired))
        self.assertEqual(storage.count_points(self.client), 2)

    def test_full_text_and_rrf_edge_cases(self):
        self.assertEqual(storage.search_text(self.client, "unmatchedphrase", 5), [])
        dense = [{"id": "a"}, {"id": "b"}]
        text = [{"id": "b"}, {"id": "c"}]
        fused, parts = _fuse(dense, text, 3)
        self.assertEqual([row["id"] for row in fused], ["b", "a", "c"])
        self.assertEqual(parts[0]["dense_rank"], 2)
        self.assertEqual(parts[0]["sparse_rank"], 1)

    def test_manual_job_queue_and_recovery(self):
        entries = [{"url": "fixture://one"}, {"url": "fixture://two"}]
        with self.assertRaises(ValueError):
            storage.enqueue_job(self.client, "ingest_urls", entries * 6)
        job = storage.enqueue_job(self.client, "ingest_urls", entries)
        self.addCleanup(self._delete_job, job["id"])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            storage.enqueue_job(self.client, "prune_expired_documents")
        claimed = storage.claim_job(self.client)
        summary = {"urls": 2, "batches": 1, "counts": {"updated": 1},
                   "chunks_written": 1, "seconds": 0, "failed": []}
        storage.update_job(self.client, job["id"], "running", 1, summary)
        storage.recover_running_jobs(self.client)
        resumed = storage.claim_job(self.client)
        self.assertEqual(resumed["next_index"], 1)
        with patch("ingestion_worker.ingest.ingest_url", return_value={"url": "fixture://two", "status": "failed", "stage": "fetch", "error": "404"}) as run_url:
            process_job(self.client, resumed)
        run_url.assert_called_once()
        finished = storage.list_jobs(self.client, "ingest_urls", 1)[0]
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["next_index"], 2)
        self.assertEqual(finished["summary"]["counts"], {"updated": 1, "failed": 1})
        self.assertEqual(len(finished["summary"]["failed"]), 1)

    def test_stop_request_checkpoints_and_leaves_job_resumable(self):
        entries = [{"url": "fixture://one"}, {"url": "fixture://two"}]
        job = storage.enqueue_job(self.client, "ingest_urls", entries)
        self.addCleanup(self._delete_job, job["id"])
        self.addCleanup(ingestion_worker.stop_requested.clear)
        ingestion_worker.stop_requested.set()
        with patch("ingestion_worker.ingest.ingest_url", return_value={"url": "fixture://one", "status": "skipped_fresh"}) as run_url:
            process_job(self.client, storage.claim_job(self.client))
        run_url.assert_called_once()
        stopped = storage.list_jobs(self.client, "ingest_urls", 1)[0]
        self.assertEqual(stopped["status"], "running")
        self.assertEqual(stopped["next_index"], 1)
        storage.recover_running_jobs(self.client)
        self.assertEqual(storage.claim_job(self.client)["next_index"], 1)

    def test_systemic_job_failure_can_be_requeued(self):
        job = storage.enqueue_job(self.client, "ingest_urls", [{"url": "fixture://one"}])
        self.addCleanup(self._delete_job, job["id"])
        with patch("ingestion_worker.ingest.ingest_url", side_effect=RuntimeError("database unavailable")):
            process_job(self.client, storage.claim_job(self.client))
        failed = storage.list_jobs(self.client, "ingest_urls", 1)[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["next_index"], 0)
        retry = storage.enqueue_job(self.client, "ingest_urls", [{"url": "fixture://one"}])
        self.addCleanup(self._delete_job, retry["id"])

    def test_manual_prune_job_removes_only_expired_rows(self):
        expired = f"fixture://expired-{uuid.uuid4()}"
        storage.upsert_chunks(self.client, expired, ["expired page"], [[1.0] + [0.0] * 255],
                              "expired", -1)
        job = storage.enqueue_job(self.client, "prune_expired_documents")
        self.addCleanup(self._delete_job, job["id"])
        process_job(self.client, storage.claim_job(self.client))
        self.assertEqual(storage.list_jobs(self.client, "prune_expired_documents", 1)[0]["status"], "success")
        self.assertIsNone(storage.get_existing_metadata(self.client, expired))
        self.assertEqual(storage.count_points(self.client), 2)

    def test_api_trigger_caps_url_jobs_at_ten(self):
        class Request:
            async def json(self):
                return {"kind": "ingest_urls"}

        entries = [{"url": f"https://example.test/{index}"} for index in range(11)]
        with tempfile.TemporaryDirectory() as directory:
            urls_path = Path(directory) / "urls.json"
            urls_path.write_text(json.dumps(entries))
            self._fresh_knowledge_system(urls_path)  # imports the file once
            urls_path.write_text("[]")              # from now on the database is the list
            with patch.object(config, "URLS_CONFIG_PATH", urls_path), \
                    patch.object(storage, "enqueue_job", return_value={"id": "fixture-id"}) as enqueue:
                response = asyncio.run(trigger(Request()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(enqueue.call_args.args[2], entries[:10])

    def _fresh_knowledge_system(self, urls_path: Path = Path("/nonexistent/urls.json")):
        """As if this database had never seen the knowledge system record;
        urls_path: the URL list it finds (default: none)."""
        with self.client.connection() as connection:
            for table in ("app_knowledge_system", "app_knowledge_system_events", "kb_urls"):
                connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        knowledge_system.reset_schema_cache()
        self.addCleanup(knowledge_system.reset_schema_cache)
        with patch.object(config, "URLS_CONFIG_PATH", urls_path):
            knowledge_system.ensure_schema()

    def test_an_install_with_knowledge_starts_ready(self):
        self._fresh_knowledge_system()        # setUp stored a page, as on every install today
        state = knowledge_system.status()
        self.assertEqual((state["state"], state["origin"], state["urls"]), ("READY", "existing", 0))
        self.assertIsNotNone(state["set_up_at"])
        self.assertEqual(knowledge_system.events()[0]["event"], "created")

    def test_an_empty_install_starts_new_and_the_demo_marks_it_ready(self):
        storage.delete_url_points(self.client, self.source)
        self._fresh_knowledge_system()
        self.assertEqual((knowledge_system.status()["state"], knowledge_system.status()["origin"]), ("NEW", None))
        self.assertFalse(knowledge_system.is_ready())
        with self.assertRaises(ValueError):
            knowledge_system.mark_ready("guess")
        knowledge_system.mark_ready("demo")
        self.assertEqual((knowledge_system.status()["state"], knowledge_system.status()["origin"]), ("READY", "demo"))
        self.assertEqual(knowledge_system.events()[0]["details"], {"origin": "demo"})

    def test_urls_json_is_imported_once_in_order_and_the_example_never(self):
        storage.delete_url_points(self.client, self.source)
        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory) / "urls.example.json"
            example.write_text(json.dumps([{"url": "https://sample.example/1"}]))
            self._fresh_knowledge_system(example)
            self.assertEqual((knowledge_system.status()["state"], knowledge_system.urls()), ("NEW", []))
            # A urls.json given to the install later is imported once, in file order.
            listed = Path(directory) / "urls.json"
            listed.write_text(json.dumps([{"url": "https://b.example", "ttl_days": 30}, {"url": "https://a.example"},
                                          {"url": "https://b.example"}, {"no": "url"}]))
            with patch.object(config, "URLS_CONFIG_PATH", listed):
                knowledge_system.reset_schema_cache()
                knowledge_system.ensure_schema()
                self.assertEqual(knowledge_system.urls(), [{"url": "https://b.example", "ttl_days": 30},
                                                           {"url": "https://a.example"}])
                self.assertEqual(knowledge_system.urls(limit=1), [{"url": "https://b.example", "ttl_days": 30}])
                self.assertIsNotNone(knowledge_system.status()["urls_imported_at"])
                listed.write_text(json.dumps([{"url": "https://c.example"}]))
                knowledge_system.reset_schema_cache()
                knowledge_system.ensure_schema()     # already imported: the file is not read again
                self.assertEqual(len(knowledge_system.urls()), 2)

    def _fresh_app_schemas(self):
        """The other modules' tables, created in this test database."""
        flow_store._schema_ready = flow_evals._ready = False
        for module in (agent_memory, identity, sessions, source_profiles):
            module.reset_schema_cache()
            self.addCleanup(module.reset_schema_cache)
        self.addCleanup(setattr, flow_store, "_schema_ready", False)
        self.addCleanup(setattr, flow_evals, "_ready", False)

    def test_reset_empties_the_knowledge_and_keeps_people_and_flows(self):
        self._fresh_app_schemas()
        with tempfile.TemporaryDirectory() as directory:
            listed = Path(directory) / "urls.json"
            listed.write_text(json.dumps([{"url": "https://a.example"}]))
            self._fresh_knowledge_system(listed)
            research_client = storage.get_client("research")
            self.addCleanup(storage.delete_url_points, research_client, self.source)
            storage.upsert_chunks(research_client, self.source, ["research text"], [[1.0] + [0.0] * 255], "h", 1)
            source_profiles.upsert([{"name": "LDB", "provider": "Rail body"}], "departures")
            source_profiles.save_report("Live departures?", [], "", [], {})
            flow_evals.save_set("kept_by_nobody", "Saved set", "", [{"question": "Refunds?"}])
            with self.client.connection() as connection:
                connection.execute("INSERT INTO agent_eval_runs (id, batch, set_id, set_name, flow_id, version, status, "
                                   "total, spec) VALUES ('run1', 'b', 's', 'S', 'f', '1', 'finished', 1, '{}')")
                connection.execute("INSERT INTO agent_eval_results (run_id, idx, answer, passed) VALUES ('run1', 0, 'a', true)")
            agent_memory.record([{"agent": "research", "kind": "source_validation", "key": "a.example", "success": True}])
            user = identity.create_user("Kept", f"kept-{uuid.uuid4().hex[:6]}@example.test", "user")
            session_id = sessions.current(user["user_id"], None)
            flow_store.ensure_schema()
            with self.client.connection() as connection:
                flows_before = connection.execute("SELECT count(*) AS n FROM agent_flows").fetchone()["n"]

            with self.assertRaises(ValueError):          # no typed confirmation: nothing happens
                knowledge_system.reset("reset", "admin-1")
            self.assertGreater(storage.count_points(self.client), 0)
            before = knowledge_system.counts()
            for table in ("rag_chunks", "research_chunks", "kb_urls", "research_source_profiles",
                          "research_source_reports", "agent_eval_sets", "agent_eval_runs", "agent_eval_results",
                          "app_agent_memory"):
                self.assertGreater(before[table], 0, table)

            deleted = knowledge_system.reset("RESET", "admin-1")
            self.assertEqual(deleted, before)
            self.assertEqual(set(knowledge_system.counts().values()), {0})
            self.assertEqual(storage.count_points(self.client), 0)
            state = knowledge_system.status()
            self.assertEqual((state["state"], state["origin"], state["set_up_at"]), ("NEW", None, None))
            event = knowledge_system.events()[0]
            self.assertEqual((event["event"], event["user_id"], event["details"]["deleted"]["kb_urls"]),
                             ("reset", "admin-1", 1))
            # Kept: people, their sessions and the flows.
            self.assertEqual(identity.get_user(user["user_id"])["first_name"], "Kept")
            with self.client.connection() as connection:
                self.assertEqual(connection.execute("SELECT count(*) AS n FROM app_sessions WHERE session_id = %s",
                                                    (session_id,)).fetchone()["n"], 1)
                self.assertEqual(connection.execute("SELECT count(*) AS n FROM agent_flows").fetchone()["n"], flows_before)
            # urls.json is not imported again after a reset.
            knowledge_system.reset_schema_cache()
            with patch.object(config, "URLS_CONFIG_PATH", listed):
                knowledge_system.ensure_schema()
            self.assertEqual(knowledge_system.urls(), [])

    def test_reset_waits_for_running_work(self):
        self._fresh_app_schemas()
        self._fresh_knowledge_system()
        flow_evals.ensure_schema()
        for table, insert in (
                ("ingestion_jobs", "INSERT INTO ingestion_jobs (id, kind, status, entries) "
                                   "VALUES (gen_random_uuid(), 'prune_expired_documents', 'queued', '[]')"),
                ("agent_eval_runs", "INSERT INTO agent_eval_runs (id, batch, set_id, set_name, flow_id, version, "
                                    "status, total, spec) VALUES ('run2', 'b', 's', 'S', 'f', '1', 'running', 1, '{}')")):
            with self.client.connection() as connection:
                connection.execute(insert)
            with self.assertRaises(knowledge_system.Busy):
                knowledge_system.reset("RESET", None)
            self.assertGreater(storage.count_points(self.client), 0)   # nothing deleted
            with self.client.connection() as connection:
                connection.execute(sql.SQL("DELETE FROM {} WHERE status IN ('queued', 'running')").format(
                    sql.Identifier(table)))

    def test_knowledge_system_api(self):
        self._fresh_app_schemas()
        self._fresh_knowledge_system()

        class Request:
            def __init__(self, permissions, body=None):
                self.state = Mock(user={"user_id": "u1", "status": "active", "role": "user",
                                        "permissions": permissions})
                self.body = body or {}
                self.session = {}

            async def json(self):
                return self.body
        viewer = json.loads(knowledge_system_status(Request(["query_rag"])).body)
        self.assertEqual(set(viewer), {"state", "origin", "set_up_at", "can_manage"})
        admin = json.loads(knowledge_system_status(Request(["manage_settings"])).body)
        self.assertEqual((admin["state"], admin["confirmation"], admin["can_manage"]), ("READY", "RESET", True))
        self.assertIn("rag_chunks", [e["table"] for e in admin["emptied"]])
        refused = asyncio.run(knowledge_system_reset(Request(["manage_settings"], {"confirm": "yes"})))
        self.assertEqual(refused.status_code, 400)
        done = asyncio.run(knowledge_system_reset(Request(["manage_settings"], {"confirm": "RESET"})))
        self.assertEqual(json.loads(done.body)["deleted"]["rag_chunks"], 2)
        # Not set up: users' questions wait; the error says why.
        waiting = asyncio.run(agent_stream(Request(["query_rag"], {"question": "Refunds?"})))
        self.assertEqual((waiting.status_code, json.loads(waiting.body)["setup"]), (409, True))
        self.assertEqual(asyncio.run(ask_stream(Request(["query_rag"], {"question": "Refunds?"}))).status_code, 409)

    def test_trigger_without_urls_is_refused(self):
        class Request:
            async def json(self):
                return {"kind": "ingest_urls"}
        self._fresh_knowledge_system()
        response = asyncio.run(trigger(Request()))
        self.assertEqual((response.status_code, json.loads(response.body)["error"]), (400, "no URLs to ingest yet"))

    def test_research_pages_are_listed_and_chosen_pages_removed(self):
        research_client = storage.get_client("research")
        other = f"fixture://research-{uuid.uuid4()}"
        for url in (self.source, other):
            self.addCleanup(storage.delete_url_points, research_client, url)
            storage.upsert_chunks(research_client, url, ["Pantry car on Indian Railways"], [[1.0] + [0.0] * 255],
                                  "research-hash", 1, extra_payload={"research_task": "Pantry car on trains",
                                                                     "research_publisher": "IRCTC"})

        class Request:
            def __init__(self, body):
                self.body = body

            async def json(self):
                return self.body

        fetch_research_overview.clear()
        listed = {r["url"]: r for r in fetch_research_overview()}
        self.assertEqual((listed[other]["task"], listed[other]["publisher"], listed[other]["chunks"]),
                         ("Pantry car on trains", "IRCTC", 1))
        for bad in ({"dataset": "other", "urls": [other]}, {"dataset": "research", "urls": []},
                    {"dataset": "research", "urls": [1]}, {"dataset": "research", "urls": ["u"] * 201}):
            self.assertEqual(asyncio.run(remove_sources(Request(bad))).status_code, 400, bad)
        response = asyncio.run(remove_sources(Request({"dataset": "research", "urls": [other, other]})))
        self.assertEqual(json.loads(response.body), {"ok": True, "removed": 1})
        self.assertIsNone(storage.get_existing_metadata(research_client, other))
        # Only the chosen page and only in the chosen table: the same URL's curated rows stay.
        self.assertIsNotNone(storage.get_existing_metadata(research_client, self.source))
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")
        self.assertNotIn(other, {r["url"] for r in fetch_research_overview()})

    def test_source_profiles_are_stored_found_and_reported(self):
        source_profiles.reset_schema_cache()
        self.addCleanup(source_profiles.reset_schema_cache)
        ldb = {"name": "LDB web service", "provider": "National Rail", "access_method": "soap_api",
               "coverage": "Live departure boards for Great Britain stations"}
        source_profiles.upsert([ldb, {"name": "", "provider": "nobody"}], "live UK train departures")
        source_profiles.upsert([{**ldb, "pricing": "Free"}], "platform changes")     # same source: updated
        found = source_profiles.find("Where can I get live departures and platforms?", 4, 30)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["profile"]["pricing"], found[0]["fresh"]), ("Free", True))
        later = datetime.now(timezone.utc) + timedelta(days=31)
        self.assertFalse(source_profiles.find("departures", 4, 30, now=later)[0]["fresh"])
        self.assertEqual(source_profiles.find("weather forecast hailstorms", 4, 30), [])
        self.assertEqual(source_profiles.find("?? !!", 4, 30), [])
        report_id = source_profiles.save_report("Live departures?", ["live times"], "UK trains", [ldb],
                                                {"recommended": "LDB web service", "method": "api_tool"}, "trace-1")
        listed = source_profiles.list_reports()
        self.assertEqual((listed[0]["report_id"], listed[0]["recommendation"]["method"]), (report_id, "api_tool"))
        listed = json.loads(source_reports(Mock(query_params={})).body)
        self.assertEqual([r["report_id"] for r in listed["reports"]], [report_id])

        class Request:
            def __init__(self, body):
                self.body = body

            async def json(self):
                return self.body
        for bad in ({}, {"report_ids": []}, {"report_ids": [3]}, {"report_ids": "r_1"}):
            self.assertEqual(asyncio.run(source_reports_remove(Request(bad))).status_code, 400, bad)
        removed = asyncio.run(source_reports_remove(Request({"report_ids": [report_id, "r_missing"]})))
        self.assertEqual(json.loads(removed.body), {"ok": True, "removed": 1})
        self.assertEqual(source_profiles.list_reports(), [])

    def _delete_job(self, job_id):
        with self.client.connection() as connection:
            connection.execute("DELETE FROM ingestion_jobs WHERE id = %s", (uuid.UUID(job_id),))
        with patch.dict(sys.modules, {"trafilatura": Mock()}):
            from common import ingest
        with patch("common.ingest.scraping.fetch_html", return_value="<html></html>"), \
                patch("common.ingest.scraping.extract_text", return_value="updated article"), \
                patch("common.ingest.dedup.dedupe_page", return_value=("updated article", [], {})), \
                patch("common.ingest.chunking.chunk_text", return_value=["updated article"]), \
                patch.object(embedding, "embed_texts", return_value=[[1.0] + [0.0] * 255]), \
                patch.object(storage, "upsert_chunks", side_effect=RuntimeError("write failed")), \
                patch.object(storage, "delete_url_points") as premature_delete:
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                ingest.ingest_url(self.client, {"url": self.source}, force=True)
            premature_delete.assert_not_called()
        self.assertEqual(storage.get_existing_metadata(self.client, self.source)["content_hash"], "fixture-hash")


if __name__ == "__main__":
    unittest.main()
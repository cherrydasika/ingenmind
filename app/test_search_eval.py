"""Search quality on its own (R20, flows/search_eval.py)."""

import unittest
import uuid
from unittest.mock import patch

import numpy as np
import psycopg
from psycopg import sql
from starlette.testclient import TestClient

import flows
import flows.search_eval as search_eval
import identity
import retrieval
import web_api
from common import config, storage


def hit(n: int, url: str = "https://site/page") -> dict:
    return {"id": f"c{n}", "payload": {"source_url": url}, "score": 1.0}


class Scoring(unittest.TestCase):
    def test_a_setting_ranks_as_hybrid_search_would(self):
        dense = [hit(n) for n in range(30)]
        text = [hit(n) for n in range(29, -1, -1)]
        self.assertEqual(search_eval.ranked(dense, text, "hybrid", 2, 5),
                         retrieval._fuse(dense[:10], text[:10], 5, 2)[0])
        self.assertEqual([h["id"] for h in search_eval.ranked(dense, text, "dense", None, 3)], ["c0", "c1", "c2"])
        self.assertEqual([h["id"] for h in search_eval.ranked(dense, text, "keyword", None, 3)], ["c29", "c28", "c27"])

    def test_hit_rate_and_mrr_per_setting(self):
        dense = [hit(n) for n in range(30)]
        text = [hit(n, "https://other/page") for n in range(100, 130)]
        questions = [{"question": "first", "chunk_id": "c0", "source": "https://site/page"},
                     {"question": "fourth", "chunk_id": "c3", "source": "https://site/page"},
                     {"question": "by hand", "chunk_id": None, "source": "https://other/page", "by": "hand"},
                     {"question": "nowhere", "chunk_id": "zz", "source": "https://none"}]
        cells, per = search_eval.score(questions, [(dense, text)] * 4)
        cell = {(c["mode"], c["rrf_k"], c["top_k"]): c for c in cells}
        dense3 = cell[("dense", None, 3)]
        self.assertEqual((dense3["hit_rate"], dense3["mrr"], dense3["missed"]), (0.25, 0.25, [1, 2, 3]))
        dense5 = cell[("dense", None, 5)]
        self.assertEqual((dense5["hit_rate"], dense5["mrr"]), (0.5, round((1 + 1 / 4) / 4, 4)))
        self.assertEqual(cell[("keyword", None, 3)]["missed"], [0, 1, 3])       # the page matches by hand
        self.assertEqual(len(cells), len(search_eval.TOP_KS) * (2 + len(search_eval.RRF_KS)))
        self.assertEqual([(p["dense"], p["keyword"]) for p in per], [(1, None), (4, None), (None, 1), (None, None)])


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        cls.reset()
        storage.ensure_collection(storage.get_client())
        cls.client = TestClient(web_api.app, base_url="http://localhost")
        cls.client.post("/auth/dev", data={"first_name": "Tess", "email": "tess@example.com"})

    @classmethod
    def reset(cls):
        flows.store.reset_schema_cache()
        flows.evals.reset_schema_cache()
        search_eval.reset_schema_cache()
        identity.reset_schema_cache()

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        cls.reset()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    @staticmethod
    def vector(axis: int) -> list[float]:
        v = np.zeros(config.EMBEDDING_DIM, dtype=np.float32)
        v[axis] = 1
        return v.tolist()

    def test_a_set_runs_against_the_knowledge_base_and_is_saved(self):
        client = storage.get_client()
        refunds, parking = "https://rail.example/refunds", "https://rail.example/parking"
        storage.upsert_chunks(client, refunds, ["Delay repay pays back part of the fare when a train is late.",
                                                "Season tickets can be refunded at the ticket office."],
                              [self.vector(0), self.vector(1)], "h1", 30)
        storage.upsert_chunks(client, parking, ["Station car parks charge by the day."], [self.vector(2)], "h2", 30)
        first = str(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, refunds), "0"))
        saved = search_eval.save_set("Search test", [
            {"question": "money back for a late train", "chunk_id": first, "source": refunds, "by": "model"},
            {"question": "season ticket refund", "chunk_id": str(uuid.uuid4()), "source": refunds, "by": "model"}])

        r = self.client.post(f"/api/flow-evals/search/sets/{saved['id']}",
                             json={"question": "how much is parking", "source": "https://nowhere.example"})
        self.assertEqual(r.status_code, 400)                                  # not a page of the knowledge base
        r = self.client.post(f"/api/flow-evals/search/sets/{saved['id']}",
                             json={"question": "how much is parking", "source": parking})
        self.assertEqual(len(r.json()["questions"]), 3)

        vectors = {"money back for a late train": self.vector(0), "season ticket refund": self.vector(1),
                   "how much is parking": self.vector(2)}
        with patch.object(search_eval.embedding, "embed_texts", lambda texts, query=False: [vectors[t] for t in texts]):
            job = search_eval.start_run(saved["id"], thread=False)
        self.assertEqual(job["status"], "done", job.get("error"))
        listing = self.client.get("/api/flow-evals/search").json()
        self.assertEqual(listing["runs"][0]["current"]["hit_rate"], 1.0)
        run = self.client.get(f"/api/flow-evals/search/runs/{job['run_id']}").json()
        self.assertEqual(run["knowledge"]["gone"], 1)                         # matched by its page instead
        self.assertEqual([(q["dense"], q["gone"]) for q in run["per_question"]], [(1, False), (1, True), (1, False)])
        dense = next(c for c in run["cells"] if c["mode"] == "dense" and c["top_k"] == 3)
        self.assertEqual(dense["hit_rate"], 1.0)

        r = self.client.post(f"/api/flow-evals/search/sets/{saved['id']}", json={"remove": 0})
        self.assertEqual([q["question"] for q in r.json()["questions"]], ["season ticket refund", "how much is parking"])
        self.assertEqual(len(self.client.get(f"/api/flow-evals/search/runs/{job['run_id']}").json()["per_question"]), 3)
        self.assertEqual(self.client.delete(f"/api/flow-evals/search/sets/{saved['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/flow-evals/search/runs/{job['run_id']}").status_code, 404)

    def test_writing_a_set_saves_one_question_per_usable_passage(self):
        chunks = [{"id": f"id{i}", "url": f"https://site/{i}", "body": f"passage {i} " * 40, "topic": "t",
                   "vector": np.eye(8, dtype=np.float32)[i]} for i in range(4)]
        writes = iter([None, "When does the first train leave?", "Can I take a bike?", "Is there step-free access?",
                       "Where do I park?"])
        with patch.object(search_eval.golden, "candidates", return_value=chunks), \
                patch.object(search_eval.golden, "WORKERS", 1), patch.object(search_eval, "COUNTS", (3,)):
            job = search_eval.generate(3, work=lambda n: search_eval._write(n, lambda chunk: next(writes)), thread=False)
        self.assertEqual(job["status"], "done", job.get("error"))
        saved = search_eval.get_set(job["set_id"])
        self.assertEqual(len(saved["questions"]), 3)
        self.assertTrue(all(q["chunk_id"].startswith("id") and q["by"] == "model" for q in saved["questions"]))
        with self.assertRaises(ValueError):
            search_eval.generate(7, thread=False)


if __name__ == "__main__":
    unittest.main()

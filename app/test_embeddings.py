"""Embedding providers against fake clients, and the database's record of
which model made its vectors (needs PostgreSQL, like test_pg_store).
Run: PYTHONPATH=app:dags python -m unittest app/test_embeddings.py"""

import sys
import types
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import patch

import psycopg
from psycopg import sql

import reindex_embeddings
from common import config, storage
from common.embedding import embedder


def settings(provider, model, dim):
    return patch.multiple(config, EMBEDDING_PROVIDER=provider, EMBEDDING_MODEL=model, EMBEDDING_DIM=dim)


def unit(dim, hot=0):
    return [1.0 if i == hot else 0.0 for i in range(dim)]


class EmbedderTest(unittest.TestCase):
    def openai_backend(self, provider, dim):
        requests = []

        class Embeddings:
            def create(self, **request):
                requests.append(request)
                # Out of order, as the API allows: the index puts them back.
                return NS(data=[NS(index=i, embedding=unit(dim, i % dim)) for i in range(len(request["input"]))][::-1])

        module = types.ModuleType("openai")
        module.OpenAI = lambda **kwargs: NS(embeddings=Embeddings())
        with patch.dict(sys.modules, {"openai": module}), settings(provider, "nomic-embed-text:latest", dim):
            return embedder._OpenAI(), requests

    def test_openai_batches_in_order_and_asks_for_the_dimension(self):
        backend, requests = self.openai_backend("openai", 4)
        with settings("openai", "text-embedding-3-small", 4):
            vectors = backend.embed([f"t{i}" for i in range(embedder.OPENAI_BATCH + 2)], False)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["dimensions"], 4)
        self.assertEqual(vectors[1], unit(4, 1))

    def test_ollama_prefixes_queries_and_documents(self):
        backend, requests = self.openai_backend("ollama", 4)
        with settings("ollama", "nomic-embed-text:latest", 4):
            backend.embed(["where"], True)
            backend.embed(["page"], False)
        self.assertEqual(requests[0]["input"], ["search_query: where"])
        self.assertEqual(requests[1]["input"], ["search_document: page"])
        self.assertNotIn("dimensions", requests[0])

    def test_wrong_dimension_is_reported(self):
        with settings("local", "m", 3), patch.object(embedder, "_backend", return_value=NS(embed=lambda t, q: [[1.0]])):
            with self.assertRaisesRegex(ValueError, "EMBEDDING_DIM"):
                embedder.embed_texts(["x"])

    def test_config_errors(self):
        with settings("nope", "m", 3):
            self.assertIn("not one of", embedder.config_error())
        with settings("local", "m", 3072):
            self.assertIn("index limit", embedder.config_error())
        with settings("local", "BAAI/bge-small-en-v1.5", 384):
            self.assertIsNone(embedder.config_error())


class EmbeddingRecordTest(unittest.TestCase):
    def setUp(self):
        self.original_database = config.PGDATABASE
        self.database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        config.PGDATABASE = self.database
        self.addCleanup(self.drop)
        self.client = storage.get_client()

    def drop(self):
        config.PGDATABASE = self.original_database
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database)))

    def store(self, dim, url="fixture://a"):
        storage.upsert_chunks(self.client, url, ["refund policy", "opening hours"], [unit(dim, 0), unit(dim, 1)], "h", 1)

    def test_empty_database_follows_the_configured_model(self):
        with settings("bedrock", "titan", 4):
            storage.ensure_collection(self.client)
        with settings("local", "bge", 6):
            storage.ensure_collection(self.client)
            self.assertEqual(storage.stored_embedding(self.client), {"provider": "local", "model": "bge", "dimension": 6})
            self.store(6)
            self.assertEqual(storage.search_dense(self.client, unit(6, 1), 1)[0]["payload"]["text"], "opening hours")

    def test_model_change_over_stored_vectors_is_refused_everywhere(self):
        with settings("bedrock", "titan", 4):
            storage.ensure_collection(self.client)
            self.store(4)
        # Same dimension, different model: only the record can tell.
        with settings("ollama", "other", 4):
            for call in (lambda: storage.ensure_collection(self.client),
                         lambda: storage.search_dense(self.client, unit(4), 1),
                         lambda: self.store(4, "fixture://b")):
                with self.assertRaisesRegex(storage.EmbeddingMismatch, "reindex_embeddings"):
                    call()

    def test_database_from_before_the_record_is_adopted_when_dimensions_agree(self):
        with settings("bedrock", "titan", 4):
            storage.ensure_collection(self.client)
            self.store(4)
        with self.client.connection() as connection:
            connection.execute("DROP TABLE embedding_config")
        with settings("local", "bge", 6), self.assertRaises(storage.EmbeddingMismatch):
            storage.ensure_collection(self.client)
        with settings("bedrock", "titan", 4):
            storage.ensure_collection(self.client)
            self.assertEqual(storage.stored_embedding(self.client)["model"], "titan")

    def test_reindex_replaces_every_vector(self):
        with settings("bedrock", "titan", 4):
            storage.ensure_collection(self.client)
            self.store(4)
        fake = lambda texts, query=False: [unit(6, 1 if "hours" in text else 0) for text in texts]
        with settings("local", "bge", 6), patch.object(reindex_embeddings.embedding, "embed_texts", side_effect=fake), \
                patch.object(reindex_embeddings.embedding, "config_error", return_value=None):
            self.assertEqual(reindex_embeddings.main(["--yes"]), 0)
            self.assertEqual(storage.stored_embedding(self.client)["dimension"], 6)
            self.assertEqual(storage.search_dense(self.client, unit(6, 1), 1)[0]["payload"]["text"], "opening hours")
            storage.ensure_collection(self.client)   # the HNSW index is back and nothing is refused
            with self.client.connection() as connection:
                self.assertTrue(connection.execute(
                    "SELECT to_regclass('rag_chunks_embedding') IS NOT NULL AS indexed").fetchone()["indexed"])


if __name__ == "__main__":
    unittest.main()

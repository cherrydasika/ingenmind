"""Structure-aware chunking (R21): common/chunking, ingestion with it, and
`python -m rechunk`."""

import json
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql

import rechunk
from common import chunking, config, embedding, ingest, storage
from common.chunking import structure
from flows import search_eval

PAGE = """# Refunds

You can get your money back in many cases, depending on the ticket.

## Delay Repay

If your train is 15 minutes late you can claim. Claims must be made within 28 days of travel.

- 15 to 29 minutes: 25% of the single fare
- 30 to 59 minutes: 50% of the single fare

## Season tickets

""" + "Season tickets can be refunded at the ticket office, minus an admin fee. " * 20 + """

# Parking

Station car parks charge by the day; weekly and monthly permits cost less.
"""


class Structure(unittest.TestCase):
    def test_chunks_follow_headings_and_say_where_they_sit(self):
        chunks = structure.chunk_sections(PAGE, 400)
        self.assertTrue(all(len(c["text"]) <= 400 for c in chunks))
        # The small intro and Delay Repay sections join under their shared top heading.
        self.assertEqual(chunks[0]["headings"], ["Refunds"])
        self.assertIn("Delay Repay", chunks[0]["text"])
        self.assertEqual(chunks[0]["text"].count("Delay Repay"), 1)
        # A long section is cut at sentences, every piece under its full path.
        season = [c for c in chunks if c["headings"] == ["Refunds", "Season tickets"]]
        self.assertGreater(len(season), 1)
        self.assertTrue(all(c["text"].startswith("Refunds > Season tickets\n\n") for c in season))
        self.assertTrue(all(c["text"].rstrip().endswith(".") for c in season))      # never mid-sentence
        # A new top heading starts a new chunk, however small the last one.
        self.assertEqual(chunks[-1]["headings"], ["Parking"])
        self.assertNotIn("Season", chunks[-1]["text"])

    def test_a_list_stays_together(self):
        chunks = structure.chunk_sections("## Fares\n\nIntro text here.\n\n- one\n- two\n  wrapped\n- three", 1000)
        self.assertEqual(len(chunks), 1)
        self.assertIn("- one\n- two\n  wrapped\n- three", chunks[0]["text"])

    def test_text_without_headings_is_cut_along_lines(self):
        text = "\n".join(f"Line {i} " + "word " * 30 for i in range(20))
        chunks = structure.chunk_sections(text, 500)
        self.assertTrue(all(c["headings"] == [] and len(c["text"]) <= 500 for c in chunks))
        self.assertTrue(all(c["text"].startswith("Line ") for c in chunks))         # whole lines
        self.assertEqual(sum(c["text"].count("Line ") for c in chunks), 20)

    def test_a_huge_sentence_is_cut_at_spaces(self):
        chunks = structure.chunk_sections("# A\n\n" + "word " * 500, 300)
        self.assertTrue(all(len(c["text"]) <= 300 for c in chunks))

    def test_the_setting_picks_the_strategy(self):
        self.assertEqual(chunking.split(PAGE, "structure"), structure.chunk_sections(PAGE, config.CHUNK_SIZE))
        fixed = chunking.split(PAGE, "fixed")
        self.assertTrue(all(c["headings"] == [] for c in fixed))
        self.assertEqual([c["text"] for c in fixed], chunking.chunk_text(PAGE, config.CHUNK_SIZE, config.CHUNK_OVERLAP))
        self.assertTrue(chunking.wants_headings("structure"))
        self.assertFalse(chunking.wants_headings("fixed"))


class MatchingAcrossChunkings(unittest.TestCase):
    def test_a_result_holding_the_passage_counts(self):
        passage = search_eval._words("Delay Repay pays back part of the fare when your train arrives fifteen "
                                     "minutes late or more, claimed online within twenty eight days")
        self.assertTrue(search_eval.holds("Refunds > Delay Repay\n\nDelay Repay pays back part of the fare when "
                                          "your train arrives fifteen minutes late or more, claimed online within "
                                          "twenty eight days. Other text follows here.", passage))
        self.assertTrue(search_eval.holds("Delay Repay pays back part of the fare when your train arrives "
                                          "fifteen minutes late", passage))                 # the first half
        self.assertFalse(search_eval.holds("Station car parks charge by the day; weekly permits cost less.", passage))
        target = {"chunk_id": None, "source": "https://a", "_words": passage}
        hits = [{"id": "1", "payload": {"source_url": "https://a", "text": "Parking costs money"}},
                {"id": "2", "payload": {"source_url": "https://a", "text": "Delay Repay pays back part of the fare "
                                        "when your train arrives fifteen minutes late or more"}}]
        self.assertEqual(search_eval.position(hits, target), 2)


class IngestWithStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database
        cls.client = storage.get_client()
        storage.ensure_collection(cls.client)

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    def ingest(self, url, strategy, entry=None):
        def extract(html, url, headings=False):
            return PAGE if headings else PAGE.replace("#", "").replace("\n\n", "\n")
        vector = lambda texts, query=False: [[1.0] + [0.0] * (config.EMBEDDING_DIM - 1) for _ in texts]
        with patch.object(config, "CHUNKING", strategy), \
                patch("common.ingest.scraping.fetch_html", return_value="<html></html>"), \
                patch("common.ingest.scraping.extract_text", side_effect=extract), \
                patch("common.ingest.scraping.extract_metadata", return_value={}), \
                patch.object(embedding, "embed_texts", side_effect=vector):
            return ingest.ingest_url(self.client, {"url": url, **(entry or {})})

    def rows(self, url):
        with self.client.connection() as connection:
            return connection.execute("SELECT body, payload FROM rag_chunks WHERE source_url = %s ORDER BY chunk_index",
                                      (url,)).fetchall()

    def test_a_page_is_cut_again_keeping_its_labels(self):
        url = "https://rail.example/refunds"
        self.assertEqual(self.ingest(url, "fixed")["status"], "updated")
        storage.merge_payload(self.client, url, {"meta": {"topic": ["refunds"]}, "origin": "setup"})
        before = self.rows(url)
        self.assertTrue(all(r["payload"]["chunking"] == "fixed" and "headings" not in r["payload"] for r in before))

        entries, adhoc = rechunk.pages(self.client)
        self.assertEqual(([e["url"] for e in entries], adhoc), ([url], 0))
        self.assertTrue(entries[0]["force"] and entries[0]["keep_payload"])
        self.assertEqual(self.ingest(url, "structure", entries[0])["status"], "updated")
        after = self.rows(url)
        self.assertEqual(after[0]["payload"]["headings"], ["Refunds"])
        self.assertTrue(after[0]["body"].startswith("Refunds\n\n"))
        self.assertTrue(all(r["payload"]["chunking"] == "structure" for r in after))
        self.assertTrue(all(r["payload"]["meta"] == {"topic": ["refunds"]} and r["payload"]["origin"] == "setup"
                            for r in after))                                       # the labels came along
        self.assertIn("Refunds > Season tickets", [" > ".join(r["payload"].get("headings", [])) for r in after])

        import web_api
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/api/sources/chunks", "headers": [],
                 "query_string": f"url={url}".encode()}
        shown = json.loads(web_api.source_chunks(Request(scope)).body)["chunks"]
        self.assertEqual([c["headings"] for c in shown], [r["payload"].get("headings", []) for r in after])
        self.assertEqual(shown[0]["chunking"], "structure")

    def test_rechunk_queues_a_job_for_web_pages_only(self):
        url = "https://rail.example/parking"
        self.ingest(url, "fixed")
        storage.upsert_chunks(self.client, "adhoc://notes", ["some notes"],
                              [[1.0] + [0.0] * (config.EMBEDDING_DIM - 1)], "h", 30)
        with patch("builtins.input", return_value="y"):
            self.assertEqual(rechunk.main([]), 0)
        job = storage.list_jobs(self.client, "ingest_urls", limit=1)[0]
        with self.client.connection() as connection:
            entries = connection.execute("SELECT entries FROM ingestion_jobs WHERE id = %s",
                                         (uuid.UUID(job["id"]),)).fetchone()["entries"]
        self.assertNotIn("adhoc://notes", [e["url"] for e in entries])
        self.assertIn(url, [e["url"] for e in entries])


if __name__ == "__main__":
    unittest.main()

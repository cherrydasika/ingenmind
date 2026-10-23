"""Scheduled re-crawl, change detection and freshness (R49, recrawl.py; common.ingest's chunk reuse)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import recrawl
from common import ingest
from test_auth import AuthDatabase

URL = "https://rail.example/policy"


class Recrawl(AuthDatabase):
    def setUp(self):
        recrawl.reset_schema_cache()
        self.addCleanup(recrawl.reset_schema_cache)
        recrawl.ensure_schema()
        with recrawl._connect() as connection:              # the class shares one database
            connection.execute("DELETE FROM source_schedules")
        self.stored = {URL: ["Bikes go free.", "Dogs travel free."]}
        summary = [{"payload": {"source_url": u}, "chunks": 1} for u in (URL, "https://rail.example/fares", "adhoc://notes")]
        for name, fake in (("source_summary", lambda client: summary),
                           ("get_existing_metadata", lambda client, url: {"ttl_days": 30}),
                           ("page_chunks", lambda client, url: [{"text": t, "vector": [0.1]} for t in self.stored.get(url, [])]),
                           ("refresh_expiry", lambda client, url, ttl: self.refreshed.append(url))):
            p = patch.object(recrawl.storage, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        self.refreshed = []

    def test_every_web_page_gets_a_schedule_and_due_follows_it(self):
        self.assertEqual(recrawl.sync(client=object()), 2)                     # not the ad-hoc document
        self.assertEqual(sorted(recrawl.due()), ["https://rail.example/fares", URL])   # never checked: due
        recrawl.set_every("https://rail.example/fares", "off")
        recrawl.set_every(URL, "daily")
        with recrawl._connect() as connection:
            connection.execute("UPDATE source_schedules SET last_checked_at = %s WHERE source_url = %s",
                               (datetime.now(timezone.utc) - timedelta(hours=2), URL))
        self.assertEqual(recrawl.due(), [])
        self.assertEqual(recrawl.due(datetime.now(timezone.utc) + timedelta(days=1)), [URL])
        with self.assertRaises(ValueError):
            recrawl.set_every(URL, "hourly")

    def test_a_changed_page_is_re_ingested_and_its_old_chunks_replaced(self):
        recrawl.sync(client=object())

        def ingest_url(client, entry, force=False):
            self.assertEqual((entry["keep_payload"], entry["recheck"], force), (True, True, False))
            self.stored[URL] = ["Bikes go free.", "Dogs travel free on a lead."]
            return {"status": "updated", "embedded": 1}
        out = recrawl.check(URL, client=object(), conditional=lambda u, e, m: {"status": 200, "etag": '"v2"'},
                            ingest_url=ingest_url)
        self.assertEqual(out["status"], "changed")
        self.assertEqual({k: out["diff"][k] for k in ("added", "removed", "kept", "embedded")},
                         {"added": 1, "removed": 1, "kept": 1, "embedded": 1})
        self.assertEqual(out["diff"]["sample_removed"], "Dogs travel free.")
        [row] = [r for r in recrawl.freshness() if r["source_url"] == URL]
        self.assertEqual((row["status"], row["etag"], row["stale"]), ("changed", '"v2"', False))

    def test_unchanged_gone_and_failing_pages(self):
        recrawl.sync(client=object())
        seen = []
        out = recrawl.check(URL, client=object(), conditional=lambda u, e, m: seen.append((e, m)) or {"status": 304},
                            ingest_url=lambda *a, **k: self.fail("a 304 is not fetched again"))
        self.assertEqual((out["status"], self.refreshed), ("unchanged", [URL]))   # its lifetime renewed
        gone = recrawl.check(URL, client=object(), conditional=lambda u, e, m: {"status": 404})
        self.assertEqual(gone["status"], "gone")
        for _ in range(recrawl.STALE_FAILURES):
            failed = recrawl.check(URL, client=object(), conditional=lambda u, e, m: (_ for _ in ()).throw(TimeoutError("slow")))
        self.assertEqual(failed["status"], "failed")
        [row] = [r for r in recrawl.freshness() if r["source_url"] == URL]
        self.assertTrue(row["stale"])


class ChunkReuse(AuthDatabase):
    def test_only_changed_chunks_are_embedded(self):
        stored = [{"text": "Bikes go free.", "vector": [1.0]}, {"text": "Dogs travel free.", "vector": [2.0]}]
        embedded, written = [], {}
        with patch.object(ingest.storage, "get_existing_metadata", return_value={"content_hash": "old", "expires_at": 0}), \
                patch.object(ingest.storage, "page_chunks", return_value=stored), \
                patch.object(ingest.scraping, "fetch_html", return_value="<html>x</html>"), \
                patch.object(ingest.scraping, "extract_text", return_value="new text"), \
                patch.object(ingest.scraping, "extract_metadata", return_value={}), \
                patch.object(ingest.dedup, "dedupe_page", return_value=("new text", [], {})), \
                patch.object(ingest.chunking, "split", return_value=[{"text": "Bikes go free.", "headings": []},
                                                                     {"text": "Dogs travel free on a lead.", "headings": []}]), \
                patch.object(ingest.embedding, "embed_texts", side_effect=lambda texts: embedded.extend(texts) or [[9.0]] * len(texts)), \
                patch.object(ingest.storage, "upsert_chunks", side_effect=lambda client, url, chunks, vectors, *a, **k: written.update(
                    chunks=chunks, vectors=vectors, replace=k.get("replace_existing"))):
            result = ingest.ingest_url(object(), {"url": URL, "recheck": True})
        self.assertEqual((result["status"], result["embedded"]), ("updated", 1))
        self.assertEqual(embedded, ["Dogs travel free on a lead."])                 # the unchanged chunk kept its vector
        self.assertEqual(written["vectors"], [[1.0], [9.0]])
        self.assertTrue(written["replace"])                                         # the old chunks go


if __name__ == "__main__":
    import unittest
    unittest.main()

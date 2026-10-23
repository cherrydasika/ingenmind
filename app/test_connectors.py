"""Document connectors (R48, app/connectors): files and S3 into the knowledge base, with provenance."""

import base64
import io
import uuid
import zipfile
from datetime import datetime, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

import connectors
import identity
from test_auth import AuthDatabase

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'


def docx(*paras) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", f"<w:document {W}><w:body>" + "".join(
            f"<w:p><w:r><w:t>{p}</w:t></w:r><w:r><w:t> more</w:t></w:r></w:p>" for p in paras) + "</w:body></w:document>")
    return out.getvalue()


def pptx(*slides) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i, text in enumerate(slides, 1):     # slide10 after slide2: in number order
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld {A}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>")
    return out.getvalue()


class Files(AuthDatabase):
    def test_each_format_is_read(self):
        self.assertEqual(connectors.parse("a.docx", docx("Kept 28 days.", "Email us.")), "Kept 28 days. more\nEmail us. more")
        slides = connectors.parse("deck.pptx", pptx(*[f"Slide {i}" for i in range(1, 12)]))
        self.assertTrue(slides.startswith("Slide 1\n\nSlide 2\n\nSlide 3"))
        self.assertTrue(slides.endswith("Slide 10\n\nSlide 11"))
        self.assertIn("Bikes", connectors.parse("p.html", b"<html><body><article><p>Bikes go free on most trains, "
                                                          b"says the operator.</p></article></body></html>"))
        self.assertEqual(connectors.parse("n.md", b"# Notes\nBikes go free."), "# Notes\nBikes go free.")
        for name, data, why in (("x.exe", b"MZ", "not a supported file"), ("e.txt", b"  ", "no text"),
                                ("bad.docx", b"not a zip", "couldn't be read"), ("big.txt", b"x" * (connectors.MAX_BYTES + 1), "larger")):
            with self.subTest(name=name), self.assertRaisesRegex(connectors.ConnectorError, why):
                connectors.parse(name, data)

    def test_a_file_keeps_its_source_and_where_it_came_from(self):
        with patch("adhoc_ingest.ingest", return_value={"status": "added", "chunks": 1}) as ingest:
            connectors.ingest("s3://docs/handbook/a.docx", "a.docx", docx("Kept 28 days."), 30, {"connector": "handbook"})
        title, text, ttl = ingest.call_args.args
        self.assertEqual((title, ttl), ("a.docx", 30))
        self.assertEqual(ingest.call_args.kwargs["source"], "s3://docs/handbook/a.docx")
        self.assertEqual(ingest.call_args.kwargs["provenance"],
                         {"file_name": "a.docx", "file_format": "Word", "connector": "handbook"})


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, **kw):
        when = datetime(2026, 10, 1, tzinfo=timezone.utc)
        return {"Contents": [{"Key": k, "Size": len(v), "LastModified": when, "ETag": '"e1"'}
                             for k, v in self.objects.items() if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class S3(AuthDatabase):
    def setUp(self):
        connectors._schema_ready = False
        self.addCleanup(setattr, connectors, "_schema_ready", False)
        for c in connectors.listing():
            connectors.remove(c["connector_id"])

    def test_a_bucket_is_listed_fetched_and_imported_with_provenance(self):
        fake = FakeS3({"handbook/": b"", "handbook/a.docx": docx("Kept 28 days."), "handbook/b.exe": b"MZ",
                       "other/c.txt": b"not under the prefix"})
        with patch.dict("os.environ", {"INTEGRATIONS_KEY": Fernet.generate_key().decode()}), \
                patch("boto3.client", return_value=fake) as client:
            connectors.add("handbook", "s3", "Handbook", {"bucket": "docs", "prefix": "/handbook/",
                                                          "endpoint_url": "http://minio:9000"},
                           {"access_key": "AK", "secret_key": "SK"})
            self.assertTrue(connectors.listing()[0]["has_secret"])
            items = connectors.get("handbook").list()
            self.assertEqual(client.call_args.kwargs["aws_access_key_id"], "AK")
            self.assertEqual([(i.id, i.public()["supported"]) for i in items],
                             [("handbook/a.docx", True), ("handbook/b.exe", False)])     # no folder marker
            self.assertEqual(connectors.get("handbook").changed_since("2026-10-02"), [])
            with patch("adhoc_ingest.ingest", return_value={"status": "added", "chunks": 1}) as ingest:
                results = connectors.import_items("handbook", ["handbook/a.docx", "handbook/b.exe", "missing"], 30)
        self.assertEqual([r["status"] for r in results], ["added", "error", "error"])   # failures stay with their item
        self.assertEqual(ingest.call_args.kwargs["source"], "s3://docs/handbook/a.docx")
        self.assertEqual(ingest.call_args.kwargs["provenance"]["etag"], "e1")
        for bad in ({"bucket": "A"}, {"bucket": "docs", "endpoint_url": "ftp://x"}):
            with self.subTest(bad=bad), self.assertRaises(connectors.ConnectorError):
                connectors.add("other", "s3", "x", bad)

    def test_a_file_uploaded_in_the_app(self):
        user = identity.create_user("Uma", f"uma-{uuid.uuid4().hex[:6]}@example.com", "admin")
        client = self.signed_in(user)
        with patch.object(connectors, "ingest", return_value={"status": "added", "chunks": 2, "source_id": "upload://a.docx"}) as ingest:
            reply = client.post("/api/adhoc/upload", json={"filename": "a.docx", "ttl_days": 7,
                                                           "data": base64.b64encode(docx("Kept.")).decode()})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(ingest.call_args.args[:2], ("upload://a.docx", "a.docx"))
        self.assertEqual(client.post("/api/adhoc/upload", json={"filename": "a.docx", "ttl_days": 7, "data": "%%%"}).status_code, 400)


if __name__ == "__main__":
    import unittest
    unittest.main()

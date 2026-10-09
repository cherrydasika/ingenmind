"""The demo documents parse, seeding marks the install set up, and a fresh
clone finds a URL list.
Run: PYTHONPATH=app:dags python -m unittest app/test_demo.py"""

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import seed_demo

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo"


class DemoTest(unittest.TestCase):
    def test_every_demo_document_has_a_title_and_says_it_is_fictional(self):
        docs = seed_demo.documents(DEMO)
        self.assertGreaterEqual(len(docs), 3)
        self.assertEqual(len({title for title, _ in docs}), len(docs))
        for path in DEMO.glob("*.md"):
            if path.name != "README.md":
                self.assertIn("<!-- Fictional demo data", path.read_text(), path.name)

    def test_the_fictional_notice_is_not_ingested(self):
        # In the ingested text it made the agents answer "this place does not exist".
        for title, text in seed_demo.documents(DEMO):
            self.assertNotIn("Fictional", text, title)
            self.assertNotIn("<!--", text, title)

    def test_url_list_falls_back_to_the_example(self):
        from common import config
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "urls.example.json").write_text("[]")
            with patch.dict(os.environ, {"URLS_CONFIG_PATH": str(Path(directory) / "urls.json")}):
                self.assertEqual(importlib.reload(config).URLS_CONFIG_PATH.name, "urls.example.json")
                (Path(directory) / "urls.json").write_text("[]")
                self.assertEqual(importlib.reload(config).URLS_CONFIG_PATH.name, "urls.json")
        importlib.reload(config)

    def test_seeding_marks_a_new_install_ready_as_demo(self):
        import sys
        from unittest.mock import Mock
        import knowledge_system
        ingest = Mock(ingest=Mock(return_value={"status": "updated", "chunks": 2}))
        for ready, marked in ((False, [(("demo",), {})]), (True, [])):
            with patch.dict(sys.modules, {"adhoc_ingest": ingest}), \
                    patch.object(seed_demo, "documents", return_value=[("Demo", "Text.")]), \
                    patch.object(knowledge_system, "is_ready", return_value=ready), \
                    patch.object(knowledge_system, "mark_ready") as mark:
                self.assertEqual(seed_demo.main(), 0)
            self.assertEqual([(c.args, c.kwargs) for c in mark.call_args_list], marked)


if __name__ == "__main__":
    unittest.main()

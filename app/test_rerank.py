"""The reranker (R22): hybrid_search's optional second pass, and the search test's reranked rows."""

import unittest
from unittest.mock import patch

import rerank
import retrieval
from flows import search_eval


def hit(n: int, text: str) -> dict:
    return {"id": f"c{n}", "score": 0.5, "vector": [0.0],
            "payload": {"text": text, "source_url": f"https://site/{n}", "chunk_index": 0}}


DENSE = [hit(n, f"passage {n}") for n in range(30)]
TEXT = [hit(n, f"passage {n}") for n in range(29, -1, -1)]


def by_number(question, texts, model=None):
    """A stand-in cross-encoder: prefers passage 17, then higher numbers."""
    return [100.0 if t == "passage 17" else float(t.split()[-1]) for t in texts]


class HybridSearch(unittest.TestCase):
    def search(self, **kwargs):
        stages = []
        with patch.object(retrieval.embedding, "embed_texts", return_value=[[0.0]]), \
                patch.object(retrieval.storage, "get_client"), \
                patch.object(retrieval.storage, "search_dense", side_effect=lambda c, v, n, f=None: DENSE[:n]), \
                patch.object(retrieval.storage, "search_text", side_effect=lambda c, q, n, f=None: TEXT[:n]), \
                patch.object(retrieval, "get_index_info", return_value={}):
            result = retrieval.hybrid_search("q", top_k=5, on_stage=lambda s, st, **i: stages.append((s, st, i)), **kwargs)
        return result, stages

    def test_off_by_default(self):
        with patch.object(rerank, "ENABLED", False), patch.object(rerank, "scores") as scores:
            result, stages = self.search()
        scores.assert_not_called()
        self.assertNotIn("rerank", result["timings"])
        self.assertNotIn("rerank", [s for s, _, _ in stages])

    def test_reranked_results_keep_the_best_top_k_of_more_candidates(self):
        with patch.object(rerank, "scores", side_effect=by_number) as scores:
            result, stages = self.search(rerank=20)
        self.assertEqual(len(scores.call_args.args[1]), 20)                  # 20 candidates re-sorted
        fused = result["rankings"]["fused"]
        self.assertEqual(len(fused), 5)
        self.assertEqual(fused[0]["id"], "c17")
        self.assertEqual(fused[0]["rerank_score"], 100.0)
        self.assertEqual(result["explain"]["fused"]["rows"][0]["rerank_score"], 100.0)
        self.assertIn("fused_rank", result["explain"]["fused"]["rows"][0])
        self.assertEqual(result["explain"]["rerank"]["candidates"], 20)
        self.assertIn("rerank", result["timings"])
        self.assertEqual([st for s, st, _ in stages if s == "rerank"], ["start", "done"])

    def test_a_failing_reranker_keeps_the_fused_order(self):
        with patch.object(rerank, "scores", side_effect=RuntimeError("no model")):
            result, stages = self.search(rerank=20)
        plain, _ = self.search(rerank=0)
        self.assertEqual([c["id"] for c in result["rankings"]["fused"]], [c["id"] for c in plain["rankings"]["fused"]])
        self.assertIn("no model", result["explain"]["rerank"]["error"])
        self.assertTrue([i for s, st, i in stages if s == "rerank" and st == "done"][0]["fallback"])

    def test_the_app_setting_applies_when_a_search_does_not_say(self):
        with patch.object(rerank, "ENABLED", True), patch.object(rerank, "CANDIDATES", 12), \
                patch.object(rerank, "scores", side_effect=by_number) as scores:
            self.search()
        self.assertEqual(len(scores.call_args.args[1]), 12)


class SearchTest(unittest.TestCase):
    def test_reranked_rows_are_scored_and_timed(self):
        questions = [{"question": "q", "chunk_id": "c17", "source": "https://site/17"}]
        searches = [(DENSE, TEXT)]
        with patch.object(rerank, "scores", side_effect=by_number):
            info = search_eval._rerank_all(questions, searches, {})
        self.assertEqual(info["candidates"], rerank.CANDIDATES)
        self.assertIsNotNone(info["seconds_median"])
        cells, per = search_eval.score(questions, searches)
        cell = {(c["mode"], c["top_k"]): c for c in cells}
        self.assertEqual(cell[("rerank", 3)]["mrr"], 1.0)
        self.assertEqual(per[0]["reranked"], 1)
        plain, _ = search_eval.score(questions, [(DENSE, TEXT)])
        self.assertNotIn("rerank", {c["mode"] for c in plain})

    def test_a_reranker_that_fails_leaves_the_rest_of_the_run(self):
        searches = [(DENSE, TEXT)]
        with patch.object(rerank, "scores", side_effect=RuntimeError("download failed")):
            info = search_eval._rerank_all([{"question": "q"}], searches, {})
        self.assertIn("download failed", info["error"])
        self.assertEqual(len(searches[0]), 2)


if __name__ == "__main__":
    unittest.main()

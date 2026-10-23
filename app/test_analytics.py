"""Analytics (R52, analytics.py): the views and queries behind the Analytics page, against the test database."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np

import analytics
import identity
import sessions
from flows import store
from test_auth import AuthDatabase

URL_A, URL_B, URL_C = "https://rail.example/bikes", "https://rail.example/dogs", "https://rail.example/old"


def search(first, ids, url=URL_A):
    return {"first": first, "retrieval": {"dense_ranking": ids, "sparse_ranking": ids[:1],
                                          "chunks": [{"id": i, "source_url": url} for i in ids]}}


class Analytics(AuthDatabase):
    def setUp(self):
        for module in (analytics, sessions):
            module.reset_schema_cache()
            self.addCleanup(module.reset_schema_cache)
        store.reset_schema_cache()
        analytics.ensure_schema()
        with analytics._connect() as connection:            # the class shares one database
            connection.execute("DELETE FROM app_session_turns")
            connection.execute("DELETE FROM agent_flow_runs")
        self.user = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        self.session = sessions.current(self.user["user_id"], None)
        now = datetime.now(timezone.utc)
        pages = {URL_A: 2, URL_B: 1, URL_C: 1}
        live = [patch.object(analytics, "_pages", return_value=pages),
                patch.object(analytics, "_count", return_value=4),
                patch.object(analytics, "_vectors", side_effect=lambda limit, prefer: [
                    {"id": i, "url": u, "vector": np.eye(4, dtype=np.float32)[n]}
                    for n, (i, u) in enumerate([("a1", URL_A), ("a2", URL_A), ("b1", URL_B), ("c1", URL_C)])]),
                patch.object(analytics, "_expiry", return_value=[{"url": URL_C, "expires_at": now + timedelta(days=3)},
                                                                 {"url": URL_B, "expires_at": now + timedelta(days=20)},
                                                                 {"url": URL_A, "expires_at": now + timedelta(days=90)}])]
        for p in live:
            p.start()
            self.addCleanup(p.stop)

    def turn(self, **result):
        return sessions.record_turn(self.session, self.user["user_id"], "q", {"answer": "a", **result})

    def test_the_funnel_a_turn_keeps(self):
        out = analytics.funnel({"answer": "Free [1], on a lead [4].", "searches": [search(1, ["a1", "a2"]), search(3, ["a2", "b1"], URL_B)]})
        self.assertEqual({k: out[k] for k in ("searches", "dense", "sparse", "kept", "cited")},
                         {"searches": 2, "dense": 4, "sparse": 2, "kept": 3, "cited": 2})
        self.assertEqual((out["cited_chunks"], out["pages"]), (["a1", "b1"], [URL_A, URL_B]))   # [n] = first + rank
        self.assertIsNone(analytics.funnel({"answer": "x", "searches": []}))

    def test_the_period(self):
        begin, end = analytics.period("2026-10-01", "2026-10-07")
        self.assertEqual((end - begin).days, 7)
        self.assertEqual((lambda b, e: (e - b).days)(*analytics.period(None, None)), 30)
        for bad in (("2026-10-07", "2026-10-01"), ("yesterday", None), ("2024-01-01", "2026-01-01")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                analytics.period(*bad)

    def test_the_report(self):
        allow = {"input": {"decision": "ALLOW"}, "output": {"decision": "ALLOW"}}
        f = analytics.funnel({"answer": "[1]", "searches": [search(1, ["a1", "a2"])]})
        self.turn(guardrails=allow, evidence=[{"decision": "GOOD_EVIDENCE"}], answer_check={"passed": True, "overall": 0.9},
                  funnel=f, sources=[{"url": URL_A, "cited": [1, 2]}])
        self.turn(guardrails=allow, evidence=[{"decision": "INSUFFICIENT_EVIDENCE"}, {"decision": "KNOWLEDGE_GAP"}],
                  answer_check={"passed": False}, funnel=analytics.funnel({"answer": "", "searches": [search(1, ["b1"], URL_B)]}),
                  sources=[], channel="slack")
        self.turn(guardrails={"input": {"decision": "BLOCK"}})
        for seconds, passed in ((10, True), (20, False), (40, None)):
            store.record_run("f", 1, "users", {"seconds": seconds, "input_tokens": 1000, "output_tokens": 500,
                                               "evaluated": passed is not None, "passed": passed})
        store.record_run("f", 1, "eval", {"seconds": 99})                   # not a user's: left out
        report = analytics.report()
        links = {(l["source"], l["target"]): l["value"] for l in report["journey"]["links"]}
        self.assertEqual(report["journey"]["asked"], 3)
        self.assertEqual(links, {("Asked", "Allowed"): 2, ("Asked", "Blocked at the start"): 1,
                                 ("Allowed", "Enough evidence"): 1, ("Allowed", "Not enough evidence"): 1,
                                 ("Enough evidence", "Answer passed"): 1, ("Not enough evidence", "Answer withheld"): 1})
        self.assertEqual(report["funnel"], {"questions": 2, "searches": 2, "dense": 3, "sparse": 2, "kept": 3, "cited": 1})
        sources = report["sources"]
        self.assertEqual(sources["most"], [{"url": URL_A, "answers": 1, "citations": 2, "found": 1, "chunks": 2}])
        self.assertEqual((sources["never"], sources["pages"]), ([URL_C], 3))       # B was found, never cited
        self.assertEqual({p["url"]: p["uses"] for p in report["map"]["points"] if p["uses"]}, {URL_A: 1, URL_B: 1})
        [day] = report["trends"]["days"]
        self.assertEqual((day["runs"], day["pass_rate"], day["p50"], day["input_tokens"]), (3, 0.5, 20.0, 3000))
        self.assertGreater(day["p95"], 30)
        self.assertEqual(report["freshness"]["buckets"], {"within_7_days": 1, "within_30_days": 1, "later": 1})
        self.assertEqual(analytics.report("2020-01-01", "2020-01-31")["journey"]["asked"], 0)

    def test_admins_only(self):
        self.assertEqual(self.signed_in(self.user).get("/api/analytics").status_code, 200)
        self.assertEqual(self.signed_in(self.user).get("/api/analytics?from=nope").status_code, 400)
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.assertEqual(self.signed_in(person).get("/api/analytics").status_code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()

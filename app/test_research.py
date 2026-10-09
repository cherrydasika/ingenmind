"""Research agent: source validation, and the knowledge-base subgraph's
knowledge gap → research → validation → automatic isolated ingestion →
retrieve again path. Search, extraction, the LLM and ingestion are faked:

    PYTHONPATH=app:dags python -m unittest -v app/test_research.py
"""

import io
import json
import threading
import unittest
import urllib.error
import uuid
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
import agent
import research
from test_evidence import GOOD_CHUNKS, NOW, TASK, FakeRun, assessment


def source(url: str, text: str = "Night trains run nightly from Vienna to Venice. " * 20, date: str | None = "2026-08-01",
           kind: str = "html", error: str | None = None) -> dict:
    return {"url": url, "kind": kind, "title": "Night trains", "date": date, "sitename": None,
            "chars": len(text), "text": text, "error": error, "seconds": 0.1}


def assessments(**scores):
    def assessor(question, missing, sources, brief=""):
        values = {"authority": 0.8, "freshness": 0.8, "consistency": 0.9, "relevance": 0.9, **scores}
        return research.SourceAssessments(sources=[research.SourceAssessment(
            url=s["url"], publisher="Operator", reasons="Official timetable page.", **values) for s in sources]), \
            {"input_tokens": 10, "output_tokens": 5}
    return assessor


class SourceValidation(unittest.TestCase):
    def test_good_source_is_accepted(self):
        result = research.validate("night trains?", ["night trains"], [source("https://www.nightjet.com/en")],
                                    assessor=assessments(), now=NOW)
        self.assertEqual(result["accepted"], 1)
        self.assertTrue(result["sources"][0]["accepted"])

    def test_official_domain_raises_authority(self):
        result = research.validate("visa?", [], [source("https://www.bmeia.gv.at/en/visa")],
                                    assessor=assessments(authority=0.3), now=NOW)
        self.assertTrue(result["sources"][0]["checks"]["official_domain"])
        self.assertGreaterEqual(result["sources"][0]["scores"]["authority"], 0.9)

    def test_stale_low_authority_or_inconsistent_sources_are_rejected(self):
        for scores in ({"freshness": 0.2}, {"authority": 0.3}, {"consistency": 0.2}, {"relevance": 0.1}):
            result = research.validate("q", [], [source("https://blog.example.com/post")],
                                        assessor=assessments(**scores), now=NOW)
            self.assertEqual(result["accepted"], 0, scores)

    def test_failed_or_thin_extraction_is_rejected_without_llm(self):
        calls = []

        def assessor(*args, **kwargs):
            calls.append(args)
            return assessments()(*args)
        result = research.validate("q", [], [source("https://a.example/x", error="HTTPError: 404"),
                                             source("https://b.example/y", text="short")], assessor=assessor, now=NOW)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(calls, [])

    def test_the_brief_reaches_the_assessor(self):
        seen = []

        def assessor(question, missing, sources, brief=""):
            seen.append(brief)
            return assessments()(question, missing, sources)
        research.validate("pantry car?", [], [source("https://a.example/x")], assessor=assessor, now=NOW,
                          brief="UK trains")
        self.assertEqual(seen, ["UK trains"])

    def test_old_metadata_date_lowers_freshness(self):
        result = research.validate("q", [], [source("https://a.example/x", date="2021-01-01")],
                                    assessor=assessments(freshness=0.8), now=NOW)
        self.assertLess(result["sources"][0]["scores"]["freshness"], 0.8)


class WebSearch(unittest.TestCase):
    def search(self, *errors, **kwargs):
        bodies = []

        def urlopen(request, timeout):
            bodies.append(json.loads(request.data))
            if len(bodies) <= len(errors):
                raise errors[len(bodies) - 1]
            return io.BytesIO(json.dumps({"results": [{"title": "T", "url": "https://x.uk", "content": "c"}]}).encode())
        with patch.object(research, "_api_key", return_value="k"), \
                patch.object(research.urllib.request, "urlopen", side_effect=urlopen):
            results = research.web_search("pantry car", **kwargs)
        return results, bodies

    def test_country_is_sent_to_tavily(self):
        results, bodies = self.search(country="United Kingdom")
        self.assertEqual((bodies[0]["country"], bodies[0]["topic"]), ("united kingdom", "general"))
        self.assertEqual(results[0]["url"], "https://x.uk")
        _, bodies = self.search()
        self.assertNotIn("country", bodies[0])

    def test_a_rejected_country_searches_worldwide(self):
        rejected = urllib.error.HTTPError("u", 400, "bad country", {}, None)
        results, bodies = self.search(rejected, country="atlantis")
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("country", bodies[1])
        self.assertEqual(len(results), 1)
        with self.assertRaises(urllib.error.HTTPError):   # other failures are not retried
            self.search(urllib.error.HTTPError("u", 401, "key", {}, None), country="france")


GAP = assessment(coverage=0.3, failure_type="KNOWLEDGE_GAP", missing_information=["night trains"])


class ResearchIngestionFlow(unittest.TestCase):
    def setUp(self):
        self.graph = agent._build_kb_graph(MemorySaver())
        self.ingested = []
        patches = [
            patch.object(agent.storage, "get_client", return_value=object()),
            patch.object(agent.guardrails, "check", return_value=agent.guardrails.Verdict(
                decision="ALLOW", reason="travel fixture", stage="research")),
            patch.object(agent.ingest, "ingest_url", side_effect=lambda client, entry: (
                self.ingested.append(entry) or {"url": entry["url"], "status": "updated", "chunks": 4})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def start(self, chunks, results, fetched):
        fake = FakeRun(chunks, results)
        fake.fetched = {s["url"]: s for s in fetched}
        fake.validator = assessments()
        config = {"configurable": {"run": fake, "thread_id": str(uuid.uuid4())}}
        return fake, config, self.graph.invoke({"task": TASK}, config=config)

    def test_gap_automatically_ingests_and_answers(self):
        fake, config, out = self.start([GOOD_CHUNKS, GOOD_CHUNKS], [GAP, assessment()],
                                        [source("https://www.nightjet.com/en")])
        self.assertNotIn("__interrupt__", out)
        agent.storage.get_client.assert_called_once_with("research")
        self.assertEqual([e["url"] for e in self.ingested], ["https://www.nightjet.com/en"])
        self.assertEqual(self.ingested[0]["metadata"]["origin"], "research_agent")
        self.assertEqual(fake.retrieval_calls, 2)  # retrieval ran again after ingestion
        finding = out["findings"][0]
        self.assertTrue(finding["status"].startswith("GOOD_EVIDENCE"))
        self.assertIn("after ingesting researched sources", finding["status"])

    def test_rejected_source_ingests_nothing(self):
        fake, config, out = self.start([GOOD_CHUNKS], [GAP], [source("https://www.nightjet.com/en", text="short")])
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(self.ingested, [])
        self.assertEqual(out["findings"][0]["status"], "Research found no source that passed validation")
        self.assertEqual(fake.retrieval_calls, 1)

    def test_no_valid_source_reports_without_asking(self):
        fake, config, out = self.start([GOOD_CHUNKS], [GAP], [source("https://a.example/x", error="HTTPError: 404")])
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(out["findings"][0]["status"], "Research found no source that passed validation")

    def test_gap_after_ingestion_is_reported_and_not_researched_again(self):
        fake, config, out = self.start([GOOD_CHUNKS], [GAP, GAP], [source("https://www.nightjet.com/en")])
        self.assertEqual(out["findings"][0]["status"], "Knowledge gap remains after ingesting researched sources")
        self.assertEqual(fake.retrieval_calls, 2)
        self.assertEqual(len(self.ingested), 1)

    def test_fetched_pages_become_candidates_when_research_runs_out_of_turns(self):
        # The research agent runs out of turns without submitting anything.
        fake2 = FakeRun([GOOD_CHUNKS, GOOD_CHUNKS], [GAP, assessment()])
        fake2.validator = assessments()
        fetched = source("https://www.cp.pt/info/en/tickets")
        fetched["delegation"] = TASK["n"]
        fake2.fetched = {fetched["url"]: fetched}
        original = fake2.specialist
        fake2.specialist = lambda role, task, n: ({"answer": "Out of turns.", "turns": 6} if role == "research"
                                                 else original(role, task, n))
        config2 = {"configurable": {"run": fake2, "thread_id": str(uuid.uuid4())}}
        out = self.graph.invoke({"task": TASK}, config=config2)
        self.assertNotIn("__interrupt__", out)
        self.assertEqual([s["url"] for s in self.ingested], ["https://www.cp.pt/info/en/tickets"])

    def test_only_accepted_sources_are_ingested(self):
        fake, config, out = self.start([GOOD_CHUNKS, GOOD_CHUNKS], [GAP, assessment()],
                                        [source("https://www.nightjet.com/en"), source("https://bad.example/thin", text="short")])
        self.assertNotIn("__interrupt__", out)
        self.assertEqual([entry["url"] for entry in self.ingested], ["https://www.nightjet.com/en"])

    def test_research_policy_blocks_writes_even_after_source_validation(self):
        with patch.object(agent.guardrails, "check", return_value=agent.guardrails.Verdict(
                decision="BLOCK", reason="source injection", stage="research")):
            fake, config, out = self.start([GOOD_CHUNKS, GOOD_CHUNKS], [GAP, GAP],
                                           [source("https://www.nightjet.com/en")])
        self.assertEqual(self.ingested, [])
        self.assertEqual(out["ingested"][0]["status"], "blocked_by_guardrail")
        self.assertEqual(fake.retrieval_calls, 1)
        self.assertEqual(out["findings"][0]["status"], "Research sources could not be ingested")


if __name__ == "__main__":
    unittest.main()


# ---------- data sources ----------

DOCS = "https://dev.example.gov.uk/ldb/docs"
SPEC = "https://dev.example.gov.uk/ldb/service.wsdl"
TERMS = "https://www.example-rail.co.uk/terms"


def page(url: str, text: str) -> dict:
    return {"url": url, "kind": "html", "title": "t", "date": None, "sitename": None, "chars": len(text),
            "text": text, "error": None, "seconds": 0.1}


def profile(candidate: int = 1, **fields) -> dict:
    values = {"candidate": candidate, "name": "LDB web service", "provider": "Rail body", "access_method": "soap_api",
              "auth": "api_key", "pricing": "Free with a token", "rate_limits": "5 million requests a month",
              "freshness": "real-time", "format": "SOAP/XML", "coverage": "UK departures",
              "licence_and_terms": "Open licence", "docs_url": DOCS, "signup_url": None, "spec_url": None,
              "evidence_urls": [DOCS], "confidence": "high", "unknowns": []}
    values.update(fields)
    return values


def profiler(*replies):
    """Returns the next reply's profiles each call; records what it was shown."""
    calls = []

    def fake(task, brief, candidates):
        calls.append({"brief": brief, "candidates": [{**c, "urls": [p["url"] for p in c["pages"]]} for c in candidates]})
        items = replies[min(len(calls), len(replies)) - 1]
        return research.SourceProfiles(profiles=[research.SourceProfile.model_validate(i) for i in items]), \
            {"input_tokens": 10, "output_tokens": 5}
    fake.calls = calls
    return fake


class SourceProfiling(unittest.TestCase):
    def test_unverifiable_fields_move_to_unknowns(self):
        checked = research.verify_profile(research.SourceProfile.model_validate(profile(
            signup_url="https://invented.example/signup", evidence_urls=[DOCS, "https://never-fetched.example"],
            pricing="unknown", access_method="graphql")), [page(DOCS, "Docs. Register for a token.")])
        self.assertIsNone(checked.signup_url)
        self.assertEqual(checked.evidence_urls, [DOCS])
        self.assertEqual(checked.access_method, "unknown")    # outside the vocabulary
        self.assertEqual(checked.confidence, "low")           # access method and pricing unverified
        for missing in ("access_method", "pricing"):
            self.assertIn(missing, checked.unknowns)
        self.assertTrue(any("signup_url" in u for u in checked.unknowns))

    def test_a_field_the_model_already_listed_is_not_repeated(self):
        checked = research.verify_profile(research.SourceProfile.model_validate(profile(
            rate_limits="unknown", format="unknown", unknowns=["specific rate limits"])), [page(DOCS, "Docs.")])
        self.assertEqual(checked.unknowns, ["specific rate limits", "format"])

    def test_a_supported_profile_keeps_its_facts(self):
        checked = research.verify_profile(research.SourceProfile.model_validate(profile()),
                                          [page(DOCS, "Free with a token. SOAP. 5 million requests a month.")])
        self.assertEqual((checked.docs_url, checked.confidence, checked.unknowns), (DOCS, "high", []))

    def test_a_linked_spec_is_probed_and_profiled_again(self):
        fetched = {DOCS: page(DOCS, f"The service is described by {SPEC}. Free with a token.")}
        fake = profiler([profile(spec_url=SPEC)], [profile(spec_url=SPEC, evidence_urls=[DOCS, SPEC])])
        probed = []

        def fetch(url):
            probed.append(url)
            return page(url, "<wsdl:definitions> GetDepartureBoard </wsdl:definitions>")
        out = research.profile_sources("live departures", [{"name": "LDB", "provider": "Rail body", "urls": [DOCS]}],
                                       fetched, brief="UK trains", profiler=fake, fetch=fetch)
        self.assertEqual(probed, [SPEC])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["candidates"][0]["urls"], [DOCS, SPEC])
        self.assertEqual(fake.calls[0]["brief"], "UK trains")
        self.assertEqual(out["profiles"][0]["spec_url"], SPEC)
        self.assertEqual(out["profiles"][0]["evidence_urls"], [DOCS, SPEC])
        self.assertEqual(out["usage"], {"input_tokens": 20, "output_tokens": 10})

    def test_a_link_no_fetched_page_gives_is_never_probed(self):
        fetched = {DOCS: page(DOCS, "Free with a token.")}
        probed = []
        out = research.profile_sources("q", [{"name": "LDB", "provider": "p", "urls": [DOCS]}], fetched,
                                       profiler=profiler([profile(spec_url=SPEC)]),
                                       fetch=lambda url: probed.append(url) or page(url, "x"))
        self.assertEqual(probed, [])
        self.assertIsNone(out["profiles"][0]["spec_url"])

    def test_candidates_are_capped_and_those_without_pages_skipped(self):
        fetched = {f"https://s{i}.example": page(f"https://s{i}.example", "text") for i in range(6)}
        fetched["https://s1.example"]["error"] = "HTTPError: 404"
        candidates = [{"name": f"S{i}", "provider": "p", "urls": [f"https://s{i}.example"]} for i in range(6)]
        fake = profiler([profile(c) for c in range(1, 5)])
        research.profile_sources("q", candidates, fetched, profiler=fake, max_profiles=4)
        self.assertEqual([c["candidate"] for c in fake.calls[0]["candidates"]], [1, 3, 4])

    def test_a_providers_separate_apis_and_feeds_are_each_profiled(self):
        fetched = {DOCS: page(DOCS, "Free with a token. SOAP departure boards. Also an XML push feed.")}
        fake = profiler([profile(name="LDB web service"),
                         profile(name="Push Port", access_method="push_feed", auth="registration"),
                         profile(name="Third"), profile(name="Fourth, over the cap")])
        out = research.profile_sources("q", [{"name": "Darwin", "provider": "Rail body", "urls": [DOCS]}], fetched,
                                       profiler=fake)
        self.assertEqual([(p["name"], p["access_method"]) for p in out["profiles"]],
                         [("LDB web service", "soap_api"), ("Push Port", "push_feed"), ("Third", "soap_api")])

    def test_no_usable_pages_makes_no_model_call(self):
        fake = profiler([])
        out = research.profile_sources("q", [{"name": "S", "provider": "p", "urls": ["https://x.example"]}], {},
                                       profiler=fake)
        self.assertEqual((out["profiles"], fake.calls), ([], []))


DATA_GAP = assessment(coverage=0.2, failure_type="KNOWLEDGE_GAP", gap_type="data_source",
                      missing_information=["live departure times"])


def judge(**fields):
    """A judge that recommends what it is told; records the profiles it saw."""
    seen = []

    def fake(task, brief, profiles):
        seen.append([p["name"] for p in profiles])
        values = {"ranking": [{"name": p["name"], "reason": "r"} for p in profiles],
                  "recommended": profiles[0]["name"], "fallback": None, "method": "api_tool",
                  "integration": {"tool_name": "uk_departures", "endpoint": "GetDepartureBoard"}, **fields}
        return research.SourceRecommendation.model_validate(values), {"input_tokens": 7, "output_tokens": 3}
    fake.seen = seen
    return fake


class SourceJudging(unittest.TestCase):
    def profiles(self):
        return [profile(name="National Rail website", access_method="scrape_only", licence_and_terms="unknown"),
                profile(name="LDB web service"),
                profile(name="Realtime Trains", access_method="rest_api",
                        licence_and_terms="Automated access to the site is prohibited; use the API.")]

    def test_a_scrape_only_source_is_never_recommended(self):
        out = research.judge_sources("live departures", self.profiles(),
                                     judge=judge(recommended="National Rail website", fallback="LDB web service"))
        rec = out["recommendation"]
        self.assertEqual((rec["recommended"], rec["method"]), ("LDB web service", "api_tool"))
        self.assertEqual(rec["integration"]["tool_name"], "")   # the judge's sketch was for another source
        avoid = {r["name"]: r["reason"] for r in rec["not_recommended"]}
        self.assertEqual(avoid["National Rail website"], "scrape only")
        self.assertIn("forbid", avoid["Realtime Trains"])     # its terms ban automated access
        self.assertIsNone(rec["fallback"])

    def test_the_method_follows_the_recommended_access_method(self):
        out = research.judge_sources("q", [profile(name="Push Port", access_method="push_feed")],
                                     judge=judge(method="api_tool"))
        self.assertEqual((out["recommendation"]["method"], out["usage"]["input_tokens"]), ("feed_consumer", 7))
        self.assertEqual(out["recommendation"]["integration"]["tool_name"], "uk_departures")

    def test_a_terms_claim_without_known_terms_is_corrected(self):
        site = profile(name="Departures website", access_method="web_page", licence_and_terms="unknown")
        stated = profile(name="Stated website", access_method="web_page",
                         licence_and_terms="Personal use only; no commercial reuse.")
        out = research.judge_sources("q", [profile(name="LDB web service"), site, stated], judge=judge(
            not_recommended=[
                {"name": "Departures website", "reason": "Web page only; no programmatic API access documented; "
                                                          "scraping would violate terms of service."},
                {"name": "Stated website", "reason": "Web page only; its licence allows personal use only."}]))
        avoid = {r["name"]: r["reason"] for r in out["recommendation"]["not_recommended"]}
        self.assertEqual(avoid["Departures website"],
                         "Web page only; no programmatic API access documented; terms of use not verified")
        # Terms the profile states: the reason is kept.
        self.assertEqual(avoid["Stated website"], "Web page only; its licence allows personal use only.")

    def test_nothing_eligible_recommends_nothing(self):
        out = research.judge_sources("q", self.profiles()[:1], judge=judge())
        self.assertEqual((out["recommendation"]["recommended"], out["recommendation"]["method"]), (None, "none"))

    def test_no_profiles_makes_no_model_call(self):
        fake = judge()
        out = research.judge_sources("q", [], judge=fake)
        self.assertEqual((fake.seen, out["recommendation"]["method"]), ([], "none"))


class DataSourceFlow(unittest.TestCase):
    def setUp(self):
        self.graph = agent._build_kb_graph(MemorySaver())
        self.ingested, self.saved, self.upserted, self.known = [], [], [], []
        for p in (patch.object(agent.storage, "get_client", return_value=object()),
                  patch.object(agent.ingest, "ingest_url", side_effect=lambda client, entry: self.ingested.append(entry)),
                  patch.object(agent.source_profiles, "find", side_effect=lambda *a, **k: self.known),
                  patch.object(agent.source_profiles, "upsert",
                               side_effect=lambda profiles, topic: self.upserted.extend(profiles)),
                  patch.object(agent.source_profiles, "save_report",
                               side_effect=lambda *a: self.saved.append(a) or "r_1")):
            p.start()
            self.addCleanup(p.stop)

    def run_graph(self, chunks, results, classify=None):
        fake = FakeRun(chunks, results)
        fake.fetched = {DOCS: page(DOCS, "Free with a token. SOAP. 5 million requests a month.")}
        fake.data_candidates, fake.budgets, fake.role_limits = {}, {}, {}
        fake.profiler = profiler([profile()])
        fake.judge = judge()
        fake.validator = assessments()
        fake.known_sources = {}
        fake.roles = []
        if classify:
            fake.gap_classifier = classify
        original = fake.specialist

        def specialist(role, task, n):
            fake.roles.append((role, task))
            if role == "research_data":
                fake.data_candidates[n] = [{"name": "LDB", "provider": "Rail body", "urls": [DOCS]}]
                return {"answer": "Submitted 1 data source.", "turns": 5}
            return original(role, task, n)
        fake.specialist = specialist
        config = {"configurable": {"run": fake, "thread_id": str(uuid.uuid4())}}
        return fake, self.graph.invoke({"task": TASK}, config=config)

    def test_a_live_data_gap_is_profiled_and_nothing_is_ingested(self):
        fake, out = self.run_graph([GOOD_CHUNKS], [DATA_GAP])
        roles = [r for r, _ in fake.roles]
        self.assertIn("research_data", roles)
        self.assertNotIn("research", roles)
        brief = next(t for r, t in fake.roles if r == "research_data")
        self.assertIn("at most 6 web_search and 10 fetch_source calls", brief)   # the node's default budget
        self.assertEqual(fake.budgets[TASK["n"]]["max_profiles"], 4)
        self.assertEqual(fake.role_limits["research_data"], 10)
        self.assertEqual(self.ingested, [])
        self.assertEqual(fake.retrieval_calls, 1)
        record = fake.research[0]
        self.assertEqual((record["gap_type"], record["profiles"][0]["name"]), ("data_source", "LDB web service"))
        self.assertEqual((record["recommendation"]["recommended"], record["report_id"]), ("LDB web service", "r_1"))
        self.assertEqual([p["name"] for p in self.upserted], ["LDB web service"])
        task, missing, brief, profiles, recommendation, trace = self.saved[0]
        self.assertEqual((task, missing, recommendation["method"]), (TASK["task"], ["live departure times"], "api_tool"))
        self.assertEqual(record["validation"]["usage"], {"input_tokens": 17, "output_tokens": 8})
        finding = out["findings"][0]
        self.assertEqual(finding["status"], "Knowledge gap needs a live data source")
        self.assertIn("not available here", finding["answer"])
        self.assertIn("recorded for an administrator with 1 candidate data source.", finding["answer"])
        self.assertNotIn("LDB", finding["answer"])     # sources are for an admin, not the user
        steps = [e["step"] for e in fake.events if e.get("type") == "research"]
        for step in ("classify", "known", "agent", "profile", "judge"):
            self.assertIn(step, steps)

    def test_a_fresh_known_profile_is_reused_without_profiling_it_again(self):
        self.known = [{"profile": profile(name="Darwin Push Port", provider="Rail body", access_method="push_feed"),
                       "verified_at": "2026-10-01T00:00:00+00:00", "fresh": True}]
        fake, out = self.run_graph([GOOD_CHUNKS], [DATA_GAP])
        brief = next(t for r, t in fake.roles if r == "research_data")
        self.assertIn("Already profiled", brief)
        self.assertIn("Darwin Push Port (Rail body)", brief)
        self.assertIn("submit at most 3 data sources", brief)
        self.assertEqual([c["name"] for c in fake.profiler.calls[0]["candidates"]], ["LDB"])   # only the new one
        self.assertEqual(fake.judge.seen, [["Darwin Push Port", "LDB web service"]])
        self.assertEqual([p["name"] for p in self.upserted], ["LDB web service"])             # the reused one is not re-stored
        self.assertEqual(fake.research[0]["reused"], 1)

    def test_enough_fresh_profiles_skip_the_search(self):
        self.known = [{"profile": profile(name=f"Source {i}", provider="p"), "verified_at": "2026-10-01T00:00:00+00:00",
                       "fresh": True} for i in range(4)]
        fake, out = self.run_graph([GOOD_CHUNKS], [DATA_GAP])
        self.assertNotIn("research_data", [r for r, _ in fake.roles])
        self.assertEqual(fake.profiler.calls, [])
        self.assertEqual(len(fake.judge.seen[0]), 4)

    def test_a_stale_known_profile_is_verified_again_from_its_pages(self):
        stale_url = "https://old.example.gov.uk/feed/docs"
        self.known = [{"profile": profile(name="Old feed", provider="Rail body", docs_url=stale_url,
                                          evidence_urls=[stale_url]),
                       "verified_at": "2026-01-01T00:00:00+00:00", "fresh": False}]
        with patch.object(research, "fetch_source", side_effect=lambda url: page(url, "Feed docs. Free.")) as fetch:
            fake, out = self.run_graph([GOOD_CHUNKS], [DATA_GAP])
        fetch.assert_called_once_with(stale_url)
        self.assertEqual([c["name"] for c in fake.profiler.calls[0]["candidates"]], ["Old feed", "LDB"])

    def test_a_store_failure_still_answers(self):
        with patch.object(agent.source_profiles, "save_report", side_effect=RuntimeError("database down")):
            fake, out = self.run_graph([GOOD_CHUNKS], [DATA_GAP])
        self.assertIsNone(fake.research[0]["report_id"])
        self.assertIn("RuntimeError", fake.research[0]["store_error"])
        self.assertNotIn("recorded for an administrator", out["findings"][0]["answer"])

    def test_a_static_gap_keeps_todays_path(self):
        fake, out = self.run_graph([GOOD_CHUNKS], [GAP])
        self.assertEqual([r for r, _ in fake.roles if r.startswith("research")], ["research"])
        self.assertEqual(fake.profiler.calls, [])
        self.assertNotIn("gap_type", fake.research[0])

    def test_a_gap_with_no_evidence_is_classified_by_one_call(self):
        calls = []

        def classify(task, missing, brief):
            calls.append(task)
            return research.GapClassification(gap_type="data_source", reason="live times"), {}
        fake, out = self.run_graph([[], []], [DATA_GAP], classify=classify)   # no chunks: no evaluator call
        self.assertEqual(fake.assessor_calls, 0)
        self.assertEqual(calls, [TASK["task"]])
        self.assertIn("research_data", [r for r, _ in fake.roles])

    def test_a_failed_classification_falls_back_to_todays_path(self):
        def classify(task, missing, brief):
            raise RuntimeError("model down")
        fake, out = self.run_graph([[], []], [DATA_GAP], classify=classify)
        self.assertIn("research", [r for r, _ in fake.roles])
        self.assertNotIn("research_data", [r for r, _ in fake.roles])


class DataSourceBudget(unittest.TestCase):
    def test_searches_and_fetches_stop_at_the_budget(self):
        run = agent._Run.__new__(agent._Run)
        run.lock, run.emit = threading.Lock(), lambda event: None
        run.budgets = {1: {"searches": 1, "fetches": 0, "max_profiles": 4, "used": {"searches": 0, "fetches": 0}}}
        run.data_candidates, run.web_searches, run.settings = {}, [], {}
        use = lambda name, **args: {"toolUseId": "t", "name": name, "input": json.dumps(args), "turn": 1}
        with patch.object(research, "web_search", return_value=[]):
            first = run._research_tool(use("web_search", query="UK rail API"), 1)
            second = run._research_tool(use("web_search", query="UK rail API pricing"), 1)
        fetch = run._research_tool(use("fetch_source", url=DOCS), 1)
        self.assertEqual(first["toolResult"]["status"], "success")
        for result in (second, fetch):
            self.assertEqual(result["toolResult"]["status"], "error")
            self.assertIn("Budget used", result["toolResult"]["content"][0]["text"])
        submitted = run._research_tool(use("submit_data_sources", sources=[
            {"name": "LDB", "provider": "Rail body", "urls": [DOCS]}, {"provider": "no name"}]), 1)
        self.assertEqual(submitted["toolResult"]["status"], "success")
        self.assertEqual([c["name"] for c in run.data_candidates[1]], ["LDB"])

    def test_static_research_has_no_budget(self):
        run = agent._Run.__new__(agent._Run)
        run.lock, run.budgets = threading.Lock(), {}
        self.assertFalse(run._over_budget(1, "searches"))

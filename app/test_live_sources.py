"""Live data sources in setup (R41, initialization/live_sources.py)."""

import uuid

import source_profiles
from initialization import live_sources
from research import SourceProfile, SourceProfiles, SourceRecommendation, Integration, RankedSource
from test_auth import AuthDatabase

BP = {"flow": {"brief": "UK rail travel.", "search_country": "united kingdom"}, "purpose": "rail",
      "knowledge_areas": [
          {"key": "refunds", "name": "Refunds", "knowledge_class": "STATIC_KNOWLEDGE", "description": "Refund rules."},
          {"key": "live_departures", "name": "Live train departures", "knowledge_class": "DYNAMIC_KNOWLEDGE",
           "description": "Real-time departures at UK stations."},
          {"key": "weather", "name": "Weather forecasts", "knowledge_class": "EXTERNAL_TOOL_API",
           "description": "Forecasts for a place."}]}


def search(query, max_results, country=None):
    if "Weather" in query:
        raise TimeoutError("search down")
    return [{"title": "Realtime Trains API", "url": "https://api.rtt.io/"},
            {"title": "RTT docs", "url": "https://www.api.rtt.io/docs"},           # same host: one candidate
            {"title": "Rail Data Marketplace", "url": "https://raildata.org.uk/"}]


def fetch(url):
    return {"url": url, "text": f"Docs for {url}: JSON API, free account.", "error": None, "kind": "html"}


def profiler(task, brief, candidates):
    return SourceProfiles(profiles=[SourceProfile(
        candidate=c["candidate"], name=c["name"], provider=c["provider"], access_method="rest_api", auth="api_key",
        pricing="free", rate_limits="unknown", freshness="real_time", format="JSON", coverage="GB stations",
        licence_and_terms="non-commercial", docs_url=c["pages"][0]["url"],
        evidence_urls=[c["pages"][0]["url"]], confidence="medium") for c in candidates]), {}


def judge(task, brief, profiles):
    return SourceRecommendation(ranking=[RankedSource(name=p["name"], reason="fits") for p in profiles],
                                recommended=profiles[0]["name"], method="api_tool",
                                integration=Integration(tool_name="realtime_trains", inputs="station (CRS code)",
                                                        endpoint="https://api.rtt.io/api/v1/json/search"),
                                rationale="Real-time and free."), {}


class LiveSources(AuthDatabase):
    def setUp(self):
        live_sources.reset_schema_cache()
        source_profiles._schema_ready = False
        self.addCleanup(live_sources.reset_schema_cache)
        self.addCleanup(setattr, source_profiles, "_schema_ready", False)

    def test_each_live_area_gets_a_report_and_the_tool_it_needs(self):
        out = live_sources.research_all(BP, search=search, fetch=fetch, profiler=profiler, judge=judge)
        self.assertEqual([(o["area"], o["status"]) for o in out], [("live_departures", "ready"), ("weather", "failed")])
        departures, weather = live_sources.view(BP)
        self.assertEqual((departures["recommended"], departures["method"]), ("Realtime Trains API", "api_tool"))
        self.assertEqual(departures["tool"], {"name": "realtime_trains", "exists": False, "method": "api_tool",
                                              "endpoint": "https://api.rtt.io/api/v1/json/search"})
        report = source_profiles.get_report(departures["report_id"])
        self.assertEqual([p["provider"] for p in report["profiles"]], ["api.rtt.io", "raildata.org.uk"])
        self.assertEqual(weather["status"], "failed")                        # one area's failure stays with it
        self.assertIn("search down", weather["error"])
        self.assertIn("get_weather", weather["existing"])                    # a tool here may already answer it
        self.assertEqual([a["key"] for a in live_sources.live_areas(BP)], ["live_departures", "weather"])   # not refunds

    def test_areas_not_yet_researched_show_as_such(self):
        self.assertEqual({a["status"] for a in live_sources.view(BP)}, {"not researched"})


if __name__ == "__main__":
    import unittest
    unittest.main()

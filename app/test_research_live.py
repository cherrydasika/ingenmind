"""Opt-in live check of the data-source research path, with real web search
(Tavily), the configured chat model and real page fetches. Skipped unless
RUN_LIVE=1. It forces a data-source gap ("live UK train departures") and
runs the research agent and the source validator steps directly, so the
outcome does not depend on what the knowledge base holds. Reports and
profiles are not stored unless LIVE_SAVE=1. On EC2, in the webapp container:

    RUN_LIVE=1 PYTHONPATH=/app:/app/dags python -m unittest -v test_research_live

The bar (issue #10): at least one Darwin source, the National Rail website
not recommended, and an API-based method.
"""

import json
import os
import unittest
import uuid
from unittest.mock import patch

import agent
import agent_runtime
import llm
import research
import source_profiles

TASK = {"n": 1, "agent": "knowledge_base", "round": 1, "turn": 1,
        "task": "Where can I find live UK train departure times, platforms and delays?"}
EVALUATION = {"decision": "KNOWLEDGE_GAP", "gap_type": "data_source", "overall_confidence": 0.1,
              "missing_information": ["live departure times, platforms and delays for UK stations"]}


def _why_skipped() -> str | None:
    if os.environ.get("RUN_LIVE") != "1":
        return "set RUN_LIVE=1 to run the live research test"
    if setup := llm.config_error():
        return f"the chat model is not configured: {setup}"
    try:
        research._api_key()
    except RuntimeError as error:
        return str(error)
    return None


@unittest.skipIf(_why_skipped(), _why_skipped() or "")
class LiveUkDepartures(unittest.TestCase):
    def test_live_uk_departures_report(self):
        events = []
        # A new session each run: AgentCore keeps a conversation per session,
        # and a reused one carries on from the last run instead of searching.
        run = agent._Run(f"live-research-{uuid.uuid4().hex[:12]}", None, agent_runtime.DEFAULT_MAX_ITERATIONS, events.append,
                         lambda search: search)
        run.settings = agent._flow_settings(agent.FLOW)
        config = {"configurable": {"run": run, "settings": {}}}
        state = {"task": TASK, "evaluation": EVALUATION}
        stores = [patch.object(source_profiles, "find", return_value=[])]   # always a fresh search
        if os.environ.get("LIVE_SAVE") != "1":
            stores += [patch.object(source_profiles, "upsert"),
                       patch.object(source_profiles, "save_report", return_value=None)]
        for p in stores:
            p.start()
            self.addCleanup(p.stop)

        state.update(agent._research_agent(state, config))
        self.assertNotIn("error", state, state.get("error"))
        state.update(agent._source_validator(state, config))
        self.assertNotIn("error", state, state.get("error"))
        record = run.research[0]
        rec = record["recommendation"]
        print("\n" + json.dumps({"searches": [w["query"] for w in run.web_searches],
                                 "profiles": record["profiles"], "recommendation": rec}, indent=1, ensure_ascii=False))

        names = [f"{p['name']} {p['provider']}".lower() for p in record["profiles"]]
        self.assertTrue(any("darwin" in n for n in names), names)
        website = [p for p in record["profiles"] if "national rail" in f"{p['name']} {p['provider']}".lower()
                   and p["access_method"] in ("web_page", "scrape_only")]
        avoided = " ".join(r["name"].lower() for r in rec["not_recommended"])
        self.assertTrue(website, "the National Rail website was not profiled")
        self.assertTrue(all(p["name"].lower() in avoided for p in website), rec["not_recommended"])
        self.assertNotIn((rec["recommended"] or "").lower(), [p["name"].lower() for p in website])
        self.assertIn(rec["method"], ("api_tool", "feed_consumer"))


if __name__ == "__main__":
    unittest.main()

"""Opt-in live check of Domain Blueprint research (#19), with real web search
(Tavily), the configured chat model and real page fetches, for two different
domains through the same code. Skipped unless RUN_LIVE=1. Stores nothing.

    RUN_LIVE=1 PYTHONPATH=/app:/app/dags python -m unittest -v test_blueprint_live

The bar: live status comes out dynamic or a tool, a rules area static, and a
regulator is named from a fetched page. For UK trains, source discovery (#20)
then finds and recommends National Rail and an official (government or
regulator) site.
"""

import os
import unittest

import llm
import research
from initialization import blueprint, sources
from initialization.requirements import SetupRequirements

LIVE = ("DYNAMIC_KNOWLEDGE", "EXTERNAL_TOOL_API")
DOMAINS = (
    ("UK trains", SetupRequirements(
        purpose="Answer passengers' questions about trains in the UK", audience=["general passengers"],
        regions=["United Kingdom"], question_types=["tickets and railcards", "refunds and delay compensation",
                                                    "accessibility"], live_information=["departures"]),
     ("depart",), ("refund", "compensation")),
    ("flights", SetupRequirements(
        purpose="Answer travellers' questions about flying from UK airports", audience=["air passengers"],
        regions=["United Kingdom"], question_types=["baggage rules", "delays and cancellations rights",
                                                    "airport assistance"], live_information=["flight status"]),
     ("status",), ("baggage",)),
)


def _why_skipped() -> str | None:
    if os.environ.get("RUN_LIVE") != "1":
        return "set RUN_LIVE=1 to run the live blueprint check"
    if setup := llm.config_error():
        return f"the chat model is not configured: {setup}"
    try:
        research._api_key()
    except RuntimeError as error:
        return str(error)
    return None


@unittest.skipIf(_why_skipped(), _why_skipped() or "")
class LiveBlueprints(unittest.TestCase):
    def test_two_domains(self):
        for name, req, live_words, static_words in DOMAINS:
            with self.subTest(domain=name):
                bp, record = blueprint.generate(req)
                print(f"\n{name}: {len(record['pages'])} pages; areas:",
                      [(a.name, a.knowledge_class) for a in bp.knowledge_areas],
                      "\n  organisations:", [(o.name, o.role) for o in bp.organisations],
                      "\n  search country:", bp.flow.search_country)
                self.assertGreaterEqual(len(record["pages"]), 3)
                text = lambda a: f"{a.key} {a.name} {a.description}".lower()
                live = [a for a in bp.knowledge_areas if any(w in text(a) for w in live_words)]
                rules = [a for a in bp.knowledge_areas if any(w in text(a) for w in static_words)]
                self.assertTrue(live and all(a.knowledge_class in LIVE for a in live), live)
                self.assertTrue(rules and all(a.knowledge_class == "STATIC_KNOWLEDGE" for a in rules), rules)
                self.assertTrue(any(o.role == "regulator" and o.evidence_urls for o in bp.organisations),
                                bp.organisations)
                self.assertEqual(bp.flow.search_country, "united kingdom")
                if name == "UK trains":
                    sites, _ = sources.discover(bp.model_dump())
                    print("  sources:", [(s["host"], s["authority"], s["recommended"]) for s in sites])
                    recommended = {s["host"] for s in sites if s["recommended"]}
                    self.assertIn("nationalrail.co.uk", recommended)
                    self.assertTrue(any(h.endswith(".gov.uk") for h in recommended), recommended)


if __name__ == "__main__":
    unittest.main()

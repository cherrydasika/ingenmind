"""Scraper trials (scraper_trials.py): today's scraper against a candidate, scored with no model calls."""

import unittest
import uuid
from unittest.mock import patch

import identity
import llm
import scraper_trials
from test_auth import AuthDatabase

NAV = "Home | Planning | Bins | Council tax | Contact us"
FOOTER = "© Example Borough Council. Cookies. Accessibility statement."


def page(title: str, body: str) -> str:
    return (f"<html><head><title>{title}</title></head><body><nav><p>{NAV}</p></nav>"
            f"<main><h1>{title}</h1>{body}</main><footer><p>{FOOTER}</p></footer></body></html>")


SITE = "https://www.example.gov.uk"
PAGES = {
    f"{SITE}/land-search": page("Request a land search", "<p>A residential search costs £120.00 inclusive of VAT. "
                                "An expedited land search costs an extra £39.60, inclusive of VAT, and is returned "
                                "within three working days.</p><p>Searches are sent by email.</p>"),
    f"{SITE}/report-breach": page("Report a planning breach", "<p>Report building work without planning permission "
                                  "online. The enforcement team visits within ten working days of a report.</p>"),
    f"{SITE}/notices": page("Public notices", "<p>Planning applications that affect a conservation area or a listed "
                            "building are advertised here and in the local newspaper for 21 days.</p>"),
    f"{SITE}/bins": page("Bin collections", "<p>Green waste bins are collected every other Tuesday from March to "
                         "November. A garden waste permit costs £55 a year.</p>"),
}
QUESTIONS = [
    {"question": "How much extra is an expedited land search?", "source": f"{SITE}/land-search",
     "expected": "An expedited land search costs an extra £39.60 inclusive of VAT."},
    {"question": "How long do planning notices for a conservation area run?", "source": f"{SITE}/notices",
     "expected": "They are advertised for 21 days."},
]


def fetcher(missing=()):
    def fetch(url):
        return (403, "") if url in missing else (200, PAGES[url])
    return fetch


class Scoring(unittest.TestCase):
    def test_facts_count_numbers_and_key_words(self):
        expected = "An expedited search costs an extra £39.60 inclusive of VAT."
        self.assertEqual(scraper_trials.fact_score(expected, "Expedited search: extra £39.60, inclusive of VAT, costs"), 1.0)
        self.assertLess(scraper_trials.fact_score(expected, "An expedited search costs an extra fee."), 0.7)
        self.assertIsNone(scraper_trials.fact_score("", "anything"))

    def test_boilerplate_is_lines_repeated_across_a_site(self):
        texts = {f"{SITE}/{n}": f"{NAV}\nPage {n} has its own words here." for n in range(4)}
        texts["https://elsewhere.org/one"] = f"{NAV}\nOnly page on its site."
        shares = scraper_trials.boilerplate(texts)
        self.assertGreater(shares[f"{SITE}/0"], 0.4)
        self.assertEqual(shares["https://elsewhere.org/one"], 0.0)       # one page: nothing repeats

    def test_keyword_search_finds_the_page(self):
        index = scraper_trials.BM25([("a", "garden waste permit costs per year"), ("b", "expedited land search fee")])
        self.assertEqual(index.search("how much is an expedited land search", 5), ["b"])

    def test_the_verdict(self):
        before = {"fetch_rate": 0.9, "text_rate": 0.9, "facts": 0.8, "hit_at_5": 0.5, "mrr": 0.4, "boilerplate": 0.2,
                  "fetch_s": 1.0}
        self.assertEqual(scraper_trials.verdict(before, dict(before))[0], "no_change")
        self.assertEqual(scraper_trials.verdict(before, {**before, "fetch_rate": 1.0})[0], "better")
        self.assertEqual(scraper_trials.verdict(before, {**before, "fetch_rate": 1.0, "facts": 0.7})[0], "worse")
        self.assertEqual(scraper_trials.verdict(before, {**before, "boilerplate": 0.1})[0], "better")
        self.assertEqual(scraper_trials.verdict(before, {**before, "fetch_s": 9.0})[0], "no_change")   # speed never decides


class Trials(AuthDatabase):
    def setUp(self):
        scraper_trials.reset_schema_cache()
        self.addCleanup(scraper_trials.reset_schema_cache)
        fake = {"key": "fake", "label": "Fake library", "about": "", "fetch": "fake", "extract": "trafilatura",
                "libraries": ["trafilatura"], "modules": ["trafilatura"]}
        for patcher in (patch.object(scraper_trials, "CANDIDATES", [fake]),
                        patch.dict(scraper_trials.FETCHERS, {"requests": fetcher(), "fake": fetcher()}),
                        patch.object(scraper_trials, "test_questions", lambda: QUESTIONS),
                        patch.object(llm, "_route", side_effect=AssertionError("a scraper trial called a model"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_same_pages_change_nothing(self):
        trial = scraper_trials.run("fake", "a note", "ada", urls=list(PAGES))
        self.assertEqual(trial["verdict"], "no_change")
        self.assertEqual((trial["pages"], trial["questions"], trial["note"]), (4, 2, "a note"))
        self.assertEqual(trial["result"]["agreement"], 1.0)
        self.assertEqual(trial["baseline"]["hit_at_5"], 1.0)
        self.assertGreater(trial["baseline"]["facts"], 0.9)
        self.assertEqual(trial["versions"]["trafilatura"], scraper_trials._version("trafilatura"))

    def test_a_page_lost_is_worse_and_a_page_gained_is_better(self):
        with patch.dict(scraper_trials.FETCHERS, {"fake": fetcher(missing={f"{SITE}/land-search"})}):
            worse = scraper_trials.run("fake", urls=list(PAGES))
        self.assertEqual(worse["verdict"], "worse")
        lost = next(p for p in worse["per_page"] if p["url"] == f"{SITE}/land-search")
        self.assertEqual((lost["before"]["error"], lost["after"]["error"]), (None, "HTTP 403"))
        self.assertEqual(worse["per_question"][0]["after"], {"rank": None, "facts": 0.0})

        with patch.dict(scraper_trials.FETCHERS, {"requests": fetcher(missing={f"{SITE}/notices"})}):
            better = scraper_trials.run("fake", urls=list(PAGES))
        self.assertEqual(better["verdict"], "better")
        self.assertEqual([t["id"] for t in scraper_trials.list_trials()], [better["id"], worse["id"]])

    def test_a_trial_in_the_background_and_its_page(self):
        with patch.object(scraper_trials, "test_pages", lambda: list(PAGES)):
            job = scraper_trials.start("fake", "from the page", "ada", thread=False)
        self.assertEqual(job["status"], "done")
        admin = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        client = self.signed_in(admin)
        view = client.get("/api/scraper-trials").json()
        self.assertEqual(view["trials"][0]["id"], job["trial_id"])
        self.assertEqual(view["candidates"][0]["key"], "fake")
        self.assertEqual(view["tried"], scraper_trials.TRIED)
        self.assertEqual(client.get(f"/api/scraper-trials/{job['trial_id']}").json()["note"], "from the page")
        self.assertEqual(client.post("/api/scraper-trials", json={"candidate": "nope"}).status_code, 404)
        self.assertEqual(client.delete(f"/api/scraper-trials/{job['trial_id']}").status_code, 200)
        self.assertEqual(client.get(f"/api/scraper-trials/{job['trial_id']}").status_code, 404)
        person = identity.create_user("Bo", f"bo-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.assertEqual(self.signed_in(person).get("/api/scraper-trials").status_code, 403)

    def test_a_library_not_installed_cant_be_tried(self):
        missing = {"key": "fake", "label": "Fake library", "about": "", "fetch": "fake", "extract": "trafilatura",
                   "libraries": ["no-such-lib"], "modules": ["no_such_module_here"]}
        with patch.object(scraper_trials, "CANDIDATES", [missing]):
            self.assertFalse(scraper_trials.candidates()[0]["available"])
            with self.assertRaisesRegex(ValueError, "isn't installed"):
                scraper_trials.start("fake", thread=False)


if __name__ == "__main__":
    unittest.main()

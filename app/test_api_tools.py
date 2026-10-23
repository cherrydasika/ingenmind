import unittest
from unittest.mock import patch

from api_tools import weather

NEWARK_NJ = {"name": "Newark", "country": "United States", "country_code": "US", "admin1": "New Jersey",
             "latitude": 40.7, "longitude": -74.2, "population": 281944}
NEWARK_UK = {"name": "Newark on Trent", "country": "United Kingdom", "country_code": "GB", "admin1": "England",
             "admin2": "Nottinghamshire", "latitude": 53.1, "longitude": -0.8, "population": 43363}
NEWARK_DE = {"name": "Newark", "country": "United States", "country_code": "US", "admin1": "Delaware",
             "latitude": 39.7, "longitude": -75.8, "population": 33817}


class Geocoding(unittest.TestCase):
    def geocode(self, location, results, by_country=None):
        """results: what a search everywhere returns; by_country: code → what a
        search within that country returns."""
        asked = []
        def get(url, params):
            asked.append(params)
            if "countryCode" in params:
                return {"results": (by_country or {}).get(params["countryCode"], [])}
            return {"results": results}
        with patch.object(weather, "_get", side_effect=get):
            return weather._geocode(location), asked

    def test_a_country_after_the_name_is_not_searched_for(self):
        leeds = {"name": "Leeds", "country_code": "GB", "country": "United Kingdom"}
        place, asked = self.geocode("Leeds, UK", [], {"GB": [leeds]})
        self.assertEqual((asked[0]["name"], asked[0]["countryCode"]), ("Leeds", "GB"))
        self.assertIs(place, leeds)

    def test_within_the_country_the_larger_place_wins(self):
        orkney = {"name": "Newark", "country_code": "GB", "admin1": "Scotland", "country": "United Kingdom"}
        self.assertIs(self.geocode("Newark, UK", [NEWARK_NJ], {"GB": [orkney, NEWARK_UK]})[0], NEWARK_UK)

    def test_a_country_with_no_match_falls_back_to_everywhere(self):
        place, asked = self.geocode("Salzburg, AT", [{"name": "Salzburg", "country_code": "AT"}], {})
        self.assertEqual(place["name"], "Salzburg")
        self.assertEqual([("countryCode" in a) for a in asked], [True, False])

    def test_the_qualifier_picks_among_the_matches(self):
        # Open-Meteo lists the most populous first: Newark, New Jersey.
        self.assertIs(self.geocode("Newark, UK", [NEWARK_NJ, NEWARK_DE, NEWARK_UK])[0], NEWARK_UK)
        self.assertIs(self.geocode("Newark, Delaware", [NEWARK_NJ, NEWARK_DE, NEWARK_UK])[0], NEWARK_DE)
        self.assertIs(self.geocode("Newark, Nottinghamshire, England", [NEWARK_NJ, NEWARK_UK])[0], NEWARK_UK)

    def test_a_named_region_beats_an_alias(self):
        lancaster_uk = {"name": "Lancaster", "country_code": "GB", "admin1": "England", "country": "United Kingdom"}
        lancaster_scot = {"name": "Lancaster", "country_code": "GB", "admin1": "Scotland", "country": "United Kingdom"}
        self.assertIs(self.geocode("Lancaster, Scotland", [lancaster_uk, lancaster_scot])[0], lancaster_scot)

    def test_without_a_qualifier_the_first_match_is_kept(self):
        place, asked = self.geocode("Newark", [NEWARK_NJ])
        self.assertIs(place, NEWARK_NJ)
        self.assertEqual(asked[0]["count"], 1)
        self.assertIs(self.geocode("Newark, Narnia", [NEWARK_NJ, NEWARK_UK])[0], NEWARK_NJ)

    def test_no_match_is_reported(self):
        self.assertIsNone(self.geocode("Nowhereville, UK", [])[0])
        weather._cache.clear()
        with patch.object(weather, "_get", return_value={}):
            result = weather.run({"location": "Nowhereville, UK"})
        self.assertFalse(result["ok"])
        self.assertIn("No place called 'Nowhereville, UK'", result["error"])


class PoliteRetry(unittest.TestCase):
    """R45: a 429 or 503 from Open-Meteo is retried once, after its Retry-After (up to 3 s)."""

    def reply(self, body=b'{"ok": 1}'):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body
        return Reply()

    def error(self, code, retry_after=None):
        import email.message
        import urllib.error
        headers = email.message.Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", code, "busy", headers, None)

    def test_a_busy_reply_is_retried_once(self):
        waits = []
        with patch("urllib.request.urlopen", side_effect=[self.error(503, 2), self.reply()]) as get:
            self.assertEqual(weather._get(weather.FORECAST, {"latitude": 1}, sleep=waits.append), {"ok": 1})
        self.assertEqual((get.call_count, waits), (2, [2.0]))

    def test_only_once_and_only_when_it_is_worth_waiting(self):
        import urllib.error
        cases = [([self.error(429), self.error(429)], 2),     # still busy: give up after one retry
                 ([self.error(404)], 1),                       # not a busy reply: no retry
                 ([self.error(503, 120)], 1)]                  # asks for two minutes: not waited for
        for replies, calls in cases:
            with self.subTest(calls=calls), patch("urllib.request.urlopen", side_effect=replies) as get:
                with self.assertRaises(urllib.error.HTTPError):
                    weather._get(weather.FORECAST, {}, sleep=lambda s: None)
                self.assertEqual(get.call_count, calls)


if __name__ == "__main__":
    unittest.main()

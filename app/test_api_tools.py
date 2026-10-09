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


if __name__ == "__main__":
    unittest.main()

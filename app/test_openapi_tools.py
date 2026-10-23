"""API tools imported from OpenAPI specs (R39, openapi_tools.py)."""

import json
import urllib.parse
from unittest.mock import patch

from cryptography.fernet import Fernet

import agent
import api_tools
import openapi_tools as o
from test_auth import AuthDatabase

SPEC = """
openapi: 3.1.0
info: {title: Rail fares}
servers: [{url: /api}]
components:
  parameters:
    Station: {name: station, in: path, required: true, schema: {type: string}}
paths:
  /stations/{station}/departures:
    parameters: [{$ref: '#/components/parameters/Station'}]
    get:
      operationId: listDepartures
      summary: Departures from a station
      parameters:
        - {name: lines, in: query, explode: false, schema: {type: array, items: {type: string}}}
        - {name: limit, in: query, schema: {type: integer}}
  /bookings:
    post:
      summary: Book a seat
      requestBody:
        content:
          application/json:
            schema: {type: object, required: [train], properties: {train: {type: string}, seats: {type: integer}}}
"""


class Spec(AuthDatabase):
    def test_operations_with_refs_path_params_and_bodies(self):
        ops = {op["id"]: op for op in o.operations(o.load(text=SPEC))}
        self.assertEqual(set(ops), {"listdepartures", "post_bookings"})
        departures = ops["listdepartures"]
        self.assertEqual((departures["method"], departures["side_effects"]), ("GET", False))
        self.assertEqual(departures["parameters"]["station"]["in"], "path")              # from a $ref, path-level
        self.assertTrue(departures["parameters"]["station"]["required"])
        self.assertFalse(departures["parameters"]["lines"]["explode"])
        booking = ops["post_bookings"]
        self.assertEqual((booking["side_effects"], booking["body"]["required"]), (True, ["train"]))
        self.assertEqual(o.base_url(o.load(text=SPEC), "https://rail.example/spec.yml"), "https://rail.example/api")
        for bad in ("not: a spec", "openapi: 2.0\npaths: {}", "{{{"):
            with self.subTest(bad=bad), self.assertRaises(o.OpenApiImportError):
                o.load(text=bad)
        with self.assertRaises(o.OpenApiImportError):
            o.load("http://evil.example/spec.yml")


class Imported(AuthDatabase):
    def setUp(self):
        o._schema_ready = False
        self.addCleanup(setattr, o, "_schema_ready", False)
        for row in o.rows():
            o.remove(row["name"])

    def test_imported_operations_are_api_tools_with_guardrails(self):
        made = o.import_operations("", ["listdepartures", "post_bookings"], "rail", "https://rail.example/api", text=SPEC)
        self.assertEqual([m["name"] for m in made], ["rail_listdepartures", "rail_post_bookings"])
        tools = api_tools.all_tools()
        self.assertIn("rail_listdepartures", tools)
        self.assertIn("get_weather", tools)                                        # the built-in ones stay
        args = {"station": "LDS", "lines": ["A", "B"], "limit": 3}
        self.assertIsNone(api_tools.check("rail_listdepartures", args))
        self.assertIn("missing", api_tools.check("rail_listdepartures", {"limit": 3}))
        self.assertIn("an email address", api_tools.check("rail_listdepartures", {"station": "jo@example.com"}))
        self.assertIn("needs a person's approval", api_tools.check("rail_post_bookings", {"train": "1A23"}))   # not GET
        self.assertIsNone(api_tools.check("rail_post_bookings", {"train": "1A23"}, approval=True))           # proposed (R32)
        request = o.request_for(o.rows()[0], args)
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual((request.get_method(), url.path), ("GET", "/api/stations/LDS/departures"))
        self.assertEqual(urllib.parse.parse_qs(url.query), {"lines": ["A,B"], "limit": ["3"]})           # explode: false
        booking = o.request_for(next(r for r in o.rows() if r["method"] == "POST"), {"train": "1A23", "seats": 2})
        self.assertEqual((booking.get_method(), json.loads(booking.data)), ("POST", {"train": "1A23", "seats": 2}))
        agent.flows.COMPONENTS["api_tools"].config.model_validate({"tools": ["rail_listdepartures"]})   # usable in a flow
        with self.assertRaises(ValueError):
            agent.flows.COMPONENTS["api_tools"].config.model_validate({"tools": ["no_such_tool"]})
        self.assertIn("rail_listdepartures",
                      agent.flows.COMPONENTS["api_tools"].describe()["config_schema"]["properties"]["tools"]["items"]["enum"])
        self.assertTrue(o.remove("rail_listdepartures"))
        self.assertNotIn("rail_listdepartures", api_tools.all_tools())

    def test_secrets_are_sealed_and_sent_only_in_the_request(self):
        with self.assertRaises(o.OpenApiImportError):                              # no INTEGRATIONS_KEY
            o.import_operations("", ["listdepartures"], "rail", "https://rail.example/api",
                                {"type": "api_key", "in": "header", "name": "X-Key", "value": "s3cret"}, text=SPEC)
        with patch.dict("os.environ", {"INTEGRATIONS_KEY": Fernet.generate_key().decode()}):
            [made] = o.import_operations("", ["listdepartures"], "rail", "https://rail.example/api",
                                         {"type": "api_key", "in": "query", "name": "key", "value": "s3cret"}, text=SPEC)
            self.assertTrue(made["has_secret"])
            row = o.rows()[0]
            self.assertNotIn("s3cret", json.dumps(made) + json.dumps(row["auth"]) + (row["secret"] or ""))
            request = o.request_for(row, {"station": "LDS"})
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["key"], ["s3cret"])

    def test_a_call_reads_json(self):
        o.import_operations("", ["listdepartures"], "rail", "https://rail.example/api", text=SPEC)

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b'{"departures": [{"to": "York", "at": "10:02"}]}'
        with patch("outbound.urlopen", return_value=Reply()):
            result = api_tools.run("rail_listdepartures", {"station": "LDS"})
        self.assertEqual((result["ok"], result["summary"]["departures"][0]["to"]), (True, "York"))
        self.assertEqual(result["display"]["attribution"], "rail.example")


if __name__ == "__main__":
    import unittest
    unittest.main()

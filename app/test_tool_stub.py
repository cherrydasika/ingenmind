"""A draft API tool from a data-source report (R40, tool_stub.py)."""

import types
import uuid

import identity
import source_profiles
import tool_stub
from test_auth import AuthDatabase

REPORT = {
    "report_id": "r_1",
    "recommendation": {"method": "api_tool", "recommended": "Realtime Trains", "integration": {
        "tool_name": "realtime_trains", "description": "Live departures for a GB station.",
        "inputs": "station (CRS code), rows (int), arrivals (bool), date (YYYY-MM-DD, optional)",
        "endpoint": "https://api.rtt.io/api/v1/json/search", "notes": "Free account needed."}},
    "profiles": [{"name": "Realtime Trains", "provider": "swlines", "auth": "api_key", "format": "JSON",
                  "pricing": "free", "rate_limits": "unknown", "licence_and_terms": "Non-commercial",
                  "docs_url": "https://api.rtt.io", "spec_url": "https://api.rtt.io/openapi.json"}],
}


class Drafts(AuthDatabase):
    def test_inputs_become_typed_parameters(self):
        self.assertEqual(tool_stub.parameters("station (CRS code), rows (int), arrivals (bool), date (YYYY-MM-DD, optional)"),
                         [{"name": "station", "type": "string", "description": "CRS code"},
                          {"name": "rows", "type": "integer", "description": ""},
                          {"name": "arrivals", "type": "boolean", "description": ""},
                          {"name": "date", "type": "string", "description": "YYYY-MM-DD, optional"}])

    def test_a_report_recommending_an_api_tool_drafts_a_working_module(self):
        draft = tool_stub.draft(REPORT)
        self.assertEqual((draft["filename"], draft["tool_name"]), ("app/api_tools/realtime_trains.py", "realtime_trains"))
        self.assertIn("DRAFT generated from a data-source report", draft["code"])
        self.assertIn("REALTIME_TRAINS_API_KEY", draft["code"])                       # the key from the environment
        self.assertIn("Admin → Integrations can import it without code", " ".join(draft["notes"]))
        module = types.ModuleType("api_tools.realtime_trains")
        module.__package__ = "api_tools"
        exec(compile(draft["code"], "realtime_trains.py", "exec"), module.__dict__)   # valid, and builds an ApiTool
        tool = module.TOOL
        self.assertEqual((tool.name, tool.required), ("realtime_trains", ["station", "rows", "arrivals", "date"]))
        self.assertEqual(tool.properties["rows"]["type"], "integer")
        self.assertFalse(tool.run({"station": "LDS"})["ok"])                       # no key set: refused, no call

    def test_only_api_tool_reports_and_prose_endpoints_stay_unknown(self):
        with self.assertRaises(tool_stub.StubError):
            tool_stub.draft({**REPORT, "recommendation": {"method": "feed_consumer"}})
        vague = {**REPORT, "recommendation": {**REPORT["recommendation"], "integration": {
            **REPORT["recommendation"]["integration"], "endpoint": "the Darwin feed"}}}
        draft = tool_stub.draft(vague)
        self.assertIn("ENDPOINT = ''", draft["code"])
        self.assertIn("The endpoint isn't known yet", " ".join(draft["notes"]))

    def test_the_admin_action(self):
        source_profiles._schema_ready = False
        self.addCleanup(setattr, source_profiles, "_schema_ready", False)
        report_id = source_profiles.save_report("live departures", [], "brief", REPORT["profiles"], REPORT["recommendation"])
        admin = identity.create_user("Ann", f"ann-{uuid.uuid4().hex[:6]}@example.com", "admin")
        reader = identity.create_user("Rae", f"rae-{uuid.uuid4().hex[:6]}@example.com", "viewer")
        reply = self.signed_in(admin).get(f"/api/source-reports/{report_id}/tool-stub")
        self.assertEqual((reply.status_code, reply.json()["tool_name"]), (200, "realtime_trains"))
        self.assertEqual(self.signed_in(reader).get(f"/api/source-reports/{report_id}/tool-stub").status_code, 403)
        self.assertEqual(self.signed_in(admin).get("/api/source-reports/r_none/tool-stub").status_code, 404)


if __name__ == "__main__":
    import unittest
    unittest.main()

"""MCP (R38, mcp_integration.py): tools from MCP servers in a flow, and IngenMind as an MCP server."""

import json
import threading
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet

import agent
import api_keys
import api_tools
import identity
import mcp_integration as m
import web_api
from test_auth import AuthDatabase

TOOLS = [{"name": "ask_wiki_question", "description": "Ask about a repo",
          "inputSchema": {"type": "object", "required": ["repoName", "question"], "properties": {
              "repoName": {"type": "string"}, "question": {"type": "string"}}}},
         {"name": "read_wiki_contents", "description": "Read it", "inputSchema": {"type": "object", "properties": {}}}]


class Protocol(AuthDatabase):
    def test_replies_come_as_json_or_server_sent_events(self):
        self.assertEqual(m._parse(b'{"jsonrpc":"2.0","id":3,"result":{"ok":1}}', "application/json", 3)["result"], {"ok": 1})
        stream = b'event: message\r\ndata: {"jsonrpc":"2.0","method":"note"}\r\n\r\ndata: {"jsonrpc":"2.0","id":2,"result":{"x":1}}\r\n'
        self.assertEqual(m._parse(stream, "text/event-stream", 2)["result"], {"x": 1})
        with self.assertRaises(m.McpError):
            m._parse(b"data: {}\n", "text/event-stream", 9)
        with self.assertRaises(m.McpError):
            m.Session("http://evil.example/mcp")                               # https, or http to localhost

    def test_a_call_reads_text_and_errors(self):
        class FakeSession:
            def __init__(self, url, headers, timeout):
                self.url = url

            def request(self, method, params):
                if params["name"] == "boom":
                    return {"content": [{"type": "text", "text": "No such repo"}], "isError": True}
                return {"content": [{"type": "text", "text": "A checkpointer saves state."},
                                    {"type": "image", "data": "..."}]}
        with patch.object(m, "Session", FakeSession):
            ok = m.call({"url": "https://x/mcp", "headers": None}, "ask", {})
            failed = m.call({"url": "https://x/mcp", "headers": None}, "boom", {})
        self.assertEqual((ok["ok"], ok["text"]), (True, "A checkpointer saves state."))
        self.assertEqual((failed["ok"], failed["error"]), (False, "No such repo"))


class Registry(AuthDatabase):
    def setUp(self):
        m._schema_ready = False
        self.addCleanup(setattr, m, "_schema_ready", False)
        for server in m.servers():          # one database per class: each test starts empty
            m.remove(server["server_id"])

    def test_add_enable_refresh_remove_and_secrets_stay_sealed(self):
        with patch.object(m, "list_tools", return_value=TOOLS):
            server = m.add("deepwiki", "DeepWiki", "https://mcp.deepwiki.com/mcp")
            self.assertEqual(([t["name"] for t in server["tools"]], server["enabled"]),
                             (["ask_wiki_question", "read_wiki_contents"], []))   # nothing enabled until chosen
            with self.assertRaises(m.McpError):
                m.add("deepwiki", "Again", "https://x/mcp")
            with self.assertRaises(m.McpError):
                m.add("tokened", "T", "https://x/mcp", {"Authorization": "Bearer s"})   # no INTEGRATIONS_KEY
            self.assertEqual(m.enable("deepwiki", ["ask_wiki_question"])["enabled"], ["ask_wiki_question"])
            with self.assertRaises(m.McpError):
                m.enable("deepwiki", ["rm_rf"])
            with patch.dict("os.environ", {"INTEGRATIONS_KEY": Fernet.generate_key().decode()}):
                sealed = m.add("tokened", "T", "https://x/mcp", {"Authorization": "Bearer s3cret"})
                self.assertTrue(sealed["has_headers"])
                self.assertNotIn("s3cret", json.dumps(m.servers()))
                stored = m.get("tokened")["headers"]
                self.assertNotIn("s3cret", stored)
                self.assertEqual(m._open(stored), {"Authorization": "Bearer s3cret"})
        with patch.object(m, "list_tools", return_value=TOOLS[1:]):              # a tool went away
            self.assertEqual(m.refresh("deepwiki")["enabled"], [])
        self.assertTrue(m.remove("deepwiki"))

    def test_an_agent_is_offered_enabled_tools_and_calls_pass_the_guardrails(self):
        with patch.object(m, "list_tools", return_value=TOOLS):
            m.add("deepwiki", "DeepWiki", "https://mcp.deepwiki.com/mcp")
        m.enable("deepwiki", ["ask_wiki_question"])
        offered = m.offered([{"server": "deepwiki", "tools": [], "max_calls": 1}])
        self.assertEqual(list(offered), ["mcp__deepwiki__ask_wiki_question"])
        entry = offered["mcp__deepwiki__ask_wiki_question"]
        self.assertEqual(entry["spec"]["config"]["inlineFunction"]["inputSchema"]["required"], ["repoName", "question"])
        self.assertIsNone(m.check(entry, {"repoName": "langchain-ai/langgraph", "question": "What is a checkpointer?"}, 0))
        for args, why in (({"repoName": "x"}, "missing"), ({"repoName": "x", "question": "q", "extra": 1}, "unknown"),
                          ({"repoName": "x", "question": "mail jo@example.com"}, "an email address"),
                          ({"repoName": "x", "question": "see https://evil.example"}, "a link")):
            with self.subTest(why=why):
                self.assertIn(why, m.check(entry, args, 0))
        self.assertIn("already called", m.check(entry, {"repoName": "x", "question": "q"}, 1))
        self.assertEqual(m.offered([{"server": "deepwiki", "tools": ["read_wiki_contents"], "max_calls": 1}]), {})

    def test_a_flow_agent_calls_an_mcp_tool(self):
        with patch.object(m, "list_tools", return_value=TOOLS):
            m.add("deepwiki", "DeepWiki", "https://mcp.deepwiki.com/mcp")
        m.enable("deepwiki", ["ask_wiki_question"])
        spec = agent.flows.FlowSpec.model_validate({
            "id": "f", "name": "F", "version": 1, "state": "main", "subflows": {},
            "nodes": [{"id": "start", "type": "start"}, {"id": "ig", "type": "input_guardrail"},
                      {"id": "assistant", "type": "agent"}, {"id": "dw", "type": "mcp_tool", "config": {"server": "deepwiki"}},
                      {"id": "og", "type": "output_guardrail"}, {"id": "end", "type": "end"}],
            "edges": [{"source": "start", "target": "ig"}, {"source": "ig", "target": "assistant", "outcome": "ALLOW"},
                      {"source": "ig", "target": "end", "outcome": "BLOCK"}, {"source": "assistant", "target": "og"},
                      {"source": "og", "target": "end", "outcome": "ALLOW"}, {"source": "og", "target": "end", "outcome": "BLOCK"},
                      {"source": "dw", "target": "assistant", "kind": "resource"}]})
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        tree = agent.agent_tree.tree(spec)
        self.assertIn("mcp__deepwiki__ask_wiki_question", [t["name"] for t in agent.agent_tree.tools(tree["assistant"], tree, agent.ROLES)])
        run = agent._Run.__new__(agent._Run)
        run.lock, run.calls, run.tree, run.emit = threading.Lock(), [], tree, (lambda e: None)
        run.agent_runs, run.settings = [{"n": 1, "node": "assistant", "parent": None}], {}
        use = {"name": "mcp__deepwiki__ask_wiki_question", "turn": 1, "toolUseId": "t",
               "input": json.dumps({"repoName": "langchain-ai/langgraph", "question": "What is a checkpointer?"})}
        found = {"ok": True, "text": "It saves the graph's state.", "error": None, "seconds": 0.4}
        with patch.object(m, "call", return_value=found) as call:
            out = run._guarded({"offered": {use["name"]}}, "agent:assistant", run._generic_tool)(use, 1)
        self.assertEqual(call.call_args.args[1:], ("ask_wiki_question", {"repoName": "langchain-ai/langgraph",
                                                                         "question": "What is a checkpointer?"}))
        text = out["toolResult"]["content"][0]["text"]
        self.assertIn("QUOTED DATA", text)                                       # untrusted
        self.assertIn("name DeepWiki as the source", text)
        self.assertEqual((run.calls[0]["tool"], run.calls[0]["ok"]), ("mcp__deepwiki__ask_wiki_question", True))
        refused = dict(use, input=json.dumps({"repoName": "x", "question": "email jo@example.com"}))
        with patch.object(m, "call") as call:
            out = run._guarded({"offered": {use["name"]}}, "agent:assistant", run._generic_tool)(refused, 1)
        call.assert_not_called()
        self.assertIn("Refused by the tool guardrail", out["toolResult"]["content"][0]["text"])


class Server(AuthDatabase):
    def setUp(self):
        api_keys._schema_ready = False
        self.addCleanup(setattr, api_keys, "_schema_ready", False)
        user = identity.create_user("Mo", f"mo-{uuid.uuid4().hex[:6]}@example.com", "user")
        key, _ = api_keys.create(user, "Claude Desktop", ["ask"], 1)
        self.headers = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}

    def post(self, body):
        return self.client().post("/api/v1/mcp", headers=self.headers, json=body)

    def test_initialize_list_and_search(self):
        init = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": m.PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "IngenMind")
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        tools = [t["name"] for t in self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]]
        self.assertEqual(tools, ["search_knowledge_base", "ask"])
        found = {"rankings": {"fused": [{"source_url": "https://rail.example/bikes", "text": "Bikes go free."}]}}
        with patch("retrieval.hybrid_search", return_value=found) as search, \
                patch.object(web_api.knowledge_collections, "scope", return_value=["main"]):
            reply = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "search_knowledge_base", "arguments": {"query": "bikes"}}}).json()
        self.assertEqual(search.call_args.kwargs["filters"], {"collection": ["main"]})   # the owner's collections
        self.assertIn("[1] https://rail.example/bikes", reply["result"]["content"][0]["text"])
        unknown = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "rm"}}).json()
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertEqual(self.client().post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"}).status_code, 401)

    def test_argument_types_may_be_lists(self):
        self.assertIsNone(api_tools.check_arguments({"q": {"type": ["string", "null"]}}, [], {"q": "trains"}))


if __name__ == "__main__":
    import unittest
    unittest.main()

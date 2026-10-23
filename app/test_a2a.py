"""Agent-to-agent (R36, a2a.py): the live flow as an A2A agent, and remote A2A agents as sub-agents."""

import json
import threading
import uuid
from unittest.mock import AsyncMock, patch

import a2a
import agent
import api_keys
import contracts
import identity
import web_api
from test_auth import AuthDatabase

ANSWER = {"answer": "Bikes go free [1].", "sources": [{"url": "https://rail.example/bikes", "cited": [1]}],
          "answer_check": {"passed": True}, "status": "done", "run_id": "run-1", "flow": {"id": "f", "version": 1}}


def rpc(method, params, rpc_id="1"):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def user_message(text, **more):
    return {"kind": "message", "role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": text}], **more}


class Server(AuthDatabase):
    def setUp(self):
        api_keys._schema_ready = False
        self.addCleanup(setattr, api_keys, "_schema_ready", False)
        self.user = identity.create_user("Ola", f"ola-{uuid.uuid4().hex[:6]}@example.com", "user")
        self.key, _ = api_keys.create(self.user, "partner agent", ["ask"], 1)
        self.headers = {"Authorization": f"Bearer {self.key}"}

    def test_the_agent_card_is_public(self):
        card = self.client().get("/.well-known/agent-card.json").json()
        self.assertEqual((card["protocolVersion"], card["preferredTransport"]), (a2a.PROTOCOL, "JSONRPC"))
        self.assertTrue(card["url"].endswith("/api/v1/a2a"))
        self.assertEqual(card["securitySchemes"]["apiKey"]["scheme"], "bearer")

    def test_message_send_answers_with_a_cited_task_and_links_traces(self):
        client = self.client()
        with patch.object(web_api, "_not_set_up", AsyncMock(return_value=None)), \
                patch.object(web_api, "_ask_live", return_value={**ANSWER, "trace_id": "their-trace"}) as ask:
            reply = client.post("/api/v1/a2a", headers=self.headers, json=rpc("message/send", {
                "message": user_message("Bikes?", metadata={a2a.TRACE_KEY: "caller-trace"})})).json()
        task = reply["result"]
        self.assertEqual((reply["id"], task["kind"], task["status"]["state"]), ("1", "task", "completed"))
        text, data = task["artifacts"][0]["parts"]
        self.assertEqual(text, {"kind": "text", "text": "Bikes go free [1]."})
        self.assertEqual(data["data"]["sources"][0]["cited"], [1])
        self.assertEqual(task["metadata"][a2a.TRACE_KEY], "their-trace")
        body = ask.call_args.args[2]
        self.assertEqual((body["user_id"], body["trace_metadata"]), (self.user["user_id"], {"a2a_caller_trace": "caller-trace"}))
        got = client.post("/api/v1/a2a", headers=self.headers, json=rpc("tasks/get", {"id": task["id"]})).json()
        self.assertEqual(got["result"]["id"], task["id"])

    def test_errors_follow_json_rpc(self):
        client = self.client()
        cases = [(rpc("tasks/get", {"id": "nope"}), a2a.TASK_NOT_FOUND), (rpc("dance", {}), a2a.METHOD_NOT_FOUND),
                 ({"id": "1", "method": "message/send"}, a2a.INVALID_REQUEST),
                 (rpc("message/send", {"message": {"role": "agent", "parts": []}}), a2a.INVALID_PARAMS)]
        for body, code in cases:
            with self.subTest(code=code):
                self.assertEqual(client.post("/api/v1/a2a", headers=self.headers, json=body).json()["error"]["code"], code)
        self.assertEqual(client.post("/api/v1/a2a", json=rpc("tasks/get", {"id": "x"})).status_code, 401)   # no key

    def test_a_task_is_only_its_owners(self):
        a2a.TASKS.put("someone_else", {"id": "t-9", "kind": "task"})
        reply = self.client().post("/api/v1/a2a", headers=self.headers, json=rpc("tasks/get", {"id": "t-9"})).json()
        self.assertEqual(reply["error"]["code"], a2a.TASK_NOT_FOUND)


class Client(AuthDatabase):
    def test_send_reads_a_task_a_message_and_an_error(self):
        sent = []

        def post(url, body, headers, timeout):
            sent.append((url, body, headers, timeout))
            return {"jsonrpc": "2.0", "id": body["id"], "result": a2a.task(ANSWER, "ctx", body["params"]["message"], "t2")}
        out = a2a.send("https://agent.example/a2a", "Bikes?", token="tok", timeout=9, trace_id="t1", post=post)
        self.assertEqual((out["text"], out["state"], out["trace_id"]), ("Bikes go free [1].", "completed", "t2"))
        self.assertEqual(out["data"][0]["sources"][0]["url"], "https://rail.example/bikes")
        url, body, headers, timeout = sent[0]
        self.assertEqual((body["method"], headers, timeout), ("message/send", {"Authorization": "Bearer tok"}, 9))
        self.assertEqual(body["params"]["message"]["metadata"], {a2a.TRACE_KEY: "t1"})
        message = a2a.send("u", "x", post=lambda *a: {"result": {"kind": "message", "parts": [{"kind": "text", "text": "Hi"}]}})
        self.assertEqual((message["text"], message["state"]), ("Hi", "completed"))
        with self.assertRaises(a2a.A2AError):
            a2a.send("u", "x", post=lambda *a: {"error": {"code": -32001, "message": "no"}})


def _run(remote_config, budget=None):
    spec = agent.flows.FlowSpec.model_validate({
        "id": "r", "name": "R", "version": 1, "state": "main", "subflows": {},
        "nodes": [{"id": "start", "type": "start"}, {"id": "ig", "type": "input_guardrail"},
                  {"id": "assistant", "type": "agent"}, {"id": "remote", "type": "remote_agent", "label": "Rail elsewhere",
                                                         "config": remote_config},
                  {"id": "og", "type": "output_guardrail"}, {"id": "end", "type": "end"}],
        "edges": [{"source": "start", "target": "ig"}, {"source": "ig", "target": "assistant", "outcome": "ALLOW"},
                  {"source": "ig", "target": "end", "outcome": "BLOCK"}, {"source": "assistant", "target": "og"},
                  {"source": "og", "target": "end", "outcome": "ALLOW"}, {"source": "og", "target": "end", "outcome": "BLOCK"},
                  {"source": "remote", "target": "assistant", "kind": "resource", "port": "agents"}]})
    run = agent._Run.__new__(agent._Run)
    run.lock, run.calls, run.contracts, run.agent_runs, run.runs = threading.Lock(), [], [], [], []
    run.tree, run.emit, run.trace_id = agent.agent_tree.tree(spec), (lambda e: None), "our-trace"
    run.settings = {"budget": budget} if budget else {}
    run.started = 0
    return run, spec


class RemoteSubAgent(AuthDatabase):
    CONFIG = {"url": "https://agent.example/a2a", "token_env": "PARTNER_TOKEN", "max_calls": 1,
              "instructions": "Answers UK rail questions elsewhere."}

    def test_a_remote_agent_is_a_sub_agent_under_the_contract(self):
        run, spec = _run(self.CONFIG)
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        self.assertEqual((run.tree["remote"].remote, run.tree["assistant"].subagents), (True, ["remote"]))
        reply = {"text": "Bikes go free [1], call jo@example.com.", "data": [{"sources": [{"url": "https://rail.example"}]}],
                 "state": "completed", "trace_id": "their-trace", "context_id": "c"}
        task = contracts.Task(goal="Are bikes free? Ask jo@example.com")
        with patch.object(agent.a2a, "send", return_value=reply) as send, \
                patch.dict("os.environ", {"PARTNER_TOKEN": "secret"}):
            result = run.delegate("remote", task, 1)
        url, text = send.call_args.args
        self.assertNotIn("jo@example.com", text)                                  # masked before it leaves
        self.assertEqual((send.call_args.kwargs["token"], send.call_args.kwargs["trace_id"]), ("secret", "our-trace"))
        self.assertEqual(result.status, "done")
        self.assertIn("QUOTED DATA", result.findings)                             # fenced: untrusted
        self.assertNotIn("[1]", result.findings)                                  # its numbers aren't ours
        self.assertIn("https://rail.example", result.findings)
        [call] = run.calls
        self.assertEqual((call["tool"], call["ok"], call["remote_trace_id"]), ("a2a:remote", True, "their-trace"))
        again = run.delegate("remote", task, 1)                                   # max_calls 1
        self.assertEqual((again.status, "1 times" in again.error), ("failed", True))

    def test_failures_and_budgets_are_failed_tasks_not_crashes(self):
        run, _ = _run(self.CONFIG)
        with patch.object(agent.a2a, "send", side_effect=TimeoutError("slow")):
            result = run.delegate("remote", contracts.Task(goal="Bikes?"), 1)
        self.assertEqual(result.status, "failed")
        self.assertIn("could not be reached", result.error)
        run, _ = _run({**self.CONFIG, "url": ""})
        self.assertIn("no A2A URL", run.delegate("remote", contracts.Task(goal="Bikes?"), 1).error)
        run, _ = _run(self.CONFIG, budget={"max_tool_calls": 1})
        run.tool_calls = 1
        with patch.object(agent.a2a, "send") as send:
            self.assertIn("budget", run.delegate("remote", contracts.Task(goal="Bikes?"), 1).error)
        send.assert_not_called()

    def test_only_https_urls_or_localhost(self):
        config = agent.flows.COMPONENTS["remote_agent"].config
        for url in ("http://evil.example/a2a", "ftp://x/a2a", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config.model_validate({**self.CONFIG, "url": url})
        config.model_validate({**self.CONFIG, "url": "http://localhost:8091/api/v1/a2a"})


if __name__ == "__main__":
    import unittest
    unittest.main()

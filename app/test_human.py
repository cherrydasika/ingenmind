"""Ask a human (R32, human.py): a run pauses for a person and resumes with their answer, after a restart too."""

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg

import agent
import api_tools
import durable
import human
import llm
import test_durable as td
from common import config

DRAFT = "Yes, every station is step-free [1]."


def flow(*modes, tools=False):
    """The starter flow with Ask a human steps: clarify before the agent, approve before the end."""
    spec = json.loads((Path(agent.flows.__file__).parent / "starter_flow.json").read_text())
    edges = spec["edges"]
    if "clarify" in modes:
        spec["nodes"].append({"id": "clarify", "type": "human", "config": {"mode": "clarify"}})
        edges.remove({"source": "input_guardrail", "target": "agent", "outcome": "ALLOW"})
        edges += [{"source": "input_guardrail", "target": "clarify", "outcome": "ALLOW"},
                  {"source": "clarify", "target": "agent"}]
    if "approve" in modes:
        spec["nodes"].append({"id": "approve", "type": "human", "config": {"mode": "approve", "timeout_minutes": 5}})
        edges.remove({"source": "answer_evaluator", "target": "end"})
        edges += [{"source": "answer_evaluator", "target": "approve"}, {"source": "approve", "target": "end"}]
    if tools:
        spec["nodes"].append({"id": "tools", "type": "api_tools", "config": {"tools": ["get_weather"]}})
        edges.append({"source": "tools", "target": "agent", "kind": "resource"})
    return agent.flows.FlowSpec.model_validate(spec)


def view(search):
    return {"chunks": search["rankings"]["fused"], "sources": []}


class AskAHuman(unittest.TestCase):
    drop, patches, run_with = td.DurableRuns.drop, td.DurableRuns.patches, td.DurableRuns.run_with
    passing = staticmethod(td.DurableRuns.passing)

    def setUp(self):
        td.DurableRuns.setUp(self)                 # a database of its own
        human._schema_ready = False
        self.addCleanup(setattr, human, "_schema_ready", False)

    def ask(self, spec, chat, source="users"):
        return self.run_with(chat, self.passing, lambda: agent.answer(
            "Is there step-free access?", f"s-{uuid.uuid4()}", "u-1", None, lambda e: None, view,
            flow=(spec, 1), source=source))

    def resume(self, run_id, answer, chat=None):
        return self.run_with(chat or td.ScriptedChat(), self.passing,
                             lambda: agent.resume_run(run_id, view, answer=answer))

    def restart(self):
        """What a restart leaves: the graph compiled again; waiting runs aren't interrupted ones."""
        agent._graphs.clear()
        self.assertEqual(durable.mark_interrupted(), [])

    def test_an_approval_pauses_the_run_and_resumes_after_a_restart(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        chat = td.ScriptedChat(td.uses_turn("search_knowledge_base"), td.answer_turn(DRAFT))
        paused = self.ask(flow("approve"), chat)
        self.assertEqual(paused["status"], "waiting")
        self.assertEqual(paused["waiting"]["kind"], "approve")
        self.assertNotIn("step-free", paused["answer"])                   # the user doesn't see it yet
        self.assertEqual(durable.get(paused["run_id"])["status"], "waiting")
        [request] = human.inbox()
        self.assertEqual((request["status"], request["draft"], request["user_id"]), ("open", DRAFT, "u-1"))
        self.restart()
        self.assertTrue(human.respond(request["request_id"], {"approved": True}, "admin-1"))
        self.assertIsNone(human.respond(request["request_id"], {"approved": False}, "admin-2"))   # once only
        resumed = self.resume(paused["run_id"], {"approved": True})        # no model turn runs again
        self.assertEqual(resumed["answer"], DRAFT)
        self.assertEqual(resumed["sources"], [{"url": "https://rail.example/access", "cited": [1]}])
        self.assertEqual(durable.get(paused["run_id"])["status"], "done")

    def test_a_proposed_action_runs_only_once_approved(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        tool = api_tools.TOOLS["get_weather"]
        done = {"ok": True, "cached": False, "error": None, "seconds": 0.1, "summary": {"booked": True},
                "display": {"headline": "Booked for 10:00"}}
        call = llm.Turn([{"toolUse": {"toolUseId": "t1", "name": "get_weather", "input": {"location": "York"}}}],
                        "tool_use", 10, 5)
        for approved, expected in ((True, "Booked for 10:00"), (False, "wasn't approved")):
            with self.subTest(approved=approved), patch.object(tool, "side_effects", True), \
                    patch.object(tool, "run", return_value=done) as run:
                chat = td.ScriptedChat(call, td.answer_turn("I've asked for it; it is waiting for approval."))
                paused = self.ask(flow("approve", tools=True), chat)
                self.assertIn("Proposed, not done", json.dumps(chat.calls[-1]))
                run.assert_not_called()                                    # proposed, not done
                request = next(r for r in human.inbox() if r["run_id"] == paused["run_id"])
                self.assertEqual([a["tool"] for a in request["actions"]], ["get_weather"])
                human.respond(request["request_id"], {"approved": approved}, "admin-1")
                resumed = self.resume(paused["run_id"], {"approved": approved})
                self.assertIn(expected, resumed["answer"])
                self.assertEqual(run.call_count, 1 if approved else 0)

    def test_without_an_approval_step_side_effects_are_refused(self):
        tool = api_tools.TOOLS["get_weather"]
        with patch.object(tool, "side_effects", True):
            self.assertIn("needs a person's approval", api_tools.check("get_weather", {"location": "York"}))
            self.assertIsNone(api_tools.check("get_weather", {"location": "York"}, approval=True))
        self.assertTrue(agent._flow_settings(flow("approve")).get("approval"))
        self.assertIsNone(agent._flow_settings(flow("clarify")).get("approval"))

    def test_an_ambiguous_question_is_clarified_by_the_user(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        chat = td.ScriptedChat(td.answer_turn(DRAFT))
        with patch.object(human, "needs_clarifying", return_value="Which station do you mean?") as decide:
            paused = self.ask(flow("clarify"), td.ScriptedChat())
            self.assertEqual((paused["status"], paused["answer"]), ("waiting", "Which station do you mean?"))
            self.restart()
            [waiting] = human.waiting_for(durable.get(paused["run_id"])["session_id"], "u-1")
            human.respond(waiting["request_id"], {"reply": "Kestrel Bay"}, "u-1")
            resumed = self.resume(paused["run_id"], {"reply": "Kestrel Bay"}, chat)
        self.assertEqual(decide.call_count, 1)                             # decided once, not again on resume
        self.assertEqual(resumed["answer"], DRAFT)
        self.assertIn("Kestrel Bay", json.dumps(chat.calls[0]))            # the agent got the reply

    def test_a_timeout_applies_the_safe_default(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        chat = td.ScriptedChat(td.uses_turn("search_knowledge_base"), td.answer_turn(DRAFT))
        paused = self.ask(flow("approve"), chat)
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=config.PGDATABASE) as connection:
            connection.execute("UPDATE human_requests SET expires_at = now() - interval '1 minute'")
        [expired] = human.expire_due()
        self.assertEqual(expired["run_id"], paused["run_id"])
        self.assertEqual(human.expire_due(), [])                           # claimed once
        resumed = self.resume(paused["run_id"], {"expired": True})
        self.assertIn("wasn't approved", resumed["answer"])

    def test_tests_and_evaluations_never_wait(self):
        chat = td.ScriptedChat(td.uses_turn("search_knowledge_base"), td.answer_turn(DRAFT))
        result = self.ask(flow("clarify", "approve"), chat, source="playground")
        self.assertEqual((result["status"], result["answer"]), ("done", DRAFT))
        self.assertEqual(human.inbox(), [])


if __name__ == "__main__":
    unittest.main()

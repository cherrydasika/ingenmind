"""Ask a human (R32, human.py): a run pauses for a person and resumes with their answer, after a restart too."""

import json
import threading
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


class ResearchedPages(unittest.TestCase):
    """#157: with approval on, a validated page waits for an approver; the run goes on without it."""
    drop = td.DurableRuns.drop

    def setUp(self):
        td.DurableRuns.setUp(self)
        human._schema_ready = False
        self.addCleanup(setattr, human, "_schema_ready", False)

    def ingest_step(self, settings, source="users"):
        class Run:
            run_id, session_id, user_id, question = "run-1", "s-1", "u-1", "Is Kestrel Bay step-free?"
            fetched = {"https://rail.example/kestrel": {"title": "Kestrel Bay station", "text": "Lifts to every platform."}}
            research, lock, settings = [{"delegation": 1}], threading.Lock(), {}
            emit = staticmethod(lambda event: None)
        Run.source = source
        state = {"task": {"n": 1, "task": "Step-free access at Kestrel Bay"}, "validation": {"sources": [
            {"url": "https://rail.example/kestrel", "accepted": True, "ttl_days": 30, "kind": "html",
             "publisher": "Rail Example", "date": "2026-10-01", "scores": {"authority": 0.9}},
            {"url": "https://rail.example/other", "accepted": False}]}}
        allowed = type("Policy", (), {"allowed": True})()
        with patch.object(agent.injection, "flags", return_value=False), \
                patch.object(agent.guardrails, "check", return_value=allowed), \
                patch.object(agent.storage, "get_client", return_value=object()), \
                patch.object(agent.labels, "blueprint_or_none", return_value=None), \
                patch.object(agent.ingest, "ingest_url", return_value={"status": "updated", "chunks": 3}) as ingest_url:
            result = agent._ingest_sources(state, {"configurable": {"run": Run(), "settings": settings}})
        return result, ingest_url

    def test_with_approval_on_the_page_waits_and_nothing_is_added(self):
        result, ingest_url = self.ingest_step({"approval": True, "approval_days": 3})
        self.assertNotIn("error", result)
        [held] = result["ingested"]
        self.assertEqual(held["status"], "awaiting_approval")
        ingest_url.assert_not_called()
        [request] = human.inbox()
        self.assertEqual((request["kind"], request["status"], request["question"]),
                         ("source", "open", "https://rail.example/kestrel"))
        self.assertEqual(request["actions"][0]["metadata"]["research_publisher"], "Rail Example")
        self.assertEqual(human.cancel_for("run-1"), 0)          # cancelling the run leaves the page waiting
        self.ingest_step({"approval": True})                     # the same run again: still one request
        self.assertEqual(len(human.inbox()), 1)

    def test_without_approval_the_page_is_added_as_before(self):
        result, ingest_url = self.ingest_step({})
        self.assertEqual([i["status"] for i in result["ingested"]], ["updated"])
        ingest_url.assert_called_once()
        self.assertEqual(human.inbox(), [])

    def test_evaluations_never_queue_pages(self):
        result, _ = self.ingest_step({"approval": True}, source="eval")
        self.assertEqual([i["status"] for i in result["ingested"]], ["not_ingested_evaluation"])
        self.assertEqual(human.inbox(), [])

    def test_an_approved_page_is_added_and_the_outcome_kept(self):
        import web_api
        self.ingest_step({"approval": True})
        [request] = human.inbox()
        decided = human.respond(request["request_id"], {"approved": True}, "u-admin")
        with patch.object(agent, "add_researched_page", return_value={"url": "x", "status": "updated", "chunks": 3}) as add:
            web_api._add_approved_page(decided)
        page = add.call_args.args[0]
        self.assertEqual((page["url"], page["ttl_days"], page["metadata"]["approved_by"]),
                         ("https://rail.example/kestrel", 30, "u-admin"))
        self.assertEqual(human.get(request["request_id"])["response"]["outcome"], {"status": "updated", "chunks": 3})

    def test_a_page_nobody_approves_expires_and_is_never_added(self):
        self.ingest_step({"approval": True})
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=config.PGDATABASE) as connection:
            connection.execute("UPDATE human_requests SET expires_at = now() - interval '1 minute'")
        [expired] = human.expire_due()
        self.assertEqual((expired["kind"], expired["status"]), ("source", "expired"))


if __name__ == "__main__":
    unittest.main()

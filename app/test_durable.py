"""Durable runs (R30, durable.py): checkpointed, recorded, resumed after a crash."""

import json
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

import agent
import agent_runtime
import answer_eval
import durable
import guardrails
import llm
from common import config


class ScriptedChat:
    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def __call__(self, system, messages, tools, max_tokens, temperature):
        self.calls.append(messages)
        return self.turns.pop(0)


def uses_turn(name):
    return llm.Turn([{"toolUse": {"toolUseId": "t0", "name": name, "input": {"query": "q"}}}], "tool_use", 10, 5)


def answer_turn(text):
    return llm.Turn([{"text": text}], "end_turn", 20, 8)


class DurableRuns(unittest.TestCase):
    def setUp(self):
        self.original_database = config.PGDATABASE
        self.database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        config.PGDATABASE = self.database
        agent_runtime._schema_ready = durable._ready = False
        self.addCleanup(self.drop)

    def drop(self):
        config.PGDATABASE = self.original_database
        agent_runtime._schema_ready = durable._ready = False
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database)))

    def starter(self):
        return agent.flows.FlowSpec.model_validate(
            json.loads((Path(agent.flows.__file__).parent / "starter_flow.json").read_text()))

    def patches(self, chat, evaluate, output_crash=False):
        found = {"rankings": {"fused": [{"source_url": "https://rail.example/access", "chunk_index": 0, "meta": {},
                                         "text": "Every station has step-free access to all platforms."}],
                              "dense": [], "sparse": []}}
        allow = guardrails.Verdict(decision="ALLOW", reason="in scope", stage="input")

        def check(stage, *args, **kwargs):
            if stage == "output" and output_crash:
                raise SystemError("the web app stopped")                  # not transient: no retry
            return allow
        return [patch.object(agent, "_local_harness", agent_runtime.LocalHarness(chat)),
                patch.object(guardrails, "check", side_effect=check),
                patch.object(agent, "hybrid_search", return_value=found),
                patch.object(agent.answer_eval, "evaluate", side_effect=evaluate),
                patch.object(agent.labels, "vocabulary", return_value=None),
                patch.object(agent, "_record_run"), patch.object(agent_runtime, "RUNTIME", "local")]

    @staticmethod
    def passing(question, draft, chunks, api_results, **kwargs):
        checks = answer_eval.AnswerChecks(cited=[1], invalid_citations=[], evidence_chunks=len(chunks),
                                          api_results=len(api_results), kb_evidence_uncited=False)
        return answer_eval.AnswerEvaluation(passed=True, overall=0.9, scores={}, checks=checks, failed_on=[])

    def run_with(self, chat, evaluate, call, output_crash=False):
        ps = self.patches(chat, evaluate, output_crash)
        for p in ps:
            p.start()
        try:
            return call()
        finally:
            for p in reversed(ps):
                p.stop()

    def ask(self, chat, evaluate, output_crash=False):
        return self.run_with(chat, evaluate, lambda: agent.answer(
            "Is there step-free access?", f"s-{uuid.uuid4()}", None, None, lambda e: None,
            lambda search: {"chunks": search["rankings"]["fused"], "sources": []}, flow=(self.starter(), 1)),
            output_crash)

    def test_a_finished_run_is_recorded_and_its_checkpoints_dropped(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        chat = ScriptedChat(uses_turn("search_knowledge_base"), answer_turn("Yes, every station is step-free [1]."))
        result = self.ask(chat, self.passing)
        record = durable.list_runs()[0]
        self.assertEqual((record["status"], record["answer"]), ("done", result["answer"]))
        self.assertGreaterEqual(record["steps"], 2)
        self.assertGreater(record["input_tokens"], 0)
        self.assertIsNone(durable.get(record["run_id"])["snapshot"])       # done: nothing left to resume
        self.assertIsNone(agent.CHECKPOINTER.get_tuple({"configurable": {"thread_id": record["run_id"]}}))

    def test_a_run_cut_short_resumes_from_its_last_step_without_redoing_it(self):
        if not durable.durable():
            self.skipTest("no PostgreSQL checkpointer")
        chat = ScriptedChat(uses_turn("search_knowledge_base"), answer_turn("Yes, every station is step-free [1]."))
        with self.assertRaises(SystemError):                               # the process died at the output check
            self.ask(chat, self.passing, output_crash=True)
        record = durable.list_runs()[0]
        self.assertEqual(record["status"], "failed")
        self.assertTrue(durable.resumable(record["run_id"]))
        # A restart: runs left running become interrupted (this one already failed).
        self.assertEqual(durable.mark_interrupted(), [])
        resumed = self.run_with(ScriptedChat(), self.passing,              # no model turn may run again
                                lambda: agent.resume_run(record["run_id"],
                                                         lambda s: {"chunks": s["rankings"]["fused"], "sources": []}))
        self.assertEqual(resumed["answer"], "Yes, every station is step-free [1].")
        self.assertEqual(resumed["sources"], [{"url": "https://rail.example/access", "cited": [1]}])   # restored
        after = durable.get(record["run_id"])
        self.assertEqual((after["status"], after["resumes"]), ("done", 1))
        with self.assertRaises(ValueError):
            agent.resume_run(record["run_id"], lambda s: s)                # done: not again

    def test_restart_recovery_and_cancel(self):
        durable.begin("r-1", source="users", session_id="s", user_id="u", flow_id="f", version=1, spec={},
                      question="q", asked="q", read_as=None)
        durable.begin("r-2", source="eval", session_id="", user_id=None, flow_id="f", version=1, spec={},
                      question="q", asked="q", read_as=None)
        resumed = []
        with patch.object(durable, "durable", return_value=True), patch.object(threading.Thread, "start",
                                                                              lambda self: self._target()):
            chosen = durable.recover(resumed.append)
        self.assertEqual(chosen, ["r-1"])                                  # only recent Ask-page runs resume themselves
        self.assertEqual(resumed, ["r-1"])
        self.assertEqual({r["run_id"]: r["status"] for r in durable.list_runs()}, {"r-1": "interrupted", "r-2": "interrupted"})
        self.assertTrue(durable.cancel("r-2"))
        self.assertFalse(durable.cancel("r-2"))
        self.assertEqual(durable.get("r-2")["status"], "cancelled")

    def test_snapshots_round_trip(self):
        class Run:
            lock = threading.Lock()
        run = Run()
        run.searches = [{"n": 1, "retrieval": {"chunks": [{"text": "t", "vector": [0.1] * 384}]}}]
        run.budgets = {2: {"searches": 1}}
        run.agents = {"knowledge_base"}
        state = durable.state_of(run)
        self.assertNotIn("vector", json.dumps(state))
        fresh = Run()
        durable.restore(fresh, state)
        self.assertEqual(fresh.budgets, {2: {"searches": 1}})
        self.assertEqual(fresh.searches[0]["retrieval"]["chunks"][0]["text"], "t")


if __name__ == "__main__":
    unittest.main()

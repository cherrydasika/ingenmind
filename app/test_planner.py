"""Planner–executor with a task graph (R31, planner.py, agent._Run.run_plan)."""

import threading
import time
import unittest
import uuid
from unittest.mock import patch

import agent
import agent_runtime
import contracts
import guardrails
import planner

TEAM = {"kb", "live"}


class Plans(unittest.TestCase):
    def test_a_plan_must_be_a_graph_of_known_agents(self):
        ok = planner.validate([{"id": "t1", "label": "Bike rule", "agent": "kb", "goal": "g", "depends_on": []},
                               {"id": "t2", "label": "Fare", "agent": "kb", "goal": "g", "depends_on": ["t1"]}], TEAM)
        self.assertEqual([s.id for s in planner.ready(ok, set())], ["t1"])
        ok[0].status = "done"
        self.assertEqual([s.id for s in planner.ready(ok, {"t1"})], ["t2"])
        bad = [[{"id": "t1", "agent": "nobody", "goal": "g"}],
               [{"id": "t1", "agent": "kb", "goal": "g", "depends_on": ["t9"]}],
               [{"id": "t1", "agent": "kb", "goal": "g", "depends_on": ["t2"]},
                {"id": "t2", "agent": "kb", "goal": "g", "depends_on": ["t1"]}],
               [{"id": "t1", "agent": "kb", "goal": "g"}, {"id": "t1", "agent": "kb", "goal": "g"}],
               [{"id": "t1", "agent": "kb", "goal": ""}], []]
        for raw in bad:
            with self.assertRaises(planner.PlanError, msg=raw):
                planner.validate(raw, TEAM)
        # A re-plan may wait for tasks that already finished, but not reuse their ids.
        planner.validate([{"id": "t3", "agent": "kb", "goal": "g", "depends_on": ["t1"]}], TEAM, taken={"t1"})
        with self.assertRaises(planner.PlanError):
            planner.validate([{"id": "t1", "agent": "kb", "goal": "g"}], TEAM, taken={"t1"})


SPEC = {"id": "planned", "name": "Planned", "version": 1, "state": "main", "subflows": {},
        "nodes": [{"id": "start", "type": "start"}, {"id": "ig", "type": "input_guardrail"},
                  {"id": "plan", "type": "planner", "label": "Planner", "config": {"parallel": 3}},
                  {"id": "kb", "type": "agent", "label": "Rail facts"},
                  {"id": "live", "type": "agent", "label": "Live data"},
                  {"id": "og", "type": "output_guardrail"}, {"id": "end", "type": "end"}],
        "edges": [{"source": "start", "target": "ig"}, {"source": "ig", "target": "plan", "outcome": "ALLOW"},
                  {"source": "ig", "target": "end", "outcome": "BLOCK"}, {"source": "plan", "target": "og"},
                  {"source": "og", "target": "end", "outcome": "ALLOW"}, {"source": "og", "target": "end", "outcome": "BLOCK"},
                  {"source": "kb", "target": "plan", "kind": "resource", "port": "agents"},
                  {"source": "live", "target": "plan", "kind": "resource", "port": "agents"}]}


def step(id_, agent_, deps=(), label=None):
    return planner.Step(id_, label or id_, agent_, f"goal of {id_}", list(deps))


class Executor(unittest.TestCase):
    def run_flow(self, plans, outcomes):
        spec = agent.flows.FlowSpec.model_validate(SPEC)
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        windows, inputs, events, asked = {}, {}, [], []

        def delegate(run, child, task, parent):
            name = task.goal.split()[-1]
            began = time.monotonic()
            time.sleep(0.2)
            windows[name] = (began, time.monotonic())
            inputs[name] = dict(task.inputs)
            status, findings = outcomes[name]
            return contracts.Result(status=status, findings=findings)

        def make_plan(question, team, max_tasks=6, done=None, failed=None):
            asked.append({"done": done, "failed": failed})
            return plans.pop(0), {"input_tokens": 10, "output_tokens": 5}
        allow = guardrails.Verdict(decision="ALLOW", reason="ok", stage="input")
        with patch.object(agent._Run, "delegate", delegate), patch.object(planner, "make_plan", side_effect=make_plan), \
                patch.object(planner, "compose", return_value=("Bikes go free [1]; it will rain.", {"input_tokens": 5})), \
                patch.object(guardrails, "check", return_value=allow), patch.object(agent, "_record_run"), \
                patch.object(agent.labels, "vocabulary", return_value=None), patch.object(agent_runtime, "RUNTIME", "local"):
            result = agent.answer("Bike, fare and weather?", f"s-{uuid.uuid4()}", None, None,
                                  lambda e: events.append(e) if e.get("type") == "plan" else None,
                                  lambda s: s, flow=(spec, 1))
        return result, windows, inputs, events, asked

    def test_independent_tasks_run_together_and_results_flow_on(self):
        plan = [step("t1", "kb", label="Bike rule"), step("t2", "live", label="Weather"), step("t3", "kb", ["t1"], "Fare")]
        result, windows, inputs, events, _ = self.run_flow(
            [plan], {"t1": ("done", "Bikes go free [1]."), "t2": ("done", "Rain."), "t3": ("done", "£5 [2].")})
        self.assertEqual(result["answer"], "Bikes go free [1]; it will rain.")
        (a1, b1), (a2, b2), (a3, _) = windows["t1"], windows["t2"], windows["t3"]
        self.assertLess(max(a1, a2), min(b1, b2))                       # t1 and t2 overlapped
        self.assertGreaterEqual(a3, b1)                                  # t3 waited for t1
        self.assertEqual(inputs["t3"], {"Bike rule": "Bikes go free [1]."})
        self.assertEqual([t["status"] for t in result["plan"]], ["done", "done", "done"])
        statuses = [[t["status"] for t in e["tasks"]] for e in events]
        self.assertIn(["running", "running", "waiting"], statuses)       # shown live
        self.assertEqual(agent.live_shape(agent.flows.FlowSpec.model_validate(SPEC), 1)["kind"], "agent")

    def test_a_failed_task_is_planned_again_once(self):
        first = [step("t1", "kb", label="Fare"), step("t2", "live", label="Weather"), step("t3", "kb", ["t1"], "Compare")]
        second = [step("t4", "kb", label="Fare, another way")]
        result, _, _, events, asked = self.run_flow(
            [first, second], {"t1": ("failed", ""), "t2": ("done", "Rain."), "t4": ("done", "£5 [1].")})
        self.assertEqual(asked[1]["failed"][0]["id"], "t1")
        self.assertEqual([d["id"] for d in asked[1]["done"]], ["t2"])
        self.assertEqual({t["id"]: t["status"] for t in result["plan"]}, {"t1": "failed", "t2": "done", "t4": "done"})
        self.assertTrue(any(e.get("replanned") for e in events))


if __name__ == "__main__":
    unittest.main()

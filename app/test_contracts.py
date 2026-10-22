"""The typed contract between agents and sub-agents (R29, contracts.py)."""

import unittest

from pydantic import ValidationError

import contracts


class TaskContract(unittest.TestCase):
    def test_a_call_validates_and_reads_the_old_single_field(self):
        task = contracts.Task.from_call({"task": " Step-free access at Kestrel Bay? ", "unknown": 1})
        self.assertEqual(task.goal, "Step-free access at Kestrel Bay?")
        full = contracts.Task.from_call({"goal": "Fares", "inputs": {"from": "Port Avalon"},
                                         "constraints": ["Lakeshore Rail only"], "expected_output": "the fare",
                                         "max_turns": 3, "deadline_seconds": 30})
        text = full.prompt()
        self.assertIn("- from: Port Avalon", text)
        self.assertIn("- Lakeshore Rail only", text)
        self.assertIn("Report: the fare", text)
        for bad in ({}, {"goal": "  "}, {"goal": "x", "max_turns": 99}, {"goal": "x", "deadline_seconds": 1},
                    {"goal": "x", "inputs": "not a dict"}):
            with self.assertRaises(ValidationError, msg=bad):
                contracts.Task.from_call(bad)

    def test_the_tool_schema_matches_the_model(self):
        schema = contracts.task_schema()
        self.assertEqual(set(schema["properties"]), set(contracts.Task.model_fields))
        self.assertEqual(schema["required"], ["goal"])
        described = contracts.describe()
        self.assertEqual([f["name"] for f in described["task"]], list(schema["properties"]))
        self.assertEqual({f["name"] for f in described["result"]} - {"cost"},
                         set(contracts.Result.model_fields) - {"cost", "failed_check", "error"})


class ResultContract(unittest.TestCase):
    def test_statuses(self):
        done = contracts.result_of("Every station is step-free [1][3].", checks=[{"overall": 0.9}],
                                   cost={"seconds": 2.5, "turns": 2, "input_tokens": 100, "output_tokens": 20})
        self.assertEqual((done.status, done.citations, done.confidence, done.cost.turns), ("done", [1, 3], 0.9, 2))
        partial = contracts.result_of("Fares are £5 [2].\nMissing: the off-peak fare.")
        self.assertEqual((partial.status, partial.findings, partial.missing),
                         ("partial", "Fares are £5 [2].", ["the off-peak fare"]))
        self.assertEqual(contracts.result_of("Something.", passed=False, failed_check="Strict").status, "partial")
        self.assertEqual(contracts.result_of("Something.", stopped="its turn limit").missing,
                         ["stopped early: its turn limit"])
        asks = contracts.result_of("Needs input: which day are you travelling?")
        self.assertEqual((asks.status, asks.question), ("needs_input", "which day are you travelling?"))
        nothing = contracts.result_of("Missing: Family Railcard fares on Christmas Day.")
        self.assertEqual((nothing.status, nothing.findings), ("partial", ""))   # it worked, found nothing
        self.assertEqual(contracts.result_of("").status, "failed")
        self.assertEqual(contracts.result_of("x", error="TimeoutError").status, "failed")

    def test_the_parent_is_told_what_to_do_with_each_status(self):
        text = contracts.result_of("Fares are £5 [2].\nMissing: the off-peak fare.").for_parent()
        self.assertIn("Status: partial. Use what it found", text)
        self.assertIn("Missing: the off-peak fare", text)
        self.assertIn("Don't retry the same task more than once", contracts.result_of("").for_parent())
        self.assertIn("It asks: which day?", contracts.result_of("Needs input: which day?").for_parent())


if __name__ == "__main__":
    unittest.main()

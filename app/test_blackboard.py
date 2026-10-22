"""Blackboard / shared state (R33, blackboard.py, agent._Run.run_board)."""

import unittest
import uuid
from unittest.mock import patch

import agent
import agent_runtime
import blackboard
import contracts
import guardrails
from flows.architectures import architectures

SPEC = {"id": "board", "name": "Board", "version": 1, "state": "main", "subflows": {},
        "nodes": [{"id": "start", "type": "start"}, {"id": "ig", "type": "input_guardrail"},
                  {"id": "board", "type": "blackboard", "label": "Controller", "config": {"max_steps": 5}},
                  {"id": "kb", "type": "agent", "label": "Rail facts"}, {"id": "retriever", "type": "retriever"},
                  {"id": "analyst", "type": "agent", "label": "Analyst"},
                  {"id": "og", "type": "output_guardrail"}, {"id": "end", "type": "end"}],
        "edges": [{"source": "start", "target": "ig"}, {"source": "ig", "target": "board", "outcome": "ALLOW"},
                  {"source": "ig", "target": "end", "outcome": "BLOCK"}, {"source": "board", "target": "og"},
                  {"source": "og", "target": "end", "outcome": "ALLOW"}, {"source": "og", "target": "end", "outcome": "BLOCK"},
                  {"source": "retriever", "target": "kb", "kind": "resource"},
                  {"source": "kb", "target": "board", "kind": "resource", "port": "agents"},
                  {"source": "analyst", "target": "board", "kind": "resource", "port": "agents"}]}


def ask(agent_, question_id, goal="g", **more):
    return {"action": "ask", "reason": "r", "agent": agent_, "question_id": question_id, "goal": goal, **more}


class Board(unittest.TestCase):
    def test_entries_are_typed_and_bounded(self):
        board = blackboard.Board()
        q = board.add("question", "Bikes  and\nfares?", "user")
        self.assertEqual((q.id, q.text, q.status), ("e1", "Bikes and fares?", "open"))
        self.assertIsNone(board.add("rumour", "x", "kb"))
        self.assertIsNone(board.add("fact", "   ", "kb"))
        board.add("fact", "Bikes go free [1].", "kb", confidence=0.9, answers="e1")
        self.assertIn("e2 [fact; kb, confidence 0.90, answers e1]: Bikes go free [1].", board.text())
        self.assertEqual(board.facts_for(), "- Bikes go free [1].")

    def test_a_move_must_fit_the_team(self):
        team = [{"id": "kb", "name": "KB", "what": "rail facts"}]
        for move, ok in ((ask("kb", "e1"), True), (ask("nobody", "e1"), False), (ask("kb", "e1", goal=" "), False),
                         ({"action": "finish", "reason": "done"}, True), ({"action": "dance"}, False)):
            with self.subTest(move=move), patch.object(blackboard.llm, "call_tool", return_value=_reply(move)):
                if ok:
                    self.assertEqual(blackboard.decide("q", blackboard.Board(), team, 3)[0]["action"], move["action"])
                else:
                    with self.assertRaises(blackboard.ControlError):
                        blackboard.decide("q", blackboard.Board(), team, 3)

    def test_the_answer_is_written_from_facts_only(self):
        board = blackboard.Board()
        board.add("question", "Bikes and weather?", "user")
        board.add("question", "Bike rule?", "controller")
        board.add("fact", "Bikes go free [1].", "kb", answers="e2")
        board.add("hypothesis", "Probably fine to cycle.", "analyst", answers="e1")
        board.add("question", "Weather?", "controller").status = "stuck"
        with patch.object(blackboard.planner, "compose", return_value=("A", {})) as compose:
            blackboard.compose("Bikes and weather?", board)
        reports = compose.call_args.args[1]
        self.assertEqual([(r["label"], r["status"], r["findings"]) for r in reports],
                         [("Bike rule?", "done", "Bikes go free [1]."), ("Weather?", "failed", "")])
        self.assertNotIn("cycle", str(reports))                                  # hypotheses never go in

    def test_the_gallery_card_is_ready_and_valid(self):
        entry = next(a for a in architectures() if a["id"] == "blackboard")
        self.assertTrue(entry["ready"])
        spec = agent.flows.FlowSpec.model_validate(entry["flow"])
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        self.assertEqual(agent.flows.compiler.runtime_issues(spec, agent._runtime()), [])


def _reply(move):
    class Reply:
        tool_input, usage = move, {"input_tokens": 1, "output_tokens": 1}
    return Reply()


class Controller(unittest.TestCase):
    def run_flow(self, moves, outcomes):
        spec = agent.flows.FlowSpec.model_validate(SPEC)
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        asked, events = [], []

        def delegate(run, child, task, parent):
            asked.append((child, task.goal, dict(task.inputs)))
            status, findings, missing = outcomes.pop(0)
            return contracts.Result(status=status, findings=findings, missing=missing, confidence=0.8)

        def decide(question, board, team, steps_left):
            return moves.pop(0), {"input_tokens": 3}
        allow = guardrails.Verdict(decision="ALLOW", reason="ok", stage="input")
        with patch.object(agent._Run, "delegate", delegate), patch.object(blackboard, "decide", side_effect=decide), \
                patch.object(blackboard.planner, "compose", return_value=("Bikes go free [1].", {"input_tokens": 5})), \
                patch.object(guardrails, "check", return_value=allow), patch.object(agent, "_record_run"), \
                patch.object(agent.labels, "vocabulary", return_value=None), patch.object(agent_runtime, "RUNTIME", "local"):
            result = agent.answer("Bikes and fares?", f"s-{uuid.uuid4()}", None, None,
                                  lambda e: events.append(e) if e.get("type") == "board" else None,
                                  lambda s: s, flow=(spec, 1))
        return result, asked, events

    def test_the_controller_picks_who_acts_from_the_board(self):
        moves = [ask("kb", "n1", "Find the bike rule", new_questions=["Bike rule?", "Fare?"]),
                 ask("kb", "e3", "Find the fare"),
                 ask("analyst", "e1", "Compare", hypothesis="Fares are low", hypothesis_confidence=0.4),
                 {"action": "finish", "reason": "answered"}]
        outcomes = [("done", "Bikes go free [1].", []), ("partial", "Fares from £5 [2].", ["Peak fare"]),
                    ("done", "Cheap overall.", [])]
        result, asked, events = self.run_flow(moves, outcomes)
        self.assertEqual(result["answer"], "Bikes go free [1].")
        self.assertEqual([a[0] for a in asked], ["kb", "kb", "analyst"])
        self.assertEqual(asked[1][2], {"Facts on the board so far": "- Bikes go free [1]."})   # facts flow on
        board = {e["id"]: e for e in result["board"]}
        self.assertEqual(board["e1"]["status"], "open")                       # the user's question: until finish
        self.assertEqual((board["e2"]["status"], board["e3"]["status"]), ("answered", "answered"))
        kinds = [(e["kind"], e["author"]) for e in result["board"]]
        self.assertIn(("question", "kb"), kinds)                              # what it couldn't find is open
        self.assertIn(("hypothesis", "controller"), kinds)
        self.assertIn(("hypothesis", "analyst"), kinds)                       # no tools: reasons, doesn't find
        self.assertTrue(any(e.get("next") for e in events) and any(e.get("finished") for e in events))

    def test_a_question_never_goes_back_to_the_agent_that_posted_it(self):
        moves = [ask("kb", "e1"), ask("kb", "e3"), {"action": "finish", "reason": "stuck"}]
        result, asked, _ = self.run_flow(moves, [("partial", "Bikes go free [1].", ["Peak fare"])])
        self.assertEqual(len(asked), 1)
        self.assertEqual(next(e for e in result["board"] if e["id"] == "e3")["status"], "stuck")

    def test_it_stops_when_the_steps_run_out(self):
        moves = [ask("kb", "e1")] * 5
        result, asked, _ = self.run_flow(moves, [("done", f"Fact {i} [1].", []) for i in range(5)])
        self.assertEqual(len(asked), 5)                                        # max_steps
        self.assertEqual(result["answer"], "Bikes go free [1].")


if __name__ == "__main__":
    unittest.main()

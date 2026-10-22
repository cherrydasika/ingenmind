"""The typed contract between an agent and its sub-agents (R29).

A parent hands a sub-agent a Task with the tool ask_<id> (its input schema is
Task's); the sub-agent's run comes back as a Result, built from the run
itself, so it costs no extra model turn:

    status       done | partial | failed | needs_input
                 failed: the run broke or reported nothing at all; needs_input: the
                 sub-agent asked for something only the parent knows (a line
                 "Needs input: …"); partial: it said what is missing ("Missing:
                 …"), or a check still failed after its retries, or it ran out
                 of turns or time; done otherwise
    findings     what it reports (its answer without those lines)
    citations    the knowledge-base chunk numbers it cites
    missing      what it couldn't find
    question     what it needs, for needs_input
    confidence   the best score its checks gave (None without checks)
    cost         seconds, model turns, input and output tokens

Both are pydantic models: a parent's call is validated before the sub-agent
runs (an invalid one goes back to the parent as a tool error), and every
Result is validated before the parent sees it; both go into the trace.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_TURNS = 12
MAX_DEADLINE = 300


class Task(BaseModel):
    """What a parent asks of a sub-agent."""
    goal: str = Field(min_length=1, max_length=4000,
                      description="The task, self-contained: the sub-agent sees nothing else of the conversation")
    inputs: dict[str, str] = Field(default_factory=dict, max_length=12,
                                   description="Facts it needs, by name (a station, a date, a ticket type)")
    constraints: list[str] = Field(default_factory=list, max_length=8,
                                   description="Rules for the work: sources to prefer, what to leave out")
    expected_output: str = Field("", max_length=500,
                                 description="What to report and in what shape (e.g. 'the fare and its conditions')")
    max_turns: int | None = Field(None, ge=1, le=MAX_TURNS,
                                  description="At most this many model turns (its own limit if lower)")
    deadline_seconds: int | None = Field(None, ge=5, le=MAX_DEADLINE,
                                         description="Report with what it has after this many seconds")

    @field_validator("goal")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("the goal is empty")
        return value

    @classmethod
    def from_call(cls, args: dict) -> "Task":
        """A tool call's arguments; "task" (the old single field) is read as the goal."""
        args = dict(args or {})
        if "goal" not in args and "task" in args:
            args["goal"] = args.pop("task")
        return cls.model_validate({k: v for k, v in args.items() if k in cls.model_fields})

    def prompt(self) -> str:
        """The task as the sub-agent reads it."""
        parts = [self.goal]
        if self.inputs:
            parts.append("Inputs:\n" + "\n".join(f"- {k}: {v}" for k, v in self.inputs.items()))
        if self.constraints:
            parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.expected_output:
            parts.append(f"Report: {self.expected_output}")
        return "\n\n".join(parts)


class Cost(BaseModel):
    seconds: float = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class Result(BaseModel):
    """What a sub-agent reports back."""
    status: Literal["done", "partial", "failed", "needs_input"]
    findings: str = ""
    citations: list[int] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    question: str = ""
    confidence: float | None = Field(None, ge=0, le=1)
    failed_check: str | None = None
    error: str | None = None
    cost: Cost = Field(default_factory=Cost)

    def for_parent(self) -> str:
        """The result as the parent agent reads it, with what to do about its status."""
        head = {"done": "Status: done.",
                "partial": "Status: partial. Use what it found, say plainly what is still missing, and don't "
                           "hand it the same task again.",
                "failed": "Status: failed. Don't retry the same task more than once; answer from your other "
                          "results or say it couldn't be looked up.",
                "needs_input": "Status: needs input. Give it what it asks in a new, complete task if you know it; "
                               "otherwise answer with what you have."}[self.status]
        lines = [head]
        if self.findings:
            lines.append(f"Findings:\n{self.findings}")
        if self.missing:
            lines.append("Missing: " + "; ".join(self.missing))
        if self.question:
            lines.append(f"It asks: {self.question}")
        if self.failed_check:
            lines.append(f"Check not passed: {self.failed_check}")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.confidence is not None:
            lines.append(f"Confidence: {self.confidence:.2f}")
        return "\n".join(lines)


SUB_RULES = ("When you report: if part of the task isn't covered by what your tools return, end with one line "
             "'Missing: <what>'. If you can't start without something only the asking agent knows (not something "
             "a tool could find), reply with just one line 'Needs input: <your question>'.")
_MISSING = re.compile(r"^\s*missing\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_NEEDS = re.compile(r"^\s*needs input\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def result_of(answer: str, *, passed: bool = True, failed_check: str | None = None, error: str | None = None,
              stopped: str | None = None, checks: list[dict] | None = None, cost: dict | None = None) -> Result:
    """A sub-agent's run as a Result. stopped: why it was cut short (turns, time, budget)."""
    answer = (answer or "").strip()
    needs = _NEEDS.search(answer)
    missing = [m.strip().rstrip(".") for m in _MISSING.findall(answer)]
    findings = _NEEDS.sub("", _MISSING.sub("", answer)).strip()
    scores = [c.get("overall", c.get("confidence")) for c in checks or []]
    scores = [float(s) for s in scores if isinstance(s, (int, float))]
    if error or (not findings and not needs and not missing):
        status = "failed"
    elif needs and not findings:
        status = "needs_input"
    elif missing or not passed or stopped:
        status = "partial"
    else:
        status = "done"
    return Result(status=status, findings=findings,
                  citations=sorted({int(n) for n in re.findall(r"\[(\d+)\]", findings)}),
                  missing=missing + ([f"stopped early: {stopped}"] if stopped else []),
                  question=needs.group(1).strip() if needs else "",
                  confidence=max(0.0, min(1.0, max(scores))) if scores else None,
                  failed_check=None if passed else failed_check, error=error,
                  cost=Cost(**(cost or {})))


def task_schema() -> dict:
    """ask_<id>'s input schema: Task's fields, plain JSON Schema."""
    return {"type": "object", "required": ["goal"], "properties": {
        "goal": {"type": "string", "description": Task.model_fields["goal"].description},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                   "description": Task.model_fields["inputs"].description},
        "constraints": {"type": "array", "items": {"type": "string"},
                        "description": Task.model_fields["constraints"].description},
        "expected_output": {"type": "string", "description": Task.model_fields["expected_output"].description},
        "max_turns": {"type": "integer", "minimum": 1, "maximum": MAX_TURNS,
                      "description": Task.model_fields["max_turns"].description},
        "deadline_seconds": {"type": "integer", "minimum": 5, "maximum": MAX_DEADLINE,
                             "description": Task.model_fields["deadline_seconds"].description}}}


RESULT_FIELDS = {
    "status": "done, partial, failed or needs_input", "findings": "what it reports",
    "citations": "the [n] chunks it cites", "missing": "what it couldn't find",
    "question": "what it needs (needs_input)", "confidence": "its checks' best score",
    "cost": "seconds, turns, tokens"}


def describe() -> dict:
    """For the flow builder: what a sub-agent is asked and what it reports."""
    task = task_schema()
    return {"task": [{"name": k, "required": k in task["required"], "description": v["description"]}
                     for k, v in task["properties"].items()],
            "result": [{"name": k, "description": v} for k, v in RESULT_FIELDS.items()]}

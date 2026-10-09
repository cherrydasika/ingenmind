"""Answer Evaluator: the last gate before the user. It judges the
summarizer's (the supervisor's) final answer against the evidence the agents
actually used — knowledge-base chunks that passed the evidence evaluator,
numbered as the answer cites them, and the external-API results — on
correctness, faithfulness, completeness and citation quality. A passing
answer goes to the user; a failing one is replaced by STANDARD_MESSAGE.

Hybrid, like the evidence evaluator (evidence.py):
1. Deterministic checks: which [n] citations the answer uses, and whether
   each refers to a chunk that exists; whether knowledge-base evidence was
   used without any citation.
2. Claude via Bedrock Converse with a forced tool whose schema is the
   Pydantic model AnswerAssessment.
3. Deterministic verdict: every score at or above its threshold, and no
   citation to a chunk that does not exist.
"""

import json
import re
import time
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

import structured
import llm
from tracing import observation

THRESHOLDS = {"correctness": 0.7, "faithfulness": 0.7, "completeness": 0.6, "citation_quality": 0.6}
PER_ISSUE = 0.1     # each named unsupported claim / citation issue lowers its score by this much
MAX_CHUNKS = 60     # chunks shown to the model: every cited chunk first, then the rest
MAX_CHUNK_CHARS = 1200  # at least common.config.CHUNK_SIZE: a cut chunk makes its last facts look unsupported
MAX_API_CHARS = 2500
STANDARD_MESSAGE = (
    "Sorry, I couldn't produce an answer that passed my quality checks for accuracy, grounding in the "
    "sources, completeness and citations. Please try rephrasing the question or asking about something "
    "more specific."
)

Score = Annotated[float, Field(ge=0.0, le=1.0)]


AnswerType = Literal["answer", "clarification", "not_available", "conversational"]


class AnswerAssessment(BaseModel):
    """What the model judges; the schema of the forced tool call."""
    answer_type: AnswerType = Field("answer", description="answer: it answers the question; clarification: it asks "
                                    "the user a clarifying question; not_available: it says plainly the information "
                                    "is not available; conversational: only a greeting, small talk or what the "
                                    "assistant can help with")
    correctness: Score = Field(description="The answer's facts agree with the evidence; nothing contradicts it")
    faithfulness: Score = Field(description="Every factual claim is supported by the evidence; no outside facts")
    completeness: Score = Field(description="Every part of the question is answered, or explicitly said to be unknown")
    citation_quality: Score = Field(description="Knowledge-base claims cite the right [n] chunk; API facts name their source")
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_parts: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    rationale: str = Field(description="One or two sentences on the verdict")


class AnswerChecks(BaseModel):
    cited: list[int]
    invalid_citations: list[int]
    evidence_chunks: int
    api_results: int
    kb_evidence_uncited: bool


class AnswerEvaluation(BaseModel):
    passed: bool
    answer_type: AnswerType = "answer"
    checked: list[str] = Field(default_factory=lambda: list(THRESHOLDS))   # the scores that had to pass
    overall: Score
    scores: dict[str, float]
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_parts: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    rationale: str = ""
    checks: AnswerChecks
    failed_on: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    seconds: float = 0.0


# ---------- 1. deterministic checks ----------

def citations(answer: str) -> list[int]:
    """[3], [1][2], [1, 4], [5–7] → sorted chunk numbers."""
    found = set()
    for group in re.findall(r"\[([\d,\s–\-]+)\]", answer or ""):
        for part in re.split(r"\s*,\s*", group.strip()):
            span = re.split(r"\s*[–-]\s*", part)
            if len(span) == 2 and all(x.isdigit() for x in span) and int(span[0]) <= int(span[1]) <= int(span[0]) + 50:
                found.update(range(int(span[0]), int(span[1]) + 1))
            elif part.isdigit():
                found.add(int(part))
    return sorted(found)


def checks(answer: str, chunks: list[dict], api_results: list[dict]) -> AnswerChecks:
    cited = citations(answer)
    valid = {c["n"] for c in chunks}
    return AnswerChecks(cited=cited, invalid_citations=[n for n in cited if n not in valid],
                        evidence_chunks=len(chunks), api_results=len(api_results),
                        kb_evidence_uncited=bool(chunks) and not cited)


# ---------- 2. LLM assessment ----------

SYSTEM_PROMPT = (
    "You evaluate an assistant's final answer before it reaches the user. Judge it ONLY against the "
    "evidence given: numbered knowledge-base chunks [n] and external API results. Do NOT use outside knowledge "
    "to decide what is true; a fact that is not in the evidence is unsupported even if it may be right. Do not "
    "rewrite or improve the answer. Check every clause, including parentheses, asides and hedged statements "
    "('likely', 'probably', 'appears to', 'may be'): each is a claim, and if the evidence does not support it, "
    "it is an unsupported claim. "
    "Score from 0 to 1: correctness (the answer's facts agree with the evidence "
    "and nothing contradicts it), faithfulness (every factual claim is supported by the evidence; generic "
    "advice, website names or facts not in the evidence lower it), completeness (every part of the question is "
    "answered, or the answer says plainly that the information is not available — an honest 'not available' "
    "counts as answered), citation_quality (claims taken from knowledge-base chunks cite the right [n]; facts "
    "from APIs name their source; citations that point to the wrong chunk lower it; 1 if the answer has no "
    "factual claims). Set answer_type: answer if it answers the question, clarification if it asks the user a "
    "clarifying question instead, not_available if it says plainly that the information is not available, "
    "conversational if it is only a greeting, small talk or a description of what the assistant can help with "
    "(statements about the assistant itself are not factual claims). "
    "List unsupported_claims, missing_parts and citation_issues. Report through the "
    "record_answer_assessment tool only."
)
TOOL_NAME = "record_answer_assessment"


HEDGE = re.compile(r"\b(?:likely|probably|presumably|possibly|perhaps|apparently|appears?|seems?|"
                   r"may be|might be|could be|i think|i believe)\b", re.IGNORECASE)


def statements_to_check(answer: str) -> list[str]:
    """Asides in parentheses and hedged clauses: a model judging whole sentences
    passes a guess tucked into a supported sentence ("Lakeshore Rail (likely
    North America) requires..."), so they are listed for it separately."""
    found = [m.strip() for m in re.findall(r"\(([^()]*[A-Za-z]{3}[^()]*)\)", answer)]
    for clause in re.split(r"(?<=[.;:!?])\s+|,\s+|\s+[—–-]\s+", re.sub(r"\[\d+\]", "", answer)):
        clause = clause.strip(" .;:")
        if HEDGE.search(clause) and not any(x in clause for x in found):
            found.append(clause)
    return found[:12]


def _prompt(question: str, answer: str, chunks: list[dict], api_results: list[dict]) -> str:
    lines = [f"QUESTION:\n{question}", "", "ANSWER TO EVALUATE:", answer or "(empty)", ""]
    statements = statements_to_check(answer or "")
    if statements:
        lines += ["ASIDES AND HEDGED STATEMENTS IN THE ANSWER (check each against the evidence; list any it does "
                  "not support as an unsupported claim):"] + [f"- {x}" for x in statements] + [""]
    lines += ["KNOWLEDGE-BASE EVIDENCE:"]
    lines += [f"[{c['n']}] {c['source_url']}\n{c['text'].strip().replace(chr(10), ' ')[:MAX_CHUNK_CHARS]}" for c in chunks] \
        or ["(none)"]
    lines += ["", "EXTERNAL API RESULTS:"]
    lines += [f"- {r['tool']}({json.dumps(r['input'], ensure_ascii=False)}): "
              f"{json.dumps(r['summary'], ensure_ascii=False)[:MAX_API_CHARS]}" for r in api_results] or ["(none)"]
    return "\n".join(lines)


LIST_FIELDS = ("unsupported_claims", "missing_parts", "citation_issues")


def normalise(raw: dict) -> dict:
    """Fix odd output shapes before validating (see structured.py)."""
    return structured.normalise(raw, scores=tuple(THRESHOLDS), lists=LIST_FIELDS, strings=("rationale",))


def assess_with_llm(question: str, answer: str, chunks: list[dict], api_results: list[dict]) -> tuple[AnswerAssessment, dict]:
    """A reply that still does not validate is asked for once more."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    for attempt in (1, 2):
        try:
            return _assess_once(question, answer, chunks, api_results, usage), usage
        except ValidationError:
            if attempt == 2:
                raise


def _assess_once(question: str, answer: str, chunks: list[dict], api_results: list[dict], usage: dict) -> AnswerAssessment:
    prompt = _prompt(question, answer, chunks, api_results)
    with observation(as_type="generation", name="answer_evaluator", model=llm.MODEL,
                     input=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=SYSTEM_PROMPT, name=TOOL_NAME, description="Record the answer assessment.",
                              schema=AnswerAssessment.model_json_schema(), max_tokens=1024, temperature=0)
        raw = reply.tool_input
        usage["input_tokens"] += reply.input_tokens
        usage["output_tokens"] += reply.output_tokens
        gen.update(output=raw, usage_details=reply.usage)
    return AnswerAssessment.model_validate(normalise(raw if isinstance(raw, dict) else {}))


# ---------- 3. verdict ----------

def evaluate(question: str, answer: str, chunks: list[dict], api_results: list[dict],
             assessor: Callable[..., tuple[AnswerAssessment, dict]] = assess_with_llm,
             thresholds: dict[str, float] | None = None) -> AnswerEvaluation:
    """chunks: [{"n", "source_url", "text"}] the answer may cite; api_results:
    [{"tool", "input", "summary"}] from successful API calls; thresholds: the
    pass mark per score (a flow's Answer evaluator node may change them)."""
    limits = {**THRESHOLDS, **(thresholds or {})}
    started = time.monotonic()
    # Citations are checked against every approved chunk; the model sees
    # every cited chunk, then as many others as fit.
    check = checks(answer, chunks, api_results)
    cited = set(check.cited)
    chunks = ([c for c in chunks if c["n"] in cited] + [c for c in chunks if c["n"] not in cited])[:MAX_CHUNKS]
    if not (answer or "").strip():
        return AnswerEvaluation(passed=False, overall=0.0, scores={k: 0.0 for k in THRESHOLDS}, checks=check,
                                failed_on=["empty answer"], rationale="The summarizer returned no answer.",
                                seconds=round(time.monotonic() - started, 3))
    assessment, usage = assessor(question, answer, chunks, api_results)
    scores = {k: getattr(assessment, k) for k in THRESHOLDS}
    issues = list(assessment.citation_issues)
    # The lists and the scores must agree: each unsupported claim or
    # citation issue the model names costs 0.1 of that score.
    scores["faithfulness"] = round(min(scores["faithfulness"], 1 - PER_ISSUE * len(assessment.unsupported_claims)), 3)
    if assessment.unsupported_claims:
        # Any claim the evidence does not support fails the answer: a 0.1 cost
        # let a guess or a piece of outside advice through.
        scores["faithfulness"] = round(min(scores["faithfulness"], limits["faithfulness"] - PER_ISSUE), 3)
    scores["citation_quality"] = round(min(scores["citation_quality"], 1 - PER_ISSUE * len(issues)), 3)
    # Deterministic citation limits: a citation to a chunk that does not
    # exist, or knowledge-base evidence used with no citation at all.
    # A grounded clarifying question, or an honest "not available", is a
    # legitimate reply: completeness is not required of it, everything else
    # is. A clarification must actually ask something.
    answer_type = assessment.answer_type
    if answer_type == "clarification" and "?" not in answer:
        answer_type = "answer"
    if answer_type == "conversational" and (chunks or api_results):
        answer_type = "answer"   # small talk is only small talk when no evidence was used
    if check.invalid_citations:
        scores["citation_quality"] = min(scores["citation_quality"], 0.3)
        issues.append(f"Cites chunks that do not exist: {check.invalid_citations}")
    elif check.kb_evidence_uncited and (answer_type == "answer" or assessment.unsupported_claims):
        # An honest "not available" or a question has nothing to cite.
        scores["citation_quality"] = min(scores["citation_quality"], 0.5)
        issues.append("Uses knowledge-base evidence without citing any chunk")
    if answer_type == "conversational":
        checked = ["faithfulness"]   # no claims about the world to be correct, complete or cited
    else:
        # An honest "not available" or a clarifying question that claims nothing
        # unsupported has nothing to complete or cite: the model sometimes still
        # scores its citations 0, which blocked the most honest answers.
        checked = [k for k in THRESHOLDS if not (k in ("completeness", "citation_quality") and answer_type != "answer"
                                                 and not assessment.unsupported_claims)]
    failed_on = [k for k in checked if scores[k] < limits[k]]
    return AnswerEvaluation(
        passed=not failed_on, answer_type=answer_type, checked=checked,
        overall=round(sum(scores.values()) / len(scores), 3), scores=scores,
        unsupported_claims=assessment.unsupported_claims, missing_parts=assessment.missing_parts,
        citation_issues=issues, rationale=assessment.rationale, checks=check, failed_on=failed_on,
        usage=usage, seconds=round(time.monotonic() - started, 3))

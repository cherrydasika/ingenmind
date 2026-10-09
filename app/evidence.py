"""Evidence Evaluator: judges whether the knowledge-base agent's retrieved
chunks are good enough to answer its task, before its findings reach the
supervisor (the answer agent). It never answers the question itself.

Hybrid, in three steps:

1. Deterministic checks: is there any evidence, how similar is it to the
   queries, how many distinct sources, how fresh (ingested_at vs ttl_days).
   No chunks at all is a retrieval failure without asking the model.
2. LLM semantic assessment (Claude on Bedrock, Converse with a forced tool
   whose schema is the Pydantic model LlmAssessment): relevance, coverage,
   entailment of the agent's draft, source quality, consistency, missing
   information, unsupported claims, contradictions, and whether a gap is a
   retrieval failure or a knowledge gap.
3. Deterministic routing: scores and lists → one Decision.

A retrieval failure gets one second chance: diagnose_and_rewrite() asks the
model why the searches missed and for better queries, the retrieval agent
searches again, and the evaluator judges the new evidence (attempt 2). If
retrieval still fails after that, the knowledge base most likely lacks the
information, so attempt 2 reports a KNOWLEDGE_GAP instead.
"""

import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

import structured
import llm
from tracing import observation

MIN_SIMILARITY = 0.30      # best cosine below this: the search missed
GOOD_CONFIDENCE = 0.70     # overall confidence needed to answer
LOW = 0.40                 # a score below this is a clear failure
WEAK = 0.60                # a score below this is not enough to answer
MAX_EVIDENCE_CHARS = 1200  # per chunk, in the prompt
MAX_ATTEMPTS = 2           # the first retrieval, plus one diagnose-and-rewrite retry


class Decision(str, Enum):
    GOOD_EVIDENCE = "GOOD_EVIDENCE"
    RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
    KNOWLEDGE_GAP = "KNOWLEDGE_GAP"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


FailureType = Literal["NONE", "RETRIEVAL_FAILURE", "KNOWLEDGE_GAP", "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE"]
ACTIONS = {
    Decision.GOOD_EVIDENCE: "ANSWER",
    Decision.RETRIEVAL_FAILURE: "RETRY_RETRIEVAL",
    Decision.KNOWLEDGE_GAP: "RESEARCH",
    Decision.CONFLICTING_EVIDENCE: "INVESTIGATE",
    Decision.INSUFFICIENT_EVIDENCE: "RETRY_OR_ASK",
}
Score = Annotated[float, Field(ge=0.0, le=1.0)]
# What a knowledge gap needs: pages to ingest (static_content), or a source of
# live or structured data to integrate (data_source; research.py profiles it).
GapType = Literal["static_content", "data_source"]
GAP_TYPES = ("static_content", "data_source")


class LlmAssessment(BaseModel):
    """What the model judges; the schema of the forced tool call."""
    relevance: Score
    coverage: Score
    entailment: Score
    source_quality: Score
    consistency: Score
    missing_information: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    failure_type: FailureType = "NONE"
    gap_type: GapType = Field("static_content", description=(
        "What the missing information is: static_content (facts a web page can hold) or data_source (live, "
        "frequently changing or structured data, or where to get it)"))
    rationale: str = Field(description="One or two sentences about the evidence, never an answer to the task")

    @field_validator("gap_type", mode="before")
    @classmethod
    def _known_gap_type(cls, value):
        return value if value in GAP_TYPES else "static_content"


class Scores(BaseModel):
    relevance: Score
    coverage: Score
    entailment: Score
    source_quality: Score
    freshness: Score
    consistency: Score


class Checks(BaseModel):
    """The deterministic signals."""
    chunks: int
    searches: int
    distinct_sources: int
    best_similarity: float | None
    freshness: float
    oldest_ingest_days: float | None


class EvidenceEvaluation(BaseModel):
    decision: Decision
    overall_confidence: Score
    scores: Scores
    missing_information: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    failure_type: FailureType = "NONE"
    gap_type: GapType | None = None   # None: not judged (no evidence, so no model call)
    recommended_action: str
    rationale: str = ""
    checks: Checks
    attempt: int = 1
    llm_used: bool = True
    usage: dict = Field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    seconds: float = 0.0


# ---------- 1. deterministic checks ----------

def deterministic_checks(searches: list[dict], now: datetime | None = None) -> Checks:
    """searches: the knowledge-base agent's searches, each {"retrieval": {"chunks",
    "dense_ranking"}}."""
    now = now or datetime.now(timezone.utc)
    chunks = [c for s in searches for c in s["retrieval"]["chunks"]]
    similarities = [c["score"] for s in searches for c in s["retrieval"].get("dense_ranking", [])]
    freshness, ages = [], []
    for chunk in chunks:
        try:
            ingested = datetime.fromisoformat(chunk["ingested_at"])
        except (KeyError, TypeError, ValueError):
            continue
        age_days = max((now - ingested).total_seconds() / 86400, 0.0)
        ages.append(age_days)
        ttl = chunk.get("ttl_days") or 30
        freshness.append(max(0.0, min(1.0, 1 - age_days / ttl)))
    return Checks(
        chunks=len(chunks),
        searches=len(searches),
        distinct_sources=len({c["source_url"] for c in chunks}),
        best_similarity=round(max(similarities), 3) if similarities else None,
        freshness=round(sum(freshness) / len(freshness), 3) if freshness else 1.0,
        oldest_ingest_days=round(max(ages), 1) if ages else None,
    )


def _source_diversity(checks: Checks) -> float:
    return {0: 0.0, 1: 0.6, 2: 0.8}.get(checks.distinct_sources, 0.9)


# ---------- 2. LLM assessment ----------

SYSTEM_PROMPT = (
    "You are an evidence evaluator in a retrieval-augmented system. You judge whether the retrieved evidence is "
    "good enough for an answer agent to answer the task. Rules: Do NOT answer the question or the task. Do NOT "
    "use outside knowledge: judge only what the numbered evidence says. Do NOT guess. Do NOT treat missing "
    "evidence as evidence of absence: if something is not in the evidence, it is missing, not false. "
    "Score from 0 to 1: relevance (does the evidence address the task), coverage (does it cover every part of "
    "the task), entailment (are the draft findings' claims supported by the evidence; 1 if there are no "
    "claims), source_quality (are the sources authoritative and specific for this task), consistency (1 = no "
    "contradictions between chunks). List missing_information (parts of the task the evidence does not "
    "cover), unsupported_claims (draft claims the evidence does not support) and contradictions (chunks that "
    "disagree, citing their [n] numbers). Judge coverage against what the task needs unconditionally: a "
    "request to cover whatever the knowledge base has (for example 'any countries or operators you have "
    "information about', or 'if the knowledge base covers several regions') is covered by the evidence it "
    "has, so do not list other places, operators or systems as missing. A place, operator or system the "
    "user's task does not name is never missing information. Set failure_type: NONE if the "
    "evidence is enough; "
    "RETRIEVAL_FAILURE if the evidence is off-topic or the searches look poorly phrased, so better searches "
    "of the same knowledge base would likely find it; KNOWLEDGE_GAP if the knowledge base itself seems not to "
    "contain the needed information (for example several differently phrased searches all returned evidence "
    "about other topics); CONFLICTING_EVIDENCE if chunks "
    "disagree on something the task needs; INSUFFICIENT_EVIDENCE if the evidence is partial and neither "
    "of those. Set gap_type: data_source when the missing information is live, frequently changing or "
    "structured data (departures, delays, platforms, live prices, availability, positions) or the task asks "
    "where to get such data, so a stored web page could not keep it current; otherwise static_content. "
    "Report through the record_evidence_assessment tool only."
)
TOOL_NAME = "record_evidence_assessment"


def _evidence_text(task: str, searches: list[dict], draft: str) -> str:
    lines = [f"TASK:\n{task}", "", "SEARCHES:"]
    lines += [f"- {s['query']}" for s in searches]
    lines += ["", "EVIDENCE:"]
    for s in searches:
        for i, chunk in enumerate(s["retrieval"]["chunks"]):
            text = chunk["text"].strip().replace("\n", " ")[:MAX_EVIDENCE_CHARS]
            lines.append(f"[{s['first'] + i}] {chunk['source_url']} (ingested {chunk.get('ingested_at') or 'unknown'})\n{text}")
    lines += ["", "DRAFT FINDINGS FROM THE RETRIEVAL AGENT (to check for entailment, not to repeat):", draft or "(none)"]
    return "\n".join(lines)


def _malformed(raw: dict) -> bool:
    """The model sometimes leaks tool-call markup into a string field
    ("</rationale><parameter name=...>"), losing the fields after it."""
    return any(isinstance(v, str) and "<parameter" in v for v in raw.values())


def assess_with_llm(task: str, searches: list[dict], draft: str) -> tuple[LlmAssessment, dict]:
    """Claude on Bedrock with a forced tool call whose input schema is
    LlmAssessment; returns the validated assessment and token usage. Odd
    output shapes are normalised (structured.py); a reply that is still
    malformed or invalid is asked for once more."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    for attempt in (1, 2):
        raw = _converse(task, searches, draft, usage)
        try:
            if _malformed(raw) and attempt == 1:
                continue
            return LlmAssessment.model_validate(structured.normalise(
                raw, scores=SCORE_FIELDS, lists=LIST_FIELDS, strings=("rationale",))), usage
        except ValidationError:
            if attempt == 2:
                raise


SCORE_FIELDS = ("relevance", "coverage", "entailment", "source_quality", "consistency")
LIST_FIELDS = ("missing_information", "unsupported_claims", "contradictions")


def _converse(task: str, searches: list[dict], draft: str, usage: dict) -> dict:
    prompt = _evidence_text(task, searches, draft)
    with observation(as_type="generation", name="evidence_evaluator", model=llm.MODEL,
                     input=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=SYSTEM_PROMPT, name=TOOL_NAME, description="Record the evidence assessment.",
                              schema=LlmAssessment.model_json_schema(), max_tokens=1024, temperature=0)
        raw = dict(reply.tool_input)
        usage["input_tokens"] += reply.input_tokens
        usage["output_tokens"] += reply.output_tokens
        gen.update(output=raw, usage_details=reply.usage)
    return raw


# ---------- 3. deterministic routing ----------

class Limits(BaseModel):
    """The evaluator's settings (a flow's Evidence evaluator node may change them)."""
    good_confidence: float = GOOD_CONFIDENCE
    min_similarity: float = MIN_SIMILARITY
    max_attempts: int = MAX_ATTEMPTS


DEFAULT_LIMITS = Limits()


def _retrieval_failure(attempt: int, limits: Limits = DEFAULT_LIMITS) -> Decision:
    # After the rewritten retry also missed, the knowledge base most likely
    # lacks the information: a third search would find nothing new.
    return Decision.RETRIEVAL_FAILURE if attempt < limits.max_attempts else Decision.KNOWLEDGE_GAP


def decide(assessment: LlmAssessment, scores: Scores, overall: float, attempt: int = 1,
           limits: Limits = DEFAULT_LIMITS) -> Decision:
    if assessment.contradictions and scores.consistency < WEAK:
        return Decision.CONFLICTING_EVIDENCE
    if scores.relevance < LOW:
        if assessment.failure_type == "KNOWLEDGE_GAP":
            return Decision.KNOWLEDGE_GAP
        return _retrieval_failure(attempt, limits)
    if scores.coverage < WEAK or scores.entailment < WEAK or overall < limits.good_confidence:
        if assessment.failure_type == "KNOWLEDGE_GAP":
            return Decision.KNOWLEDGE_GAP
        if assessment.failure_type == "RETRIEVAL_FAILURE":
            return _retrieval_failure(attempt, limits)
        if assessment.failure_type == "CONFLICTING_EVIDENCE":
            return Decision.CONFLICTING_EVIDENCE
        return Decision.INSUFFICIENT_EVIDENCE
    return Decision.GOOD_EVIDENCE


def _overall(scores: Scores) -> float:
    weights = {"relevance": 0.25, "coverage": 0.25, "entailment": 0.25, "source_quality": 0.1,
               "freshness": 0.05, "consistency": 0.1}
    return round(sum(getattr(scores, k) * w for k, w in weights.items()), 3)


def evaluate(task: str, searches: list[dict], draft: str,
             assessor: Callable[[str, list[dict], str], tuple[LlmAssessment, dict]] = assess_with_llm,
             now: datetime | None = None, attempt: int = 1, limits: Limits = DEFAULT_LIMITS) -> EvidenceEvaluation:
    started = time.monotonic()
    checks = deterministic_checks(searches, now)
    if checks.chunks == 0:
        # Nothing to judge: no model call.
        decision = _retrieval_failure(attempt, limits)
        return EvidenceEvaluation(
            decision=decision, overall_confidence=0.0,
            scores=Scores(relevance=0, coverage=0, entailment=0, source_quality=0, freshness=0, consistency=1),
            missing_information=[task] if task else [], failure_type=decision.value,
            recommended_action=ACTIONS[decision],
            rationale="The retrieval agent found no chunks.", checks=checks, attempt=attempt, llm_used=False,
            seconds=round(time.monotonic() - started, 3))

    try:
        assessment, usage = assessor(task, searches, draft)
    except (ValidationError, llm.NoToolCall, KeyError) as error:
        raise RuntimeError(f"evidence evaluator returned no valid assessment: {error}") from error

    # Blend the model's judgement with the deterministic signals: relevance
    # cannot exceed what the similarity search supports, source quality is
    # tempered by how many distinct sources there are, freshness is measured.
    similarity_cap = 1.0
    if checks.best_similarity is not None and checks.best_similarity < limits.min_similarity:
        similarity_cap = checks.best_similarity / limits.min_similarity * LOW
    scores = Scores(
        relevance=round(min(assessment.relevance, max(similarity_cap, 0.0)), 3),
        coverage=assessment.coverage,
        entailment=assessment.entailment,
        source_quality=round((assessment.source_quality + _source_diversity(checks)) / 2, 3),
        freshness=checks.freshness,
        consistency=assessment.consistency,
    )
    overall = _overall(scores)
    decision = decide(assessment, scores, overall, attempt, limits)
    return EvidenceEvaluation(
        decision=decision, overall_confidence=overall, scores=scores,
        missing_information=assessment.missing_information, unsupported_claims=assessment.unsupported_claims,
        contradictions=assessment.contradictions,
        failure_type="NONE" if decision is Decision.GOOD_EVIDENCE else decision.value,
        gap_type=assessment.gap_type,
        recommended_action=ACTIONS[decision], rationale=assessment.rationale, checks=checks, usage=usage,
        attempt=attempt, seconds=round(time.monotonic() - started, 3))


# ---------- retrieval failure: diagnose and rewrite (once) ----------

class QueryRewrite(BaseModel):
    """Why the searches missed, and better queries; the forced tool's schema."""
    diagnosis: str = Field(description="One or two sentences: why the searches returned off-topic or no evidence")
    queries: list[str] = Field(min_length=1, max_length=3,
                               description="1–3 rewritten search queries for the same knowledge base")


REWRITE_PROMPT = (
    "You diagnose failed searches in a retrieval-augmented system. Given a task, the search queries that were "
    "tried and what they returned, explain briefly why the searches missed (for example too broad, wrong "
    "terms, wrong language or spelling, several questions in one query) and write 1 to 3 better search "
    "queries for the same knowledge base: short, specific, using the words the knowledge base's documents would use. Do NOT "
    "answer the task and do NOT use outside knowledge beyond choosing search terms. Report through the "
    "record_query_rewrite tool only."
)
REWRITE_TOOL = "record_query_rewrite"


def diagnose_and_rewrite(task: str, searches: list[dict], evaluation: EvidenceEvaluation) -> tuple[QueryRewrite, dict]:
    """Claude on Bedrock (forced tool, Pydantic schema): a diagnosis and
    rewritten queries for the one retry."""
    tried = "\n".join(f"- {s['query']}: " + ("; ".join(c["source_url"] for c in s["retrieval"]["chunks"][:3]) or "no results")
                      for s in searches) or "(no searches)"
    prompt = (f"TASK:\n{task}\n\nSEARCHES TRIED (query: top sources):\n{tried}\n\n"
              f"EVALUATOR: {evaluation.rationale}\nMissing: {'; '.join(evaluation.missing_information) or '—'}")
    with observation(as_type="generation", name="query_rewriter", model=llm.MODEL,
                     input=[{"role": "system", "content": REWRITE_PROMPT}, {"role": "user", "content": prompt}]) as gen:
        reply = llm.call_tool(prompt, system=REWRITE_PROMPT, name=REWRITE_TOOL,
                              description="Record the diagnosis and rewritten queries.",
                              schema=QueryRewrite.model_json_schema(), max_tokens=512, temperature=0)
        raw = dict(reply.tool_input)
        if isinstance(raw.get("queries"), list):
            raw["queries"] = [str(q).strip() for q in raw["queries"] if str(q).strip()][:3]
        rewrite = QueryRewrite.model_validate(raw)
        usage = reply.usage
        gen.update(output=rewrite.model_dump(), usage_details=usage)
    return rewrite, usage


def as_json(evaluation: EvidenceEvaluation) -> str:
    return json.dumps(evaluation.model_dump(mode="json"), ensure_ascii=False)

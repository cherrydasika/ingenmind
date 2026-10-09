"""Component types the flow JSON may use: what each is, its settings (a
Pydantic model, so the builder can render a form from its JSON Schema), its
outcomes (the branches a routed component chooses between), the resource
ports it accepts, and whether it bounds a loop.

Kinds:
- control:  start and end of a flow
- gate:     a check that runs before the graph (the input guardrail)
- flow:     a step that runs as a graph node
- resource: something plugged into a step's port (LLM, retriever, tools…);
            it configures the step and never becomes a graph node

"impl" names the implementation the runtime supplies (agent.py), so this
module describes components without importing the agents. Settings mirror
the code's defaults; "applied" lists the ones the runtime reads from the
node (the rest document what the component uses and are not yet settable).
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

import agent_runtime
import api_tools
import guardrails
import llm

Kind = Literal["control", "gate", "flow", "resource"]
PORTS = ("llm", "memory", "retriever", "vector_db", "tools", "web_search", "scraper")


class NoConfig(BaseModel):
    pass


# Long text the builder edits in a multi-line box.
MULTILINE = {"x-multiline": True}

DEFAULT_DOMAIN = "UK rail and weather"
# The main prompt: the context every agent, web search and source check
# assumes. Rolling the assistant out to another domain or country starts
# with this and the search country (SupervisorConfig).
UK_RAIL_BRIEF = (
    "An assistant for trains and rail travel in the United Kingdom (England, Scotland, Wales and Northern "
    "Ireland), plus the weather anywhere. Every train question is about UK railways unless the user clearly "
    "names another country: a question such as \"is there a pantry car on trains?\" means UK trains."
)
DEFAULT_SEARCH_COUNTRY = "united kingdom"
UK_RAIL_SUPERVISOR_INSTRUCTIONS = (
    "This assistant covers trains in the UK and the weather. The knowledge base holds ingested rail web pages "
    "(train operators, routes, tickets, fares, railcards, refunds and compensation, stations, accessibility "
    "and travel tips): send every train question to knowledge_base. Use external_apis only for the weather "
    "(forecasts and current conditions anywhere, and weather that may disrupt a train journey). There is no "
    "live UK timetable, departures or delays tool: live UK train times also go to knowledge_base, and when "
    "the findings have no live times, say that live times are not available here. Do not use the Swiss "
    "transport or entry-rule tools: they are outside this assistant's scope."
)


class SupervisorConfig(BaseModel):
    brief: str = Field(UK_RAIL_BRIEF, min_length=20, max_length=1000, json_schema_extra=MULTILINE,
                       description="The main prompt: what the assistant is for, and the country or region every "
                                   "question is about unless the user says otherwise. Every agent, the web "
                                   "search and source validation assume it; an empty guardrail scope follows it.")
    search_country: str = Field(DEFAULT_SEARCH_COUNTRY, max_length=40,
                                description="Web research favours pages from this country (its English name, "
                                            "such as united kingdom or france); empty for worldwide")
    domain: str = Field(DEFAULT_DOMAIN, min_length=1, max_length=60,
                        description="What the assistant is for, in a word or two (\"a small team of … agents\")")
    instructions: str = Field(UK_RAIL_SUPERVISOR_INSTRUCTIONS, max_length=2000, json_schema_extra=MULTILINE,
                              description="What the knowledge base holds and which questions go to which "
                                          "specialist. Delegation, citation and answer rules are fixed.")
    max_rounds: int = Field(2, ge=1, le=5, description="Delegation rounds per question")
    max_turns: int = Field(6, ge=1, le=10, description="Model turns per agent (harness default)")
    answer_word_limit: int = Field(120, ge=40, le=400, description="The summarizer's hard word limit (in its prompt)")


class SpecialistConfig(BaseModel):
    role: Literal["external_apis"] = Field("external_apis", description="Agent role in agent.py ROLES")


class SubflowConfig(BaseModel):
    subflow: str = Field(description="Id of the nested flow in this flow's subflows")


class InputGuardrailConfig(BaseModel):
    """The flow's scope: every guardrail in the flow (input, output, research sources) enforces it.
    An empty scope follows the supervisor's brief, and an empty message is a
    generic one naming the supervisor's domain."""
    stage: Literal["input"] = "input"
    scope: str = Field(guardrails.UK_RAIL_SCOPE, max_length=guardrails.MAX_SCOPE_CHARS,
                       json_schema_extra=MULTILINE,
                       description="What is in scope, and what is not; empty: the supervisor's brief. The rules "
                                   "around it (untrusted data, prompt injection, secrets, mixed requests) are "
                                   "fixed and always apply.")
    block_message: str = Field(guardrails.BLOCK_MESSAGE, max_length=400,
                               description="Shown when a question is out of scope")
    clarify_message: str = Field(guardrails.CLARIFY_MESSAGE, max_length=400,
                                 description="Shown when a question needs more context")
    output_message: str = Field(guardrails.OUTPUT_MESSAGE, max_length=400,
                                description="Shown when an answer is blocked")
    unavailable_message: str = Field(guardrails.UNAVAILABLE_MESSAGE, max_length=400,
                                     description="Shown when the checks cannot run")

    @field_validator("scope")
    @classmethod
    def _scope_says_something(cls, value: str) -> str:
        if value.strip() and len(value.strip()) < 20:
            raise ValueError("a scope is empty (the brief) or at least 20 characters")
        return value


class OutputGuardrailConfig(BaseModel):
    stage: Literal["output"] = Field("output", description="Enforces the scope set on the input guardrail")


class AnswerEvaluatorConfig(BaseModel):
    correctness: float = Field(0.7, ge=0, le=1)
    faithfulness: float = Field(0.7, ge=0, le=1)
    completeness: float = Field(0.6, ge=0, le=1)
    citation_quality: float = Field(0.6, ge=0, le=1)


class EvidenceEvaluatorConfig(BaseModel):
    good_confidence: float = Field(0.7, ge=0, le=1, description="Overall confidence needed to answer")
    min_similarity: float = Field(0.3, ge=0, le=1, description="Best cosine below this: the search missed")
    max_attempts: int = Field(2, ge=1, le=3, description="First retrieval plus rewrite retries")


class ResearchAgentConfig(BaseModel):
    """Budgets for a gap about live or structured data (the data-source path);
    a gap about static content keeps the research agent's own budget."""
    data_source_searches: int = Field(6, ge=1, le=12, description="Web searches for a data-source gap")
    data_source_fetches: int = Field(10, ge=1, le=20, description="Pages fetched for a data-source gap")
    data_source_turns: int = Field(10, ge=4, le=10, description="Model turns for a data-source gap (several "
                                                                 "tool calls fit in one turn; 10 is the app's cap)")
    max_profiles: int = Field(4, ge=1, le=6, description="Candidate data sources profiled per gap")
    profile_max_age_days: int = Field(30, ge=1, le=365,
                                      description="A stored profile older than this is verified again")


class SourceValidatorConfig(BaseModel):
    authority: float = Field(0.6, ge=0, le=1)
    freshness: float = Field(0.5, ge=0, le=1)
    consistency: float = Field(0.6, ge=0, le=1)
    relevance: float = Field(0.6, ge=0, le=1)


class IngestConfig(BaseModel):
    table: str = "research_chunks"
    ttl_days: int = Field(90, ge=1, le=3650)


class PlaceholderConfig(BaseModel):
    message: str = Field(description="Status returned until the agent behind it exists")


HARNESS_MODEL = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"   # the only model the harness role may call


# What the agents run on is set by the deployment (AGENT_RUNTIME, LLM_PROVIDER,
# LLM_MODEL), not by a flow: the LLM node shows it.
if agent_runtime.RUNTIME == "agentcore":
    RUNTIME_PROVIDER, RUNTIME_MODEL = "bedrock_agentcore_harness", HARNESS_MODEL
else:
    RUNTIME_PROVIDER, RUNTIME_MODEL = "local", (llm.label() if llm.MODEL else "chat model not configured")
# Values saved by earlier versions of the builder, read as the current runtime's.
EARLIER_PROVIDERS = ("bedrock_agentcore_harness", "bedrock_converse", "local")


class LlmConfig(BaseModel):
    provider: Literal[RUNTIME_PROVIDER] = Field(RUNTIME_PROVIDER, description="The agent runtime (AGENT_RUNTIME)")
    model: Literal[RUNTIME_MODEL] = Field(RUNTIME_MODEL, description="Set by the deployment, not the flow: "
                                          "LLM_PROVIDER and LLM_MODEL on the local runtime, the harness's model "
                                          "on AgentCore")

    @field_validator("provider", mode="before")
    @classmethod
    def _current_provider(cls, value):
        return RUNTIME_PROVIDER if value in EARLIER_PROVIDERS else value

    @field_validator("model", mode="before")
    @classmethod
    def _current_model(cls, value):
        return RUNTIME_MODEL if value == HARNESS_MODEL else value
    temperature: float = Field(0.2, ge=0, le=1, description="For the agents this LLM plugs into (default 0.2)")
    max_tokens: int = Field(2048, ge=64, le=8192, description="Per model turn of those agents (default 2048)")


class MemoryConfig(BaseModel):
    memory: str = "agentcore_kb"
    scope: Literal["per_browser_session"] = "per_browser_session"
    short_term_days: int = 30
    long_term: list[str] = Field(["semantic facts", "session summaries"])


class RetrieverConfig(BaseModel):
    """At least one of the two searches runs."""

    top_k: int = Field(5, ge=1, le=20, description="Chunks per search after fusion")
    prefetch: int = Field(10, ge=1, le=50, description="Candidates from each search before fusion (at least top_k)")
    rrf_k: int = Field(2, ge=1, le=100, description="Reciprocal rank fusion constant: higher flattens rank differences")
    dense: bool = Field(True, description="Vector (semantic) search")
    full_text: bool = Field(True, description="PostgreSQL full-text (keyword) search")

    @model_validator(mode="after")
    def _one_search(self):
        if not (self.dense or self.full_text):
            raise ValueError("turn on the dense or the full-text search")
        return self


class VectorDbConfig(BaseModel):
    engine: Literal["pgvector"] = "pgvector"
    tables: list[str] = Field(["rag_chunks", "research_chunks"])
    embedding: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 256
    distance: Literal["cosine"] = "cosine"


class WebSearchConfig(BaseModel):
    provider: Literal["tavily"] = "tavily"
    max_results: int = Field(5, ge=1, le=10)
    api_key_parameter: str = Field("/rag-systems/prod/tavily-api-key", description="SSM SecureString name, never the key")


class ScraperConfig(BaseModel):
    formats: list[str] = Field(["html", "pdf"])
    chunk_size: int = 1000
    chunk_overlap: int = 200


ApiToolName = Literal[tuple(api_tools.TOOLS)]


class ApiToolsConfig(BaseModel):
    tools: list[ApiToolName] = Field(list(api_tools.TOOLS), min_length=1,
                                     description="The external APIs the agent may call (app/api_tools)")


class McpToolConfig(BaseModel):
    gateway_arn: str = Field("", description="AgentCore Gateway in front of the MCP server")
    allowed_tools: list[str] = Field([])


@dataclass(frozen=True)
class Component:
    type: str
    title: str
    kind: Kind
    category: str
    description: str
    config: type[BaseModel] = NoConfig
    impl: str | None = None                 # flow/gate: the runtime implementation key
    outcomes: tuple[str, ...] = ()          # routed: one flow edge per outcome
    fan_out: bool = False                   # routes to "delegate:<agent>" targets in parallel (Send)
    inputs: tuple[str, ...] = ()            # resource ports it accepts
    provides: str | None = None             # resource: the port it plugs into
    bounds_loops: bool = False              # a cycle through it is limited by its own counter
    accepts_subflow_payload: bool = False   # a Send to it carries {"task": …}
    runnable: bool = True                   # resource types the runtime cannot build yet are False
    applied: tuple[str, ...] = ()           # settings the runtime reads from the node
    extra: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {"type": self.type, "title": self.title, "kind": self.kind, "category": self.category,
                "description": self.description, "outcomes": list(self.outcomes), "fan_out": self.fan_out,
                "inputs": list(self.inputs), "provides": self.provides, "bounds_loops": self.bounds_loops,
                "runnable": self.runnable, "applied": list(self.applied),
                "config_schema": self.config.model_json_schema()}


OK_ERROR = ("ok", "error")
EVIDENCE_OUTCOMES = ("GOOD_EVIDENCE", "RETRIEVAL_FAILURE", "KNOWLEDGE_GAP", "ALREADY_RESEARCHED",
                     "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE", "error")

COMPONENTS: dict[str, Component] = {c.type: c for c in (
    # control
    Component("start", "Start", "control", "control", "Where the flow begins."),
    Component("end", "End", "control", "control", "Where the flow ends."),
    Component("input_guardrail", "Input guardrail", "gate", "guardrail",
              "Scope check on the question, before the graph runs (guardrails.py). Its scope applies to every "
              "guardrail in the flow; a blocked question never reaches the agents.", InputGuardrailConfig,
              impl="input_guardrail", outcomes=("ALLOW", "BLOCK"),
              applied=("stage", "scope", "block_message", "clarify_message", "output_message", "unavailable_message")),
    # agents and steps
    Component("supervisor", "Supervisor agent", "flow", "agent",
              "Reads the question, assigns tasks to specialists in parallel, joins their findings and writes "
              "the brief answer (the summarizer).", SupervisorConfig, impl="supervisor",
              outcomes=("answer",), fan_out=True, inputs=("llm", "memory"), bounds_loops=True,
              applied=("domain", "instructions", "max_rounds", "answer_word_limit")),
    Component("specialist_agent", "Specialist agent", "flow", "agent",
              "An agent with tools that works on one delegated task and reports back.", SpecialistConfig,
              impl="specialist", inputs=("llm", "memory", "tools"), applied=("role",)),
    Component("subflow", "Subflow", "flow", "agent",
              "A nested flow run as one step (one task per delegation).", SubflowConfig,
              accepts_subflow_payload=True, applied=("subflow",)),
    Component("output_guardrail", "Output guardrail", "flow", "guardrail",
              "Scope check on the draft answer (guardrails.py), with the scope set on the input guardrail.",
              OutputGuardrailConfig, impl="output_guardrail", outcomes=("ALLOW", "BLOCK"), applied=("stage",)),
    Component("answer_evaluator", "Answer evaluator", "flow", "evaluator",
              "Judges the answer against the evidence used: correctness, faithfulness, completeness, citations "
              "(answer_eval.py). Fail → a standard message.", AnswerEvaluatorConfig, impl="answer_evaluator",
              applied=("correctness", "faithfulness", "completeness", "citation_quality")),
    Component("task_start", "Task start", "flow", "step", "Opens one delegated task.", impl="task_start"),
    Component("retrieval_agent", "RAG retrieval agent", "flow", "agent",
              "Searches the knowledge base with hybrid retrieval and drafts findings with [n] citations.",
              impl="retrieval_agent", outcomes=OK_ERROR, inputs=("llm", "memory", "retriever")),
    Component("evidence_evaluator", "Evidence evaluator", "flow", "evaluator",
              "Judges the retrieved evidence (checks + Claude, Pydantic) and routes on its decision; bounds the "
              "rewrite and research loops (evidence.py).", EvidenceEvaluatorConfig, impl="evidence_evaluator",
              outcomes=EVIDENCE_OUTCOMES, bounds_loops=True,
              applied=("good_confidence", "min_similarity", "max_attempts")),
    Component("query_rewriter", "Query rewriter", "flow", "step",
              "Diagnoses why the searches missed and rewrites the queries (once).", impl="query_rewriter",
              outcomes=OK_ERROR, inputs=("llm",)),
    Component("research_agent", "Research agent", "flow", "agent",
              "Searches the web, fetches and extracts candidate sources for a knowledge gap. A gap about live or "
              "structured data finds data sources (APIs, feeds, downloads) instead, which the source validator "
              "profiles for an admin report; nothing is ingested for those.", ResearchAgentConfig,
              impl="research_agent", outcomes=OK_ERROR, inputs=("llm", "memory", "web_search", "scraper"),
              applied=("data_source_searches", "data_source_fetches", "data_source_turns", "max_profiles",
                       "profile_max_age_days")),
    Component("source_validator", "Source validator", "flow", "evaluator",
              "Authority, freshness, consistency, relevance of each candidate (research.py).", SourceValidatorConfig,
              impl="source_validator", outcomes=("accepted", "none", "error"), inputs=("llm",),
              applied=("authority", "freshness", "consistency", "relevance")),
    Component("ingest", "Ingest sources", "flow", "step",
              "Cleans, chunks, embeds and stores validated sources with provenance.", IngestConfig,
              impl="ingest", outcomes=("ingested", "nothing", "error"), inputs=("vector_db", "scraper"),
              applied=("ttl_days",)),
    Component("handoff", "Answer handoff", "flow", "step", "Passes approved findings to the supervisor.",
              impl="handoff"),
    Component("report", "Research report", "flow", "step", "Reports a gap research could not close.",
              impl="report"),
    Component("placeholder", "Placeholder", "flow", "step", "Stands in for an agent not built yet.",
              PlaceholderConfig, impl="placeholder", applied=("message",)),
    Component("task_finish", "Task finish", "flow", "step", "Closes the task and returns its finding.",
              impl="task_finish"),
    # resources
    Component("llm", "LLM", "resource", "model",
              "The model an agent reasons with. Temperature and max tokens apply to the agents it plugs into, on "
              "either agent runtime; the evaluators and the query rewriter keep temperature 0 for structured "
              "output.", LlmConfig, provides="llm", applied=("provider", "model", "temperature", "max_tokens")),
    Component("memory", "Memory", "resource", "memory",
              "Each agent's conversation per browser session: PostgreSQL on the local runtime, AgentCore Memory "
              "(with long-term facts and summaries) on AgentCore.", MemoryConfig, provides="memory"),
    Component("retriever", "Retriever", "resource", "data", "Hybrid search: dense + full-text, fused by RRF.",
              RetrieverConfig, provides="retriever", inputs=("vector_db",),
              applied=("top_k", "prefetch", "rrf_k", "dense", "full_text")),
    Component("vector_db", "Vector DB", "resource", "data", "pgvector tables of embedded chunks.", VectorDbConfig,
              provides="vector_db"),
    Component("web_search", "Web search", "resource", "tool", "Web search for the research agent.", WebSearchConfig,
              provides="web_search", applied=("max_results",)),
    Component("scraper", "Scraper", "resource", "tool", "Fetch and extract HTML and PDF.", ScraperConfig,
              provides="scraper"),
    Component("api_tools", "API tools", "resource", "tool", "External APIs from the api_tools registry.",
              ApiToolsConfig, provides="tools", applied=("tools",)),
    Component("mcp_tool", "MCP tool", "resource", "tool", "Tools from an MCP server behind an AgentCore Gateway.",
              McpToolConfig, provides="tools", runnable=False),
)}

"""Generic agents (#66): one Agent component whose behaviour comes from what is
connected to it. agent.py runs them; this module reads them from a flow and
holds what does not need a run: each agent's tools and prompt, and the checks.

    tree(spec)          {node id: Agent} for every agent in the flow
    tools(agent, tree)  the tools an agent is offered (from its ports and sub-agents)
    prompt(agent, tree) its instructions, sub-agents and word limit
    run_check(...)      one evaluator on an agent's result: (passed, feedback, details)

An agent plugged into another agent's `agents` port is a sub-agent: its parent
can hand it a task with the tool `ask_<id>`. Evaluators plugged into an
agent's `checks` port check that agent's result before it goes up or out.
"""

from dataclasses import dataclass, field

import answer_eval
import api_tools
import contracts
import evidence
import flows
import llm

SUBAGENT_TOOL = "ask_"   # + the sub-agent's id
# The components that run as one generic agent with sub-agents: the Agent, a Planner (R31), a Blackboard (R33).
AGENT_TYPES = ("agent", "planner", "blackboard")


@dataclass
class Agent:
    id: str
    name: str
    config: dict                        # AgentConfig, defaults filled
    ports: set = field(default_factory=set)            # resource types plugged in
    api_tools: set = field(default_factory=set)        # chosen API tools (from an API tools node)
    subagents: list = field(default_factory=list)      # child agent ids, in edge order
    checks: list = field(default_factory=list)         # [{"name", "config"}]
    parent: str | None = None
    llm: dict | None = None                            # the plugged LLM's settings, if any
    collection: str = ""                               # its Retriever's collection (R35); "": every one it may see
    remote: bool = False                               # an A2A agent elsewhere (R36): config has its url
    mcp: list = field(default_factory=list)            # its MCP tool nodes' settings (R38): server, tools, max_calls


def tree(spec: flows.FlowSpec) -> dict[str, Agent]:
    """Every agent node of the flow's main graph, with what is connected to it."""
    agents: dict[str, Agent] = {}
    for node in spec.nodes:
        if node.type in AGENT_TYPES or node.type == "remote_agent":   # a planner (R31), a blackboard (R33)
            config = flows.COMPONENTS[node.type].config.model_validate(node.config).model_dump()
            agents[node.id] = Agent(node.id, flows.compiler.node_name(node), config,
                                    remote=node.type == "remote_agent")   # and an A2A agent elsewhere (R36)
    for edge in spec.edges:
        if edge.kind != "resource" or edge.target not in agents:
            continue
        source = spec.node(edge.source)
        target = agents[edge.target]
        if source is None:
            continue
        if source.type in AGENT_TYPES or source.type == "remote_agent":
            target.subagents.append(source.id)
            agents[source.id].parent = target.id
        elif source.type == "check":
            target.checks.append({"name": flows.compiler.node_name(source),
                                  "config": flows.COMPONENTS["check"].config.model_validate(source.config).model_dump()})
        else:
            target.ports.add(source.type)
            if source.type == "api_tools":
                target.api_tools |= set(flows.COMPONENTS["api_tools"].config.model_validate(source.config).tools)
            if source.type == "llm":
                target.llm = flows.COMPONENTS["llm"].config.model_validate(source.config).model_dump()
            if source.type == "mcp_tool":
                target.mcp.append(flows.COMPONENTS["mcp_tool"].config.model_validate(source.config).model_dump())
            if source.type == "retriever":
                target.collection = flows.COMPONENTS["retriever"].config.model_validate(source.config).collection
    return agents


def top(agents: dict[str, Agent]) -> list[str]:
    return [a.id for a in agents.values() if a.parent is None]


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    return text.split(". ")[0][:200] if text else ""


def tools(agent: Agent, agents: dict[str, Agent], roles: dict) -> list[dict]:
    """The agent's tools: what its ports give it, then one per sub-agent."""
    out = []
    if "retriever" in agent.ports:
        out += roles["knowledge_base"]["tools"][:1]                       # search_knowledge_base
    research_tools = {t["name"]: t for t in roles["research"]["tools"]}
    if "web_search" in agent.ports:
        out.append(research_tools["web_search"])
    if "scraper" in agent.ports:
        out.append(research_tools["fetch_source"])
    out += [t.spec() for name, t in api_tools.all_tools().items() if name in agent.api_tools]   # with imported ones (R39)
    if agent.mcp:   # tools from MCP servers (R38): the enabled ones only
        import mcp_integration
        out += [entry["spec"] for entry in mcp_integration.offered(agent.mcp).values()]
    for child_id in agent.subagents:
        child = agents[child_id]
        what = _first_sentence(child.config.get("instructions")) or "answers tasks in its area"
        out.append({"name": f"{SUBAGENT_TOOL}{child_id}", "config": {"inlineFunction": {
            "description": f"Hand a self-contained task to {child.name} ({what}). It works on it with its own "
                           "tools and reports back with a status (done, partial, failed or needs input), its "
                           "findings citing [n] where it used the knowledge base, and what is missing.",
            "inputSchema": contracts.task_schema()}}})
    return out


TOP_PROMPT = (
    "You are {name}, and you answer the user yourself. Use your tools and sub-agents as the question needs: "
    "search the knowledge base first when you have it, rephrasing and searching again if the results don't "
    "cover the question. Answer only from what your tools and sub-agents return, citing knowledge-base chunks "
    "by their [n] numbers right after the facts they support; keep the [n] numbers a sub-agent cites. Facts "
    "from other tools or agents elsewhere have no [n]: name their source in the sentence instead (for "
    "example 'according to DeepWiki'), and add no conversions or figures they didn't give. Never "
    "invent facts, numbers or links. If nothing answers the question, say so plainly in one or two sentences. "
    "Write a short, direct answer with a hard limit of {words} words, with no sentence about how you found it.")
SUB_PROMPT = (
    "You are {name}, a sub-agent: another agent gives you a task, and you report back to it. Use your tools "
    "and sub-agents for the task; report only what they return, citing knowledge-base chunks by their [n] "
    "numbers right after the facts they support. Be concise (a hard limit of {words} words) and do not write "
    "to the end user. " + contracts.SUB_RULES)


def prompt(agent: Agent, agents: dict[str, Agent]) -> str:
    template = SUB_PROMPT if agent.parent else TOP_PROMPT
    text = template.format(name=agent.name, words=agent.config.get("answer_word_limit") or 120)
    instructions = (agent.config.get("instructions") or "").strip()
    if instructions:
        text += f"\n\n{instructions}"
    if agent.subagents:
        team = "; ".join(f"{agents[c].name} ({SUBAGENT_TOOL}{c})" for c in agent.subagents)
        text += (f"\n\nYour sub-agents: {team}. Give each a self-contained task (a goal, and inputs and "
                 "constraints when they matter) when the question needs what it knows; independent tasks can go "
                 "out in the same turn. Each reports a status: act on it as its report says.")
    return text


# ---------- checks ----------

CRITERIA_SYSTEM = ("You check an assistant's result against criteria. Judge only what is asked; be strict but fair. "
                   "Call record_verdict once.")
CRITERIA_SCHEMA = {"type": "object", "properties": {
    "passed": {"type": "boolean", "description": "Whether the result meets every criterion"},
    "feedback": {"type": "string", "description": "What is missing or wrong, in one or two sentences (empty if passed)"}},
    "required": ["passed", "feedback"]}


def check_criteria(task: str, result: str, criteria: str) -> tuple[bool, str, dict]:
    reply = llm.call_tool(f"Task:\n{task}\n\nResult:\n{result}\n\nCriteria:\n{criteria}", system=CRITERIA_SYSTEM,
                          name="record_verdict", description="Record whether the result meets the criteria",
                          schema=CRITERIA_SCHEMA, max_tokens=400)
    verdict = reply.tool_input
    return bool(verdict.get("passed")), str(verdict.get("feedback") or "")[:600], {}


def run_check(check: dict, task: str, result: str, chunks: list[dict], api_results: list[dict],
              searches: list[dict], answer_assessor=answer_eval.assess_with_llm,
              evidence_assessor=evidence.assess_with_llm, criteria=None) -> tuple[bool, str, dict]:
    """(passed, feedback for a retry, details): one evaluator on one result."""
    config = check["config"]
    if config["checks"] == "answer":
        verdict = answer_eval.evaluate(task, result, chunks, api_results, assessor=answer_assessor)
        feedback = "; ".join(filter(None, [
            f"failed on {', '.join(verdict.failed_on)}" if verdict.failed_on else "",
            ("unsupported: " + "; ".join(verdict.unsupported_claims[:3])) if verdict.unsupported_claims else "",
            ("missing: " + "; ".join(verdict.missing_parts[:3])) if verdict.missing_parts else "",
            verdict.rationale]))
        return verdict.passed, feedback, {"overall": verdict.overall, "failed_on": verdict.failed_on}
    if config["checks"] == "evidence":
        verdict = evidence.evaluate(task, searches, result, assessor=evidence_assessor)
        passed = verdict.decision == evidence.Decision.GOOD_EVIDENCE
        feedback = "; ".join(filter(None, [verdict.recommended_action,
                                           "missing: " + "; ".join(verdict.missing_information[:3])
                                           if verdict.missing_information else ""]))
        return passed, feedback, {"decision": verdict.decision.value, "confidence": verdict.overall_confidence}
    return (criteria or check_criteria)(task, result, config["criteria"])

"""Architectures (#69): ready-made agent flows built from the generic Agent, its
sub-agents, evaluators and tools (#66), for the Agent Builder's gallery.

Each entry says what it is for and how it flows; `flow` is a complete flow the
builder applies to the canvas (None for patterns the runtime cannot run yet,
which the gallery shows as coming soon). Every flow here validates and runs
(test_flows checks it). An agent is described as a small dict:

    {"id", "label", "instructions", "tools": ["retriever", "web_search", ...],
     "checks": [{"id", "label", "checks", "criteria"?, "max_attempts"?}], "subs": [agents]}
"""

from copy import deepcopy

KB = ["retriever"]
WEB = ["web_search", "scraper"]
LIVE = ["api_tools"]


def _agent(id_, label, instructions, tools=(), checks=(), subs=()):
    return {"id": id_, "label": label, "instructions": instructions, "tools": list(tools),
            "checks": list(checks), "subs": list(subs)}


def _check(id_, label, checks="answer", criteria="", max_attempts=2):
    out = {"id": id_, "label": label, "checks": checks, "max_attempts": max_attempts}
    if criteria:
        out["criteria"] = criteria
    return out


def _flow(top: dict, answer_check: bool = True) -> dict:
    """The main path (guardrails around the top agent, the answer evaluator) and
    every agent's tools, checks and sub-agents as plugged-in nodes."""
    nodes = [{"id": "start", "type": "start"},
             {"id": "input_guardrail", "type": "input_guardrail", "config": {"stage": "input"}},
             {"id": "output_guardrail", "type": "output_guardrail", "config": {"stage": "output"}},
             {"id": "end", "type": "end"}, {"id": "llm", "type": "llm"}]
    edges = [{"source": "start", "target": "input_guardrail"},
             {"source": "input_guardrail", "target": top["id"], "outcome": "ALLOW"},
             {"source": "input_guardrail", "target": "end", "outcome": "BLOCK"},
             {"source": top["id"], "target": "output_guardrail"},
             {"source": "output_guardrail", "target": "end", "outcome": "BLOCK"}]
    if answer_check:
        nodes.append({"id": "answer_evaluator", "type": "answer_evaluator"})
        edges += [{"source": "output_guardrail", "target": "answer_evaluator", "outcome": "ALLOW"},
                  {"source": "answer_evaluator", "target": "end"}]
    else:
        edges.append({"source": "output_guardrail", "target": "end", "outcome": "ALLOW"})

    def add(agent: dict, parent: str | None) -> None:
        nodes.append({"id": agent["id"], "type": "agent", "label": agent["label"],
                      "config": {"instructions": agent["instructions"]}})
        edges.append({"source": "llm", "target": agent["id"], "kind": "resource"})
        if parent:
            edges.append({"source": agent["id"], "target": parent, "kind": "resource", "port": "agents"})
        for tool in agent["tools"]:
            tool_id = f"{agent['id']}_{tool}"
            nodes.append({"id": tool_id, "type": tool})
            edges.append({"source": tool_id, "target": agent["id"], "kind": "resource"})
        for check in agent["checks"]:
            config = {k: v for k, v in check.items() if k not in ("id", "label")}
            nodes.append({"id": check["id"], "type": "check", "label": check["label"], "config": config})
            edges.append({"source": check["id"], "target": agent["id"], "kind": "resource"})
        for sub in agent["subs"]:
            add(sub, agent["id"])

    add(top, None)
    return {"id": "architecture", "name": "Architecture", "version": 1, "state": "main", "description": "",
            "nodes": nodes, "edges": edges, "subflows": {}}


def _entry(id_, name, group, steps, summary, when, top=None, answer_check=True, ready=True):
    return {"id": id_, "name": name, "group": group, "steps": steps, "summary": summary, "when": when,
            "ready": ready, "flow": _flow(top, answer_check) if ready else None}


KB_HELPER = "Searches the knowledge base and reports what it says, with [n] citations; says what is missing."


def architectures() -> list[dict]:
    """The gallery, simplest first. Steps are the pattern in a few words."""
    return deepcopy([
        _entry("tool_agent", "Tool-calling agent", "Single agent",
               ["Question", "Agent", "Tools (search, APIs)", "Answer"],
               "One agent that calls its tools in a loop (search the knowledge base, call an API, look at the "
               "result, decide again) until it can answer.",
               "Most questions about one knowledge base. The fastest and cheapest; start here.",
               _agent("assistant", "Assistant", "Answer from the knowledge base; use the live-data tools when the "
                      "question needs live data.", KB + LIVE)),
        _entry("evaluator_refinement", "Evaluator–refinement", "Single agent",
               ["Generate", "Evaluate", "Retry with feedback", "Answer"],
               "The agent answers; an evaluator checks the answer against the evidence and sends it back with "
               "feedback when it falls short.",
               "When accuracy matters more than speed: each failure costs one more try.",
               _agent("assistant", "Assistant", "Answer from the knowledge base.", KB,
                      [_check("answer_check", "Answer check", "answer", max_attempts=3)])),
        _entry("self_correcting", "Self-correcting", "Single agent",
               ["Execute", "Check the evidence", "Diagnose", "Fix", "Re-check"],
               "Two checks in turn: is the evidence enough, then is the answer right. Each failure comes back "
               "with what was missing, so the agent searches again before it answers again.",
               "Thin or scattered knowledge bases, where the first search often misses.",
               _agent("assistant", "Assistant", "Answer from the knowledge base; when told something is missing, "
                      "search again with other words before answering.", KB,
                      [_check("evidence_check", "Evidence check", "evidence", max_attempts=3),
                       _check("answer_check", "Answer check", "answer", max_attempts=3)])),
        _entry("generator_critic", "Generator–critic", "Single agent",
               ["Generator", "Critic", "Refine", "Answer"],
               "A generator writes; a critic with your own criteria judges it and asks for a better version.",
               "When answers must follow house rules: tone, format, what must always be said.",
               _agent("generator", "Generator", "Answer from the knowledge base.", KB,
                      [_check("critic", "Critic", "criteria", "Answers the question directly in the first sentence, "
                              "cites every fact, and says plainly what it does not know.", max_attempts=3)])),
        _entry("fallback", "Fallback", "Multi-agent",
               ["Primary", "Fails?", "Fallback", "Answer"],
               "A coordinator asks the primary agent (the knowledge base) first, and the fallback agent (the "
               "web) only when the primary cannot answer.",
               "When the knowledge base is the trusted source but questions sometimes go beyond it.",
               _agent("coordinator", "Coordinator", "Always ask Primary first. Only if Primary says it cannot "
                      "answer, ask Fallback, and say the answer comes from the web.", (), (),
                      [_agent("primary", "Primary", KB_HELPER, KB),
                       _agent("fallback", "Fallback", "Searches the web and reads the best pages; reports what "
                              "they say with their links.", WEB)])),
        _entry("supervisor_worker", "Supervisor–worker", "Multi-agent",
               ["Supervisor", "Delegate to specialists", "Aggregate", "Answer"],
               "A supervisor splits the question into tasks for specialist agents, which work in parallel, and "
               "writes one answer from what they report.",
               "Questions that need several kinds of knowledge at once, such as the knowledge base and live data.",
               _agent("supervisor", "Supervisor", "Give each specialist a self-contained task when the question needs "
                      "what it knows, then combine what they report into one answer.", (), (),
                      [_agent("knowledge_base", "Knowledge base", KB_HELPER, KB,
                              [_check("evidence_check", "Evidence check", "evidence")]),
                       _agent("live_data", "Live data", "Gets live data (such as the weather) with its tools.", LIVE)])),
        _entry("router_specialists", "Router–specialists", "Multi-agent",
               ["Router", "Pick one specialist", "Answer"],
               "A router reads the question and sends it to the one specialist that fits; also covers "
               "conditional routing.",
               "Clearly separate areas (e.g. tickets vs. accessibility), where one specialist is enough.",
               _agent("router", "Router", "Send the question to exactly one specialist: the one whose area fits "
                      "best. Answer with what it reports.", (), (),
                      [_agent("specialist_a", "Specialist A", "Covers the first area of the knowledge base. "
                              + KB_HELPER, KB),
                       _agent("specialist_b", "Specialist B", "Covers the second area of the knowledge base. "
                              + KB_HELPER, KB)])),
        _entry("parallel_agents", "Parallel agents", "Multi-agent",
               ["Fan out", "Agents work independently", "Merge"],
               "A coordinator gives the same question to several agents at once, each with its own angle, and "
               "merges their findings; also covers parallel processing.",
               "Broad questions where different angles each find part of the answer.",
               _agent("coordinator", "Coordinator", "Ask every agent in the same turn, each from its own angle, then "
                      "merge what they report, keeping their citations.", (), (),
                      [_agent("facts", "Facts", "Finds the facts and figures. " + KB_HELPER, KB),
                       _agent("rules", "Rules", "Finds the rules, conditions and exceptions. " + KB_HELPER, KB),
                       _agent("contacts", "Contacts", "Finds who to contact and how. " + KB_HELPER, KB)])),
        _entry("sequential_agents", "Sequential agents", "Multi-agent",
               ["Agent A", "Agent B", "Agent C", "Answer"],
               "An orchestrator runs agents in order, each working on the previous one's result: a researcher, "
               "then a writer, then a fact-checker.",
               "Work with clear stages. (The orchestrator passes results along; there is no fixed pipeline yet.)",
               _agent("orchestrator", "Orchestrator", "Run the agents in order: give Researcher the question, give "
                      "Writer what Researcher found, give Fact-checker what Writer wrote together with the "
                      "findings. Answer with Fact-checker's version.", (), (),
                      [_agent("researcher", "Researcher", KB_HELPER, KB),
                       _agent("writer", "Writer", "Writes a short, clear answer from the findings it is given, "
                              "keeping their [n] citations."),
                       _agent("fact_checker", "Fact-checker", "Checks every sentence of a draft against the findings "
                              "it is given; returns the draft with anything unsupported removed.")])),
        _entry("planner_executor_reviewer", "Planner–executor–reviewer", "Multi-agent",
               ["Planner", "Executor", "Reviewer", "Retry or finish"],
               "A planner breaks the question into steps and hands each to an executor; a reviewer checks the "
               "result covers every step and sends it back if not.",
               "Multi-part questions (compare, then recommend; find, then explain).",
               _agent("planner", "Planner", "Break the question into the steps that answer it, give each step to "
                      "Executor in order, then answer from the results.", (),
                      [_check("reviewer", "Reviewer", "criteria", "Covers every part of the question, and each "
                              "part is supported by a citation or a tool result.")],
                      [_agent("executor", "Executor", "Carries out one step with its tools and reports the result. "
                              + KB_HELPER, KB + LIVE)])),
        _entry("research_synthesis", "Research–synthesis", "Multi-agent",
               ["Researchers", "Validate evidence", "Synthesiser", "Answer"],
               "Researchers gather evidence from the knowledge base and the web, each checked, and a synthesiser "
               "writes the answer from what passed.",
               "Questions the knowledge base only partly covers, where the web fills the gaps.",
               _agent("synthesiser", "Synthesiser", "Ask both researchers, then write one answer from what they "
                      "found; say which part comes from the web.", (), (),
                      [_agent("kb_researcher", "Knowledge-base researcher", KB_HELPER, KB,
                              [_check("kb_evidence", "Knowledge-base evidence", "evidence")]),
                       _agent("web_researcher", "Web researcher", "Searches the web and reads the best pages; "
                              "reports what they say with their links.", WEB,
                              [_check("web_sources", "Web sources", "criteria", "Every claim names the page it "
                                      "comes from, and the pages are official or well known.")])])),
        _entry("debate_consensus", "Debate / consensus", "Multi-agent",
               ["Independent agents", "Compare", "Adjudicator"],
               "Several agents answer the same question independently; an adjudicator compares their answers and "
               "decides, noting any disagreement.",
               "High-stakes questions, where agreement between independent answers is worth the cost.",
               _agent("adjudicator", "Adjudicator", "Ask all three analysts the same question in the same turn. "
                      "Compare their answers: keep what they agree on, and for any disagreement go with the "
                      "best-cited one and say it was disputed.", (), (),
                      [_agent("analyst_1", "Analyst 1", KB_HELPER, KB),
                       _agent("analyst_2", "Analyst 2", KB_HELPER, KB),
                       _agent("analyst_3", "Analyst 3", KB_HELPER, KB)])),
        _entry("hierarchical", "Hierarchical delegation", "Multi-agent",
               ["Manager", "Sub-managers", "Specialists", "Aggregate"],
               "A manager delegates to sub-managers, who each run their own specialists; results flow back up.",
               "Large domains with several areas, each with sub-areas.",
               _agent("manager", "Manager", "Give each sub-manager the part of the question in its area, then "
                      "combine what they report.", (), (),
                      [_agent("travel_manager", "Travel manager", "Runs the travel specialists: hand each the part of "
                              "the task it covers and combine what they report.", (), (),
                              [_agent("tickets", "Tickets", "Covers tickets and fares. " + KB_HELPER, KB),
                               _agent("refunds", "Refunds", "Covers refunds and compensation. " + KB_HELPER, KB)]),
                       _agent("help_manager", "Help manager", "Runs the help specialists: hand each the part of the "
                              "task it covers and combine what they report.", (), (),
                              [_agent("accessibility", "Accessibility", "Covers accessibility and assistance. "
                                      + KB_HELPER, KB),
                               _agent("live", "Live", "Gets live data with its tools.", LIVE)])])),
        _entry("human_in_the_loop", "Human-in-the-loop", "Coming soon",
               ["Propose an action", "Human approves", "Execute"],
               "An agent proposes an action and waits for a person to approve it before it runs.",
               "Sensitive or irreversible actions. Needs an approval step in the runtime first.", ready=False),
        _entry("self_learning", "Self-learning", "Coming soon",
               ["Execute", "Evaluate", "Store lessons", "Apply next time"],
               "Lessons from evaluated answers are stored and applied to later questions, after an admin "
               "approves them (#18).",
               "Mature deployments with steady traffic. Agent memory exists; applying lessons does not yet.",
               ready=False),
        _entry("blackboard", "Blackboard / shared state", "Coming soon",
               ["Agents read and write shared state", "Coordinator decides when complete"],
               "Agents work on a shared board instead of passing messages; a coordinator ends the run.",
               "Open-ended work with many contributors. Needs shared state in the runtime first.", ready=False),
    ])

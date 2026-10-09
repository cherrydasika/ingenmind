"""Starting points for a new flow, derived from the built-in flow so they
stay in step with it. Every template runs (test_flows checks it)."""

import copy

from .spec import FlowSpec

RESEARCH_NODES = {"research_agent", "source_validator", "ingest_sources", "web_search", "scraper"}


def _drop(flow: dict, ids: set[str]) -> None:
    flow["nodes"] = [n for n in flow["nodes"] if n["id"] not in ids]
    flow["edges"] = [e for e in flow["edges"] if e["source"] not in ids and e["target"] not in ids]


def _without_specialist(flow: dict, node_id: str, outcome: str) -> None:
    _drop(flow, {node_id})
    flow["edges"] = [e for e in flow["edges"] if e.get("outcome") != outcome]


def _without_research(kb: dict) -> None:
    """A knowledge gap is reported instead of researched."""
    _drop(kb, RESEARCH_NODES)
    kb["edges"].append({"source": "evidence_evaluator", "target": "research_report", "outcome": "KNOWLEDGE_GAP"})


def _strip_positions(flow: dict) -> None:
    for node in flow["nodes"]:
        node.pop("position", None)
    for sub in flow.get("subflows", {}).values():
        _strip_positions(sub)


def templates(builtin: FlowSpec) -> list[dict]:
    """[{"id", "name", "description", "flow"}]"""
    base = builtin.model_dump(mode="json", exclude_none=True)
    _strip_positions(base)

    def make(key: str, name: str, description: str, change=None) -> dict:
        flow = copy.deepcopy(base)
        flow.update(name=name, description=description, version=1)
        if change:
            change(flow)
        return {"id": key, "name": name, "description": description, "flow": flow}

    def rag_research(flow):
        _without_specialist(flow, "external_apis_agent", "delegate:external_apis")
        _drop(flow, {"api_tools"})

    def rag_only(flow):
        rag_research(flow)
        _without_research(flow["subflows"]["knowledge_base"])

    def live_data(flow):
        _without_specialist(flow, "knowledge_base_agent", "delegate:knowledge_base")
        flow["subflows"] = {}

    return [
        make("multi_agent_travel", "Multi-agent travel",
             "The built-in flow: knowledge base with research plus live external APIs, guarded and evaluated."),
        make("rag_research", "RAG + research",
             "Knowledge base only. A knowledge gap triggers web research, validation and ingestion, then "
             "retrieval again.", rag_research),
        make("rag_only", "RAG only",
             "Knowledge base only, no web research: a knowledge gap is reported to the user.", rag_only),
        make("live_data", "Live data only",
             "External APIs only (weather, entry rules, Swiss transport); no knowledge base.", live_data),
    ]

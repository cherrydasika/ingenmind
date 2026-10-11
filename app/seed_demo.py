"""Load the fictional demo documents (data/demo/*.md) into the knowledge
base, so a fresh install has something to answer from.

    docker compose run --rm worker python -m seed_demo

Compose runs it once at startup when COMPOSE_PROFILES includes demo. It is
safe to repeat: an unchanged document only has its lifetime renewed."""

import os
import re
import sys
from pathlib import Path

DEMO_DIR = Path(os.environ.get("DEMO_DIR", "/data/demo"))
TTL_DAYS = 3650

# The demo's own flow: the built-in flow with a brief and scope for its fictional
# operator. Under the built-in UK rail scope, the output guardrail blocked answers
# about Lakeshore Rail ("not a UK train operator").
DEMO_FLOW_ID = "lakeshore_demo"
DEMO_BLUEPRINT = {
    "name": "Lakeshore Rail demo",
    "purpose": "the fictional demo documents in data/demo",
    "flow": {
        "domain": "Lakeshore Rail and the weather",
        "brief": ("An assistant for Lakeshore Rail, a fictional train operator in the fictional Merrow Valley "
                  "(a demo), plus the weather anywhere. Every train question is about Lakeshore Rail unless the "
                  "user clearly names another operator."),
        "supervisor_instructions": (
            "The knowledge base holds Lakeshore Rail's documents: stations and accessible travel, tickets and "
            "fares, refunds and delay compensation, luggage, bicycles and pets, and weather disruption. Send "
            "every train question to knowledge_base. Use external_apis only for the weather. There is no live "
            "timetable: say so when asked for live times."),
        "search_country": "",
        "scope": ("Lakeshore Rail, a fictional demo train operator in the Merrow Valley: its stations, tickets, "
                  "fares, refunds, delays, luggage, bicycles, pets, accessibility and disruption; trains and rail "
                  "travel in general; and the weather anywhere."),
    },
}


def install_demo_flow() -> bool:
    """Publish the demo's flow and make it live, while the built-in flow is live
    (never over a flow someone chose); True when it did."""
    import flows
    from initialization import evaluation
    if flows.store.live_pointer() is not None:
        return False
    builtin = flows.load_flow()
    spec = evaluation.apply_blueprint(builtin, DEMO_BLUEPRINT, DEMO_FLOW_ID)
    spec = spec.model_copy(update={"name": "Lakeshore Rail demo",
                                   "description": "The built-in flow, scoped to the fictional demo operator."})
    try:
        flows.store.create_flow(spec, builtin)
    except flows.store.FlowConflict:
        flows.store.save_draft(spec, builtin)
    version = flows.store.publish(DEMO_FLOW_ID, "The demo's scope", evaluation._check_flow)
    flows.store.set_live(DEMO_FLOW_ID, version, builtin, evaluation._check_flow)
    return True


def documents(directory: Path = DEMO_DIR) -> list[tuple[str, str]]:
    """(title, text) per document: the title is the first "# " line. HTML
    comments are left out: each file's fictional-data notice is for people
    reading it, and the agents would otherwise answer "this place does not
    exist" instead of answering from the text."""
    found = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = re.sub(r"<!--.*?-->\s*", "", path.read_text(encoding="utf-8"), flags=re.DOTALL).strip()
        first, _, body = text.partition("\n")
        if not first.startswith("# "):
            raise ValueError(f"{path} must start with a '# Title' line")
        found.append((first[2:].strip(), body.strip()))
    return found


def main() -> int:
    import knowledge_system
    if not knowledge_system.demo_wanted():
        print("The knowledge system is not the demo (it was reset or set up for a domain): demo not loaded.")
        return 0
    import adhoc_ingest   # here, so documents() needs no ingestion stack (evaluator_probe uses it)
    docs = documents()
    if not docs:
        print(f"No demo documents in {DEMO_DIR}", file=sys.stderr)
        return 1
    for title, text in docs:
        result = adhoc_ingest.ingest(title, text, TTL_DAYS)
        print(f"{result['status']:>24}  {title} ({result['chunks']} chunks)")
    if not knowledge_system.is_ready():
        knowledge_system.mark_ready("demo")   # the demo brings its own knowledge: no setup needed
    if install_demo_flow():
        print(f"The demo's flow ({DEMO_FLOW_ID}) is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

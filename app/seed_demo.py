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
    import adhoc_ingest   # here, so documents() needs no ingestion stack (evaluator_probe uses it)
    docs = documents()
    if not docs:
        print(f"No demo documents in {DEMO_DIR}", file=sys.stderr)
        return 1
    for title, text in docs:
        result = adhoc_ingest.ingest(title, text, TTL_DAYS)
        print(f"{result['status']:>24}  {title} ({result['chunks']} chunks)")
    import knowledge_system
    if not knowledge_system.is_ready():
        knowledge_system.mark_ready("demo")   # the demo brings its own knowledge: no setup needed
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The docs site (R62): MkDocs pages made from README.md's sections, so the
README stays the one source.

    python scripts/docs/build.py          # writes build/docs/ (mkdocs.yml reads it)
    mkdocs build --strict                 # or: mkdocs serve

Each top-level (##) section of the README goes to the page PAGES names. A
new section that no page claims fails the build rather than vanishing.
Links are rewritten for the site: docs/images/… to images/…, a #heading to
the page that now holds it, and repo files (app/slo.py, .env.example…) to
GitHub. docs/upgrading.md, docs/security.md and CHANGELOG.md are pages too.
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "docs"
REPO = "https://github.com/cherrydasika/ingenmind/blob/main/"
# file, title, README ## sections (the README's opening goes to the first page)
PAGES = [
    ("index.md", None, ["Why IngenMind", "Agents all around", "How it works", "You stay in control", "Built for"]),
    ("install.md", "Install", ["Get started: one command, one API key"]),
    ("features.md", "Features", ["Under the hood"]),
    ("configuration.md", "Configuration", ["Configuration at a glance", "Adapting it to your domain"]),
    ("web-app.md", "The web app", ["Web app", "Retrieval", "Ad-hoc documents"]),
    ("pipeline.md", "Ingestion pipeline", ["Adding URLs to ingest", "Running the pipeline"]),
    ("development.md", "Development", ["Development", "Stack", "Getting started", "Observability (Langfuse, optional)",
                                       "Project layout"]),
    ("aws.md", "AWS", ["Accessing the app on AWS"]),
    ("project.md", "Roadmap and licence", ["Roadmap", "License"]),
]
EXTRA = [("upgrading.md", ROOT / "docs" / "upgrading.md"), ("security.md", ROOT / "docs" / "security.md"),
         ("on-call.md", ROOT / "docs" / "on-call.md"), ("launch-checklist.md", ROOT / "docs" / "launch-checklist.md"),
         ("changelog.md", ROOT / "CHANGELOG.md")]


def slug(heading: str) -> str:
    """MkDocs' (Python-Markdown toc) anchor for a heading."""
    text = re.sub(r"[`*_]", "", heading)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def sections(readme: str) -> tuple[str, dict[str, str]]:
    """The opening (title line dropped) and each ## section's text, by heading."""
    parts = re.split(r"(?m)^## (.+)\n", readme)
    opening = re.sub(r"\A# .+\n", "", parts[0])
    return opening, {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def build() -> list[str]:
    readme = (ROOT / "README.md").read_text()
    opening, found = sections(readme)
    claimed = [h for _, _, hs in PAGES for h in hs]
    problems = [f"README section not on any page (add it to PAGES): {h!r}" for h in found if h not in claimed]
    problems += [f"PAGES names a section the README doesn't have: {h!r}" for h in claimed if h not in found]
    if problems:
        return problems
    pages, where = {}, {}
    for file, title, headings in PAGES:
        text = (f"# {title}\n\n" if title else "# IngenMind\n" + opening)
        for heading in headings:
            text += f"\n## {heading}\n{found[heading]}"
        pages[file] = text
        for line in re.findall(r"(?m)^#{1,6} (.+)$", text):
            where.setdefault(slug(line), file)
    for file, source in EXTRA:
        pages[file] = source.read_text()

    def link(match: re.Match) -> str:
        label, target = match[1], match[2]
        if re.match(r"[a-z]+:", target):
            return match[0]
        path, _, anchor = target.partition("#")
        if not path:
            return f"[{label}]({where.get(anchor, '')}#{anchor})"
        if path.startswith("docs/images/"):
            return f"[{label}]({path.removeprefix('docs/')})"
        local = {"docs/upgrading.md": "upgrading.md", "docs/security.md": "security.md", "CHANGELOG.md": "changelog.md",
                 "docs/on-call.md": "on-call.md", "docs/launch-checklist.md": "launch-checklist.md",
                 "README.md": "index.md"}
        if path in local:
            return f"[{label}]({local[path]}{'#' + anchor if anchor else ''})"
        return f"[{label}]({REPO}{path.removeprefix('../')}{'#' + anchor if anchor else ''})"

    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "images").mkdir(parents=True)
    for image in (ROOT / "docs" / "images").iterdir():
        shutil.copy(image, OUT / "images" / image.name)
    for file, text in pages.items():
        (OUT / file).write_text(re.sub(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)", link, text)
                                .replace("](docs/images/", "](images/"))
    return []


if __name__ == "__main__":
    errors = build()
    for error in errors:
        print(error, file=sys.stderr)
    sys.exit(1 if errors else 0)

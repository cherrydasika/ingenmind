"""Paragraph-level deduplication across pages, applied before chunking.

SEO pages repeat the same blocks (railcard blurbs, route conditions, CTAs)
inside otherwise different pages. A whole-page hash can't see that, and
chunk hashes can't either: chunks are fixed-size windows, so the same
paragraph lands at different offsets on different pages. So each paragraph
is normalised and hashed on its own:

- repeated within the same page → later copies dropped;
- already stored by another page → dropped here ("first page wins").

A page's surviving paragraph hashes are stored on its first chunk
(`paragraph_hashes`), which is what later pages are checked against. Only
exact matches after normalisation are removed — near-duplicates on these
pages turned out to be different facts (e.g. different route conditions)."""

import hashlib
import re

from . import config, storage


def normalise(paragraph: str) -> str:
    """Case, punctuation and whitespace don't make a paragraph different."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", paragraph.lower())).strip()


def paragraph_hash(normalised: str) -> str:
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:16]


def dedupe_page(client: storage.PgStore, source: str, text: str) -> tuple[str, list[str], dict]:
    """Returns (text without duplicate paragraphs, hashes this page now owns,
    stats). Paragraphs shorter than DEDUP_MIN_CHARS (headings, labels) are
    always kept — they carry context and aren't worth deduplicating."""
    if not config.DEDUP_ENABLED:
        return text, [], {"removed_within_page": 0, "removed_cross_page": 0, "removed_chars": 0}

    lines = text.split("\n")
    line_hashes: list[str | None] = []
    first_seen: set[str] = set()
    removed_within = 0
    for line in lines:
        norm = normalise(line)
        if len(norm) < config.DEDUP_MIN_CHARS:
            line_hashes.append(None)
            continue
        h = paragraph_hash(norm)
        if h in first_seen:
            line_hashes.append("dup")
            removed_within += 1
        else:
            first_seen.add(h)
            line_hashes.append(h)

    owned_elsewhere = storage.find_paragraph_owners(client, list(first_seen), exclude_source=source)

    kept, removed_chars = [], 0
    for line, h in zip(lines, line_hashes):
        if h == "dup" or (h is not None and h in owned_elsewhere):
            removed_chars += len(line)
            continue
        kept.append(line)

    stats = {
        "removed_within_page": removed_within,
        "removed_cross_page": len(owned_elsewhere),
        "removed_chars": removed_chars,
    }
    return "\n".join(kept).strip(), sorted(first_seen - owned_elsewhere), stats

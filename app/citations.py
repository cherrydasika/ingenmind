"""The passage behind each citation, with the sentences that back the claim
marked (R23), for the Ask page's citation previews.

    passages(answer, searches)   {"<n>": {url, title, site, headings, text, marks}}

Only for an answer users are shown (the caller decides: it passed the
output guardrail and the answer check). Of a cited chunk, the browser gets
its text (at most MAX_CHARS, cut at a sentence), never one that reads like
instructions to an AI (injection.py: the agents never saw those either),
and its page's title and site. Nothing else of the run: no prompts, drafts,
tool replies or uncited chunks.

Marking is lexical and costs no model call: the answer's sentences that
cite [n] are the claim; each sentence of the passage scores by the claim's
content words it shares (numbers count double: fares, times, dates), and
the sentences that share enough, and nearly as much as the best one, are
marked.
"""

import re
from urllib.parse import urlparse

import injection

MAX_CHARS = 1200
MIN_SHARED = 2                # content words a marked sentence shares with the claim
MIN_SHARE = 0.25              # … and its share of the sentence's content words
NEAR_BEST = 0.6               # … and at least this much of the best sentence's score
STOP = set("""a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having
he her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once
only or other our ours out over own same she should so some such than that the their theirs them then there
these they this those through to too under until up very was we were what when where which while who whom why
will with would you your yours get gets got may might must also""".split())
SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|\n|$)")
CITE = re.compile(r"\[(\d+)\]")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9£$€%]+(?:[.:][0-9]+)?", text.lower()) if w not in STOP and len(w) > 1]


def _weight(word: str) -> float:
    return 2.0 if any(ch.isdigit() for ch in word) else 1.0


def claims(answer: str) -> dict[int, str]:
    """Per citation number, the answer's text it is attached to: the sentence
    (or list item) holding it."""
    out: dict[int, list[str]] = {}
    for line in (answer or "").splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9*\"'(])", line):
            for n in {int(x) for x in CITE.findall(sentence)}:
                out.setdefault(n, []).append(CITE.sub("", sentence))
    return {n: " ".join(parts) for n, parts in out.items()}


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence or line of text, trailing spaces excluded."""
    spans = []
    for m in SENTENCE.finditer(text):
        start, end = m.start(), m.end()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def marks(text: str, claim: str) -> list[list[int]]:
    """[start, end] of the passage's sentences that back the claim."""
    wanted = set(_words(claim))
    if not wanted:
        return []
    scored = []
    for start, end in sentences(text):
        words = set(_words(text[start:end]))
        if not words:
            continue
        shared = words & wanted
        weight = sum(_weight(w) for w in shared)
        scored.append((weight, len(shared) / len(words), start, end))
    best = max((w for w, *_ in scored), default=0)
    chosen = [(s, e) for w, share, s, e in scored
              if w >= MIN_SHARED and w >= NEAR_BEST * best and (share >= MIN_SHARE or w == best)]
    return [[s, e] for s, e in sorted(chosen)]


def _trim(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_CHARS:
        return text
    cut = max(text.rfind(". ", 0, MAX_CHARS), text.rfind("\n", 0, MAX_CHARS))
    return text[:cut + 1].rstrip() if cut > MAX_CHARS // 2 else text[:MAX_CHARS].rstrip()


def _site(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def passages(answer: str, searches: list[dict]) -> dict[str, dict]:
    """The cited chunks, keyed by citation number (a string, for JSON)."""
    attached = claims(answer)
    out = {}
    for search in searches:
        for i, chunk in enumerate(search["retrieval"]["chunks"]):
            n = search["first"] + i
            if n not in attached or injection.flags(chunk.get("text") or ""):
                continue
            text = _trim(chunk.get("text") or "")
            url = chunk.get("source_url") or ""
            out[str(n)] = {"url": url, "title": chunk.get("title") or "", "site": _site(url) if url.startswith(("http://", "https://")) else "",
                           "headings": chunk.get("headings") or [], "text": text, "marks": marks(text, attached[n])}
    return out

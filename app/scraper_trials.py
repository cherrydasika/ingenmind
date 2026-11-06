"""Scraper trials: does a new scraping library help, change nothing, or make
things worse? (Monitoring → Scraper trials, admins.)

    candidates()       the libraries that can be tried here (installed ones only)
    TRIED              the libraries tried and removed, with what they did
    start(candidate)   in the background: fetch and extract the same pages with
                       the scraper ingestion uses today (the baseline, "before")
                       and with the candidate ("after"), score both, save the
                       comparison and a verdict
    list_trials()      every trial so far, newest first: the record to look
                       back on whenever a library is tried
    get_trial(id)      one trial: the scores side by side, what changed per
                       page and per question

No model is called, local or hosted: every score is counted from the pages.

    fetch_rate       pages fetched (2xx with a body) / pages tried
    text_rate        pages with extractable text / pages tried
    words            median words of text per page
    boilerplate      share of text in lines repeated on 30%+ of a site's
                     pages (menus, cookie notices, footers): lower is better
    facts            of the evaluation questions with a known page: the share
                     of the expected answer's numbers and key words found in
                     that page's text
    hit_at_5, mrr    keyword search (BM25) over the chunks of every page: is
                     a chunk of the expected page among the first 5, and 1 /
                     its position
    agreement        words shared with the baseline's text (Jaccard): how
                     much the candidate's text differs, not better or worse

The pages: the HTML pages of the latest ingestion plan (MAX_PAGES at most),
every source's home page (sources found but not ingested yet, where a site
blocking bots shows), and the pages evaluation questions name. Both sides
fetch every page once, with the same address checks as ingestion
(scraper.check_url at every redirect) and the same polite delay.

    scraper_trials   one trial: candidate, library versions, both sides'
                     scores, deltas, verdict, per page and per question detail
"""

import importlib.metadata
import importlib.util
import logging
import math
import re
import statistics
import threading
import time
import uuid
from collections import Counter
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

from common import chunking
from common.scraping import scraper
from flows import store

logger = logging.getLogger(__name__)

MAX_PAGES = 120
TOP_K = 5
BOILERPLATE_SHARE = 0.3       # a line on this share of a site's pages (3 pages at least) is boilerplate

# Score → (higher is better, the change that counts). The verdict only looks
# at these; speed is shown but never decides.
QUALITY = {"fetch_rate": (True, 0.01), "text_rate": (True, 0.01), "facts": (True, 0.02),
           "hit_at_5": (True, 0.02), "mrr": (True, 0.02), "boilerplate": (False, 0.02)}

_lock = threading.Lock()
_job: dict | None = None
_ready = False


# ---------- fetchers and extractors ----------
# A candidate is a fetcher and an extractor; each fetches a page once per trial.
# fetch(url) → (status, html); extract(html, url) → text ("" when none).

def _fetch_baseline(url: str) -> tuple[int, str]:
    try:
        return 200, scraper.fetch_html(url)
    except scraper.requests.HTTPError as error:
        return (error.response.status_code if error.response is not None else 0), ""


def _extract_baseline(html: str, url: str) -> str:
    try:
        return scraper.extract_text(html, url, headings=chunking.wants_headings())
    except ValueError:
        return ""


# To try a library: add it to requirements-trials.txt, its fetch(url) → (status,
# html) or extract(html, url) → text here, and a candidate naming them:
#   {"key", "label", "about", "fetch", "extract", "libraries" (for their versions),
#    "modules" (to import: missing ones mean "not installed")}
# Once a library is judged, remove it and record it in TRIED.
FETCHERS = {"requests": _fetch_baseline}
EXTRACTORS = {"trafilatura": _extract_baseline}

BASELINE = {"key": "baseline", "label": "Today's scraper (requests + trafilatura)",
            "fetch": "requests", "extract": "trafilatura", "libraries": ["requests", "trafilatura"]}
CANDIDATES: list[dict] = []

# Libraries tried and removed, newest first: the record every deployment shows,
# whatever trials its own database holds.
TRIED = [
    {"library": "Scrapling", "version": "0.5.1", "tried": "2026-11-06", "verdict": "no_change",
     "candidate": "Scrapling fetcher + trafilatura", "outcome": "removed",
     "summary": "Its Fetcher (curl_cffi with a Chrome browser's TLS fingerprint) returned the same text as "
                "requests on all 102 pages of the Cheshunt knowledge base: fetched 101 / 102, facts 75%, "
                "keyword search 80% on both. broxbourne.moderngov.co.uk still answered 403; getting past it "
                "would need Scrapling's real-browser fetcher (Chromium in the image), not tried."},
    {"library": "Scrapling", "version": "0.5.1", "tried": "2026-11-06", "verdict": "worse",
     "candidate": "requests + Scrapling main content", "outcome": "removed",
     "summary": "Its main-content extraction (the body without noise tags, as markdown) kept menus and "
                "footers: boilerplate 0% → 50%, chunks 235 → 1,351, 36% of the words the same as "
                "trafilatura's. Facts found 75% → 76%, keyword search unchanged at 80%."},
]


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def candidates() -> list[dict]:
    """Every candidate, with whether its libraries are installed here."""
    return [{**{k: c[k] for k in ("key", "label", "about")},
             "available": all(importlib.util.find_spec(m) for m in c["modules"]),
             "versions": {name: _version(name) for name in c["libraries"]}} for c in CANDIDATES]


# ---------- storage ----------

def reset_schema_cache() -> None:
    global _ready
    _ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    with store._connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616721)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS scraper_trials (
                id text PRIMARY KEY,
                candidate text NOT NULL,
                label text NOT NULL,
                note text NOT NULL DEFAULT '',
                started_by text,
                versions jsonb NOT NULL,
                pages int NOT NULL,
                questions int NOT NULL,
                baseline jsonb NOT NULL,
                result jsonb NOT NULL,
                deltas jsonb NOT NULL,
                verdict text NOT NULL CHECK (verdict IN ('better', 'no_change', 'worse')),
                per_page jsonb NOT NULL,
                per_question jsonb NOT NULL,
                seconds real NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
    _ready = True


def list_trials() -> list[dict]:
    ensure_schema()
    with store._connect() as connection:
        return connection.execute("""
            SELECT id, candidate, label, note, started_by, versions, pages, questions, baseline, result,
                   deltas, verdict, seconds, created_at
            FROM scraper_trials ORDER BY created_at DESC""").fetchall()


def get_trial(trial_id: str) -> dict | None:
    ensure_schema()
    with store._connect() as connection:
        return connection.execute("SELECT * FROM scraper_trials WHERE id = %s", (trial_id,)).fetchone()


def delete_trial(trial_id: str) -> None:
    ensure_schema()
    with store._connect() as connection:
        connection.execute("DELETE FROM scraper_trials WHERE id = %s", (trial_id,))


# ---------- the pages and questions ----------

def test_pages(limit: int = MAX_PAGES) -> list[str]:
    """The latest ingestion plan's HTML pages, the sources' home pages and the
    pages questions name, in a stable order, so trials compare."""
    with store._connect() as connection:
        def rows(sql: str) -> list[str]:
            try:
                return [r["url"] for r in connection.execute(sql).fetchall()]
            except Exception:           # the table isn't there yet: a new deployment
                connection.rollback()
                return []
        planned = rows("""SELECT url FROM kb_ingestion_plan_pages
                          WHERE version = (SELECT max(version) FROM kb_ingestion_plan_pages) AND kind = 'html'
                          ORDER BY position""")
        homes = rows("SELECT base_url AS url FROM kb_sources ORDER BY relevance DESC, host")
    named = [q["source"] for q in test_questions()]
    return list(dict.fromkeys(u for u in named + planned[:limit] + homes if not scraper.is_pdf(u)))


def test_questions() -> list[dict]:
    """Questions whose answer's page is known: the evaluation sets' answer
    questions and the search test sets. [{question, expected, source}]"""
    found: list[dict] = []
    with store._connect() as connection:
        for sql in ("SELECT questions FROM agent_eval_sets", "SELECT questions FROM search_eval_sets"):
            try:
                sets = connection.execute(sql).fetchall()
            except Exception:
                connection.rollback()
                continue
            for row in sets:
                for q in row["questions"] or []:
                    source = (q.get("expected_source") or q.get("source") or "").strip()
                    if q.get("question") and source.startswith("http"):
                        found.append({"question": q["question"], "expected": q.get("expected") or q.get("passage") or "",
                                      "source": source})
    return list({(q["question"], q["source"]): q for q in found}.values())


# ---------- scoring ----------

STOP = set("""a an and are as at be by can do does for from has have how i in is it its of on or that the this to
was what when where which who will with you your there their they them than then into about would could should
much many also any each per more most other some such only over under after before""".split())
WORD = re.compile(r"[a-z0-9£$€%][a-z0-9.,£$€%'-]*[a-z0-9%]|[a-z0-9]", re.I)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def words(text: str) -> list[str]:
    return [w.strip(".,").lower() for w in WORD.findall(text or "")]


def fact_score(expected: str, text: str) -> float | None:
    """The share of the expected answer's numbers and key words in the page's text."""
    numbers = {n.rstrip(".,").replace(",", "") for n in NUMBER.findall(expected or "")}
    keys = {w for w in words(expected) if len(w) >= 4 and w not in STOP and not NUMBER.fullmatch(w)}
    if not numbers and not keys:
        return None
    have_numbers = {n.replace(",", "") for n in NUMBER.findall(text or "")}
    have_words = set(words(text))
    found = len(numbers & have_numbers) + len(keys & have_words)
    return found / (len(numbers) + len(keys))


def boilerplate(texts: dict[str, str]) -> dict[str, float]:
    """Per page, the share of its text in lines repeated on BOILERPLATE_SHARE of its site's pages."""
    by_host: dict[str, list[str]] = {}
    for url in texts:
        by_host.setdefault(urlparse(url).hostname or "", []).append(url)
    shares: dict[str, float] = {}
    for urls in by_host.values():
        counts = Counter(line for url in urls for line in {l.strip() for l in texts[url].splitlines() if l.strip()})
        floor = max(3, math.ceil(BOILERPLATE_SHARE * len(urls)))
        for url in urls:
            lines = [l.strip() for l in texts[url].splitlines() if l.strip()]
            total = sum(len(l) for l in lines)
            repeated = sum(len(l) for l in lines if counts[l] >= floor)
            shares[url] = repeated / total if total else 0.0
    return shares


class BM25:
    """Keyword search over chunks, as plain as it gets (k1 1.5, b 0.75)."""

    def __init__(self, docs: list[tuple[str, str]]):
        self.urls = [url for url, _ in docs]
        self.terms = [Counter(w for w in words(body) if w not in STOP) for _, body in docs]
        self.lengths = [sum(t.values()) for t in self.terms]
        self.average = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0
        frequency = Counter(term for t in self.terms for term in t)
        n = len(docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in frequency.items()}

    def search(self, query: str, k: int) -> list[str]:
        query_terms = [w for w in words(query) if w not in STOP and w in self.idf]
        scores = []
        for i, terms in enumerate(self.terms):
            score = sum(self.idf[q] * terms[q] * 2.5 / (terms[q] + 1.5 * (0.25 + 0.75 * self.lengths[i] / self.average))
                        for q in query_terms if terms[q])
            if score:
                scores.append((score, i))
        return [self.urls[i] for _, i in sorted(scores, reverse=True)[:k]]


def score(pages: list[dict], questions: list[dict]) -> tuple[dict, list[dict]]:
    """One side's scores from its pages [{url, status, error, html_bytes, text, fetch_s}], and per question."""
    texts = {p["url"]: p["text"] for p in pages if p["text"]}
    noise = boilerplate(texts)
    chunks = [(url, c["text"]) for url, text in texts.items() for c in chunking.split(text)]
    index = BM25(chunks)
    per_question = []
    for q in questions:
        found = index.search(q["question"], TOP_K)
        rank = next((n for n, url in enumerate(found, 1) if url == q["source"]), None)
        per_question.append({"question": q["question"], "source": q["source"], "rank": rank,
                             "facts": fact_score(q["expected"], texts.get(q["source"], ""))})
    n = len(pages) or 1
    facts = [r["facts"] for r in per_question if r["facts"] is not None]
    times = [p["fetch_s"] for p in pages if p["status"] and 200 <= p["status"] < 300]
    counts = [len(words(t)) for t in texts.values()]
    return {
        "pages": len(pages),
        "fetched": sum(1 for p in pages if p["html_bytes"]),
        "fetch_rate": sum(1 for p in pages if p["html_bytes"]) / n,
        "text_rate": len(texts) / n,
        "words": statistics.median(counts) if counts else 0,
        "boilerplate": statistics.fmean(noise.values()) if noise else 0.0,
        "chunks": len(chunks),
        "facts": statistics.fmean(facts) if facts else None,
        "hit_at_5": (sum(1 for r in per_question if r["rank"]) / len(per_question)) if per_question else None,
        "mrr": (statistics.fmean(1 / r["rank"] if r["rank"] else 0 for r in per_question)) if per_question else None,
        "fetch_s": statistics.median(times) if times else None,
    }, per_question


def verdict(before: dict, after: dict) -> tuple[str, dict]:
    """better: a quality score improved and none got worse; worse: any got worse; else no_change."""
    deltas, better, worse = {}, False, False
    for key in (*QUALITY, "words", "fetch_s", "agreement"):
        if before.get(key) is None or after.get(key) is None:
            continue
        deltas[key] = after[key] - before[key]
        if key in QUALITY:
            higher, matters = QUALITY[key]
            change = deltas[key] if higher else -deltas[key]
            better |= change >= matters
            worse |= change <= -matters
    return ("worse" if worse else "better" if better else "no_change"), deltas


def jaccard(a: str, b: str) -> float:
    x, y = set(words(a)), set(words(b))
    return len(x & y) / len(x | y) if x | y else 1.0


# ---------- running a trial ----------

def _side(spec: dict, urls: list[str], fetched: dict, job: dict) -> list[dict]:
    """Fetch (once per fetcher, shared between sides) and extract every page."""
    fetch, extract = FETCHERS[spec["fetch"]], EXTRACTORS[spec["extract"]]
    cache = fetched.setdefault(spec["fetch"], {})
    out = []
    for url in urls:
        if url not in cache:
            started = time.monotonic()
            try:
                status, html = fetch(url)
                error = None if html else f"HTTP {status}"
            except Exception as exc:
                status, html, error = 0, "", f"{type(exc).__name__}: {str(exc)[:200]}"
            cache[url] = (status, html, error, time.monotonic() - started)
            with _lock:
                job["done"] += 1
        status, html, error, seconds = cache[url]
        text = ""
        if html:
            try:
                text = extract(html, url)
            except Exception as exc:
                error = f"extract: {type(exc).__name__}: {str(exc)[:200]}"
        out.append({"url": url, "status": status, "error": error if not text else None,
                    "html_bytes": len(html.encode()), "text": text, "fetch_s": seconds})
    return out


def run(candidate: str, note: str = "", started_by: str | None = None, urls: list[str] | None = None) -> dict:
    """Run a trial now (start() runs it in the background) and save it."""
    spec = next((c for c in CANDIDATES if c["key"] == candidate), None)
    if spec is None:
        raise LookupError(f"no candidate {candidate!r}")
    job = _job if _job and _job.get("candidate") == candidate else {"done": 0}
    started = time.monotonic()
    urls = urls if urls is not None else test_pages()
    if not urls:
        raise ValueError("there are no pages to try: build the knowledge base first")
    questions = [q for q in test_questions() if q["source"] in urls]
    shared_fetchers = {BASELINE["fetch"]} & {spec["fetch"]}
    job.update(phase="fetching", total=len(urls) * (1 if shared_fetchers else 2))
    fetched: dict = {}
    before_pages = _side(BASELINE, urls, fetched, job)
    after_pages = _side(spec, urls, fetched, job)
    job.update(phase="scoring")
    before, before_q = score(before_pages, questions)
    after, after_q = score(after_pages, questions)
    shared = [jaccard(b["text"], a["text"]) for b, a in zip(before_pages, after_pages) if b["text"] or a["text"]]
    after["agreement"] = statistics.fmean(shared) if shared else None
    before["agreement"] = 1.0 if shared else None
    outcome, deltas = verdict(before, after)
    per_page = [{"url": b["url"],
                 "before": {k: b[k] for k in ("status", "error", "html_bytes")} | {"words": len(words(b["text"])),
                                                                                  "fetch_s": round(b["fetch_s"], 2)},
                 "after": {k: a[k] for k in ("status", "error", "html_bytes")} | {"words": len(words(a["text"])),
                                                                                 "fetch_s": round(a["fetch_s"], 2)},
                 "agreement": round(jaccard(b["text"], a["text"]), 3) if b["text"] or a["text"] else None}
                for b, a in zip(before_pages, after_pages)]
    per_question = [{"question": b["question"], "source": b["source"],
                     "before": {"rank": b["rank"], "facts": b["facts"]}, "after": {"rank": a["rank"], "facts": a["facts"]}}
                    for b, a in zip(before_q, after_q)]
    versions = {name: _version(name) for name in dict.fromkeys(BASELINE["libraries"] + spec["libraries"])}
    ensure_schema()
    trial_id = f"trial_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"
    with store._connect() as connection:
        connection.execute("""
            INSERT INTO scraper_trials (id, candidate, label, note, started_by, versions, pages, questions, baseline,
                                        result, deltas, verdict, per_page, per_question, seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (trial_id, candidate, spec["label"], (note or "")[:500], started_by, Jsonb(versions), len(urls),
             len(questions), Jsonb(before), Jsonb(after), Jsonb(deltas), outcome, Jsonb(per_page),
             Jsonb(per_question), time.monotonic() - started))
    return get_trial(trial_id)


def _work(candidate: str, note: str, started_by: str | None) -> None:
    job = _job
    try:
        trial = run(candidate, note, started_by)
        job.update(status="done", phase="done", trial_id=trial["id"], finished_at=time.time())
    except Exception as exc:
        logger.exception("Scraper trial %s failed", candidate)
        job.update(status="failed", error=str(exc), finished_at=time.time())


def start(candidate: str, note: str = "", started_by: str | None = None, thread: bool = True) -> dict:
    """Start a trial in the background; ValueError when one is running or the library isn't installed."""
    global _job
    found = next((c for c in candidates() if c["key"] == candidate), None)
    if found is None:
        raise LookupError(f"no candidate {candidate!r}")
    if not found["available"]:
        raise ValueError(f"{found['label']}: its library isn't installed in this deployment")
    with _lock:
        if _job and _job["status"] == "running":
            raise ValueError("a scraper trial is running already")
        _job = {"status": "running", "phase": "picking pages", "candidate": candidate, "done": 0, "total": 0,
                "started_at": time.time()}
    if thread:
        threading.Thread(target=_work, args=(candidate, note, started_by), name="scraper-trial", daemon=True).start()
    else:
        _work(candidate, note, started_by)
    return status()


def status() -> dict | None:
    with _lock:
        return dict(_job) if _job else None


if __name__ == "__main__":     # python -m scraper_trials <candidate> [note]: run one here and print the verdict
    import sys

    logging.basicConfig(level=logging.INFO)
    found = run(sys.argv[1], " ".join(sys.argv[2:]), "command line")
    print(found["verdict"], {k: round(v, 3) for k, v in found["deltas"].items()})

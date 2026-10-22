"""Search quality on its own (R20): does the search find the right passage,
before any model writes an answer from it? (Evaluations page.)

    generate(count)   in the background: pick `count` passages spread across the
                      knowledge base (as the golden set does), have the model write
                      the everyday question each one answers, and save them as a
                      test set; each question knows its right passage
    add_question()    a real question by hand, with the page that answers it
    start_run(set)    run every question through the search once, then score
                      every combination of settings from those results: no model,
                      no cost
    get_run(id)       per combination: hit rate (the right passage among the
                      results kept) and MRR (1 / its position, 0 when missing),
                      and the questions it missed

Settings compared: meaning-only (dense), keyword-only, or both fused by
reciprocal rank with RRF_K in RRF_KS, each keeping TOP_KS results. Each
combination is scored exactly as retrieval.hybrid_search ranks (candidates
per search: 2 × top_k), from one deep search per question.

A question written from a passage matches that passage (its chunk id). When
the passage has changed since (the page was cut again, e.g. with another
CHUNKING, R21, or removed), a result matches when it holds the passage's text:
at least half of its words, or one whose words come mostly from it and
cover a third of it (the passage cut in two). So sets compare chunkings
fairly. A question added by hand matches any passage of its page.

    search_eval_sets   id, name, questions [{question, chunk_id, source, passage, topic, by}]
    search_eval_runs   one run of a set: the settings, the scores, and per question
                       (as it was then) where the right passage came in each search
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb

import llm
import retrieval
from common import config, embedding, storage

from . import golden, store

COUNTS = (20, 50, 100)
MAX_QUESTIONS = 200
TOP_KS = (3, 5, 8, 10)
RRF_KS = (1, 2, 5, 10, 30, 60)
DEEP = 30                 # results read per search, to say where a missed passage came
EMBED_BATCH = 32

SYSTEM = ("You write test questions for the search of a question-answering assistant, from one passage of its "
          "knowledge base. Write the question a real user would type that this passage answers, in everyday "
          "words: do not copy its distinctive phrases or headings, do not mention 'the passage' or 'the "
          "document', and ask something specific enough that this passage is the best place to answer it. "
          "If the passage holds nothing a user would ask about (navigation, cookie notices, lists of links, "
          "legal boilerplate), say it is not usable.")
SCHEMA = {"type": "object", "properties": {
    "usable": {"type": "boolean", "description": "Whether the passage answers a question a user would ask"},
    "question": {"type": "string", "description": "The user's question (empty when not usable)"}},
    "required": ["usable", "question"]}

_lock = threading.Lock()
_job: dict | None = None      # the one set being written or run being scored
_ready = False


def reset_schema_cache() -> None:
    global _ready
    _ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    with store._connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616720)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS search_eval_sets (
                id text PRIMARY KEY,
                name text NOT NULL,
                questions jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS search_eval_runs (
                id text PRIMARY KEY,
                set_id text NOT NULL REFERENCES search_eval_sets (id) ON DELETE CASCADE,
                set_name text NOT NULL,
                questions int NOT NULL,
                settings jsonb NOT NULL,
                knowledge jsonb NOT NULL,
                cells jsonb NOT NULL,
                per_question jsonb NOT NULL,
                seconds real NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )""")
    _ready = True


# ---------- sets ----------

def list_sets() -> list[dict]:
    ensure_schema()
    with store._connect() as connection:
        return connection.execute("""
            SELECT s.id, s.name, jsonb_array_length(s.questions) AS questions, s.created_at,
                   (SELECT count(*) FROM search_eval_runs r WHERE r.set_id = s.id) AS runs
            FROM search_eval_sets s ORDER BY s.created_at DESC""").fetchall()


def get_set(set_id: str) -> dict | None:
    ensure_schema()
    with store._connect() as connection:
        return connection.execute("SELECT * FROM search_eval_sets WHERE id = %s", (set_id,)).fetchone()


def save_set(name: str, questions: list[dict]) -> dict:
    ensure_schema()
    set_id = f"search_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"
    with store._connect() as connection:
        connection.execute("INSERT INTO search_eval_sets (id, name, questions) VALUES (%s, %s, %s)",
                           (set_id, name[:120], Jsonb(questions)))
    return get_set(set_id)


def add_question(set_id: str, question: str, source: str) -> dict:
    """A real question by hand; it matches any passage of `source` (a page's address)."""
    question, source = str(question or "").strip(), str(source or "").strip()
    if not question or not source:
        raise ValueError("a question and the page that answers it are both needed")
    if len(question) > 1000 or len(source) > 2000:
        raise ValueError("the question or the page address is too long")
    with storage.get_client().connection() as connection:
        known = connection.execute(
            "SELECT 1 FROM rag_chunks WHERE source_url = %s UNION ALL "
            "SELECT 1 FROM research_chunks WHERE source_url = %s LIMIT 1", (source, source)).fetchone()
    if not known:
        raise ValueError("that page isn't in the knowledge base (use its address as Sources lists it)")
    with store._connect() as connection:
        row = connection.execute("""
            UPDATE search_eval_sets SET questions = questions || %s
            WHERE id = %s AND jsonb_array_length(questions) < %s RETURNING id""",
            (Jsonb([{"question": question, "chunk_id": None, "source": source, "passage": "", "topic": "",
                     "by": "hand"}]), set_id, MAX_QUESTIONS)).fetchone()
    if not row:
        raise ValueError(f"no such set, or it has {MAX_QUESTIONS} questions already")
    return get_set(set_id)


def remove_question(set_id: str, index: int) -> dict:
    with store._connect() as connection:
        row = connection.execute("UPDATE search_eval_sets SET questions = questions - %s WHERE id = %s RETURNING id",
                                 (int(index), set_id)).fetchone()
    if not row:
        raise ValueError("no such set")
    return get_set(set_id)


def delete_set(set_id: str) -> None:
    ensure_schema()
    with store._connect() as connection:
        connection.execute("DELETE FROM search_eval_sets WHERE id = %s", (set_id,))


# ---------- writing a set ----------

def write_one(chunk: dict) -> str | None:
    reply = llm.call_tool(f"Passage (from {chunk['url']}):\n{chunk['body'][:golden.PASSAGE_CHARS]}", system=SYSTEM,
                          name="record_question", description="Record the question this passage answers",
                          schema=SCHEMA, max_tokens=300)
    q = reply.tool_input
    question = str(q.get("question") or "").strip()
    return question[:1000] if q.get("usable") and question else None


def _write(count: int, write=write_one) -> None:
    job = _job
    try:
        chunks = golden.candidates()
        if not chunks:
            raise ValueError("the knowledge base has no pages to write questions from yet")
        queues = golden.pick(chunks, count)
        job.update(phase="writing", total=len(queues))
        results: list[dict | None] = [None] * len(queues)

        def attempt(slot: int) -> None:
            for i in queues[slot][:4]:                       # a few tries per cluster
                chunk = chunks[i]
                question = write(chunk)
                with _lock:
                    if question and not any(r and golden._alike(r["question"], question) for r in results):
                        results[slot] = {"question": question, "chunk_id": chunk["id"], "source": chunk["url"],
                                         "passage": chunk["body"][:golden.PASSAGE_CHARS], "topic": chunk["topic"],
                                         "by": "model"}
                        job["done"] += 1
                        return
            with _lock:
                job["skipped"] += 1

        with ThreadPoolExecutor(golden.WORKERS) as pool:
            list(pool.map(attempt, range(len(queues))))
        questions = [r for r in results if r]
        if not questions:
            raise ValueError("no passage had a question to ask")
        saved = save_set(f"Search test ({len(questions)} questions)", questions)
        job.update(status="done", phase="done", set_id=saved["id"], finished_at=time.time())
    except Exception as exc:
        job.update(status="failed", error=str(exc), finished_at=time.time())


def _begin(job: dict) -> None:
    global _job
    with _lock:
        if _job and _job["status"] == "running":
            raise ValueError("a search test is running already")
        _job = job


def generate(count: int, work=_write, thread: bool = True) -> dict:
    """Write a test set of about `count` questions; ValueError when a job is running."""
    if count not in COUNTS:
        raise ValueError(f"count is one of {', '.join(map(str, COUNTS))}")
    ensure_schema()
    _begin({"kind": "write", "status": "running", "phase": "picking", "count": count, "total": count,
            "done": 0, "skipped": 0, "started_at": time.time()})
    if thread:
        threading.Thread(target=work, args=(count,), name="search-test-set", daemon=True).start()
    else:
        work(count)
    return status()


def status() -> dict | None:
    with _lock:
        return dict(_job) if _job else None


# ---------- scoring ----------

def current_settings() -> dict:
    """What the Ask page's search uses: both searches fused, RRF_K, TOP_K results."""
    return {"mode": "hybrid", "rrf_k": retrieval.RRF_K, "top_k": retrieval.TOP_K}


def combinations() -> list[dict]:
    """The settings compared: rows (method, RRF_K) × columns (results kept)."""
    rows = [{"mode": "dense", "rrf_k": None}, {"mode": "keyword", "rrf_k": None}]
    rows += [{"mode": "hybrid", "rrf_k": k} for k in RRF_KS]
    return [{**row, "top_k": top_k} for row in rows for top_k in TOP_KS]


def ranked(dense_hits: list[dict], text_hits: list[dict], mode: str, rrf_k: int | None, top_k: int) -> list[dict]:
    """The results hybrid_search would return with these settings, from deeper rankings."""
    prefetch = top_k * 2
    dense = dense_hits[:prefetch] if mode in ("dense", "hybrid") else []
    text = text_hits[:prefetch] if mode in ("keyword", "hybrid") else []
    return retrieval._fuse(dense, text, top_k, rrf_k or retrieval.RRF_K)[0]


def _words(text: str) -> set[str]:
    return golden._words(text or "")


def holds(hit_text: str, passage: set[str]) -> bool:
    """Whether a result holds a passage's text (after the page was cut again)."""
    words = _words(hit_text)
    shared = len(words & passage)
    return bool(passage) and (shared >= 0.5 * len(passage)
                              or (len(words) >= 15 and shared >= 0.7 * len(words) and shared >= 0.3 * len(passage)))


def position(hits: list[dict], target: dict) -> int | None:
    """1-based position of the right passage among hits, or None."""
    passage = target.get("_words")
    for n, hit in enumerate(hits, 1):
        payload = hit.get("payload") or {}
        if target.get("chunk_id"):
            if hit["id"] == target["chunk_id"]:
                return n
        elif passage:
            if payload.get("source_url") == target["source"] and holds(payload.get("text") or "", passage):
                return n
        elif payload.get("source_url") == target["source"]:
            return n
    return None


def score(questions: list[dict], searches: list[tuple[list[dict], list[dict]]]) -> tuple[list[dict], list[dict]]:
    """(cells, per question) from each question's deep (dense, keyword) results."""
    cells = []
    for combo in combinations():
        found = [position(ranked(d, t, combo["mode"], combo["rrf_k"], combo["top_k"]), q)
                 for q, (d, t) in zip(questions, searches)]
        n = max(1, len(questions))
        cells.append({**combo, "hit_rate": round(sum(1 for p in found if p) / n, 4),
                      "mrr": round(sum(1 / p for p in found if p) / n, 4),
                      "missed": [i for i, p in enumerate(found) if not p]})
    per_question = [{"question": q["question"], "source": q["source"], "by": q.get("by"), "gone": bool(q.get("gone")),
                     "dense": position(d, q), "keyword": position(t, q),
                     "current": position(ranked(d, t, "hybrid", retrieval.RRF_K, retrieval.TOP_K), q)}
                    for q, (d, t) in zip(questions, searches)]
    return cells, per_question


def _targets(questions: list[dict]) -> list[dict]:
    """A question whose passage changed since (a chunk id is reused when a page
    is cut again) is matched by the passage's text instead."""
    ids = [q["chunk_id"] for q in questions if q.get("chunk_id")]
    bodies = {}
    if ids:
        with storage.get_client().connection() as connection:
            bodies = {str(r["id"]): r["body"] for r in connection.execute(
                "SELECT id, body FROM rag_chunks WHERE id = ANY(%s::uuid[]) AND expires_at > now() UNION ALL "
                "SELECT id, body FROM research_chunks WHERE id = ANY(%s::uuid[]) AND expires_at > now()",
                (ids, ids)).fetchall()}
    out = []
    for q in questions:
        passage = q.get("passage") or ""
        same = q.get("chunk_id") in bodies and bodies[q["chunk_id"]][:len(passage)] == passage
        if q.get("chunk_id") and not same:
            q = {**q, "chunk_id": None, "gone": True}
        out.append({**q, "_words": _words(passage) if q.get("gone") else None})
    return out


def _run(set_row: dict) -> None:
    job = _job
    try:
        started = time.perf_counter()
        questions = _targets(set_row["questions"])
        client = storage.get_client()
        searches = []
        for start in range(0, len(questions), EMBED_BATCH):
            batch = questions[start:start + EMBED_BATCH]
            vectors = embedding.embed_texts([q["question"] for q in batch], query=True)
            for q, vector in zip(batch, vectors):
                searches.append((storage.search_dense(client, vector, DEEP), storage.search_text(client, q["question"], DEEP)))
                job["done"] += 1
        cells, per_question = score(questions, searches)
        run_id = f"sr_{uuid.uuid4().hex[:12]}"
        with store._connect() as connection:
            chunks = connection.execute("SELECT count(*) AS n FROM rag_chunks WHERE expires_at > now()").fetchone()["n"]
            chunking = {r["chunking"]: r["n"] for r in connection.execute(
                "SELECT coalesce(payload->>'chunking', 'fixed') AS chunking, count(*) AS n FROM rag_chunks "
                "WHERE expires_at > now() GROUP BY 1").fetchall()}
            connection.execute("""
                INSERT INTO search_eval_runs (id, set_id, set_name, questions, settings, knowledge, cells,
                                              per_question, seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, set_row["id"], set_row["name"], len(questions),
                 Jsonb({"top_k": retrieval.TOP_K, "rrf_k": retrieval.RRF_K, "top_ks": TOP_KS, "rrf_ks": RRF_KS}),
                 Jsonb({"chunks": chunks, "embedding_model": config.EMBEDDING_MODEL, "chunking": chunking,
                        "gone": sum(1 for q in questions if q.get("gone"))}),
                 Jsonb(cells), Jsonb(per_question), time.perf_counter() - started))
        job.update(status="done", phase="done", run_id=run_id, finished_at=time.time())
    except Exception as exc:
        job.update(status="failed", error=str(exc), finished_at=time.time())


def start_run(set_id: str, work=_run, thread: bool = True) -> dict:
    set_row = get_set(set_id)
    if not set_row:
        raise LookupError("no such set")
    if not set_row["questions"]:
        raise ValueError("the set has no questions")
    _begin({"kind": "run", "status": "running", "phase": "searching", "set_id": set_id,
            "total": len(set_row["questions"]), "done": 0, "started_at": time.time()})
    if thread:
        threading.Thread(target=work, args=(set_row,), name="search-test-run", daemon=True).start()
    else:
        work(set_row)
    return status()


def list_runs(limit: int = 50) -> list[dict]:
    ensure_schema()
    with store._connect() as connection:
        rows = connection.execute("""
            SELECT id, set_id, set_name, questions, settings, knowledge, cells, seconds, created_at
            FROM search_eval_runs ORDER BY created_at DESC LIMIT %s""", (limit,)).fetchall()
    out = []
    for r in rows:
        current = next((c for c in r["cells"] if c["mode"] == "hybrid" and c["rrf_k"] == r["settings"]["rrf_k"]
                        and c["top_k"] == r["settings"]["top_k"]), None)
        best = max(r["cells"], key=lambda c: (c["hit_rate"], c["mrr"]))
        out.append({k: r[k] for k in ("id", "set_id", "set_name", "questions", "knowledge", "seconds", "created_at")}
                   | {"current": _headline(current), "best": _headline(best)})
    return out


def _headline(cell: dict | None) -> dict | None:
    return cell and {k: cell[k] for k in ("mode", "rrf_k", "top_k", "hit_rate", "mrr")}


def get_run(run_id: str) -> dict | None:
    ensure_schema()
    with store._connect() as connection:
        return connection.execute("SELECT * FROM search_eval_runs WHERE id = %s", (run_id,)).fetchone()

"""A golden question set written from the knowledge base (Evaluations page).

    start(count)   in the background: pick `count` passages spread across the
                   knowledge base, then write one question and its expected
                   answer from each
    status()       the run: its progress, then the questions, a map of where
                   they come from and how evenly they spread over the topics

Picking passages, so the set covers the knowledge base instead of clustering:
chunks of usable length are grouped into `count` clusters by their embeddings
(k-means, cosine), and each cluster gives the chunk nearest its centre: a
typical passage of a different part of the knowledge base. At most one
passage per page while there are enough pages (else an even share each), and
no topic takes more than about twice its fair share.
A passage the model finds unusable (a menu, a cookie notice), or whose
question repeats an earlier one, is swapped for the next one in its cluster.

A chunk's topic: its labelled topic (meta.topic, named from the blueprint),
else the section setup read it under, else its site.

One run at a time, kept in memory: the set is the user's to review, trim and
save (as an evaluation set) or throw away.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import numpy as np

import llm
from common import storage

COUNTS = (5, 10, 20, 50, 100)
MIN_CHARS = 300          # shorter chunks rarely answer a question on their own
MAX_CANDIDATES = 6000    # chunks read for the clustering (a random sample beyond that)
MAP_BACKGROUND = 600     # chunks drawn grey on the map
WORKERS = 4
SIMILAR = 0.6            # word overlap above which two questions count as the same
PASSAGE_CHARS = 2500

SYSTEM = ("You write evaluation questions for a question-answering assistant from one passage of its knowledge "
          "base. Write the question a real user would ask that this passage answers, in their words (do not "
          "quote the passage or mention 'the passage' or 'the document'), and the expected answer: short, "
          "complete and taken only from the passage. If the passage holds nothing a user would ask about "
          "(navigation, cookie notices, lists of links, legal boilerplate), say it is not usable.")
SCHEMA = {"type": "object", "properties": {
    "usable": {"type": "boolean", "description": "Whether the passage answers a question a user would ask"},
    "question": {"type": "string", "description": "The user's question (empty when not usable)"},
    "expected": {"type": "string", "description": "The expected answer, from the passage only (empty when not usable)"},
    "topic": {"type": "string", "description": "What the question is about, in two to four words"}},
    "required": ["usable", "question", "expected", "topic"]}

_lock = threading.Lock()
_run: dict | None = None


# ---------- reading the knowledge base ----------

def _topic_names() -> dict[str, str]:
    try:
        from initialization import labels
        bp = labels.blueprint_or_none() or {}
    except Exception:
        bp = {}
    return {a["key"]: a["name"] for a in bp.get("knowledge_areas") or []}


def _readable(name: str) -> str:
    """A slug ("refunds-and-delays", "station_facilities") as words."""
    name = str(name).strip()
    if " " not in name and re.search(r"[-_]", name):
        name = re.sub(r"[-_]+", " ", name)
        return name[:1].upper() + name[1:]
    return name


def _topic(payload: dict, url: str, names: dict) -> str:
    topics = ((payload or {}).get("meta") or {}).get("topic") or (payload or {}).get("areas") or []
    if topics:
        return names.get(topics[0], _readable(topics[0]))
    if (payload or {}).get("section"):
        return _readable(payload["section"])
    return _readable(urlparse(url).hostname or "other")   # an ad-hoc document's name, or the site


def candidates() -> list[dict]:
    """Chunks of usable length: {id, url, body, topic, vector (unit length)}."""
    client = storage.get_client()
    names = _topic_names()
    with client.connection() as connection:
        rows = connection.execute(
            f"SELECT id, source_url, body, embedding, payload FROM {client.table} "
            "WHERE expires_at > now() AND length(body) >= %s ORDER BY random() LIMIT %s",
            (MIN_CHARS, MAX_CANDIDATES)).fetchall()
    out = []
    for r in rows:
        raw = r["embedding"]
        vector = np.asarray(raw.to_numpy() if hasattr(raw, "to_numpy") else raw, dtype=np.float32)   # pgvector's Vector
        norm = float(np.linalg.norm(vector))
        if norm:
            out.append({"id": str(r["id"]), "url": r["source_url"], "body": r["body"],
                        "topic": _topic(r["payload"], r["source_url"], names), "vector": vector / norm})
    return out


# ---------- picking passages ----------

def _kmeans(x: np.ndarray, k: int, rounds: int = 25, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(labels, centres) for unit vectors x: cosine k-means from a k-means++ start."""
    rng = np.random.default_rng(seed)
    centres = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        far = 1 - np.max(x @ np.stack(centres).T, axis=1)
        far = np.clip(far, 0, None) ** 2
        centres.append(x[rng.choice(len(x), p=far / far.sum())] if far.sum() else x[rng.integers(len(x))])
    centres = np.stack(centres)
    labels = np.zeros(len(x), dtype=int)
    for step in range(rounds):
        new = np.argmax(x @ centres.T, axis=1)
        if step and np.array_equal(new, labels):
            break
        labels = new
        for c in range(k):
            members = x[labels == c]
            if len(members):
                centre = members.sum(axis=0)
                centres[c] = centre / (np.linalg.norm(centre) or 1)
    return labels, centres


def pick(chunks: list[dict], count: int) -> list[list[int]]:
    """For each of `count` clusters, its chunks nearest the centre first (indexes
    into chunks). Pages share the questions evenly (one each when there are
    enough pages), and no topic takes beyond about twice its fair share."""
    if not chunks:
        return []
    x = np.stack([c["vector"] for c in chunks])
    k = min(count, len(chunks))
    page_cap = int(np.ceil(k / len({c["url"] for c in chunks})))
    labels, centres = _kmeans(x, k)
    queues = []
    for c in range(k):
        members = np.where(labels == c)[0]
        order = members[np.argsort(-(x[members] @ centres[c]))]
        if len(order):
            queues.append(([int(i) for i in order], centres[c]))
    # Biggest clusters choose first. A cluster whose passages all sit on pages or
    # topics that have their share takes the nearest passage that fits, from anywhere.
    queues.sort(key=lambda q: len(q[0]), reverse=True)
    topics = {c["topic"] for c in chunks}
    cap = max(2, int(np.ceil(2 * k / max(1, len(topics)))))
    pages, per_topic, used, out = {}, {}, set(), []

    def fits(i: int) -> bool:
        return (i not in used and pages.get(chunks[i]["url"], 0) < page_cap
                and per_topic.get(chunks[i]["topic"], 0) < cap)

    for queue, centre in queues:
        ordered = [i for i in queue if fits(i)]
        if not ordered:
            ordered = [int(i) for i in np.argsort(-(x @ centre)) if fits(int(i))][:len(queue)] or queue
        seen = set(ordered) | used
        ordered += [i for i in queue if i not in seen]
        first = chunks[ordered[0]]
        used.add(ordered[0])
        pages[first["url"]] = pages.get(first["url"], 0) + 1
        per_topic[first["topic"]] = per_topic.get(first["topic"], 0) + 1
        out.append(ordered)
    return out


# ---------- writing questions ----------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _alike(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= SIMILAR


def write_one(chunk: dict) -> dict | None:
    reply = llm.call_tool(f"Passage (from {chunk['url']}):\n{chunk['body'][:PASSAGE_CHARS]}", system=SYSTEM,
                          name="record_question", description="Record the question this passage answers",
                          schema=SCHEMA, max_tokens=600)
    q = reply.tool_input
    if not q.get("usable") or not str(q.get("question") or "").strip() or not str(q.get("expected") or "").strip():
        return None
    return {"question": str(q["question"]).strip()[:1000], "expected": str(q["expected"]).strip()[:4000],
            "label": str(q.get("topic") or "").strip()[:60]}


def _map(chunks: list[dict], chosen: list[int]) -> list[dict]:
    """2-D positions (PCA, 0–1) of a sample of the knowledge base and the chosen chunks."""
    rng = np.random.default_rng(1)
    rest = [i for i in range(len(chunks)) if i not in set(chosen)]
    background = list(rng.choice(rest, size=min(MAP_BACKGROUND, len(rest)), replace=False)) if rest else []
    index = [int(i) for i in background] + chosen
    if len(index) < 3:
        return []
    x = np.stack([chunks[i]["vector"] for i in index])
    x = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    xy = x @ vt[:2].T
    xy = (xy - xy.min(axis=0)) / np.where(np.ptp(xy, axis=0) == 0, 1, np.ptp(xy, axis=0))
    position = {i: n for n, i in enumerate(chosen)}
    return [{"x": round(float(p[0]), 4), "y": round(float(p[1]), 4), "topic": chunks[i]["topic"],
             "q": position.get(i)} for i, p in zip(index, xy)]


def spread(chunks: list[dict], questions: list[dict], chosen: list[int]) -> dict:
    """Per topic: questions and the knowledge base's share; the flags; the closest pair."""
    total = len(chunks)
    shares = {}
    for c in chunks:
        shares[c["topic"]] = shares.get(c["topic"], 0) + 1
    asked = {}
    for q in questions:
        asked[q["topic"]] = asked.get(q["topic"], 0) + 1
    n = max(1, len(questions))
    topics = []
    for topic in sorted(set(shares) | set(asked), key=lambda t: (-asked.get(t, 0), -shares.get(t, 0), t)):
        share = shares.get(topic, 0) / max(1, total)
        got = asked.get(topic, 0)
        flag = ("cluster" if got >= 3 and got / n > max(2 * share, 1.5 / max(1, len(shares))) else
                "missing" if got == 0 and share >= 0.05 else None)
        topics.append({"topic": topic, "questions": got, "share": round(share, 4), "flag": flag})
    closest = None
    if len(chosen) > 1:
        x = np.stack([chunks[i]["vector"] for i in chosen])
        sims = x @ x.T
        np.fill_diagonal(sims, -1)
        a, b = np.unravel_index(int(np.argmax(sims)), sims.shape)
        closest = {"a": int(a), "b": int(b), "similarity": round(float(sims[a, b]), 3)}
    return {"topics": topics, "covered": sum(1 for t in topics if t["questions"]),
            "of": len(shares), "closest": closest}


def _work(count: int, write=write_one) -> None:
    run = _run
    try:
        chunks = candidates()
        if not chunks:
            raise ValueError("the knowledge base has no pages to write questions from yet")
        queues = pick(chunks, count)
        run.update(phase="writing", total=len(queues))
        results: list[dict | None] = [None] * len(queues)
        position = [0] * len(queues)

        def attempt(slot: int) -> None:
            queue = queues[slot]
            while position[slot] < min(len(queue), 4):           # a few tries per cluster
                chunk = chunks[queue[position[slot]]]
                written = write(chunk)
                with _lock:
                    clash = written and any(r and _alike(r["question"], written["question"]) for r in results)
                    if written and not clash:
                        results[slot] = {**written, "topic": chunk["topic"], "source": chunk["url"],
                                         "chunk": queue[position[slot]], "passage": chunk["body"][:600]}
                        run["done"] += 1
                        return
                position[slot] += 1
            with _lock:
                run["skipped"] += 1

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(attempt, range(len(queues))))
        kept = [r for r in results if r]
        chosen = [r["chunk"] for r in kept]
        questions = [{"question": r["question"], "expected": r["expected"], "topic": r["topic"], "label": r["label"],
                      "source": r["source"], "passage": r["passage"]} for r in kept]
        run.update(status="done", phase="done", questions=questions, map=_map(chunks, chosen),
                   spread=spread(chunks, questions, chosen), pages=len({c["url"] for c in chunks}),
                   chunks=len(chunks), finished_at=time.time())
    except Exception as exc:
        run.update(status="failed", error=str(exc), finished_at=time.time())


def start(count: int, work=_work, thread=True) -> dict:
    """Begin writing a golden set of `count` questions; ValueError when one is running."""
    global _run
    if count not in COUNTS:
        raise ValueError(f"count is one of {', '.join(map(str, COUNTS))}")
    with _lock:
        if _run and _run["status"] == "running":
            raise ValueError("a golden set is being written already")
        _run = {"status": "running", "phase": "picking", "count": count, "total": count, "done": 0, "skipped": 0,
                "started_at": time.time()}
    if thread:
        threading.Thread(target=work, args=(count,), name="golden-set", daemon=True).start()
    else:
        work(count)
    return status()


def status() -> dict | None:
    with _lock:
        return dict(_run) if _run else None

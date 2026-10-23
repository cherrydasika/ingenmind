"""Knowledge gaps (R54): what people ask that the knowledge base can't answer.

When the evidence evaluator decides KNOWLEDGE_GAP for a task it lists the
missing information. Each item is kept here with the question's session turn
(admins see it on Analytics; the asker never does), and the report groups
similar items into topics:

    knowledge_gaps  turn_id (its session turn: deleted with it), missing (one
                    item), after_research, answered (the answer still passed
                    its check), embedding (filled when the report first needs it)

Topics: items whose embeddings are close (cosine ≥ SIMILAR) to a topic's
centre join it, the largest first; each is named by its most typical item.
Questions blocked as off-topic never ran, so they are never gaps.
"""

import sys
import threading
from datetime import datetime

import numpy as np
import psycopg
from psycopg.rows import dict_row

from common import config

SIMILAR = 0.78
MAX_ITEMS = 8
MAX_ROWS = 5000
EXAMPLES = 3
TOPICS = 50

_schema_ready = False
_schema_lock = threading.Lock()


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    import sessions
    with _schema_lock:
        if _schema_ready:
            return
        sessions.ensure_schema()
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616754)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    id bigserial PRIMARY KEY,
                    turn_id bigint NOT NULL REFERENCES app_session_turns (turn_id) ON DELETE CASCADE,
                    missing text NOT NULL,
                    after_research boolean NOT NULL DEFAULT false,
                    answered boolean NOT NULL DEFAULT false,
                    embedding real[]
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS knowledge_gaps_turn ON knowledge_gaps (turn_id)")
        _schema_ready = True


def of_run(evaluations: list[dict]) -> list[dict]:
    """A run's knowledge gaps from its evidence evaluations: what was missing, and whether research had run."""
    out, seen = [], set()
    for e in evaluations:
        if e.get("decision") != "KNOWLEDGE_GAP":
            continue
        for item in e.get("missing_information") or [""]:
            key = item.strip().lower()
            if key not in seen:
                seen.add(key)
                out.append({"missing": item.strip()[:500], "after_research": bool(e.get("after_research"))})
    return out[:MAX_ITEMS]


def record(turn_id: int, question: str, gaps: list[dict], answered: bool) -> None:
    """Keep a turn's gaps; an item with no text stands for the question itself."""
    if not gaps:
        return
    ensure_schema()
    with _connect() as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO knowledge_gaps (turn_id, missing, after_research, answered) VALUES (%s, %s, %s, %s)",
            [(turn_id, g.get("missing") or question[:500], bool(g.get("after_research")), answered) for g in gaps])


def _embed(connection, rows: list[dict]) -> None:
    """Fill in the embeddings the rows don't have yet (and keep them)."""
    from common.embedding import embed_texts
    todo = [r for r in rows if r["embedding"] is None]
    if not todo:
        return
    vectors = embed_texts([r["missing"] for r in todo])
    with connection.cursor() as cursor:
        cursor.executemany("UPDATE knowledge_gaps SET embedding = %s WHERE id = %s",
                           [(list(map(float, v)), r["id"]) for r, v in zip(todo, vectors)])
    for r, v in zip(todo, vectors):
        r["embedding"] = v


def _groups(rows: list[dict]) -> list[list[dict]]:
    """Rows grouped by meaning (greedy, against each group's running centre); by exact text if no embeddings."""
    if any(r["embedding"] is None for r in rows):
        by_text: dict[str, list[dict]] = {}
        for r in rows:
            by_text.setdefault(r["missing"].strip().lower(), []).append(r)
        return list(by_text.values())
    groups: list[list[dict]] = []
    centres: list[np.ndarray] = []
    for r in rows:
        vector = np.asarray(r["embedding"], dtype=np.float32)
        vector = vector / (np.linalg.norm(vector) or 1.0)
        r["unit"] = vector
        if centres:
            sims = np.stack(centres) @ vector
            best = int(np.argmax(sims))
            if sims[best] >= SIMILAR:
                groups[best].append(r)
                total = sum(g["unit"] for g in groups[best])
                centres[best] = total / (np.linalg.norm(total) or 1.0)
                continue
        groups.append([r])
        centres.append(vector)
    for group, centre in zip(groups, centres):
        group.sort(key=lambda g: -float(g["unit"] @ centre))   # the most typical first
    return groups


def report(begin: datetime, end: datetime) -> dict:
    """The period's gap topics, most asked first: label, questions, still unanswered, example questions."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT g.id, g.turn_id, g.missing, g.after_research, g.answered, g.embedding, t.question, t.asked_at
            FROM knowledge_gaps g JOIN app_session_turns t ON t.turn_id = g.turn_id
            WHERE t.asked_at >= %s AND t.asked_at < %s
              AND coalesce(t.result->'guardrails'->'input'->>'decision', 'ALLOW') = 'ALLOW'
            ORDER BY t.asked_at DESC LIMIT %s""", (begin, end, MAX_ROWS)).fetchall()
        grouped_by = "meaning"
        try:
            _embed(connection, rows)
        except Exception as error:     # no embedding model reachable: group identical items only
            print(f"knowledge gaps not embedded: {error}", file=sys.stderr)
            grouped_by = "text"
    topics = []
    for group in _groups(rows):
        turns: dict[int, dict] = {}
        for r in group:
            turn = turns.setdefault(r["turn_id"], {"question": r["question"], "asked_at": r["asked_at"], "answered": r["answered"]})
            turn["answered"] = turn["answered"] and r["answered"]
        examples = []
        for r in group:                # the most typical items' questions first
            if r["question"] not in examples:
                examples.append(r["question"])
        topics.append({"topic": group[0]["missing"], "questions": len(turns),
                       "unanswered": sum(1 for t in turns.values() if not t["answered"]),
                       "after_research": sum(1 for r in group if r["after_research"]),
                       "examples": examples[:EXAMPLES], "items": sorted({r["missing"] for r in group})[:10],
                       "last_asked": max(t["asked_at"] for t in turns.values()).isoformat()})
    topics.sort(key=lambda t: (-t["questions"], -t["unanswered"], t["topic"]))
    return {"topics": topics[:TOPICS], "questions": len({r["turn_id"] for r in rows}), "grouped_by": grouped_by}

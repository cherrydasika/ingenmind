"""Answer feedback (R51): thumbs up or down on an answer, with a reason when
it is down, from the person who asked.

    answer_feedback  turn_id (app_session_turns: the answer), user_id, rating
                     (up | down), reason (wrong | incomplete | out_of_date |
                     not_cited | other), comment, created_at, updated_at,
                     promoted_to (the golden set it went into), promoted_at

Each rating is also a score on the answer's Langfuse trace ("user_feedback",
1 or 0, the reason and comment as its comment); a changed rating replaces it.
An admin can turn a thumbs-down into a golden-set question with the corrected
answer (Evaluations → From answer feedback).
"""

import threading

import psycopg
from psycopg.rows import dict_row

import sessions
import tracing
from common import config

REASONS = {"wrong": "Wrong", "incomplete": "Incomplete", "out_of_date": "Out of date",
           "not_cited": "Not cited", "other": "Other"}
COMMENT_CHARS = 1000
SCORE = "user_feedback"
FEEDBACK_SET = ("user_feedback", "From answer feedback",
                "Questions users marked thumbs-down, with the corrected answer an admin wrote.")

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
    with _schema_lock:
        if _schema_ready:
            return
        sessions.ensure_schema()
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616751)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS answer_feedback (
                    turn_id bigint PRIMARY KEY REFERENCES app_session_turns (turn_id) ON DELETE CASCADE,
                    user_id text NOT NULL,
                    rating text NOT NULL CHECK (rating IN ('up', 'down')),
                    reason text,
                    comment text NOT NULL DEFAULT '',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    promoted_to text,
                    promoted_at timestamptz
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS answer_feedback_recent ON answer_feedback (rating, updated_at DESC)")
        _schema_ready = True


def _public(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def give(turn_id: int, user_id: str, rating: str, reason: str | None = None, comment: str = "") -> dict:
    """The asker's rating of one of their answers (again: it replaces the last). ValueError when it can't be."""
    if rating not in ("up", "down"):
        raise ValueError("rating: up or down")
    reason = (reason or None) if rating == "down" else None
    if reason is not None and reason not in REASONS:
        raise ValueError(f"reason: one of {', '.join(REASONS)}")
    comment = " ".join(str(comment or "").split())[:COMMENT_CHARS] if rating == "down" else ""
    ensure_schema()
    with _connect() as connection:
        turn = connection.execute("SELECT turn_id, user_id, trace_id FROM app_session_turns WHERE turn_id = %s",
                                  (turn_id,)).fetchone()
        if not turn or turn["user_id"] != user_id:
            raise LookupError("no such answer")          # others' answers are not acknowledged
        row = connection.execute("""
            INSERT INTO answer_feedback (turn_id, user_id, rating, reason, comment) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (turn_id) DO UPDATE SET rating = EXCLUDED.rating, reason = EXCLUDED.reason,
                                                comment = EXCLUDED.comment, updated_at = now()
            RETURNING *""", (turn_id, user_id, rating, reason, comment)).fetchone()
    note = "; ".join(filter(None, [REASONS.get(reason), comment])) or None
    tracing.score(turn["trace_id"], SCORE, 1 if rating == "up" else 0, "BOOLEAN", note, score_id=f"feedback-{turn_id}")
    return {**_public(row), "traced": bool(turn["trace_id"])}


def for_turns(turn_ids: list[int]) -> dict[int, dict]:
    """turn_id → its feedback, for the turns that have some."""
    if not turn_ids:
        return {}
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM answer_feedback WHERE turn_id = ANY(%s)", (list(turn_ids),)).fetchall()
    return {r["turn_id"]: _public(r) for r in rows}


def attach(session: dict | None) -> dict | None:
    """A session (sessions.get_session) with each turn's feedback."""
    if session:
        found = for_turns([t["turn_id"] for t in session["turns"]])
        session["turns"] = [{**t, "feedback": found.get(t["turn_id"])} for t in session["turns"]]
    return session


def recent(rating: str | None = "down", limit: int = 100) -> list[dict]:
    """Feedback, newest first, with the question and answer it is about (Evaluations)."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute(f"""
            SELECT f.*, t.question, t.answer, t.session_id, t.asked_at, t.result->'flow' AS flow, u.first_name
            FROM answer_feedback f
            JOIN app_session_turns t ON t.turn_id = f.turn_id
            LEFT JOIN app_users u ON u.user_id = f.user_id
            {"WHERE f.rating = %s" if rating else ""}
            ORDER BY f.updated_at DESC LIMIT %s""", ((rating, limit) if rating else (limit,))).fetchall()
    return [_public(r) for r in rows]


def summary(days: int = 7) -> dict:
    """Ratings in the last days: {up, down, reasons {reason: n}} (Monitoring)."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT rating, reason, count(*) AS n FROM answer_feedback
            WHERE updated_at > now() - make_interval(days => %s) GROUP BY rating, reason""", (days,)).fetchall()
    out = {"days": days, "up": 0, "down": 0, "reasons": {}}
    for r in rows:
        out[r["rating"]] += r["n"]
        if r["rating"] == "down":
            out["reasons"][r["reason"] or "none"] = out["reasons"].get(r["reason"] or "none", 0) + r["n"]
    return out


def promote(turn_id: int, expected: str, set_id: str = FEEDBACK_SET[0]) -> dict:
    """A thumbs-down answer's question, with the corrected answer, into a golden
    (evaluation) set: the feedback set unless another saved set is named."""
    from flows import evals
    expected = str(expected or "").strip()
    if not expected:
        raise ValueError("write the corrected answer first")
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""SELECT f.rating, t.question FROM answer_feedback f
                                    JOIN app_session_turns t ON t.turn_id = f.turn_id WHERE f.turn_id = %s""",
                                 (turn_id,)).fetchone()
    if not row or row["rating"] != "down":
        raise LookupError("no thumbs-down for that answer")
    found = evals.get_set(set_id)
    if found and found.get("builtin"):
        raise ValueError(f"{set_id!r} is a built-in set")
    questions = [q for q in (found or {}).get("questions", []) if q["question"] != row["question"]]
    questions = evals.clean_questions([*questions, {"question": row["question"], "expected": expected}])
    name, description = ((found["name"], found.get("description") or "") if found
                         else (FEEDBACK_SET[1], FEEDBACK_SET[2]) if set_id == FEEDBACK_SET[0] else (set_id, ""))
    evals.save_set(set_id, name, description, questions)
    with _connect() as connection:
        connection.execute("UPDATE answer_feedback SET promoted_to = %s, promoted_at = now() WHERE turn_id = %s",
                           (set_id, turn_id))
    return {"set_id": set_id, "questions": len(questions)}

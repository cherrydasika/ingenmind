"""Evaluate flow versions: run a question set against one or more versions
and keep, per question, the answer users would see plus numbers (answer
evaluator pass and scores, guardrail blocks, time, tokens). Prompts, tasks,
drafts and evidence are never stored, as with the playground's traces.

    agent_eval_sets     id, name, description, questions [{question, expected, expect_blocked}],
                        and on a set setup wrote (#16): area, kind, expected_source
    agent_eval_runs     one flow version × one set; runs of one request share a batch
    agent_eval_results  one row per question of a run

Built-in sets live in eval_sets/*.json. Runs execute one question at a time
on a background thread (one at a time across the app, to bound cost).
"""

import json
import statistics
import threading
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from . import store

BUILTIN_DIR = Path(__file__).with_name("eval_sets")
MAX_QUESTIONS = 50
MAX_VERSIONS = 3
# What a question tests (sets setup writes, initialization/evaluation.py): an answer
# from the knowledge base, an area with no pages, a live tool, or a refusal.
KINDS = ("answer", "not_covered", "live", "out_of_scope")
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    store.ensure_schema()
    with store._connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_eval_sets (
                id text PRIMARY KEY,
                name text NOT NULL,
                description text NOT NULL DEFAULT '',
                questions jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            )""")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_eval_runs (
                id text PRIMARY KEY,
                batch text NOT NULL,
                set_id text NOT NULL,
                set_name text NOT NULL,
                flow_id text NOT NULL,
                version text NOT NULL,
                status text NOT NULL,
                total int NOT NULL,
                spec jsonb NOT NULL,
                completed int NOT NULL DEFAULT 0,
                cancel boolean NOT NULL DEFAULT false,
                created_at timestamptz NOT NULL DEFAULT now(),
                started_at timestamptz,
                finished_at timestamptz,
                summary jsonb
            )""")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_eval_results (
                run_id text NOT NULL REFERENCES agent_eval_runs (id) ON DELETE CASCADE,
                idx int NOT NULL,
                answer text NOT NULL DEFAULT '',
                expectation_met boolean,
                passed boolean,
                overall real,
                scores jsonb,
                failed_on jsonb,
                answer_type text,
                input_blocked boolean NOT NULL DEFAULT false,
                output_blocked boolean NOT NULL DEFAULT false,
                failed boolean NOT NULL DEFAULT false,
                seconds real NOT NULL DEFAULT 0,
                tokens int NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, idx)
            )""")
        # What a question of a setup-written set tests and found (#16): its kind and area, frozen
        # with the result, the pages retrieved and cited (URLs only), and the live tools called.
        for column in ("kind text", "area text", "retrieved jsonb", "cited jsonb", "live_tools jsonb",
                       "expected_retrieved boolean", "expected_cited boolean"):
            connection.execute(f"ALTER TABLE agent_eval_results ADD COLUMN IF NOT EXISTS {column}")
        # A run that was going when the app stopped will not finish.
        connection.execute("""
            UPDATE agent_eval_runs SET status = 'interrupted', finished_at = now()
            WHERE status IN ('queued', 'running')""")
    _ready = True


def reset_schema_cache() -> None:
    global _ready
    _ready = False


def _iso(value):
    return value.isoformat() if value else None


# ---------- question sets ----------

def builtin_sets() -> dict[str, dict]:
    sets = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        sets[data["id"]] = {**data, "questions": clean_questions(data["questions"]), "builtin": True}
    return sets


def clean_questions(raw) -> list[dict]:
    """[{question, expected, expect_blocked}] from user input (with area, kind and
    expected_source when given); raises ValueError."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("a set needs at least one question")
    if len(raw) > MAX_QUESTIONS:
        raise ValueError(f"a set may have at most {MAX_QUESTIONS} questions")
    out = []
    for item in raw:
        item = {"question": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            raise ValueError("each question is text or {question, expected}")
        question = str(item.get("question") or "").strip()[:1000]
        if not question:
            raise ValueError("a question is empty")
        cleaned = {"question": question, "expected": str(item.get("expected") or "").strip()[:4000],
                   "expect_blocked": bool(item.get("expect_blocked"))}
        if item.get("kind") is not None:
            if item["kind"] not in KINDS:
                raise ValueError(f"kind is one of {', '.join(KINDS)}")
            cleaned["kind"] = item["kind"]
        for key, limit in (("area", 60), ("expected_source", 2000)):
            if str(item.get(key) or "").strip():
                cleaned[key] = str(item[key]).strip()[:limit]
        out.append(cleaned)
    return out


def list_sets() -> list[dict]:
    ensure_schema()
    sets = builtin_sets()
    with store._connect() as connection:
        for row in connection.execute("SELECT * FROM agent_eval_sets ORDER BY id").fetchall():
            sets[row["id"]] = {"id": row["id"], "name": row["name"], "description": row["description"],
                               "questions": row["questions"], "builtin": False, "updated_at": _iso(row["updated_at"])}
    return [{**s, "count": len(s["questions"])} for s in sets.values()]


def get_set(set_id: str) -> dict | None:
    return next((s for s in list_sets() if s["id"] == set_id), None)


def save_set(set_id: str, name: str, description: str, questions: list[dict]) -> None:
    if set_id in builtin_sets():
        raise ValueError(f"{set_id!r} is a built-in set; save under another id")
    ensure_schema()
    with store._connect() as connection:
        connection.execute("""
            INSERT INTO agent_eval_sets (id, name, description, questions) VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
                                           questions = EXCLUDED.questions, updated_at = now()""",
            (set_id, name[:200], description[:1000], Jsonb(questions)))


# ---------- runs ----------

def start(set_id: str, flow_id: str, targets: list[tuple], execute) -> list[str]:
    """Queue one run per (version label, flow JSON) — the flow is frozen now,
    so editing a draft does not change a run — and make sure the worker runs.
    execute(run, index, question) → result dict (see _store_result)."""
    question_set = get_set(set_id)
    if question_set is None:
        raise LookupError(f"unknown question set {set_id!r}")
    ensure_schema()
    batch = uuid.uuid4().hex[:8]
    ids = []
    with store._connect() as connection:
        for version, spec in targets:
            run_id = uuid.uuid4().hex[:12]
            connection.execute("""
                INSERT INTO agent_eval_runs (id, batch, set_id, set_name, flow_id, version, status, total, spec)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s)""",
                (run_id, batch, set_id, question_set["name"], flow_id, str(version), len(question_set["questions"]),
                 Jsonb(spec)))
            ids.append(run_id)
    _ensure_worker(execute)
    return ids


def _ensure_worker(execute) -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, args=(execute,), name="flow-evals", daemon=True)
            _worker.start()


def _claim():
    with store._connect() as connection, connection.transaction():
        row = connection.execute("""
            SELECT * FROM agent_eval_runs WHERE status = 'queued' ORDER BY created_at
            LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
        if row:
            connection.execute("UPDATE agent_eval_runs SET status = 'running', started_at = now() WHERE id = %s",
                               (row["id"],))
    return row


def _cancelled(run_id: str) -> bool:
    with store._connect() as connection:
        return connection.execute("SELECT cancel FROM agent_eval_runs WHERE id = %s", (run_id,)).fetchone()["cancel"]


def _work(execute) -> None:
    while True:
        try:
            run = _claim()
        except Exception:
            return
        if run is None:
            return
        question_set = get_set(run["set_id"]) or {"questions": []}
        status = "done"
        for index, question in enumerate(question_set["questions"][:run["total"]]):
            if _cancelled(run["id"]):
                status = "cancelled"
                break
            try:
                result = execute(run, index, question)
            except Exception:
                result = {"failed": True}
            _store_result(run["id"], index, question, result)
        _finish(run["id"], status)


def _same_page(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def expectation(question: dict, result: dict) -> bool | None:
    """Whether the answer did what the question expects (None: the run failed).

        out_of_scope (or expect_blocked)  a guardrail blocked it
        answer                            not blocked, an answer (not "not available" or a
                                          question back), and the answer check did not fail
        (a set without kinds)             not blocked, and the answer check did not fail
        not_covered                       not blocked, and it says the information is not
                                          available (or asks), or it passes the answer check:
                                          it claims nothing the evidence does not support
        live                              not blocked, and a live tool was called or it says
                                          the information is not available
    """
    blocked = bool(result.get("input_blocked") or result.get("output_blocked"))
    if question.get("expect_blocked") or question.get("kind") == "out_of_scope":
        return blocked
    if result.get("failed"):
        return None
    if blocked:
        return False
    kind, answer_type = question.get("kind"), result.get("answer_type")
    if kind == "not_covered":
        return answer_type in ("not_available", "clarification") or result.get("passed") is True
    if kind == "live":
        return bool(result.get("live_tools")) or answer_type == "not_available"
    if kind == "answer" and answer_type not in (None, "answer"):
        return False
    return result.get("passed") is not False


def _store_result(run_id: str, index: int, question: dict, result: dict) -> None:
    expected = question.get("expected_source")
    retrieved, cited = result.get("retrieved"), result.get("cited")
    found = lambda pages: any(_same_page(expected, p) for p in pages) if expected and pages is not None else None
    with store._connect() as connection:
        connection.execute("""
            INSERT INTO agent_eval_results (run_id, idx, answer, expectation_met, passed, overall, scores, failed_on,
                                            answer_type, input_blocked, output_blocked, failed, seconds, tokens,
                                            kind, area, retrieved, cited, live_tools, expected_retrieved,
                                            expected_cited)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, idx) DO NOTHING""",
            (run_id, index, str(result.get("answer") or "")[:8000], expectation(question, result), result.get("passed"),
             result.get("overall"), Jsonb(result.get("scores")) if result.get("scores") else None,
             Jsonb(result.get("failed_on") or []), result.get("answer_type"), bool(result.get("input_blocked")),
             bool(result.get("output_blocked")), bool(result.get("failed")), float(result.get("seconds") or 0),
             int(result.get("tokens") or 0), question.get("kind"), question.get("area"),
             Jsonb(retrieved) if retrieved is not None else None, Jsonb(cited) if cited is not None else None,
             Jsonb(result.get("live_tools")) if result.get("live_tools") is not None else None,
             found(retrieved), found(cited)))
        connection.execute("UPDATE agent_eval_runs SET completed = completed + 1 WHERE id = %s", (run_id,))


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def summarise_areas(results: list[dict]) -> dict:
    """Per knowledge area, from its questions (results of a set setup wrote):

        retrieval_relevance  answer questions whose expected page was retrieved ÷ answer questions
        answer_correctness   mean of the answer check's correctness score (answer questions)
        groundedness         mean of its faithfulness score (answer questions)
        citation_accuracy    answer questions citing the expected page ÷ answer questions
        citation_quality     mean of its citation-quality score (answer questions)
        coverage             answer questions whose expectation was met ÷ answer questions;
                             0 for an area with no pages (its questions are not_covered)
        refusals_right       not_covered, live and out_of_scope questions whose
                             expectation was met ÷ those questions
    """
    areas: dict[str, list[dict]] = {}
    for r in results:
        if r.get("area"):
            areas.setdefault(r["area"], []).append(r)
    out = {}
    for area, rows in areas.items():
        answers = [r for r in rows if r.get("kind") == "answer"]
        others = [r for r in rows if r.get("kind") in ("not_covered", "live", "out_of_scope")]
        scored = [r["scores"] or {} for r in answers if r.get("scores")]
        out[area] = {
            "kinds": sorted({r.get("kind") for r in rows if r.get("kind")}),
            "questions": len(rows),
            "met": sum(1 for r in rows if r["expectation_met"]),
            "answer_questions": len(answers),
            "retrieval_relevance": _ratio(sum(1 for r in answers if r.get("expected_retrieved")), len(answers)),
            "answer_correctness": _mean([s.get("correctness") for s in scored]),
            "groundedness": _mean([s.get("faithfulness") for s in scored]),
            "citation_accuracy": _ratio(sum(1 for r in answers if r.get("expected_cited")), len(answers)),
            "citation_quality": _mean([s.get("citation_quality") for s in scored]),
            "coverage": _ratio(sum(1 for r in answers if r["expectation_met"]), len(answers))
            if answers else (0.0 if any(r.get("kind") == "not_covered" for r in rows) else None),
            "refusals_right": _ratio(sum(1 for r in others if r["expectation_met"]), len(others)),
        }
    return out


def summarise(results: list[dict]) -> dict:
    evaluated = [r for r in results if r["passed"] is not None]
    met = [r for r in results if r["expectation_met"] is not None]
    seconds = sorted(r["seconds"] for r in results if not r["failed"])
    scored = [r["scores"] for r in evaluated if r["scores"]]
    keys = sorted({k for s in scored for k in s})
    return {
        "questions": len(results),
        "expectations_met": sum(1 for r in met if r["expectation_met"]),
        "expectations": len(met),
        "evaluated": len(evaluated),
        "passed": sum(1 for r in evaluated if r["passed"]),
        "pass_rate": round(sum(1 for r in evaluated if r["passed"]) / len(evaluated), 3) if evaluated else None,
        "overall": round(statistics.fmean(r["overall"] for r in evaluated if r["overall"] is not None), 3)
        if any(r["overall"] is not None for r in evaluated) else None,
        "scores": {k: round(statistics.fmean(s[k] for s in scored if k in s), 3) for k in keys},
        "input_blocked": sum(1 for r in results if r["input_blocked"]),
        "output_blocked": sum(1 for r in results if r["output_blocked"]),
        "failed": sum(1 for r in results if r["failed"]),
        "p50_seconds": round(statistics.median(seconds), 2) if seconds else None,
        "total_seconds": round(sum(seconds), 1),
        "tokens": sum(r["tokens"] for r in results),
        **({"areas": areas} if (areas := summarise_areas(results)) else {}),
    }


def _results(connection, run_id: str) -> list[dict]:
    return connection.execute("SELECT * FROM agent_eval_results WHERE run_id = %s ORDER BY idx", (run_id,)).fetchall()


def _finish(run_id: str, status: str) -> None:
    with store._connect() as connection:
        summary = summarise(_results(connection, run_id))
        connection.execute("""
            UPDATE agent_eval_runs SET status = %s, finished_at = now(), summary = %s WHERE id = %s""",
            (status, Jsonb(summary), run_id))


def _run_view(row: dict) -> dict:
    return {"id": row["id"], "batch": row["batch"], "set_id": row["set_id"], "set_name": row["set_name"],
            "flow_id": row["flow_id"], "version": row["version"], "status": row["status"], "total": row["total"],
            "completed": row["completed"], "created_at": _iso(row["created_at"]), "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]), "summary": row["summary"]}


def list_runs(flow_id: str | None = None, limit: int = 50) -> list[dict]:
    ensure_schema()
    with store._connect() as connection:
        rows = connection.execute(f"""
            SELECT * FROM agent_eval_runs {"WHERE flow_id = %s" if flow_id else ""}
            ORDER BY created_at DESC LIMIT %s""", ((flow_id, limit) if flow_id else (limit,))).fetchall()
    return [_run_view(r) for r in rows]


def get_run(run_id: str) -> dict | None:
    ensure_schema()
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM agent_eval_runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            return None
        results = _results(connection, run_id)
    question_set = get_set(row["set_id"]) or {"questions": []}
    view = _run_view(row)
    if view["summary"] is None:
        view["summary"] = summarise(results)
    return {**view, "questions": question_set["questions"],
            "results": [{k: r[k] for k in r if k != "run_id"} for r in results]}


def cancel(run_id: str) -> None:
    ensure_schema()
    with store._connect() as connection:
        connection.execute("""
            UPDATE agent_eval_runs SET cancel = true,
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END
            WHERE id = %s""", (run_id,))

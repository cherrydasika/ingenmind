"""Agent memory: what the agents learn across runs, kept apart from any
user's session. It is built only from run outcomes — counts, scores, web
sites, tool names and error kinds — never from questions, answers, tasks or
queries, so it cannot carry one user's conversation to another. Session
memory is separate: each session's agent conversations, keyed by user and
session (agent._actor_id, the AgentCore Memory actor or agent_sessions).

    app_agent_memory  agent, kind, key, successes, failures, score_sum,
                      score_n, note, first_seen, last_seen

What is learned (agent / kind / key):
    research / source_validation / web site — sources that passed or failed
        validation, their mean score, and the checks that last failed
    knowledge_base / cited_source / web site — sites cited by answers that
        passed the answer check
    knowledge_base / evidence_decision / decision — how often each evidence
        decision is reached
    knowledge_base / query_rewrite / recovered | still_missing — whether a
        rewrite after a missed retrieval found good evidence
    external_apis / tool / tool name — calls that worked or failed, and the
        last error, with anything quoted in it blanked out

Agents do not read it yet (decision 5 in the identity plan): it is shown on
the Agent memory page for people with view_agent_memory.
"""

import re
import threading
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

import research
from common import config

AGENTS = {"research": "Research agent", "knowledge_base": "Knowledge-base agent", "external_apis": "External-APIs agent"}
KINDS = {
    "source_validation": "Sources it validated, by web site",
    "cited_source": "Sites cited by answers that passed the answer check",
    "evidence_decision": "Evidence decisions",
    "query_rewrite": "Query rewrites after a missed retrieval",
    "tool": "Tool calls",
}
NOTE_CHARS = 200

_schema_ready = False
_schema_lock = threading.Lock()


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def reset_schema_cache() -> None:
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_agent_memory (
                    agent text NOT NULL,
                    kind text NOT NULL,
                    key text NOT NULL,
                    successes int NOT NULL DEFAULT 0,
                    failures int NOT NULL DEFAULT 0,
                    score_sum double precision NOT NULL DEFAULT 0,
                    score_n int NOT NULL DEFAULT 0,
                    note text NOT NULL DEFAULT '',
                    first_seen timestamptz NOT NULL DEFAULT now(),
                    last_seen timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (agent, kind, key)
                )""")
        _schema_ready = True


def site(url: str) -> str:
    """The web site of a URL (no path: paths can carry search terms)."""
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or "unknown"


def anonymise(error: str) -> str:
    """An error message without what a user may have typed: quoted parts
    ('Leeds, UK') are blanked, and it is cut short."""
    text = re.sub(r"'[^']*'|\"[^\"]*\"|“[^”]*”", "'…'", str(error or ""))
    return re.sub(r"\s+", " ", text).strip()[:NOTE_CHARS]


def outcomes(research_records: list[dict], calls: list[dict], evaluations: list[dict], rewrites: list[dict],
             sources: list[dict], answer_passed: bool | None) -> list[dict]:
    """The learnings one run yields: (agent, kind, key, success?, score, note)."""
    out = []
    for record in research_records:
        for source in (record.get("validation") or {}).get("sources", []):
            scores = source.get("scores") or {}
            failed = [name for name, limit in research.ACCEPT.items() if (scores.get(name) or 0) < limit]
            out.append({"agent": "research", "kind": "source_validation", "key": site(source.get("url")),
                        "success": bool(source.get("accepted")), "score": source.get("overall"),
                        "note": "" if source.get("accepted") else ("failed: " + ", ".join(failed) if failed else
                                                                   "not extracted")})
    if answer_passed:
        for source in sources:
            out.append({"agent": "knowledge_base", "kind": "cited_source", "key": site(source.get("url")),
                        "success": True})
    by_task = {}
    for evaluation in evaluations:
        out.append({"agent": "knowledge_base", "kind": "evidence_decision", "key": evaluation.get("decision") or "?",
                    "success": evaluation.get("decision") == "GOOD_EVIDENCE",
                    "score": evaluation.get("overall_confidence")})
        by_task.setdefault(evaluation.get("delegation"), []).append(evaluation)
    for rewrite in rewrites:
        later = [e for e in by_task.get(rewrite.get("delegation"), []) if (e.get("attempt") or 1) > 1]
        if later:
            recovered = later[-1].get("decision") == "GOOD_EVIDENCE"
            out.append({"agent": "knowledge_base", "kind": "query_rewrite",
                        "key": "recovered" if recovered else "still_missing", "success": recovered})
    for call in calls:
        if call.get("ok") is None:
            continue
        out.append({"agent": "external_apis", "kind": "tool", "key": call.get("tool") or "?",
                    "success": bool(call.get("ok")), "note": "" if call.get("ok") else anonymise(call.get("error"))})
    return out


def record(learnings: list[dict]) -> None:
    if not learnings:
        return
    ensure_schema()
    with _connect() as connection:
        for item in learnings:
            score = item.get("score")
            connection.execute("""
                INSERT INTO app_agent_memory (agent, kind, key, successes, failures, score_sum, score_n, note)
                VALUES (%(agent)s, %(kind)s, %(key)s, %(s)s, %(f)s, %(sum)s, %(n)s, %(note)s)
                ON CONFLICT (agent, kind, key) DO UPDATE SET
                    successes = app_agent_memory.successes + EXCLUDED.successes,
                    failures = app_agent_memory.failures + EXCLUDED.failures,
                    score_sum = app_agent_memory.score_sum + EXCLUDED.score_sum,
                    score_n = app_agent_memory.score_n + EXCLUDED.score_n,
                    note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note ELSE app_agent_memory.note END,
                    last_seen = now()""",
                {"agent": item["agent"], "kind": item["kind"], "key": str(item["key"])[:200],
                 "s": 1 if item["success"] else 0, "f": 0 if item["success"] else 1,
                 "sum": float(score) if score is not None else 0.0, "n": 1 if score is not None else 0,
                 "note": item.get("note") or ""})


def learn(research_records, calls, evaluations, rewrites, sources, answer_passed) -> None:
    record(outcomes(research_records, calls, evaluations, rewrites, sources, answer_passed))


def entries(agent: str | None = None) -> list[dict]:
    """Everything learned, most used first, with rates and mean scores."""
    ensure_schema()
    where, params = ("WHERE agent = %s", (agent,)) if agent else ("", ())
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM app_agent_memory {where} ORDER BY agent, kind, successes + failures DESC, key",
            params).fetchall()
    for row in rows:
        total = row["successes"] + row["failures"]
        row["total"] = total
        row["success_rate"] = round(row["successes"] / total, 3) if total else None
        row["mean_score"] = round(row["score_sum"] / row["score_n"], 3) if row["score_n"] else None
        del row["score_sum"], row["score_n"]
    return rows

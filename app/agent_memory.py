"""Agent memory: what the agents learn across runs, kept apart from any
user's session. It is built only from run outcomes — counts, scores, web
sites, tool names and error kinds — never from questions, answers, tasks or
queries, so it cannot carry one user's conversation to another. Session
memory is separate: each session's agent conversations, keyed by user and
session (agent._actor_id, the AgentCore Memory actor or agent_sessions).

    app_agent_memory  agent, kind, key, successes, failures, score_sum,
                      score_n, note, first_seen, last_seen, pinned

What is learned (agent / kind / key):
    research / source_validation / web site — sources that passed or failed
        validation, their mean score, and the checks that last failed
    knowledge_base / cited_source / web site — sites cited by answers that
        passed the answer check
    knowledge_base / evidence_decision / decision — how often each evidence
        decision is reached
    knowledge_base / query_rewrite / recovered | still_missing — whether a
        rewrite after a missed retrieval found good evidence
    knowledge_base / rewrite_strategy / strategy — the same, per kind of
        rewrite (narrower, broader, synonyms…), never the queries
    external_apis / tool / tool name — calls that worked or failed, and the
        last error, with anything quoted in it blanked out

How the agents use it (R27), while USE is on (AGENT_MEMORY_USE, default
true; an evaluation can run with it off, see using()):
    research        web search results carry each site's validation record,
                    and sites that validated well come first, ones that keep
                    failing last (site_records)
    external_apis   a tool that failed most of its recent calls is skipped
                    for that run, saying why (tool_unreliable)
    query rewriter  sees which kinds of rewrite recovered evidence before
                    (strategy_note)

Counts decay: each one halves every HALF_LIFE_DAYS (AGENT_MEMORY_HALF_LIFE_DAYS,
default 30), applied when an entry is updated and when it is read, so old
outcomes fade. An admin may pin an entry (it stops decaying) or forget it
on the Agent memory page.
"""

import contextlib
import contextvars
import os
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
    "rewrite_strategy": "Query rewrites, by kind of rewrite",
    "tool": "Tool calls",
}
NOTE_CHARS = 200
USE = os.environ.get("AGENT_MEMORY_USE", "true").strip().lower() not in ("0", "false", "no")
HALF_LIFE_DAYS = float(os.environ.get("AGENT_MEMORY_HALF_LIFE_DAYS", "30"))
MIN_EVIDENCE = 3          # recent outcomes before a record changes anything
TOOL_SKIP_RATE = 0.2      # a tool that worked in fewer than this share of its recent calls is skipped
STRATEGIES = ("narrower", "broader", "synonyms", "split", "spelling", "language", "other")
# For a whole run (an evaluation with memory off): None follows USE.
_use: contextvars.ContextVar[bool | None] = contextvars.ContextVar("agent_memory_use", default=None)


def in_use() -> bool:
    flag = _use.get()
    return USE if flag is None else flag


@contextlib.contextmanager
def using(flag: bool):
    """Memory on or off for what runs inside (read and learned alike)."""
    token = _use.set(bool(flag))
    try:
        yield
    finally:
        _use.reset(token)


# Decayed counts: the factor since last_seen (pinned entries keep theirs).
DECAY = ("CASE WHEN pinned THEN 1.0 ELSE power(0.5, extract(epoch FROM now() - last_seen) / 86400.0 / {half}) END")

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
            # R27: counts decay (fractional) and admins can pin an entry.
            connection.execute("ALTER TABLE app_agent_memory ALTER COLUMN successes TYPE double precision, "
                               "ALTER COLUMN failures TYPE double precision, "
                               "ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false")
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
            strategy = rewrite.get("strategy")
            if strategy in STRATEGIES:
                out.append({"agent": "knowledge_base", "kind": "rewrite_strategy", "key": strategy,
                            "success": recovered})
    for call in calls:
        if call.get("ok") is None:
            continue
        out.append({"agent": "external_apis", "kind": "tool", "key": call.get("tool") or "?",
                    "success": bool(call.get("ok")), "note": "" if call.get("ok") else anonymise(call.get("error"))})
    return out


def record(learnings: list[dict]) -> None:
    if not learnings or not in_use():
        return
    ensure_schema()
    with _connect() as connection:
        for item in learnings:
            score = item.get("score")
            connection.execute("""
                INSERT INTO app_agent_memory (agent, kind, key, successes, failures, score_sum, score_n, note)
                VALUES (%(agent)s, %(kind)s, %(key)s, %(s)s, %(f)s, %(sum)s, %(n)s, %(note)s)
                ON CONFLICT (agent, kind, key) DO UPDATE SET
                    successes = app_agent_memory.successes * {decay} + EXCLUDED.successes,
                    failures = app_agent_memory.failures * {decay} + EXCLUDED.failures,
                    score_sum = app_agent_memory.score_sum + EXCLUDED.score_sum,
                    score_n = app_agent_memory.score_n + EXCLUDED.score_n,
                    note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note ELSE app_agent_memory.note END,
                    last_seen = now()""".format(decay=DECAY.format(half=HALF_LIFE_DAYS).replace("pinned", "app_agent_memory.pinned")
                                                .replace("last_seen", "app_agent_memory.last_seen")),
                {"agent": item["agent"], "kind": item["kind"], "key": str(item["key"])[:200],
                 "s": 1 if item["success"] else 0, "f": 0 if item["success"] else 1,
                 "sum": float(score) if score is not None else 0.0, "n": 1 if score is not None else 0,
                 "note": item.get("note") or ""})


def learn(research_records, calls, evaluations, rewrites, sources, answer_passed) -> None:
    record(outcomes(research_records, calls, evaluations, rewrites, sources, answer_passed))


def _decayed(where: str = "", params: tuple = ()) -> list[dict]:
    ensure_schema()
    decay = DECAY.format(half=HALF_LIFE_DAYS)
    with _connect() as connection:
        return connection.execute(
            f"SELECT agent, kind, key, successes * {decay} AS successes, failures * {decay} AS failures, "
            f"score_sum, score_n, note, first_seen, last_seen, pinned FROM app_agent_memory {where}", params).fetchall()


def entries(agent: str | None = None) -> list[dict]:
    """Everything learned, most used first, with its decayed (recent) counts,
    rates and mean scores."""
    rows = _decayed(*(("WHERE agent = %s", (agent,)) if agent else ()))
    for row in rows:
        row["successes"], row["failures"] = round(row["successes"], 2), round(row["failures"], 2)
        total = row["successes"] + row["failures"]
        row["total"] = round(total, 2)
        row["success_rate"] = round(row["successes"] / total, 3) if total else None
        row["mean_score"] = round(row["score_sum"] / row["score_n"], 3) if row["score_n"] else None
        del row["score_sum"], row["score_n"]
    rows.sort(key=lambda r: (r["agent"], r["kind"], -r["total"], r["key"]))
    return rows


def pin(agent: str, kind: str, key: str, pinned: bool) -> bool:
    """Pin an entry (it stops decaying; its counts are brought up to date first) or unpin it."""
    ensure_schema()
    decay = DECAY.format(half=HALF_LIFE_DAYS)
    with _connect() as connection:
        return connection.execute(
            f"UPDATE app_agent_memory SET successes = successes * {decay}, failures = failures * {decay}, "
            "last_seen = now(), pinned = %s WHERE agent = %s AND kind = %s AND key = %s",
            (bool(pinned), agent, kind, key)).rowcount > 0


def forget(agent: str, kind: str, key: str) -> bool:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("DELETE FROM app_agent_memory WHERE agent = %s AND kind = %s AND key = %s",
                                  (agent, kind, key)).rowcount > 0


# ---------- read at run time (R27) ----------

def _records(agent: str, kind: str, keys: list[str] | None = None) -> dict[str, dict]:
    if not in_use():
        return {}
    try:
        where, params = "WHERE agent = %s AND kind = %s", (agent, kind)
        if keys is not None:
            where, params = where + " AND key = ANY(%s)", params + (list(keys),)
        return {r["key"]: r for r in _decayed(where, params)}
    except Exception:          # memory is a hint: a run never fails for it
        return {}


def site_records(urls: list[str]) -> dict[str, dict]:
    """Per site of urls with enough recent outcomes: {"validated", "of", "rate"}."""
    found = _records("research", "source_validation", sorted({site(u) for u in urls}))
    out = {}
    for key, r in found.items():
        total = r["successes"] + r["failures"]
        if total >= MIN_EVIDENCE:
            out[key] = {"validated": round(r["successes"]), "of": round(total), "rate": r["successes"] / total}
    return out


def rank_results(results: list[dict]) -> list[dict]:
    """Web search results with each site's validation record, sites that
    validated well first and ones that keep failing last; otherwise in the
    search's order."""
    records = site_records([r.get("url") or "" for r in results])
    if not records:
        return results
    def weight(item):
        rec = records.get(site(item[1].get("url") or ""))
        tier = 1 if rec is None else 0 if rec["rate"] >= 0.6 else 2 if rec["rate"] < 0.25 else 1
        return (tier, item[0])
    ranked = [r for _, r in sorted(enumerate(results), key=weight)]
    return [{**r, "site_record": f"validated {records[site(r.get('url') or '')]['validated']} of "
                                 f"{records[site(r.get('url') or '')]['of']} recent times"}
            if site(r.get("url") or "") in records else r for r in ranked]


def tool_unreliable(tool: str) -> str | None:
    """Why a tool should be skipped this run (it failed most recent calls), or None."""
    r = _records("external_apis", "tool", [tool]).get(tool)
    if not r or r["pinned"]:
        return None
    total = r["successes"] + r["failures"]
    if total >= MIN_EVIDENCE + 2 and r["successes"] / total < TOOL_SKIP_RATE:
        return (f"{tool} failed {round(r['failures'])} of its last {round(total)} calls"
                + (f" (last error: {r['note']})" if r["note"] else ""))
    return None


def strategy_note() -> str:
    """For the query rewriter: which kinds of rewrite recovered evidence before."""
    found = _records("knowledge_base", "rewrite_strategy")
    lines = []
    for key, r in sorted(found.items(), key=lambda kv: -(kv[1]["successes"] + kv[1]["failures"])):
        total = r["successes"] + r["failures"]
        if total >= 1.5:
            lines.append(f"{key}: recovered {round(r['successes'])} of {round(total)}")
    return ("Rewrites that recovered the evidence in earlier runs, by kind: " + "; ".join(lines) + ".") if lines else ""

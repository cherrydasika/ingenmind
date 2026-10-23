"""Learning from live use (R58): what real questions teach, as
recommendations an admin approves. Nothing here changes the knowledge base
on its own: recommend() only writes recommendations, and each action runs
when an admin approves it (approve, with the actions web_api passes in).

    use_learnings        one row per question that ran: its outcome (worked |
                         missing_knowledge | poor_retrieval | bad_source |
                         wrong_answer | blocked), the final evidence decision,
                         the answer check, the pages it used and cited. No
                         question or answer text: that stays with the session.
    use_recommendations  kind, target, why (with the numbers), status (open |
                         running | done | dismissed), who decided and when,
                         what the action did

What an outcome means (outcome()):
    blocked            the input guardrail stopped it (left out of the report)
    missing_knowledge  the evidence check's last word was KNOWLEDGE_GAP
    poor_retrieval     RETRIEVAL_FAILURE / INSUFFICIENT_EVIDENCE at the end, or
                       nothing kept from the searches
    bad_source         the sources contradicted each other (CONFLICTING_EVIDENCE)
    wrong_answer       the answer check failed, or the output guardrail withheld it
    worked             otherwise

The recommendations, over the last WINDOW_DAYS (a thumbs-down on an answer
counts against the pages it cited, as a poor answer):
    refresh_source    a cited page people marked out of date, a stale one
                      (recrawl: gone, failing or overdue), or one behind
                      POOR_SHARE of its answers not checked in FRESH_DAYS
    drop_source       a cited page that is gone, or still behind DROP_SHARE of
                      at least DROP_MIN answers although checked recently
    add_source        a knowledge-gap topic (R54) asked UNANSWERED_MIN times
                      without a passing answer; approving needs a URL
    rerun_evaluation  a set last run on the live flow before the content
                      changed (a page changed, an ingestion finished, or an
                      approved recommendation changed sources)

A dismissed recommendation doesn't come back for SNOOZE_DAYS, a done one
for FRESH_DAYS (a re-run: until the content changes again). A reset (knowledge_system) empties both tables, so a new
domain starts clean; this builds on the Analytics page (R52) and the gap
report (R54) rather than being a report of its own.
"""

import threading
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

WINDOW_DAYS = 30
POOR_MIN, POOR_SHARE = 2, 0.5
DROP_MIN, DROP_SHARE = 3, 0.6
FRESH_DAYS = 7
SNOOZE_DAYS = 30
UNANSWERED_MIN = 2
KINDS = ("refresh_source", "drop_source", "add_source", "rerun_evaluation")
OUTCOMES = ("worked", "missing_knowledge", "poor_retrieval", "bad_source", "wrong_answer", "blocked")
CONTENT_KINDS = ("refresh_source", "drop_source", "add_source")

_schema_ready = False
_schema_lock = threading.Lock()


class NotOpen(LookupError):
    """No open recommendation with that id (decided already, or withdrawn)."""


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
        import sessions
        sessions.ensure_schema()
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616758)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS use_learnings (
                    turn_id bigint PRIMARY KEY REFERENCES app_session_turns (turn_id) ON DELETE CASCADE,
                    asked_at timestamptz NOT NULL DEFAULT now(),
                    flow_id text,
                    outcome text NOT NULL,
                    decision text,
                    passed boolean,
                    overall double precision,
                    failed_on text[] NOT NULL DEFAULT '{}',
                    used text[] NOT NULL DEFAULT '{}',
                    cited text[] NOT NULL DEFAULT '{}'
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS use_learnings_asked ON use_learnings (asked_at)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS use_recommendations (
                    id bigserial PRIMARY KEY,
                    kind text NOT NULL,
                    target text NOT NULL,
                    dataset text,
                    why text NOT NULL,
                    evidence jsonb NOT NULL DEFAULT '{}',
                    status text NOT NULL DEFAULT 'open',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    decided_by text,
                    decided_at timestamptz,
                    result jsonb
                )""")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS use_recommendations_open "
                               "ON use_recommendations (kind, target) WHERE status IN ('open', 'running')")
        _schema_ready = True


# ---------- Learnings ----------

def _decision(result: dict, side: str) -> str:
    return (((result.get("guardrails") or {}).get(side) or {}).get("decision")) or "ALLOW"


def outcome(result: dict) -> str:
    """What one question's run says (the module docstring has the rules)."""
    if _decision(result, "input") != "ALLOW":
        return "blocked"
    decisions = [e.get("decision") for e in result.get("evidence") or [] if isinstance(e, dict)]
    final = decisions[-1] if decisions else None
    if final == "KNOWLEDGE_GAP":
        return "missing_knowledge"
    if final in ("RETRIEVAL_FAILURE", "INSUFFICIENT_EVIDENCE") or (result.get("funnel") or {}).get("kept") == 0:
        return "poor_retrieval"
    if final == "CONFLICTING_EVIDENCE":
        return "bad_source"
    if (result.get("answer_check") or {}).get("passed") is False or _decision(result, "output") != "ALLOW":
        return "wrong_answer"
    return "worked"


def record(turn_id: int, result: dict) -> str:
    """Keep what a question's run taught (no text); its outcome."""
    ensure_schema()
    check = result.get("answer_check") or {}
    decisions = [e.get("decision") for e in result.get("evidence") or [] if isinstance(e, dict)]
    cited = list(dict.fromkeys(s["url"] for s in result.get("sources") or [] if isinstance(s, dict) and s.get("url")))
    used = list(dict.fromkeys((result.get("funnel") or {}).get("pages") or [])) or cited
    kind = outcome(result)
    with _connect() as connection:
        connection.execute("""
            INSERT INTO use_learnings (turn_id, flow_id, outcome, decision, passed, overall, failed_on, used, cited)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (turn_id) DO UPDATE SET outcome = EXCLUDED.outcome, decision = EXCLUDED.decision,
                passed = EXCLUDED.passed, overall = EXCLUDED.overall, failed_on = EXCLUDED.failed_on,
                used = EXCLUDED.used, cited = EXCLUDED.cited""",
            (turn_id, (result.get("flow") or {}).get("id"), kind, decisions[-1] if decisions else None,
             check.get("passed"), check.get("overall"), list(check.get("failed_on") or []), used, cited))
    return kind


def _rows(connection, since: datetime) -> list[dict]:
    return connection.execute("""
        SELECT l.*, f.rating, f.reason FROM use_learnings l
        LEFT JOIN answer_feedback f ON f.turn_id = l.turn_id
        WHERE l.asked_at >= %s AND l.outcome <> 'blocked'""", (since,)).fetchall()


def _feedback_ready() -> None:
    import answer_feedback
    answer_feedback.ensure_schema()


def _datasets(connection, urls: list[str]) -> dict[str, str]:
    """Which knowledge base each page is in now (curated first); pages no longer in either are left out."""
    if not urls:
        return {}
    out = {}
    for table, name in (("research_chunks", "research"), ("rag_chunks", "curated")):
        if connection.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"]:
            for r in connection.execute(f"SELECT DISTINCT source_url FROM {table} WHERE source_url = ANY(%s)", (urls,)):
                out[r["source_url"]] = name
    return out


def sources(rows: list[dict], connection) -> list[dict]:
    """Per cited page: answers, how many worked, poor answers (failed or thumbs down), out-of-date votes."""
    pages: dict[str, dict] = {}
    for r in rows:
        poor = r["outcome"] in ("wrong_answer", "bad_source") or r["rating"] == "down"
        for url in r["cited"]:
            page = pages.setdefault(url, {"url": url, "answers": 0, "worked": 0, "poor": 0, "out_of_date": 0})
            page["answers"] += 1
            page["worked"] += r["outcome"] == "worked" and r["rating"] != "down"
            page["poor"] += poor
            page["out_of_date"] += r["reason"] == "out_of_date"
    where = _datasets(connection, list(pages))
    out = [{**p, "dataset": where.get(p["url"]), "poor_share": round(p["poor"] / p["answers"], 3)} for p in pages.values()]
    return sorted(out, key=lambda p: (-p["poor"], -p["answers"], p["url"]))


# ---------- Recommendations ----------

def _schedules() -> dict[str, dict]:
    import recrawl
    recrawl.ensure_schema()
    return {r["source_url"]: r for r in recrawl.freshness()}


def _parse(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _source_recommendations(pages: list[dict], schedules: dict[str, dict], now: datetime) -> list[dict]:
    out = []
    for p in pages:
        if not p["dataset"]:                       # no longer in the knowledge base
            continue
        s = schedules.get(p["url"]) or {}
        checked = _parse(s.get("last_checked_at"))
        recent = checked is not None and checked >= now - timedelta(days=FRESH_DAYS)
        numbers = {k: p[k] for k in ("answers", "worked", "poor", "out_of_date")} | {"status": s.get("status")}
        if s.get("status") == "gone":
            out.append(("drop_source", p, f"The page is gone (last check: {s.get('error') or 'not found'}) "
                                          f"but was cited by {p['answers']} answer(s) in the last {WINDOW_DAYS} days.", numbers))
        elif p["poor"] >= DROP_MIN and p["poor_share"] >= DROP_SHARE and recent:
            out.append(("drop_source", p, f"{p['poor']} of {p['answers']} answers citing it were poor, although the page "
                                          f"was checked in the last {FRESH_DAYS} days.", numbers))
        elif p["out_of_date"]:
            out.append(("refresh_source", p, f"{p['out_of_date']} answer(s) citing it were marked out of date.", numbers))
        elif s.get("stale"):
            out.append(("refresh_source", p, f"It is stale ({s.get('status')}, {s.get('failures') or 0} failed check(s)) "
                                             f"and was cited by {p['answers']} answer(s).", numbers))
        elif p["poor"] >= POOR_MIN and p["poor_share"] >= POOR_SHARE and not recent:
            out.append(("refresh_source", p, f"{p['poor']} of {p['answers']} answers citing it were poor, and it hasn't been "
                                             f"checked in {FRESH_DAYS} days.", numbers))
    return [{"kind": kind, "target": p["url"], "dataset": p["dataset"], "why": why, "evidence": numbers}
            for kind, p, why, numbers in out]


def _gap_recommendations(begin: datetime, end: datetime) -> list[dict]:
    import knowledge_gaps
    report = knowledge_gaps.report(begin, end, learned_only=True)
    return [{"kind": "add_source", "target": t["topic"], "dataset": None,
             "why": f"Asked {t['questions']} time(s), {t['unanswered']} without a passing answer: the knowledge base "
                    f"doesn't cover it.",
             "evidence": {"questions": t["questions"], "unanswered": t["unanswered"], "items": t["items"][:5]}}
            for t in report["topics"] if t["unanswered"] >= UNANSWERED_MIN]


def _changed_at(connection) -> datetime | None:
    """When the knowledge base last changed: a page re-crawled with changes, an ingestion, an approved change."""
    times = [connection.execute("SELECT max(decided_at) AS t FROM use_recommendations "
                                "WHERE status = 'done' AND kind = ANY(%s)", (list(CONTENT_KINDS),)).fetchone()["t"]]
    for table, sql in (("source_schedules", "SELECT max(last_changed_at) AS t FROM source_schedules"),
                       ("ingestion_jobs", "SELECT max(finished_at) AS t FROM ingestion_jobs WHERE status = 'success'")):
        if connection.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"]:
            times.append(connection.execute(sql).fetchone()["t"])
    times = [t for t in times if t]
    return max(times) if times else None


def _eval_recommendations(connection, live: tuple[str, str] | None) -> list[dict]:
    if not live:
        return []
    from flows import eval_gate
    eval_gate.ensure_schema()
    changed = _changed_at(connection)
    if changed is None:
        return []
    queued = {r["target"] for r in connection.execute(   # re-runs approved since the change: on their way
        "SELECT target FROM use_recommendations WHERE kind = 'rerun_evaluation' AND status = 'done' AND decided_at > %s",
        (changed,))}
    out = []
    for set_id, run in eval_gate._newest(connection, *live).items():
        finished = _parse(run.get("finished_at"))
        if finished and finished < changed and set_id not in queued:
            out.append({"kind": "rerun_evaluation", "target": set_id, "dataset": None,
                        "why": f"“{run.get('set_name') or set_id}” last ran on the live flow ({live[0]} v{live[1]}) on "
                               f"{finished:%d %b %H:%M}, before the knowledge base changed ({changed:%d %b %H:%M}).",
                        "evidence": {"flow_id": live[0], "version": live[1], "last_run": run.get("id"),
                                     "pass_rate": (run.get("summary") or {}).get("pass_rate")}})
    return out


def recommend(live: tuple[str, str] | None = None, now: datetime | None = None) -> list[dict]:
    """Work out the recommendations again (open ones refreshed, ones no longer warranted withdrawn) and return
    the open ones. live: (flow id, version) of the live flow. Writes only use_recommendations."""
    now = now or datetime.now(timezone.utc)
    ensure_schema()
    _feedback_ready()
    begin = now - timedelta(days=WINDOW_DAYS)
    with _connect() as connection:
        pages = sources(_rows(connection, begin), connection)
    schedules = _schedules()
    wanted = _source_recommendations(pages, schedules, now) + _gap_recommendations(begin, now + timedelta(seconds=1))
    with _connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72616759)")
        wanted += _eval_recommendations(connection, live)
        quiet = {(r["kind"], r["target"]) for r in connection.execute("""
            SELECT kind, target FROM use_recommendations
            WHERE (status = 'dismissed' AND decided_at > %s)
               OR (status = 'done' AND decided_at > %s AND kind <> 'rerun_evaluation')""",
            (now - timedelta(days=SNOOZE_DAYS), now - timedelta(days=FRESH_DAYS)))}
        wanted = [w for w in wanted if (w["kind"], w["target"]) not in quiet]
        keys = [f"{w['kind']}\x1f{w['target']}" for w in wanted]
        connection.execute("DELETE FROM use_recommendations WHERE status = 'open' AND NOT (kind || chr(31) || target = ANY(%s))",
                           (keys,))
        for w in wanted:
            connection.execute("""
                INSERT INTO use_recommendations (kind, target, dataset, why, evidence) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (kind, target) WHERE status IN ('open', 'running')
                DO UPDATE SET why = EXCLUDED.why, evidence = EXCLUDED.evidence, dataset = EXCLUDED.dataset,
                    updated_at = now()""", (w["kind"], w["target"], w["dataset"], w["why"], Jsonb(w["evidence"])))
    return recommendations()


def _view(r: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}


def recommendations(status: str = "open", limit: int = 100) -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM use_recommendations WHERE status = ANY(%s) "
                                  "ORDER BY array_position(%s, kind), updated_at DESC LIMIT %s",
                                  ([status] if status != "decided" else ["done", "dismissed"], list(KINDS), limit))
        return [_view(r) for r in rows]


def _claim(rec_id: int) -> dict:
    with _connect() as connection:
        row = connection.execute("UPDATE use_recommendations SET status = 'running', updated_at = now() "
                                 "WHERE id = %s AND status = 'open' RETURNING *", (rec_id,)).fetchone()
    if not row:
        raise NotOpen(f"no open recommendation {rec_id}")
    return row


def approve(rec_id: int, user_id: str | None, actions: dict, url: str | None = None) -> dict:
    """Run what an open recommendation proposes, as the admin who approved it; the recommendation, done.
    actions: kind → callable(recommendation, url) → result. add_source needs the URL to add. On an error the
    recommendation is open again and the error raised."""
    ensure_schema()
    if url is not None:
        url = url.strip()
    row = _claim(rec_id)
    try:
        if row["kind"] == "add_source" and not (url and url.startswith(("https://", "http://"))):
            raise ValueError("add_source needs the http(s) URL of a page covering it")
        result = actions[row["kind"]](row, url)
    except BaseException:
        with _connect() as connection:
            connection.execute("UPDATE use_recommendations SET status = 'open' WHERE id = %s", (rec_id,))
        raise
    with _connect() as connection:
        done = connection.execute("""
            UPDATE use_recommendations SET status = 'done', decided_by = %s, decided_at = now(), updated_at = now(),
                result = %s WHERE id = %s RETURNING *""", (user_id, Jsonb(result or {}), rec_id)).fetchone()
    return _view(done)


def dismiss(rec_id: int, user_id: str | None) -> dict:
    """Set an open recommendation aside (it stays away SNOOZE_DAYS); nothing else changes."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            UPDATE use_recommendations SET status = 'dismissed', decided_by = %s, decided_at = now(), updated_at = now()
            WHERE id = %s AND status = 'open' RETURNING *""", (user_id, rec_id)).fetchone()
    if not row:
        raise NotOpen(f"no open recommendation {rec_id}")
    return _view(row)


def report(begin: datetime, end: datetime) -> dict:
    """For Analytics: outcomes in the period, per-source numbers, and the open and recently decided recommendations."""
    ensure_schema()
    _feedback_ready()
    with _connect() as connection:
        rows = [r for r in _rows(connection, begin) if r["asked_at"] < end]
        pages = sources(rows, connection)
    counts = {o: 0 for o in OUTCOMES if o != "blocked"}
    for r in rows:
        counts[r["outcome"]] += 1
    return {"questions": len(rows), "outcomes": counts, "sources": pages[:50],
            "recommendations": recommendations(), "decided": recommendations("decided", 20)}

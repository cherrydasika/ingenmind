"""Analytics (R52): where questions fail, which sources help, quality and cost
over time, from what the app already keeps. The data is small (thousands of
rows), so PostgreSQL answers it directly:

    analytics_questions  a view over app_session_turns: one row per question
                         (asked, channel, input/output guardrail decision, the
                         evidence decision, answer check, seconds, the
                         retrieval funnel)
    analytics_runs       a view over agent_flow_runs: real users' runs (time,
                         tokens, blocked, passed)

Each turn keeps a small retrieval summary when it is recorded (funnel()):
how many chunks each search method found, how many were kept, which were
cited, by chunk id and page (no text).
"""

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg
from psycopg.rows import dict_row

import knowledge_gaps
from common import config

MAX_DAYS = 366
MAP_POINTS = 1500
TOP_SOURCES = 15
EXPIRING_DAYS = (7, 30)

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
    from flows import store
    with _schema_lock:
        if _schema_ready:
            return
        sessions.ensure_schema()
        store.ensure_schema()
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616752)")
            connection.execute("""
                CREATE OR REPLACE VIEW analytics_questions AS
                SELECT turn_id, asked_at, user_id,
                       coalesce(result->>'channel', 'app') AS channel,
                       coalesce(result->'guardrails'->'input'->>'decision', 'ALLOW') AS input_decision,
                       coalesce(result->'guardrails'->'output'->>'decision', 'ALLOW') AS output_decision,
                       result->'evidence'->-1->>'decision' AS evidence,
                       (result->'answer_check'->>'passed')::boolean AS passed,
                       (result->'answer_check'->>'overall')::real AS overall,
                       (result->>'total')::real AS seconds,
                       result->'funnel' AS funnel,
                       coalesce(result->'sources', '[]'::jsonb) AS sources
                FROM app_session_turns""")
            connection.execute("""
                CREATE OR REPLACE VIEW analytics_runs AS
                SELECT started_at, flow_id, version, seconds, input_tokens, output_tokens,
                       input_blocked, output_blocked, evaluated, passed, overall, failed
                FROM agent_flow_runs WHERE source = 'users'""")
        _schema_ready = True


# ---------- the knowledge base (curated pages and the pages research added) ----------

def _live(sql: str, params: tuple = ()) -> list[dict]:
    """sql over both chunk tables ({table}); a table not created yet adds nothing."""
    from common import storage
    rows = []
    for dataset in ("curated", "research"):
        client = storage.get_client(dataset)
        try:
            with client.connection() as connection:
                rows += connection.execute(sql.format(table=client.table), params).fetchall()
        except psycopg.errors.UndefinedTable:
            continue
    return rows


def _pages() -> dict[str, int]:
    """Each live page and its number of chunks."""
    return {r["source_url"]: r["n"] for r in _live(
        "SELECT source_url, count(*) AS n FROM {table} WHERE expires_at > now() GROUP BY 1")}


def _count() -> int:
    return sum(r["n"] for r in _live("SELECT count(*) AS n FROM {table} WHERE expires_at > now()"))


def _vectors(limit: int, prefer: list[str]) -> list[dict]:
    """A sample of live chunks with their unit vectors; the chunks in prefer (used ones) always among them."""
    rows = _live("""SELECT id::text AS id, source_url, embedding, (id::text = ANY(%s)) AS used FROM {table}
                    WHERE expires_at > now() ORDER BY (id::text = ANY(%s)) DESC, random() LIMIT %s""", (prefer, prefer, limit))
    rows = sorted(rows, key=lambda r: not r["used"])[:limit]
    out = []
    for r in rows:
        raw = r["embedding"]
        vector = np.asarray(raw.to_numpy() if hasattr(raw, "to_numpy") else raw, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm:
            out.append({"id": r["id"], "url": r["source_url"], "vector": vector / norm})
    return out


def _expiry() -> list[dict]:
    """Each live page and when its first chunk expires, the soonest first."""
    return sorted(_live("SELECT source_url AS url, min(expires_at) AS expires_at FROM {table} "
                        "WHERE expires_at > now() GROUP BY 1"), key=lambda r: r["expires_at"])


def funnel(result: dict) -> dict | None:
    """The retrieval summary a turn keeps: per search method how many chunks, kept, cited (ids and pages)."""
    searches = [s for s in result.get("searches") or [] if s and s.get("retrieval")]
    if not searches:
        return None
    dense = sum(len(s["retrieval"].get("dense_ranking") or []) for s in searches)
    sparse = sum(len(s["retrieval"].get("sparse_ranking") or []) for s in searches)
    numbers = {int(n) for n in re.findall(r"\[(\d+)\]", result.get("answer") or "")}   # [n]: search["first"] + its rank
    kept, cited = {}, set()
    for s in searches:
        for i, chunk in enumerate(s["retrieval"].get("chunks") or []):
            kept[str(chunk["id"])] = chunk["source_url"]
            if s.get("first") is not None and s["first"] + i in numbers:
                cited.add(str(chunk["id"]))
    return {"searches": len(searches), "dense": dense, "sparse": sparse, "kept": len(kept), "cited": len(cited),
            "chunks": sorted(kept), "cited_chunks": sorted(cited), "pages": sorted(set(kept.values()))}


def period(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    """[start, end) from two ISO dates (inclusive end); the last 30 days by default. ValueError when off."""
    today = datetime.now(timezone.utc).date()
    try:
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=29)
    except ValueError:
        raise ValueError("dates are YYYY-MM-DD") from None
    if first > last:
        raise ValueError("the start is after the end")
    if (last - first).days >= MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days")
    begin = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    return begin, datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)


def journey(begin: datetime, end: datetime) -> dict:
    """Asked → allowed or blocked → enough evidence or not → passed, withheld or blocked: the Sankey's links."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT input_decision = 'ALLOW' AS allowed,
                   CASE WHEN evidence IS NULL THEN 'not assessed' WHEN evidence = 'GOOD_EVIDENCE' THEN 'enough'
                        ELSE 'not enough' END AS evidence,
                   CASE WHEN output_decision <> 'ALLOW' THEN 'blocked' WHEN passed THEN 'passed'
                        WHEN passed IS NULL THEN 'not checked' ELSE 'withheld' END AS outcome,
                   count(*) AS n
            FROM analytics_questions WHERE asked_at >= %s AND asked_at < %s GROUP BY 1, 2, 3""", (begin, end)).fetchall()
    links: dict[tuple[str, str], int] = {}
    add = lambda a, b, n: links.__setitem__((a, b), links.get((a, b), 0) + n)
    for r in rows:
        if not r["allowed"]:
            add("Asked", "Blocked at the start", r["n"])
            continue
        add("Asked", "Allowed", r["n"])
        evidence = {"enough": "Enough evidence", "not enough": "Not enough evidence",
                    "not assessed": "Evidence not assessed"}[r["evidence"]]
        add("Allowed", evidence, r["n"])
        add(evidence, {"passed": "Answer passed", "withheld": "Answer withheld", "blocked": "Blocked at the end",
                       "not checked": "Not checked"}[r["outcome"]], r["n"])
    return {"asked": sum(r["n"] for r in rows),
            "links": [{"source": a, "target": b, "value": n} for (a, b), n in links.items()]}


def search_funnel(begin: datetime, end: datetime) -> dict:
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            SELECT count(*) AS questions, coalesce(sum((funnel->>'searches')::int), 0) AS searches,
                   coalesce(sum((funnel->>'dense')::int), 0) AS dense, coalesce(sum((funnel->>'sparse')::int), 0) AS sparse,
                   coalesce(sum((funnel->>'kept')::int), 0) AS kept, coalesce(sum((funnel->>'cited')::int), 0) AS cited
            FROM analytics_questions WHERE asked_at >= %s AND asked_at < %s AND funnel IS NOT NULL""", (begin, end)).fetchone()
    return dict(row)


def sources(begin: datetime, end: datetime) -> dict:
    """Pages by how often answers cite them and searches find them; pages no search found."""
    ensure_schema()
    with _connect() as connection:
        cited = connection.execute("""
            SELECT s->>'url' AS url, count(DISTINCT q.turn_id) AS answers,
                   sum(jsonb_array_length(coalesce(s->'cited', '[]'))) AS citations
            FROM analytics_questions q, jsonb_array_elements(q.sources) s
            WHERE q.asked_at >= %s AND q.asked_at < %s AND jsonb_array_length(coalesce(s->'cited', '[]')) > 0
            GROUP BY 1""", (begin, end)).fetchall()
        found = connection.execute("""
            SELECT p AS url, count(*) AS searches FROM analytics_questions q, jsonb_array_elements_text(q.funnel->'pages') p
            WHERE q.asked_at >= %s AND q.asked_at < %s GROUP BY 1""", (begin, end)).fetchall()
    pages = _pages()
    retrieved = {r["url"]: r["searches"] for r in found}
    by_url = {r["url"]: r for r in cited}
    ranked = sorted(({"url": u, "answers": by_url[u]["answers"], "citations": int(by_url[u]["citations"]),
                      "found": retrieved.get(u, 0), "chunks": pages.get(u)} for u in by_url),
                    key=lambda r: (-r["answers"], -r["citations"], r["url"]))
    never = sorted(u for u in pages if u not in retrieved and u not in by_url)
    return {"most": ranked[:TOP_SOURCES], "least": sorted(ranked, key=lambda r: (r["answers"], r["url"]))[:TOP_SOURCES]
            if len(ranked) > TOP_SOURCES else [], "never": never[:200], "never_count": len(never), "pages": len(pages),
            "tracked": bool(found)}


def knowledge_map(begin: datetime, end: datetime) -> dict:
    """A 3-D map (PCA) of a sample of the knowledge base, each chunk with how often searches found it."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT c AS id, count(*) AS n FROM analytics_questions q, jsonb_array_elements_text(q.funnel->'chunks') c
            WHERE q.asked_at >= %s AND q.asked_at < %s GROUP BY 1""", (begin, end)).fetchall()
    uses = {r["id"]: r["n"] for r in rows}
    chunks = _vectors(MAP_POINTS, prefer=list(uses))
    if len(chunks) < 3:
        return {"points": []}
    x = np.stack([c["vector"] for c in chunks])
    x = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    xyz = x @ vt[:3].T
    return {"points": [{"x": round(float(p[0]), 4), "y": round(float(p[1]), 4), "z": round(float(p[2]), 4),
                        "url": c["url"], "uses": uses.get(c["id"], 0)} for c, p in zip(chunks, xyz)],
            "sampled": len(chunks), "total": _count()}


def _prices() -> tuple[float | None, float | None, str]:
    import llm
    prices = json.loads((Path(__file__).with_name("model_prices.json")).read_text())
    found = prices["models"].get(llm.MODEL) or {}
    return found.get("input"), found.get("output"), llm.MODEL


def trends(begin: datetime, end: datetime) -> dict:
    """Per day: questions, pass rate, median and 95th-percentile seconds, tokens and their approximate cost."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("""
            SELECT date_trunc('day', started_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs,
                   count(*) FILTER (WHERE evaluated) AS evaluated, count(*) FILTER (WHERE passed) AS passed,
                   count(*) FILTER (WHERE input_blocked OR output_blocked) AS blocked,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds) AS p95,
                   sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens
            FROM analytics_runs WHERE started_at >= %s AND started_at < %s GROUP BY 1 ORDER BY 1""", (begin, end)).fetchall()
    price_in, price_out, model = _prices()
    days = []
    for r in rows:
        cost = (r["input_tokens"] * price_in + r["output_tokens"] * price_out) / 1e6 if price_in is not None else None
        days.append({"day": r["day"].isoformat(), "runs": r["runs"], "blocked": r["blocked"],
                     "pass_rate": round(r["passed"] / r["evaluated"], 3) if r["evaluated"] else None,
                     "p50": round(r["p50"], 2), "p95": round(r["p95"], 2), "input_tokens": int(r["input_tokens"]),
                     "output_tokens": int(r["output_tokens"]), "cost": round(cost, 4) if cost is not None else None})
    return {"days": days, "model": model, "priced": price_in is not None}


def freshness() -> dict:
    """Pages by when they expire (the soonest first), and the re-crawl's stale pages (R49)."""
    expiring = _expiry()
    now = datetime.now(timezone.utc)
    soon, later = (now + timedelta(days=EXPIRING_DAYS[0]), now + timedelta(days=EXPIRING_DAYS[1]))
    buckets = {"within_7_days": 0, "within_30_days": 0, "later": 0}
    for page in expiring:
        key = "within_7_days" if page["expires_at"] <= soon else "within_30_days" if page["expires_at"] <= later else "later"
        buckets[key] += 1
    try:
        import recrawl
        stale = sum(1 for r in recrawl.freshness() if r["stale"])
    except Exception:
        stale = None
    return {"buckets": buckets, "stale": stale,
            "soonest": [{"url": p["url"], "expires_at": p["expires_at"].isoformat()} for p in expiring[:10]]}


def report(start: str | None = None, end: str | None = None) -> dict:
    begin, finish = period(start, end)
    return {"from": begin.date().isoformat(), "to": (finish - timedelta(days=1)).isoformat()[:10],
            "journey": journey(begin, finish), "funnel": search_funnel(begin, finish), "sources": sources(begin, finish),
            "map": knowledge_map(begin, finish), "trends": trends(begin, finish), "freshness": freshness(),
            "gaps": knowledge_gaps.report(begin, finish)}

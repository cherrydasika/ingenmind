"""Analytics export (R57): the app's analytics data as Parquet files, to
explore offline with DuckDB, without touching the live database.

    python export_analytics.py --out DIR [--include-text]

or, for admins, Analytics → Download export (a zip of the same files), which
is how to get them off the EC2 server through the usual tunnel.

Personal data is left out by default: no email addresses or names ever;
people and sessions are pseudonymous references (a hash of the id, the same
in every export of one install); the text people wrote (questions, answers,
feedback comments, and the knowledge-gap items paraphrasing questions) only
with include_text. Vectors and chunk text are never exported: pages come as a
per-page summary.

The files (one per table; a table not created yet is left out) and
explore.sql, DuckDB views over them and example queries:

    duckdb -init explore.sql        (in the folder)

Each query below reads PostgreSQL into newline-delimited JSON, which DuckDB
then writes as Parquet, each column with its PostgreSQL type (DUCK_TYPES).
"""

import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from common import config

STARTER = Path(__file__).with_name("analytics_explore.sql")

# file: (SQL, the columns that are people's text: dropped unless include_text)
TABLES = {
    "questions": ("""
        SELECT turn_id, session_id AS session_ref, user_id AS user_ref, asked_at,
               coalesce(result->>'channel', 'app') AS channel, result->'flow'->>'id' AS flow_id,
               result->'flow'->>'version' AS flow_version,
               coalesce(result->'guardrails'->'input'->>'decision', 'ALLOW') AS input_decision,
               coalesce(result->'guardrails'->'output'->>'decision', 'ALLOW') AS output_decision,
               result->'evidence'->-1->>'decision' AS evidence,
               (result->'answer_check'->>'passed')::boolean AS passed,
               (result->'answer_check'->>'overall')::float8 AS overall,
               (result->>'total')::float8 AS seconds, coalesce((result->>'cached')::boolean, false) AS cached,
               jsonb_array_length(coalesce(result->'sources', '[]')) AS cited_pages,
               (result->'funnel'->>'kept')::int AS chunks_kept, (result->'funnel'->>'cited')::int AS chunks_cited,
               question, answer
        FROM app_session_turns ORDER BY turn_id""", ("question", "answer")),
    "citations": ("""
        SELECT t.turn_id, t.asked_at, s->>'url' AS url, jsonb_array_length(coalesce(s->'cited', '[]')) AS citations
        FROM app_session_turns t, jsonb_array_elements(coalesce(t.result->'sources', '[]')) s ORDER BY t.turn_id""", ()),
    "runs": ("""
        SELECT id, flow_id, version, source, started_at, seconds, rounds, tasks, input_tokens, output_tokens,
               input_blocked, output_blocked, evaluated, passed, overall, failed, timed_out, budget_hit,
               user_id AS user_ref, channel, model
        FROM agent_flow_runs ORDER BY id""", ()),
    "run_agents": ("SELECT run_id, role, model, seconds, input_tokens, output_tokens, turns FROM agent_flow_run_agents "
                   "ORDER BY run_id", ()),
    "eval_runs": ("""
        SELECT id, batch, set_id, set_name, flow_id, version, status, total, completed, created_at, started_at,
               finished_at, (summary->>'pass_rate')::float8 AS pass_rate, (summary->>'overall')::float8 AS overall,
               (summary->>'questions')::int AS questions, (summary->>'evaluated')::int AS evaluated,
               (summary->>'passed')::int AS passed, (summary->>'failed')::int AS failed,
               (summary->>'tokens')::int AS tokens, coalesce((options->>'scheduled')::boolean, false) AS scheduled,
               coalesce((options->>'memory')::boolean, true) AS memory
        FROM agent_eval_runs ORDER BY created_at""", ()),
    "eval_results": ("""
        SELECT run_id, idx, kind, area, expectation_met, passed, overall, failed_on::text AS failed_on, answer_type,
               input_blocked, output_blocked, failed, seconds, tokens, answer
        FROM agent_eval_results ORDER BY run_id, idx""", ("answer",)),
    "ingestion_jobs": ("""
        SELECT id::text AS id, kind, status, created_at, started_at, finished_at, jsonb_array_length(entries) AS entries,
               next_index, left(error, 500) AS error
        FROM ingestion_jobs ORDER BY created_at""", ()),
    "pages": ("""
        SELECT 'curated' AS dataset, source_url, count(*) AS chunks, sum(length(body)) AS chars,
               min(expires_at) AS first_expires, max(expires_at) AS last_expires, max(payload->>'organisation') AS organisation
        FROM rag_chunks GROUP BY source_url
        UNION ALL
        SELECT 'research', source_url, count(*), sum(length(body)), min(expires_at), max(expires_at),
               max(payload->>'organisation')
        FROM research_chunks GROUP BY source_url""", ()),
    "knowledge_gaps": ("""
        SELECT g.turn_id, t.asked_at, g.after_research, g.answered, g.missing
        FROM knowledge_gaps g JOIN app_session_turns t USING (turn_id) ORDER BY g.id""", ("missing",)),
    "feedback": ("SELECT turn_id, user_id AS user_ref, rating, reason, created_at, promoted_to IS NOT NULL AS promoted, "
                 "comment FROM answer_feedback ORDER BY turn_id", ("comment",)),
}
REFERENCES = ("user_ref", "session_ref")   # ids replaced by a hash
# PostgreSQL type oid → DuckDB type (anything else: text), so an empty table's file has its real types too
DUCK_TYPES = {16: "BOOLEAN", 20: "BIGINT", 21: "BIGINT", 23: "BIGINT", 700: "DOUBLE", 701: "DOUBLE", 1700: "DOUBLE",
              1082: "DATE", 1114: "TIMESTAMP", 1184: "TIMESTAMPTZ"}


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def _ref(value: str | None) -> str | None:
    """A pseudonymous reference: the same id gives the same reference; it can't be turned back into the id."""
    if value is None:
        return None
    salt = hashlib.sha256(f"{config.PGPASSWORD}:{config.PGDATABASE}".encode()).hexdigest()   # stable, secret, per install
    return hashlib.sha256(f"ingenmind-export:{salt}:{value}".encode()).hexdigest()[:16]


def _plain(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def export(out: Path, include_text: bool = False) -> dict:
    """Write each table as <out>/<name>.parquet and explore.sql; the manifest (also written as manifest.json)."""
    import duckdb
    from flows import store
    store.ensure_schema()       # the run columns R53 added, on an older install
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(), "include_text": include_text, "tables": {},
                "skipped": []}
    with _connect() as connection, tempfile.TemporaryDirectory() as scratch:
        db = duckdb.connect()
        for name, (sql, text_columns) in TABLES.items():
            try:
                with connection.transaction():
                    cursor = connection.execute(sql)
                    columns = {c.name: DUCK_TYPES.get(c.type_code, "VARCHAR") for c in cursor.description}
                    rows = cursor.fetchall()
            except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):   # not created on this install yet
                manifest["skipped"].append(name)
                continue
            kept = [c for c in columns if include_text or c not in text_columns]
            types = {c: "VARCHAR" if c in REFERENCES else columns[c] for c in kept}
            typed = ", ".join(f'NULL::{t} AS "{c}"' for c, t in types.items())
            spec = "{" + ", ".join(f"'{c}': '{t}'" for c, t in types.items()) + "}"
            target = out / f"{name}.parquet"
            if not rows:
                db.execute(f"COPY (SELECT {typed} LIMIT 0) "
                           f"TO '{target}' (FORMAT parquet)")
            else:
                lines = Path(scratch) / f"{name}.ndjson"
                with lines.open("w") as f:
                    for row in rows:
                        f.write(json.dumps({c: (_ref(row[c]) if c in REFERENCES else _plain(row[c])) for c in kept},
                                           default=str) + "\n")
                db.execute(f"COPY (SELECT * FROM read_json('{lines}', format='newline_delimited', columns={spec})) "
                           f"TO '{target}' (FORMAT parquet)")
            manifest["tables"][name] = {"rows": len(rows), "columns": kept}
        db.close()
    shutil.copyfile(STARTER, out / "explore.sql")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def bundle(include_text: bool = False) -> tuple[bytes, dict]:
    """The export as one zip (for the download): (its bytes, the manifest)."""
    with tempfile.TemporaryDirectory() as folder:
        manifest = export(Path(folder), include_text)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(Path(folder).iterdir()):
                archive.write(path, path.name)
    return buffer.getvalue(), manifest

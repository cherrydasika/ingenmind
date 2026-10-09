import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import config

DOCUMENT_TABLES = ("rag_chunks", "research_chunks")


class EmbeddingMismatch(RuntimeError):
    """The stored vectors come from a different embedding model than the configured one."""


def _configured_embedding() -> dict:
    return {"provider": config.EMBEDDING_PROVIDER, "model": config.EMBEDDING_MODEL,
            "dimension": config.EMBEDDING_DIM}


def _mismatch_message(stored: dict) -> str:
    current = _configured_embedding()
    return (f"The knowledge base holds {stored['provider']} {stored['model']} vectors ({stored['dimension']} "
            f"dimensions) but EMBEDDING_PROVIDER/EMBEDDING_MODEL select {current['provider']} {current['model']} "
            f"({current['dimension']}). Restore the old settings, or re-embed every chunk with the new model: "
            f"docker compose run --rm worker python -m reindex_embeddings")


def _stored_embedding(connection) -> dict | None:
    """The model behind the stored vectors; None before ensure_collection
    first ran on this database."""
    if connection.execute("SELECT to_regclass('embedding_config') AS name").fetchone()["name"] is None:
        return None
    return connection.execute("SELECT provider, model, dimension FROM embedding_config").fetchone()


def _check_embedding(connection) -> None:
    stored = _stored_embedding(connection)
    if stored is not None and dict(stored) != _configured_embedding():
        raise EmbeddingMismatch(_mismatch_message(stored))


class PgStore:
    def __init__(self, table: str = "rag_chunks"):
        if table not in DOCUMENT_TABLES:
            raise ValueError("unknown document table")
        self.table = table

    @contextmanager
    def connection(self):
        with psycopg.connect(
            host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
            password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row,
        ) as connection:
            register_vector(connection)
            yield connection


_store = PgStore()
_research_store = PgStore("research_chunks")


def get_client(dataset: str = "curated") -> PgStore:
    if dataset == "curated":
        return _store
    if dataset == "research":
        return _research_store
    raise ValueError("unknown document dataset")


# Serialises schema setup: the worker and the demo seed may both run it at once.
SCHEMA_LOCK = 72_616_701


def ensure_collection(client: PgStore) -> None:
    with psycopg.connect(
        host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
        password=config.PGPASSWORD, dbname=config.PGDATABASE,
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with client.connection() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        for table in DOCUMENT_TABLES:
            connection.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {table} (
                id uuid PRIMARY KEY,
                source_url text NOT NULL,
                chunk_index integer NOT NULL,
                body text NOT NULL,
                embedding vector({dimension}) NOT NULL,
                search_document tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
                paragraph_hashes text[] NOT NULL DEFAULT '{{}}',
                expires_at timestamptz NOT NULL,
                payload jsonb NOT NULL
            )
            """).format(table=sql.Identifier(table), dimension=sql.Literal(config.EMBEDDING_DIM)))
        _reconcile_embedding(connection)
        for table in DOCUMENT_TABLES:
            for suffix, definition in (
                ("source", "(source_url)"), ("expiry", "(expires_at)"),
                ("paragraphs", "USING gin (paragraph_hashes)"), ("text", "USING gin (search_document)"),
                ("embedding", "USING hnsw (embedding vector_cosine_ops)"),
            ):
                connection.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} {}").format(
                    sql.Identifier(f"{table}_{suffix}"), sql.Identifier(table), sql.SQL(definition)))
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id uuid PRIMARY KEY,
                kind text NOT NULL,
                status text NOT NULL CHECK (status IN ('queued', 'running', 'success', 'failed')),
                created_at timestamptz NOT NULL DEFAULT now(),
                started_at timestamptz,
                finished_at timestamptz,
                entries jsonb NOT NULL DEFAULT '[]',
                next_index integer NOT NULL DEFAULT 0,
                summary jsonb,
                error text
            )
        """)
        # The kinds, as a named constraint so a new kind can be added to an existing table.
        connection.execute("ALTER TABLE ingestion_jobs DROP CONSTRAINT IF EXISTS ingestion_jobs_kind_check")
        connection.execute("ALTER TABLE ingestion_jobs ADD CONSTRAINT ingestion_jobs_kind_check "
                           f"CHECK (kind IN ({', '.join(repr(k) for k in JOB_KINDS)}))")
        connection.execute("CREATE INDEX IF NOT EXISTS ingestion_jobs_recent ON ingestion_jobs (kind, created_at DESC)")
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_one_active ON ingestion_jobs ((true))
            WHERE status IN ('queued', 'running')
        """)


def _reconcile_embedding(connection) -> None:
    """Record the configured model on a new database, or move an empty one
    to it; refuse a model change over stored vectors (they would not be
    comparable with new ones)."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS embedding_config (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            provider text NOT NULL,
            model text NOT NULL,
            dimension integer NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    current = _configured_embedding()
    stored = connection.execute("SELECT provider, model, dimension FROM embedding_config").fetchone()
    if stored is not None and dict(stored) == current:
        return
    empty = all(connection.execute(sql.SQL("SELECT NOT EXISTS (SELECT 1 FROM {}) AS empty")
                                   .format(sql.Identifier(table))).fetchone()["empty"] for table in DOCUMENT_TABLES)
    column_dims = {_column_dimension(connection, table) for table in DOCUMENT_TABLES}
    if not empty:
        if stored is not None:
            raise EmbeddingMismatch(_mismatch_message(stored))
        # A database from before embedding_config: its vectors are taken to
        # come from the configured model when the dimensions agree.
        if column_dims != {current["dimension"]}:
            raise EmbeddingMismatch(_mismatch_message(
                {"provider": "an earlier", "model": "model", "dimension": ", ".join(map(str, sorted(column_dims)))}))
    elif column_dims != {current["dimension"]}:
        for table in DOCUMENT_TABLES:
            connection.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(f"{table}_embedding")))
            connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN embedding TYPE vector({})").format(
                sql.Identifier(table), sql.Literal(current["dimension"])))
    _record_embedding(connection)


def _column_dimension(connection, table: str) -> int:
    return connection.execute("""
        SELECT atttypmod AS dimension FROM pg_attribute
        WHERE attrelid = %s::regclass AND attname = 'embedding'
    """, (table,)).fetchone()["dimension"]


def _record_embedding(connection) -> None:
    current = _configured_embedding()
    connection.execute("""
        INSERT INTO embedding_config (id, provider, model, dimension) VALUES (true, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, model = EXCLUDED.model,
            dimension = EXCLUDED.dimension, updated_at = now()
    """, (current["provider"], current["model"], current["dimension"]))


def stored_embedding(client: PgStore) -> dict | None:
    with client.connection() as connection:
        stored = _stored_embedding(connection)
    return dict(stored) if stored else None


def chunk_bodies(client: PgStore) -> dict[str, list[tuple]]:
    """Every stored chunk's id and text, per document table (for re-embedding)."""
    with client.connection() as connection:
        return {table: [(row["id"], row["body"]) for row in connection.execute(
                    sql.SQL("SELECT id, body FROM {} ORDER BY id").format(sql.Identifier(table))).fetchall()]
                for table in DOCUMENT_TABLES}


def replace_embeddings(client: PgStore, vectors: dict[str, dict]) -> None:
    """Swap every table's embedding column for new vectors ({table: {id:
    vector}}, every row covered) from the configured model, in one
    transaction, and record that model."""
    dimension = config.EMBEDDING_DIM
    with client.connection() as connection:
        for table in DOCUMENT_TABLES:
            ids = {row["id"] for row in connection.execute(
                sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))).fetchall()}
            if ids != set(vectors.get(table, {})):
                raise RuntimeError(f"{table} changed during re-embedding; stop the worker and run it again")
            name = sql.Identifier(table)
            connection.execute(sql.SQL("ALTER TABLE {} ADD COLUMN embedding_next vector({})").format(
                name, sql.Literal(dimension)))
            with connection.cursor() as cursor:
                cursor.executemany(sql.SQL("UPDATE {} SET embedding_next = %s WHERE id = %s").format(name), [
                    (np.asarray(vector, dtype=np.float32), row_id) for row_id, vector in vectors[table].items()])
            connection.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(f"{table}_embedding")))
            connection.execute(sql.SQL("ALTER TABLE {} DROP COLUMN embedding").format(name))
            connection.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN embedding_next TO embedding").format(name))
            connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN embedding SET NOT NULL").format(name))
            connection.execute(sql.SQL("CREATE INDEX {} ON {} USING hnsw (embedding vector_cosine_ops)").format(
                sql.Identifier(f"{table}_embedding"), name))
        _record_embedding(connection)


# ingest_plan: an approved ingestion plan from guided setup ({"plan_version"}), no URL cap.
JOB_KINDS = ("ingest_urls", "prune_expired_documents", "ingest_plan")


def enqueue_job(client: PgStore, kind: str, entries: list[dict] | None = None) -> dict:
    if kind not in JOB_KINDS:
        raise ValueError("unknown ingestion job")
    if kind == "ingest_plan" and not (entries and isinstance(entries[0].get("plan_version"), int)):
        raise ValueError("an ingest_plan job needs its plan_version")
    entries = entries or []
    if kind == "ingest_urls" and not (1 <= len(entries) <= config.MAX_URLS_PER_JOB):
        raise ValueError(f"select between 1 and {config.MAX_URLS_PER_JOB} URLs")
    job_id = uuid.uuid4()
    with client.connection() as connection:
        row = connection.execute("""
            INSERT INTO ingestion_jobs (id, kind, status, entries)
            VALUES (%s, %s, 'queued', %s) RETURNING id, kind, status, created_at
        """, (job_id, kind, Jsonb(entries))).fetchone()
    return {**row, "id": str(row["id"])}


def list_jobs(client: PgStore, kind: str, limit: int = 5) -> list[dict]:
    with client.connection() as connection:
        rows = connection.execute("""
            SELECT id, kind, status, created_at, started_at, finished_at,
                   next_index, jsonb_array_length(entries) AS total, summary, error
            FROM ingestion_jobs WHERE kind = %s ORDER BY created_at DESC LIMIT %s
        """, (kind, limit)).fetchall()
    return [{**row, "id": str(row["id"])} for row in rows]


def recover_running_jobs(client: PgStore) -> None:
    with client.connection() as connection:
        connection.execute("UPDATE ingestion_jobs SET status = 'queued' WHERE status = 'running'")


def claim_job(client: PgStore) -> dict | None:
    with client.connection() as connection:
        row = connection.execute("""
            UPDATE ingestion_jobs SET status = 'running', started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM ingestion_jobs WHERE status = 'queued'
                ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
            ) RETURNING id, kind, entries, next_index, summary
        """).fetchone()
    return {**row, "id": str(row["id"])} if row else None


def update_job(client: PgStore, job_id: str, status: str, next_index: int, summary: dict,
               error: str | None = None) -> None:
    if status not in ("running", "success", "failed"):
        raise ValueError("invalid job status")
    with client.connection() as connection:
        connection.execute("""
            UPDATE ingestion_jobs SET status = %s, next_index = %s, summary = %s,
                error = %s, finished_at = CASE WHEN %s = 'running' THEN NULL ELSE now() END
            WHERE id = %s
        """, (status, next_index, Jsonb(summary), error, status, uuid.UUID(job_id)))


def find_paragraph_owners(client: PgStore, hashes: list[str], exclude_source: str) -> set[str]:
    if not hashes:
        return set()
    with client.connection() as connection:
        rows = connection.execute(
            sql.SQL("SELECT paragraph_hashes FROM {} WHERE paragraph_hashes && %s::text[] AND source_url <> %s")
            .format(sql.Identifier(client.table)),
            (hashes, exclude_source),
        ).fetchall()
    return set(hashes).intersection(hash_value for row in rows for hash_value in row["paragraph_hashes"])


def get_existing_metadata(client: PgStore, url: str) -> dict | None:
    with client.connection() as connection:
        row = connection.execute(
            sql.SQL("SELECT payload FROM {} WHERE source_url = %s LIMIT 1").format(sql.Identifier(client.table)), (url,),
        ).fetchone()
    return row["payload"] if row else None


def delete_url_points(client: PgStore, url: str) -> None:
    with client.connection() as connection:
        connection.execute(sql.SQL("DELETE FROM {} WHERE source_url = %s").format(sql.Identifier(client.table)), (url,))


def upsert_chunks(
    client: PgStore,
    url: str,
    chunks: list[str],
    vectors: list[list[float]],
    page_hash: str,
    ttl_days: float,
    extra_payload: dict | None = None,
    paragraph_hashes: list[str] | None = None,
    replace_existing: bool = False,
) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("each chunk needs one embedding")
    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + ttl_days * 86400
    point_namespace = uuid.uuid5(uuid.NAMESPACE_URL, url if client.table == "rag_chunks" else f"research:{url}")
    rows = []
    for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
        payload = {
            "source_url": url, "chunk_index": index, "text": chunk,
            "content_hash": page_hash, "ingested_at": now.isoformat(),
            "ttl_days": ttl_days, "expires_at": expires_at,
            **(extra_payload or {}),
            **({"paragraph_hashes": paragraph_hashes} if index == 0 and paragraph_hashes else {}),
        }
        rows.append((uuid.uuid5(point_namespace, str(index)), url, index, chunk,
                     np.asarray(vector, dtype=np.float32), paragraph_hashes if index == 0 and paragraph_hashes else [],
                     datetime.fromtimestamp(expires_at, timezone.utc), Jsonb(payload)))
    with client.connection() as connection:
        _check_embedding(connection)
        with connection.cursor() as cursor:
            if replace_existing:
                cursor.execute(sql.SQL("DELETE FROM {} WHERE source_url = %s").format(sql.Identifier(client.table)), (url,))
            cursor.executemany(sql.SQL("""
                INSERT INTO {} (id, source_url, chunk_index, body, embedding, paragraph_hashes, expires_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET body = EXCLUDED.body, embedding = EXCLUDED.embedding,
                    paragraph_hashes = EXCLUDED.paragraph_hashes, expires_at = EXCLUDED.expires_at,
                    payload = EXCLUDED.payload
            """).format(sql.Identifier(client.table)), rows)


def merge_payload(client: PgStore, url: str, fields: dict) -> None:
    """Add or replace payload fields on every chunk of a page (provenance of a page not re-read)."""
    with client.connection() as connection:
        connection.execute(sql.SQL("UPDATE {} SET payload = payload || %s WHERE source_url = %s").format(
            sql.Identifier(client.table)), (Jsonb(fields), url))


def refresh_expiry(client: PgStore, url: str, ttl_days: float) -> None:
    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + ttl_days * 86400
    with client.connection() as connection:
        connection.execute(sql.SQL("""
            UPDATE {} SET expires_at = %s,
                payload = payload || %s WHERE source_url = %s
        """).format(sql.Identifier(client.table)), (datetime.fromtimestamp(expires_at, timezone.utc),
              Jsonb({"ingested_at": now.isoformat(), "expires_at": expires_at, "ttl_days": ttl_days}), url))


def delete_expired(client: PgStore) -> None:
    with client.connection() as connection:
        for table in DOCUMENT_TABLES:
            connection.execute(sql.SQL("DELETE FROM {} WHERE expires_at < now()").format(sql.Identifier(table)))


def search_dense(client: PgStore, vector: list[float], limit: int) -> list[dict]:
    vector = np.asarray(vector, dtype=np.float32)
    with client.connection() as connection:
        _check_embedding(connection)
        rows = connection.execute("""
            SELECT id, payload, embedding, 1 - distance AS score FROM (
                (SELECT id, payload, embedding, embedding <=> %s AS distance
                 FROM rag_chunks WHERE expires_at > now() ORDER BY embedding <=> %s LIMIT %s)
                UNION ALL
                (SELECT id, payload, embedding, embedding <=> %s AS distance
                 FROM research_chunks WHERE expires_at > now() ORDER BY embedding <=> %s LIMIT %s)
            ) AS candidates ORDER BY distance, id LIMIT %s
        """, (vector, vector, limit, vector, vector, limit, limit)).fetchall()
    return [{**row, "id": str(row["id"]), "vector": row["embedding"].to_list()} for row in rows]


def search_text(client: PgStore, question: str, limit: int) -> list[dict]:
    with client.connection() as connection:
        rows = connection.execute("""
            SELECT id, payload, embedding, score FROM (
                (SELECT id, payload, embedding, ts_rank_cd(search_document, plainto_tsquery('english', %s)) AS score
                 FROM rag_chunks WHERE expires_at > now() AND search_document @@ plainto_tsquery('english', %s)
                 ORDER BY score DESC, id LIMIT %s)
                UNION ALL
                (SELECT id, payload, embedding, ts_rank_cd(search_document, plainto_tsquery('english', %s)) AS score
                 FROM research_chunks WHERE expires_at > now() AND search_document @@ plainto_tsquery('english', %s)
                 ORDER BY score DESC, id LIMIT %s)
            ) AS candidates ORDER BY score DESC, id LIMIT %s
        """, (question, question, limit, question, question, limit, limit)).fetchall()
    return [{**row, "id": str(row["id"]), "vector": row["embedding"].to_list()} for row in rows]


def count_points(client: PgStore) -> int:
    with client.connection() as connection:
        return connection.execute(sql.SQL("SELECT count(*) AS total FROM {}").format(sql.Identifier(client.table))).fetchone()["total"]


def source_summary(client: PgStore) -> list[dict]:
    with client.connection() as connection:
        return connection.execute(sql.SQL("""
            SELECT first.payload, counts.chunks FROM
                (SELECT source_url, count(*) AS chunks FROM {table} GROUP BY source_url) AS counts
            JOIN {table} AS first ON first.source_url = counts.source_url AND first.chunk_index = 0
            ORDER BY first.payload->>'ingested_at' DESC
        """).format(table=sql.Identifier(client.table))).fetchall()


def list_adhoc(client: PgStore) -> list[dict]:
    return [{"source_id": row["payload"]["source_url"],
             "title": row["payload"].get("title", row["payload"]["source_url"]),
             "chunks": row["chunks"], "ingested_at": row["payload"].get("ingested_at", ""),
             "expires_at": row["payload"].get("expires_at")}
            for row in source_summary(client) if row["payload"].get("source_type") == "adhoc"]


def sample_record(client: PgStore) -> dict | None:
    with client.connection() as connection:
        row = connection.execute(sql.SQL("SELECT id, payload, embedding FROM {} LIMIT 1").format(sql.Identifier(client.table))).fetchone()
    return {**row, "id": str(row["id"]), "vector": row["embedding"].to_list()} if row else None


def background_vectors(client: PgStore, limit: int) -> list[dict]:
    with client.connection() as connection:
        rows = connection.execute(sql.SQL("SELECT id, embedding FROM {} LIMIT %s").format(sql.Identifier(client.table)), (limit,)).fetchall()
    return [{"id": str(row["id"]), "vector": row["embedding"].to_list()} for row in rows]
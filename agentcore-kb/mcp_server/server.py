"""MCP server exposing one tool, search_knowledge_base, over the RAG app's
pgvector table (rag_chunks) on EC2.

The stored chunks were embedded with Amazon Titan Text Embeddings v2
(256 dimensions, normalized), so queries are embedded the same way; a query
vector from any other model could not be compared with them.

    python mcp_server/server.py            # stdio, for local MCP clients
    python mcp_server/server.py --http     # streamable HTTP on 0.0.0.0:8000/mcp (AgentCore Runtime)

Reads PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD and AWS_REGION from
the environment (or a .env file). On AgentCore Runtime, PGPASSWORD_PARAMETER
names an SSM SecureString holding the password instead, so it never appears
in the runtime's configuration. Exits at startup if the password, the
database or the table is unavailable.
"""

import argparse
import contextlib
import json
import logging
import os
import sys

import boto3
import psycopg2
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv()

# stdout carries the MCP protocol in stdio mode, so all logging goes to stderr.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
log = logging.getLogger("knowledge-base")

TABLE = os.environ.get("KB_TABLE", "rag_chunks")
TOP_K = 5
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIM = 256
MAX_QUERY_CHARS = 2000
# Payload fields worth showing the model; the rest (hashes, dedup counters) is noise.
METADATA_FIELDS = ("source_url", "chunk_index", "ingested_at", "ttl_days")

_password = None


def db_password() -> str:
    """PGPASSWORD, or the SSM SecureString named by PGPASSWORD_PARAMETER
    (read once, at startup)."""
    global _password
    if _password is None:
        if os.environ.get("PGPASSWORD"):
            _password = os.environ["PGPASSWORD"]
        else:
            name = os.environ["PGPASSWORD_PARAMETER"]
            ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
            _password = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
            log.info("database password read from SSM parameter %s", name)
    return _password


mcp = MCPServer(
    "knowledge-base",
    instructions="Search the RAG knowledge base (ingested web pages) with search_knowledge_base.",
)
_bedrock = None


@contextlib.contextmanager
def _cursor():
    """A cursor on a fresh connection. psycopg2's own `with connection` only
    ends the transaction, so the connection is closed here explicitly."""
    connection = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=db_password(),
        connect_timeout=5,
    )
    try:
        with connection, connection.cursor() as cursor:
            yield cursor
    finally:
        connection.close()


def check_database() -> None:
    """Fail fast with a clear message if the database or table is unusable."""
    missing = [name for name in ("PGHOST", "PGDATABASE", "PGUSER") if not os.environ.get(name)]
    if not (os.environ.get("PGPASSWORD") or os.environ.get("PGPASSWORD_PARAMETER")):
        missing.append("PGPASSWORD (or PGPASSWORD_PARAMETER)")
    if missing:
        sys.exit(f"knowledge-base: missing environment variables: {', '.join(missing)}")
    try:
        db_password()
    except (BotoCoreError, ClientError) as error:
        sys.exit(f"knowledge-base: cannot read the database password from SSM "
                 f"{os.environ.get('PGPASSWORD_PARAMETER')!r}: {error}")
    try:
        with _cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
            if not cursor.fetchone()[0]:
                sys.exit(f"knowledge-base: table {TABLE!r} not found in database {os.environ['PGDATABASE']!r}")
            cursor.execute(f"SELECT count(*) FROM {TABLE}")
            log.info("database reachable: %s has %d chunks", TABLE, cursor.fetchone()[0])
    except psycopg2.OperationalError as error:
        sys.exit(f"knowledge-base: cannot reach PostgreSQL at "
                 f"{os.environ['PGHOST']}:{os.environ.get('PGPORT', '5432')}: {str(error).strip()}")


def embed(text: str) -> list[float]:
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
    response = _bedrock.invoke_model(
        modelId=EMBEDDING_MODEL,
        body=json.dumps({"inputText": text, "dimensions": EMBEDDING_DIM, "normalize": True}),
        contentType="application/json",
        accept="application/json",
    )
    vector = json.loads(response["body"].read())["embedding"]
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"embedding has {len(vector)} dimensions, expected {EMBEDDING_DIM}")
    return vector


def search(query: str) -> list[dict]:
    vector = "[" + ",".join(f"{value:.8f}" for value in embed(query)) + "]"
    with _cursor() as cursor:
        cursor.execute(f"""
            SELECT payload->>'text', payload - 'text', 1 - (embedding <=> %s::vector)
            FROM {TABLE}
            WHERE expires_at > now()
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (vector, vector, TOP_K))
        return [{"content": content,
                 "metadata": {k: metadata[k] for k in METADATA_FIELDS if k in (metadata or {})},
                 "similarity": float(similarity)}
                for content, metadata, similarity in cursor.fetchall()]


def format_results(query: str, results: list[dict]) -> str:
    if not results:
        return f"No knowledge base results for: {query}"
    lines = [f"Top {len(results)} knowledge base results for: {query}", ""]
    for number, result in enumerate(results, 1):
        metadata = result["metadata"] or {}
        source = metadata.get("source_url", "unknown source")
        lines.append(f"[{number}] {source} (similarity {result['similarity']:.3f})")
        lines.append(result["content"].strip())
        lines.append(f"metadata: {json.dumps(metadata, default=str)}")
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
def search_knowledge_base(query: str) -> str:
    """Semantic search over the knowledge base. Returns the 5 most similar
    chunks as a numbered list ([1] … [5]) with source URL, similarity score,
    text and metadata; cite them by number."""
    query = query.strip()[:MAX_QUERY_CHARS]
    if not query:
        return "Error: the query is empty."
    try:
        results = search(query)
    except (BotoCoreError, ClientError, ValueError, KeyError) as error:
        log.exception("embedding failed")
        return f"Error: could not embed the query ({type(error).__name__}: {error})."
    except psycopg2.Error as error:
        log.exception("search failed")
        return f"Error: knowledge base search failed ({type(error).__name__}: {str(error).strip()})."
    return format_results(query, results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP on 0.0.0.0:8000/mcp instead of stdio")
    args = parser.parse_args()
    check_database()
    if args.http:
        # AgentCore Runtime expects a stateless MCP server on 0.0.0.0:8000/mcp.
        mcp.run("streamable-http", host="0.0.0.0", port=8000, stateless_http=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()

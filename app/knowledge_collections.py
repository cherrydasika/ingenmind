"""Collections (R35): several knowledge bases in one deployment, each a named
set of sources with its own description and who may search it. A knowledge
agent is an Agent whose Retriever is set to one collection; a router (or
supervisor) above them picks per question, and answers cite which collection
each source is from.

    knowledge_collections         collection_id, name, description, roles
                                  (empty: everyone), created_at
    knowledge_collection_sources  source_url → collection_id (a source in no
                                  row is in the main collection)

The main collection is every source not assigned elsewhere: the one guided
setup builds; it is always there and everyone may search it. Membership is
kept per source, outside the chunks, so re-ingesting a page keeps it.

Searching (agent._Run._search): scope(user_id, wanted) is the collection
filter for one search: only the collections the user's role may see; a
Retriever set to a collection the user can't see finds nothing.
"""

import re
import threading

import psycopg
from psycopg.rows import dict_row

import identity
from common import config

MAIN = "main"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
NAME_CHARS = 80
DESCRIPTION_CHARS = 600

_schema_ready = False
_schema_lock = threading.Lock()


class CollectionError(ValueError):
    """A collection that can't be made or changed as asked."""


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616743)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_collections (
                    collection_id text PRIMARY KEY,
                    name text NOT NULL,
                    description text NOT NULL DEFAULT '',
                    roles text[] NOT NULL DEFAULT '{}',
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_collection_sources (
                    source_url text PRIMARY KEY,
                    collection_id text NOT NULL REFERENCES knowledge_collections ON DELETE CASCADE
                )""")
        _schema_ready = True


MAIN_NAME = "Main knowledge base"


def _clean(name: str, description: str, roles) -> tuple[str, str, list[str]]:
    name = " ".join(str(name or "").split())[:NAME_CHARS]
    if not name:
        raise CollectionError("a collection needs a name")
    roles = sorted({r for r in (roles or []) if isinstance(r, str)})
    unknown = [r for r in roles if r not in identity.ROLES]
    if unknown:
        raise CollectionError(f"unknown role(s): {', '.join(unknown)}")
    return name, str(description or "").strip()[:DESCRIPTION_CHARS], roles


def create(collection_id: str, name: str, description: str = "", roles=()) -> dict:
    if not ID_PATTERN.match(collection_id or "") or collection_id == MAIN:
        raise CollectionError("the id must be 2–40 lower-case letters, digits or _, starting with a letter, not 'main'")
    name, description, roles = _clean(name, description, roles)
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO knowledge_collections (collection_id, name, description, roles) VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING RETURNING *""", (collection_id, name, description, roles)).fetchone()
    if row is None:
        raise CollectionError(f"there is already a collection {collection_id!r}")
    return row


def update(collection_id: str, name: str, description: str = "", roles=()) -> dict:
    """The main collection may be renamed and described (kept as a row), but everyone may always search it."""
    name, description, roles = _clean(name, description, roles)
    ensure_schema()
    with _connect() as connection:
        if collection_id == MAIN:
            return connection.execute("""
                INSERT INTO knowledge_collections (collection_id, name, description, roles) VALUES (%s, %s, %s, '{}')
                ON CONFLICT (collection_id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description
                RETURNING *""", (MAIN, name, description)).fetchone()
        row = connection.execute("""
            UPDATE knowledge_collections SET name = %s, description = %s, roles = %s WHERE collection_id = %s
            RETURNING *""", (name, description, roles, collection_id)).fetchone()
    if row is None:
        raise CollectionError(f"no collection {collection_id!r}")
    return row


def delete(collection_id: str) -> bool:
    """Its sources go back to the main collection (nothing is deleted from the knowledge base)."""
    if collection_id == MAIN:
        raise CollectionError("the main collection can't be deleted")
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute("DELETE FROM knowledge_collections WHERE collection_id = %s",
                                       (collection_id,)).rowcount)


def assign(source_urls: list[str], collection_id: str) -> int:
    """Move sources into a collection (MAIN: back to the main one)."""
    ensure_schema()
    urls = sorted({u for u in source_urls if isinstance(u, str) and u})
    with _connect() as connection:
        if collection_id == MAIN:
            return connection.execute("DELETE FROM knowledge_collection_sources WHERE source_url = ANY(%s)",
                                      (urls,)).rowcount
        if not connection.execute("SELECT 1 FROM knowledge_collections WHERE collection_id = %s AND collection_id <> 'main'",
                                  (collection_id,)).fetchone():
            raise CollectionError(f"no collection {collection_id!r}")
        with connection.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO knowledge_collection_sources (source_url, collection_id) VALUES (%s, %s)
                ON CONFLICT (source_url) DO UPDATE SET collection_id = EXCLUDED.collection_id""",
                               [(u, collection_id) for u in urls])
        return len(urls)


def _rows() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("SELECT * FROM knowledge_collections ORDER BY name").fetchall()


def defined() -> list[dict]:
    """The collections an admin made (not the main one)."""
    return [r for r in _rows() if r["collection_id"] != MAIN]


def all_collections() -> list[dict]:
    """Every collection, the main one first: id, name, description, roles (the main one's always empty)."""
    rows = _rows()
    main = next((r for r in rows if r["collection_id"] == MAIN), None)
    return [{"collection_id": MAIN, "name": (main or {}).get("name") or MAIN_NAME, "roles": [],
             "created_at": (main or {}).get("created_at"),
             "description": (main or {}).get("description") or "Every source not assigned to another collection "
                                                                 "(what guided setup builds)."},
            *[r for r in rows if r["collection_id"] != MAIN]]


def of_sources(source_urls) -> dict[str, str]:
    """source_url → collection id, for the given sources (MAIN when unassigned)."""
    urls = sorted({u for u in source_urls if u})
    if not urls:
        return {}
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT source_url, collection_id FROM knowledge_collection_sources "
                                  "WHERE source_url = ANY(%s)", (urls,)).fetchall()
    found = {r["source_url"]: r["collection_id"] for r in rows}
    return {u: found.get(u, MAIN) for u in urls}


def counts() -> dict[str, int]:
    """Assigned sources per collection."""
    ensure_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT collection_id, count(*) AS n FROM knowledge_collection_sources "
                                  "GROUP BY collection_id").fetchall()
    return {r["collection_id"]: r["n"] for r in rows}


def visible(role: str | None, collections: list[dict] | None = None) -> set[str]:
    """The collection ids a role may search (None: a system run, such as an evaluation, sees all)."""
    collections = all_collections() if collections is None else collections
    return {c["collection_id"] for c in collections
            if role is None or c["collection_id"] == MAIN or not c["roles"] or role in c["roles"]}


def role_of(user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = identity.get_user(user_id)
    return (user or {}).get("role") or ""


def scope(user_id: str | None, wanted: str = "") -> list[str] | None | bool:
    """The collection filter for one search: None when no filter is needed (no collections, or
    the user sees them all and no collection is wanted), a list of ids, or False when the wanted
    collection isn't one the user may see (or doesn't exist)."""
    collections = all_collections()
    if len(collections) == 1 and not wanted:
        return None
    allowed = visible(role_of(user_id), collections)
    if wanted:
        return [wanted] if wanted in allowed else False
    return None if allowed == {c["collection_id"] for c in collections} else sorted(allowed)


TOKEN = "{collections}"


def expand(text: str, user_id: str | None = None, everyone: bool = False) -> str:
    """A brief's {collections}: the collections this user may search (everyone: all of them, for the
    scope check), each with its description, so a flow never names one the user can't see."""
    if TOKEN not in (text or ""):
        return text
    collections = all_collections()
    allowed = None if everyone else visible(role_of(user_id), collections)
    listed = "; ".join(f"{c['name']} ({(c.get('description') or '').strip() or 'its own sources'})"
                       for c in collections if allowed is None or c["collection_id"] in allowed)
    return text.replace(TOKEN, listed)


def names() -> dict[str, str]:
    return {c["collection_id"]: c["name"] for c in all_collections()}

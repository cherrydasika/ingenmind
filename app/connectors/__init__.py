"""Document connectors (R48): knowledge that lives in files and drives, not on
public websites, into the knowledge base with provenance.

    Connector       list() → [Item], fetch(item_id) → bytes,
                    changed_since(when) → [Item]; a document keeps where it
                    came from (connector, item id, modified, etag)
    parse(name, data)   text of a PDF, DOCX, PPTX, HTML, Markdown or text file
    ingest(...)         one file through the ad-hoc path (chunk → embed → store),
                        keyed by its source (upload://…, s3://bucket/key)

    connectors      connector_id, kind (s3), name, config (bucket, prefix,
                    endpoint_url, region), secret (sealed access keys; none:
                    the server's own AWS credentials), created_at

Connectors here: uploads (Data → Ad-hoc documents) and S3 (or anything that
speaks S3, such as MinIO). Google Drive, SharePoint/OneDrive, Confluence and
Notion need an OAuth app on the organisation's own account first.
"""

import io
import re
import threading
import zipfile
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import config

MAX_BYTES = 10_000_000
FORMATS = {".pdf": "PDF", ".docx": "Word", ".pptx": "PowerPoint", ".html": "HTML", ".htm": "HTML",
           ".md": "Markdown", ".markdown": "Markdown", ".txt": "text"}

_schema_ready = False
_schema_lock = threading.Lock()


class ConnectorError(ValueError):
    """A file or a connector that can't be used."""


@dataclass
class Item:
    id: str                      # the connector's own id (an S3 key)
    name: str
    size: int = 0
    modified: str | None = None  # ISO time
    etag: str | None = None
    meta: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size, "modified": self.modified, "etag": self.etag,
                "supported": extension(self.name) in FORMATS}


def extension(name: str) -> str:
    match = re.search(r"\.[A-Za-z0-9]+$", name or "")
    return match.group(0).lower() if match else ""


# ---------- reading files ----------

def _xml_text(data: bytes, tag: str) -> list[str]:
    from lxml import etree
    root = etree.fromstring(data)
    return ["".join(t.text or "" for t in p.iter() if t.tag.endswith(tag)) for p in root.iter()
            if p.tag.endswith("}p")]


def parse(name: str, data: bytes) -> str:
    """The text of a supported file; ConnectorError when it isn't one or has no text."""
    kind = extension(name)
    if kind not in FORMATS:
        raise ConnectorError(f"{name}: not a supported file ({', '.join(sorted(FORMATS))})")
    if len(data) > MAX_BYTES:
        raise ConnectorError(f"{name}: larger than {MAX_BYTES // 1_000_000} MB")
    try:
        if kind == ".pdf":
            from common import scraping
            text = scraping.extract_pdf_text(data, name)
        elif kind == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as box:
                text = "\n".join(t for t in _xml_text(box.read("word/document.xml"), "}t") if t.strip())
        elif kind == ".pptx":
            with zipfile.ZipFile(io.BytesIO(data)) as box:
                slides = sorted((n for n in box.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
                text = "\n\n".join("\n".join(t for t in _xml_text(box.read(s), "}t") if t.strip()) for s in slides)
        elif kind in (".html", ".htm"):
            from common import scraping
            text = scraping.extract_text(data.decode("utf-8", errors="replace"), name)
        else:
            text = data.decode("utf-8", errors="replace")
    except ConnectorError:
        raise
    except Exception as error:
        raise ConnectorError(f"{name}: couldn't be read ({type(error).__name__}: {error})") from None
    text = (text or "").strip()
    if not text:
        raise ConnectorError(f"{name}: no text in it (a scanned document needs OCR first)")
    return text


def ingest(source: str, name: str, data: bytes, ttl_days: float, provenance: dict | None = None) -> dict:
    """One file into the knowledge base, keyed by its source, with where it came from."""
    import adhoc_ingest
    text = parse(name, data)
    return adhoc_ingest.ingest(name, text, ttl_days, source=source,
                               provenance={"file_name": name, "file_format": FORMATS[extension(name)], **(provenance or {})})


# ---------- stored connectors ----------

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
            connection.execute("SELECT pg_advisory_xact_lock(72616748)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS connectors (
                    connector_id text PRIMARY KEY,
                    kind text NOT NULL,
                    name text NOT NULL,
                    config jsonb NOT NULL DEFAULT '{}',
                    secret text,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def add(connector_id: str, kind: str, name: str, settings: dict, secret: dict | None = None) -> dict:
    import secrets_box
    if not re.match(r"^[a-z][a-z0-9_]{1,23}$", connector_id or ""):
        raise ConnectorError("the id must be 2–24 lower-case letters, digits or _, starting with a letter")
    if kind not in KINDS:
        raise ConnectorError(f"kind: one of {', '.join(KINDS)}")
    settings = KINDS[kind].check(settings)
    try:
        sealed = secrets_box.seal(secret) if secret else None
    except secrets_box.SecretError as error:
        raise ConnectorError(str(error)) from None
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("""
            INSERT INTO connectors (connector_id, kind, name, config, secret) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING RETURNING *""", (connector_id, kind, " ".join(str(name or connector_id).split())[:80],
                                                    Jsonb(settings), sealed)).fetchone()
    if row is None:
        raise ConnectorError(f"there is already a connector {connector_id!r}")
    return public(row)


def get(connector_id: str):
    """The connector, ready to list and fetch."""
    import secrets_box
    ensure_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM connectors WHERE connector_id = %s", (connector_id,)).fetchone()
    if row is None:
        raise ConnectorError(f"no connector {connector_id!r}")
    return KINDS[row["kind"]](row["config"], secrets_box.open_(row["secret"]) if row["secret"] else {})


def remove(connector_id: str) -> bool:
    ensure_schema()
    with _connect() as connection:
        return bool(connection.execute("DELETE FROM connectors WHERE connector_id = %s", (connector_id,)).rowcount)


def listing() -> list[dict]:
    ensure_schema()
    with _connect() as connection:
        return [public(r) for r in connection.execute("SELECT * FROM connectors ORDER BY name").fetchall()]


def public(row: dict) -> dict:
    return {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k != "secret"},
            "has_secret": bool(row.get("secret"))}


def import_items(connector_id: str, item_ids: list[str], ttl_days: float) -> list[dict]:
    """Ingest these items of a connector; one result per item (a failure stays with its item)."""
    connector = get(connector_id)
    items = {i.id: i for i in connector.list()}
    out = []
    for item_id in item_ids:
        item = items.get(item_id)
        if item is None:
            out.append({"id": item_id, "status": "error", "error": "not found"})
            continue
        try:
            result = ingest(connector.source(item), item.name, connector.fetch(item.id), ttl_days,
                            {"connector": connector_id, "item_id": item.id, "modified": item.modified, "etag": item.etag})
            out.append({"id": item_id, **result})
        except Exception as error:
            out.append({"id": item_id, "status": "error", "error": f"{error}"[:300]})
    return out


from .s3 import S3Connector  # noqa: E402  (it uses Item)

KINDS = {"s3": S3Connector}

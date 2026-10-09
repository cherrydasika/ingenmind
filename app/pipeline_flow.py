"""Clickable pipeline diagram: Scrape -> Chunk -> Embed -> Store.

Each step leads with a plain-language summary (for a non-technical demo
audience), followed by technical detail pulled from live config values and
real sample data, so the specifics don't require reading the source.

The content is data (step_detail, schema_fields, sample_record), rendered
by the web app's Pipeline docs page.
"""

from common import config, storage

_STEPS = [
    ("scrape", "🌐 Scrape"),
    ("chunk", "✂️ Chunk"),
    ("embed", "🧮 Embed"),
    ("store", "🗄️ Store (Database)"),
]

_SCHEMA_FIELDS = [
    {"Field": "id", "Type": "UUID", "Description": "Deterministic per URL + chunk position, so re-ingesting a page overwrites instead of duplicating."},
    {"Field": "embedding", "Type": f"vector({config.EMBEDDING_DIM})", "Description": f"{config.EMBEDDING_MODEL} embedding, indexed by pgvector."},
    {"Field": "search_document", "Type": "tsvector", "Description": "PostgreSQL-generated English full-text index."},
    {"Field": "payload.source_url", "Type": "string", "Description": "Which page this chunk came from."},
    {"Field": "payload.chunk_index", "Type": "int", "Description": "Position of this chunk within its page."},
    {"Field": "payload.text", "Type": "string", "Description": "The actual chunk text — what gets shown/used at answer time."},
    {"Field": "payload.content_hash", "Type": "string", "Description": "Hash of the page text, used to detect when a page has changed."},
    {"Field": "payload.ingested_at", "Type": "timestamp", "Description": "When this chunk was last (re)ingested."},
    {"Field": "payload.ttl_days", "Type": "number", "Description": "How many days this chunk lives before it's pruned."},
    {"Field": "payload.expires_at", "Type": "timestamp", "Description": "When this chunk gets deleted if not refreshed by then."},
]


def _scrape() -> dict:
    return {
        "summary": "Fetches each page's HTML and pulls out just the clean article text — "
        "stripping navigation, ads, and other boilerplate.",
        "blocks": [
        f"- **Fetch:** `requests.get()` with a custom `User-Agent` header\n"
        f"- **Extraction:** `trafilatura.extract(html, url, include_comments=False, "
        f"include_tables=False)` — boilerplate-removal heuristics, not a fixed CSS selector\n"
        f"- **Change detection:** `sha256` hash of the extracted text — compared against "
        f"the stored `content_hash` to decide re-embed vs. skip\n"
        f"- **Rate limiting:** jittered delay before every request, "
        f"`random.uniform({config.FETCH_MIN_DELAY_SECONDS}, {config.FETCH_MAX_DELAY_SECONDS})` seconds\n"
        f"- **Retry policy:** up to `{config.FETCH_MAX_RETRIES}` attempts, exponential backoff "
        f"`{config.FETCH_BACKOFF_BASE_SECONDS} × 2^attempt` seconds, honors a `Retry-After` header "
        f"when present\n"
        f"- **Retry scope:** only on `429` / `5xx` / connection errors — a `404` or other `4xx` "
        f"fails immediately, no retry (it's a permanent error, not transient)",
        ],
    }

def _chunk() -> dict:
    return {
        "summary": "Splits each page's text into smaller, overlapping pieces — AI embedding "
        "models can only read a limited amount of text at once.",
        "blocks": [
        f"- **Strategy:** fixed-size sliding window over **characters** (not tokens) — "
        f"`dags/common/chunking/fixed_size.py`\n"
        f"- **Chunk size:** `CHUNK_SIZE = {config.CHUNK_SIZE}` characters\n"
        f"- **Overlap:** `CHUNK_OVERLAP = {config.CHUNK_OVERLAP}` characters carried into the next "
        f"chunk, so a sentence split across a boundary isn't lost entirely from either chunk\n"
        f"- **Boundary snapping:** the split point backs up to the nearest preceding space "
        f"(`str.rfind(\" \", start, end)`) so a chunk never ends mid-word\n"
        f"- **Alternative strategy available:** `chunking/per_url.py` — whole page as a single "
        f"chunk instead of splitting. Not active (see the caveat below); swappable via one import "
        f"line in `chunking/__init__.py`\n"
        f"- **Why sliding-window is active, not per-url:** smaller passages keep retrieved context "
        f"focused even though embedding models accept longer inputs",
        f"**🧹 Paragraph dedup, before chunking** (`dags/common/dedup.py`, "
        f"{'on' if config.DEDUP_ENABLED else 'off'}): SEO pages repeat the same paragraphs across pages. "
        f"Each paragraph of at least `DEDUP_MIN_CHARS = {config.DEDUP_MIN_CHARS}` characters is normalised "
        f"(case, punctuation, whitespace) and hashed; it's dropped if it already appeared earlier on the "
        f"same page or another page already stores it — **the first page to store a paragraph keeps the "
        f"only copy**. Shorter lines (headings, labels) are always kept for context. A whole-page hash "
        f"can't do this (it only spots identical pages), and neither can chunk hashes (the same paragraph "
        f"lands at different offsets in different pages' fixed-size windows). Near-duplicates are kept: "
        f"on these pages they're usually different facts (e.g. different route conditions).",
        ],
    }

_EMBEDDING_PROVIDERS = {"bedrock": "Amazon Bedrock", "openai": "OpenAI", "ollama": "Ollama",
                        "local": "fastembed (in-process ONNX)"}


def _embed() -> dict:
    return {
        "summary": "Converts each text chunk into lists of numbers (\"vectors\") that capture "
        "its meaning, so it can be found later by similarity — not just exact keyword matches.",
        "blocks": [
        f"Each chunk stores a dense vector and searchable text:\n\n"
        f"- **Dense (`embedding`):** `{config.EMBEDDING_MODEL}` via "
        f"{_EMBEDDING_PROVIDERS.get(config.EMBEDDING_PROVIDER, config.EMBEDDING_PROVIDER)}, "
        f"{config.EMBEDDING_DIM} floats/chunk, cosine distance. "
        f"A neural embedding — captures meaning, e.g. matches \"how do I get a refund\" to a page "
        f"about cancellations with no shared words. "
        f"{'Runs in-process, free.' if config.EMBEDDING_PROVIDER == 'local' else 'Each chunk and question makes an embedding request.'}\n"
        f"- **Full text:** PostgreSQL generates an English `tsvector` from each chunk, indexed "
        f"with GIN. `plainto_tsquery` selects matching rows and `ts_rank_cd` ranks them.\n\n"
        f"At query time, pgvector cosine results and full-text results are combined in Python "
        f"with reciprocal rank fusion. PostgreSQL full-text ranking is not Qdrant BM25.",
        ],
    }

def _store() -> dict:
    return {
        "summary": "Stores chunk text and embedding vectors in PostgreSQL with pgvector.",
        "blocks": [
        f"- **Table:** `{config.COLLECTION_NAME}`, with `embedding vector({config.EMBEDDING_DIM})`, "
        f"a generated `search_document` and HNSW/GIN indexes.\n"
        f"- **Point ID:** deterministic, `uuid5(uuid5(NAMESPACE_URL, url), str(chunk_index))` — same "
        f"URL + chunk position always produces the same ID, so re-ingesting **overwrites** (`upsert`) "
        f"instead of duplicating\n"
        f"- **Indexes:** B-tree on `source_url` and `expires_at`, GIN on paragraph hashes and text.\n"
        f"- **Write path:** batched `INSERT ... ON CONFLICT` of chunks for each URL",
        ],
    }


_DETAILS = {"scrape": _scrape, "chunk": _chunk, "embed": _embed, "store": _store}


def step_detail(key: str) -> dict:
    """{"key", "title", "summary", "markdown"} for one pipeline step."""
    detail = _DETAILS[key]()
    return {"key": key, "title": dict(_STEPS)[key], "summary": detail["summary"],
            "markdown": "\n\n".join(detail["blocks"])}


def step_keys() -> list[str]:
    return [key for key, _ in _STEPS]


def schema_fields() -> list[dict]:
    return _SCHEMA_FIELDS


def sample_record() -> dict | str:
    """One stored record, vectors shortened — or a message if there's none."""
    client = storage.get_client()
    try:
        point = storage.sample_record(client)
    except Exception:
        return "Table doesn't exist yet — run the ingestion pipeline first."

    if not point:
        return "No records yet — run the ingestion pipeline first."

    dense_vec = point["vector"]
    text = point["payload"].get("text", "")

    sample = {
        "id": point["id"],
        "embedding": f"[{', '.join(f'{v:.3f}' for v in dense_vec[:5])}, ... ({len(dense_vec)} numbers total)]",
        "search_document": "PostgreSQL generated full-text search vector",
        "payload.source_url": point["payload"].get("source_url"),
        "payload.chunk_index": point["payload"].get("chunk_index"),
        "payload.text": (text[:300] + "…") if len(text) > 300 else text,
        "payload.content_hash": point["payload"].get("content_hash"),
        "payload.ingested_at": point["payload"].get("ingested_at"),
        "payload.ttl_days": point["payload"].get("ttl_days"),
        "payload.expires_at": point["payload"].get("expires_at"),
    }
    return sample

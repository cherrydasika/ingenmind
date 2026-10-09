import os
from pathlib import Path

PGHOST = os.environ.get("PGHOST", "pgvector")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGUSER = os.environ.get("PGUSER", "rag")
PGPASSWORD = os.environ.get("PGPASSWORD", "")
PGDATABASE = os.environ.get("PGDATABASE", "rag")
COLLECTION_NAME = "rag_chunks"

# Embeddings (common/embedding/embedder.py): bedrock (default) | openai |
# ollama | local (fastembed, in-process: no key, no service). Every stored
# vector comes from one model; the database records which (pg_store.py).
EMBEDDING_PROVIDERS = ("bedrock", "openai", "ollama", "local")
EMBEDDING_DEFAULTS = {   # provider: (model, dimensions)
    "bedrock": ("amazon.titan-embed-text-v2:0", 256),
    "openai": ("text-embedding-3-small", 1536),
    "ollama": ("nomic-embed-text", 768),
    "local": ("BAAI/bge-small-en-v1.5", 384),
}
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "bedrock").strip().lower() or "bedrock"
_default_model, _default_dim = EMBEDDING_DEFAULTS.get(EMBEDDING_PROVIDER, ("", 0))
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "").strip() or _default_model
# A model other than the provider's default needs EMBEDDING_DIM unless the
# dimensions match; Titan V2 and OpenAI text-embedding-3 shorten to it.
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "").strip() or _default_dim)
# pgvector's HNSW index takes at most this many dimensions.
MAX_EMBEDDING_DIM = 2000
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-2")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
# Where fastembed keeps downloaded models (a volume in Compose).
FASTEMBED_CACHE = os.environ.get("FASTEMBED_CACHE_PATH", "/models")
DENSE_VECTOR_NAME = "dense"

# PostgreSQL generated full-text search field.
SPARSE_MODEL_NAME = "PostgreSQL full-text"
SPARSE_VECTOR_NAME = "search_document"

# Your URL list (gitignored); a fresh clone falls back to the example next to it.
_urls_path = Path(os.environ.get("URLS_CONFIG_PATH", "/data/urls.json"))
URLS_CONFIG_PATH = _urls_path if _urls_path.exists() else _urls_path.with_name("urls.example.json")

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# Paragraph-level dedup across pages before chunking (see common/dedup.py).
# Paragraphs shorter than DEDUP_MIN_CHARS (normalised) — headings, labels —
# are always kept.
DEDUP_ENABLED = os.environ.get("DEDUP_ENABLED", "true").lower() in ("1", "true", "yes")
DEDUP_MIN_CHARS = int(os.environ.get("DEDUP_MIN_CHARS", "50"))
DEFAULT_TTL_DAYS = 90

# A URL is skipped entirely (no fetch at all) if its stored expires_at is
# still further out than this many days. Only new URLs and URLs nearing
# expiry get processed, so adding a few new links doesn't re-crawl everything.
REFRESH_MARGIN_DAYS = float(os.environ.get("REFRESH_MARGIN_DAYS", "3"))

# Polite delay before every fetch, plus backoff on 429/5xx/network errors.
FETCH_MIN_DELAY_SECONDS = float(os.environ.get("FETCH_MIN_DELAY_SECONDS", "1.0"))
FETCH_MAX_DELAY_SECONDS = float(os.environ.get("FETCH_MAX_DELAY_SECONDS", "2.5"))
FETCH_MAX_RETRIES = int(os.environ.get("FETCH_MAX_RETRIES", "4"))
FETCH_BACKOFF_BASE_SECONDS = float(os.environ.get("FETCH_BACKOFF_BASE_SECONDS", "2.0"))
MAX_URLS_PER_JOB = 10
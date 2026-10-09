# Active chunking strategy. Swap this import to try a different one
# (e.g. `from .per_url import chunk_text`) without touching callers.
from .fixed_size import chunk_text

__all__ = ["chunk_text"]

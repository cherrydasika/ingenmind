"""How text is cut into chunks: config.CHUNKING picks the strategy (R21).

    split(text)       [{"text", "headings"}]: what ingestion stores
    chunk_text(...)   the fixed-size windows (the default strategy)
"""

from .. import config
from . import structure
from .fixed_size import chunk_text

__all__ = ["chunk_text", "split", "wants_headings"]


def wants_headings(strategy: str | None = None) -> bool:
    """Whether extraction should keep the page's headings (as markdown)."""
    return (strategy or config.CHUNKING) == "structure"


def split(text: str, strategy: str | None = None) -> list[dict]:
    if (strategy or config.CHUNKING) == "structure":
        return structure.chunk_sections(text, config.CHUNK_SIZE)
    return [{"text": t, "headings": []} for t in chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)]

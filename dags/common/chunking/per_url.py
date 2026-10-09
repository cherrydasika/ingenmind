def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    # chunk_size/overlap are accepted to match the other chunking strategies'
    # signature, but ignored here: a URL's whole extracted text is one chunk.
    text = text.strip()
    return [text] if text else []

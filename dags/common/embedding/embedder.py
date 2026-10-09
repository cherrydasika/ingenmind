"""Dense embeddings from the provider in config.EMBEDDING_PROVIDER.

query=True embeds a search question: some models want questions and
passages marked differently (nomic-embed-text prefixes, bge's query
instruction, which fastembed adds itself)."""

import json
import os
import threading

from .. import config

OPENAI_BATCH = 128
# Ollama models trained with task prefixes: (query prefix, document prefix).
PREFIXES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "mxbai-embed-large": ("Represent this sentence for searching relevant passages: ", ""),
}


def config_error() -> str | None:
    """Why the configured embedding provider cannot run, or None."""
    if config.EMBEDDING_PROVIDER not in config.EMBEDDING_PROVIDERS:
        return (f"EMBEDDING_PROVIDER={config.EMBEDDING_PROVIDER!r} is not one of "
                f"{', '.join(config.EMBEDDING_PROVIDERS)}")
    if not config.EMBEDDING_MODEL or config.EMBEDDING_DIM <= 0:
        return f"set EMBEDDING_MODEL and EMBEDDING_DIM for EMBEDDING_PROVIDER={config.EMBEDDING_PROVIDER}"
    if config.EMBEDDING_DIM > config.MAX_EMBEDDING_DIM:
        return (f"EMBEDDING_DIM={config.EMBEDDING_DIM} is above pgvector's index limit of "
                f"{config.MAX_EMBEDDING_DIM}; set a smaller EMBEDDING_DIM if the model can shorten its vectors")
    if config.EMBEDDING_PROVIDER == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return "set OPENAI_API_KEY for EMBEDDING_PROVIDER=openai"
    return None


def embed_texts(texts: list[str], query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    vectors = _backend().embed(texts, query)
    for vector in vectors:
        if len(vector) != config.EMBEDDING_DIM:
            raise ValueError(f"{config.EMBEDDING_MODEL} returned {len(vector)} dimensions; expected "
                             f"{config.EMBEDDING_DIM} (set EMBEDDING_DIM to the model's size)")
    return vectors


_lock = threading.Lock()
_instance = None


def _backend():
    global _instance
    with _lock:
        if _instance is None:
            error = config_error()
            if error:
                raise RuntimeError(f"Embeddings are not configured: {error}")
            _instance = {"bedrock": _Bedrock, "openai": _OpenAI, "ollama": _OpenAI,
                         "local": _FastEmbed}[config.EMBEDDING_PROVIDER]()
        return _instance


class _Bedrock:
    """Amazon Titan Text Embeddings V2: one text per request."""

    def __init__(self):
        import boto3
        self.client = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)

    def embed(self, texts, query):
        vectors = []
        for text in texts:
            response = self.client.invoke_model(
                modelId=config.EMBEDDING_MODEL,
                body=json.dumps({"inputText": text, "dimensions": config.EMBEDDING_DIM, "normalize": True}),
                contentType="application/json",
                accept="application/json",
            )
            vectors.append(json.loads(response["body"].read())["embedding"])
        return vectors


class _OpenAI:
    """OpenAI, or Ollama's OpenAI-compatible endpoint."""

    def __init__(self):
        import openai
        self.ollama = config.EMBEDDING_PROVIDER == "ollama"
        self.client = (openai.OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama") if self.ollama
                       else openai.OpenAI())
        base = config.EMBEDDING_MODEL.split(":")[0]
        self.prefixes = PREFIXES.get(base, ("", "")) if self.ollama else ("", "")

    def embed(self, texts, query):
        prefix = self.prefixes[0 if query else 1]
        options = {} if self.ollama else {"dimensions": config.EMBEDDING_DIM}
        vectors = []
        for start in range(0, len(texts), OPENAI_BATCH):
            batch = [prefix + text for text in texts[start:start + OPENAI_BATCH]]
            response = self.client.embeddings.create(model=config.EMBEDDING_MODEL, input=batch, **options)
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda item: item.index))
        return vectors


class _FastEmbed:
    """A small ONNX model run in-process; downloaded once into FASTEMBED_CACHE."""

    def __init__(self):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(model_name=config.EMBEDDING_MODEL, cache_dir=config.FASTEMBED_CACHE)

    def embed(self, texts, query):
        vectors = self.model.query_embed(texts) if query else self.model.embed(texts)
        return [vector.tolist() for vector in vectors]

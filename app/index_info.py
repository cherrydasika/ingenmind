"""PostgreSQL index information for the retrieval page."""

from common import config, storage


def get_index_info() -> dict:
    client = storage.get_client()
    return {
        "collection": config.COLLECTION_NAME,
        "points": storage.count_points(client),
        "dense": {"name": "embedding", "dim": config.EMBEDDING_DIM,
                  "distance": "cosine", "index": "pgvector HNSW"},
        "sparse": {"index": "PostgreSQL GIN", "rank": "ts_rank_cd", "language": "english"},
    }
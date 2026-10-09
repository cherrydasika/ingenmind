"""Re-embed every stored chunk with the configured embedding model, after
EMBEDDING_PROVIDER / EMBEDDING_MODEL / EMBEDDING_DIM change.

    docker compose stop worker
    docker compose run --rm worker python -m reindex_embeddings [--yes]
    docker compose start worker

Chunk text is kept, so nothing is fetched again: only the vectors are
replaced, in one transaction once every chunk is embedded. A paid provider
bills one request per chunk (batched for OpenAI)."""

import argparse
import sys
import time

from common import config, embedding, storage

BATCH = 64


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="do not ask before embedding")
    args = parser.parse_args(argv)

    error = embedding.config_error()
    if error:
        print(f"Embeddings are not configured: {error}", file=sys.stderr)
        return 1
    client = storage.get_client()
    stored = storage.stored_embedding(client)
    target = f"{config.EMBEDDING_PROVIDER} {config.EMBEDDING_MODEL} ({config.EMBEDDING_DIM} dimensions)"
    bodies = storage.chunk_bodies(client)
    total = sum(len(rows) for rows in bodies.values())
    print(f"Stored vectors: {stored['provider']} {stored['model']} ({stored['dimension']} dimensions)" if stored
          else "Stored vectors: model not recorded yet")
    print(f"Re-embedding {total} chunks with {target}")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        return 1

    started = time.monotonic()
    vectors, done = {}, 0
    for table, rows in bodies.items():
        vectors[table] = {}
        for start in range(0, len(rows), BATCH):
            batch = rows[start:start + BATCH]
            for (row_id, _), vector in zip(batch, embedding.embed_texts([body for _, body in batch])):
                vectors[table][row_id] = vector
            done += len(batch)
            print(f"  {done}/{total}", end="\r", flush=True)
    storage.replace_embeddings(client, vectors)
    print(f"Done: {total} chunks in {time.monotonic() - started:.1f}s; the knowledge base now uses {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

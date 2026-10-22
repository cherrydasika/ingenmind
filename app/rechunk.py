"""Cut every stored page again with the configured chunking (CHUNKING, R21),
after changing it:

    docker compose run --rm worker python -m rechunk [--yes]

Queues one ingestion job (the worker runs it; the Ingestion page shows its
progress) that fetches each web page and PDF again, cuts it, embeds the new
chunks and replaces the page's old ones. What a page's chunks carried
(labels, provenance, its lifetime) is kept. Ad-hoc documents can't be fetched
again: submit them again to re-cut them. The embedding provider is billed for
every chunk, as for a first ingestion."""

import argparse
import sys

from common import config, storage


def pages(client: storage.PgStore) -> tuple[list[dict], int]:
    """(ingestion entries for the web pages and PDFs, how many ad-hoc documents were left out)."""
    with client.connection() as connection:
        rows = connection.execute(f"""
            SELECT DISTINCT ON (source_url) source_url, payload->>'ttl_days' AS ttl_days, payload->>'chunking' AS chunking
            FROM {client.table} WHERE expires_at > now() ORDER BY source_url, chunk_index""").fetchall()
    web = [r for r in rows if r["source_url"].startswith(("http://", "https://"))]
    entries = [{"url": r["source_url"], "force": True, "keep_payload": True,
                **({"ttl_days": float(r["ttl_days"])} if r["ttl_days"] else {})} for r in web]
    return entries, len(rows) - len(web)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="do not ask before queuing")
    args = parser.parse_args(argv)
    client = storage.get_client()
    entries, adhoc = pages(client)
    if not entries:
        print("No web pages or PDFs are stored: nothing to re-cut.")
        return 0
    print(f"Chunking: {config.CHUNKING}. {len(entries)} pages will be fetched, cut and embedded again"
          + (f"; {adhoc} ad-hoc documents are left as they are (submit them again to re-cut them)." if adhoc else "."))
    if not args.yes and input("Queue it? [y/N] ").strip().lower() != "y":
        print("Nothing queued.")
        return 1
    job = storage.enqueue_job(client, "ingest_urls", entries)
    print(f"Queued job {job['id']}: the worker runs it; the Ingestion page shows its progress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

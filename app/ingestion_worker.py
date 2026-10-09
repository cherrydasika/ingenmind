"""Process manually queued ingestion and prune jobs, one at a time."""

import logging
import signal
import threading
import time

from common import ingest, storage
from initialization import build

logger = logging.getLogger(__name__)

# Set on SIGTERM/SIGINT (docker stop). The current URL finishes and is
# checkpointed; the job stays "running" so recover_running_jobs resumes it.
stop_requested = threading.Event()


def process_job(client: storage.PgStore, job: dict) -> None:
    summary = job["summary"] or {
        "urls": len(job["entries"]), "batches": 1, "counts": {},
        "chunks_written": 0, "seconds": 0.0, "failed": [],
    }
    next_index = job["next_index"]
    try:
        if job["kind"] == "prune_expired_documents":
            storage.delete_expired(client)
        elif job["kind"] == "ingest_plan":
            # The plan's pages are its checkpoint (initialization/build.py).
            if not build.run(client, job["entries"][0]["plan_version"], stop_requested):
                logger.info("Stopping; job %s resumes with the plan's pending pages", job["id"])
                return
        else:
            for index in range(next_index, len(job["entries"])):
                started = time.perf_counter()
                result = ingest.ingest_url(client, job["entries"][index])
                status = result["status"]
                summary["counts"][status] = summary["counts"].get(status, 0) + 1
                summary["chunks_written"] += result.get("chunks", 0)
                summary["seconds"] += time.perf_counter() - started
                if status == "failed":
                    summary["failed"].append(result)
                next_index = index + 1
                storage.update_job(client, job["id"], "running", next_index, summary)
                if stop_requested.is_set() and next_index < len(job["entries"]):
                    logger.info("Stopping; job %s resumes at URL %d", job["id"], next_index)
                    return
        storage.update_job(client, job["id"], "success", next_index, summary)
    except Exception as error:
        logger.exception("Ingestion job %s failed", job["id"])
        storage.update_job(client, job["id"], "failed", next_index, summary, str(error))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # As PID 1 in its container, Python ignores SIGTERM unless it handles it.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop_requested.set())
    client = storage.get_client()
    try:
        storage.ensure_collection(client)
    except storage.EmbeddingMismatch as error:
        # Idle rather than exit: a restart loop would only repeat this.
        logger.error("Not processing jobs: %s", error)
        stop_requested.wait()
        return
    storage.recover_running_jobs(client)
    while not stop_requested.is_set():
        job = storage.claim_job(client)
        if job:
            process_job(client, job)
        else:
            stop_requested.wait(2)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
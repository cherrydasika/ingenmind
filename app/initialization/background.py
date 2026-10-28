"""Setup's background work (research, discovery, analysis, evaluation,
relabelling), each a thread of the web app.

A run still marked as running after its step's STALE_MINUTES used to count
as cut off by a restart, but on local models a step can take longer than
that. A run of a kind whose thread is still alive in this process is never
expired; the timeout is for runs a restart left behind.
"""

import threading
from collections import Counter

_alive: Counter = Counter()
_lock = threading.Lock()


def start(kind: str, work) -> None:
    """Run work in a daemon thread named kind, counted while it runs."""
    def run():
        try:
            work()
        finally:
            with _lock:
                _alive[kind] -= 1
    with _lock:
        _alive[kind] += 1
    threading.Thread(target=run, name=kind, daemon=True).start()


def running(kind: str) -> bool:
    """Whether a thread of this kind is still working in this process."""
    with _lock:
        return _alive[kind] > 0

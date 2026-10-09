"""A tiny time-based cache for functions with hashable arguments, with a
`.clear()` to drop everything (e.g. when a page asks for fresh data)."""

import functools
import threading
import time


def ttl_cache(seconds: float):
    def decorate(fn):
        entries: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args):
            now = time.monotonic()
            with lock:
                hit = entries.get(args)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args)
            with lock:
                entries[args] = (now, value)
            return value

        wrapper.clear = lambda: entries.clear()
        return wrapper
    return decorate

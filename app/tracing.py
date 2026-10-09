"""Fail-soft Langfuse helpers shared by the retrieval and summarisation
stages: tracing must never break the actual RAG pipeline."""

from contextlib import contextmanager

from langfuse import get_client as get_langfuse_client


class _NoOpObservation:
    def update(self, **kwargs) -> None:
        pass


# The chat model reports input_tokens / output_tokens; Langfuse counts and
# prices "input" / "output".
USAGE_KEYS = {"input_tokens": "input", "output_tokens": "output"}


class _Observation:
    """A Langfuse observation whose usage_details use Langfuse's key names."""
    def __init__(self, obs):
        self._obs = obs

    def update(self, **kwargs) -> None:
        usage = kwargs.get("usage_details")
        if isinstance(usage, dict):
            kwargs["usage_details"] = {USAGE_KEYS.get(k, k): v for k, v in usage.items()}
        self._obs.update(**kwargs)


@contextmanager
def observation(**kwargs):
    """Langfuse tracing span/generation that never breaks the app: if
    Langfuse itself can't be reached or isn't configured, this silently
    falls back to a no-op so the actual RAG pipeline keeps working."""
    try:
        langfuse = get_langfuse_client()
        cm = langfuse.start_as_current_observation(**kwargs)
        obs = cm.__enter__()
    except Exception:
        yield _NoOpObservation()
        return

    try:
        yield _Observation(obs)
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def tag_current_trace(**kwargs) -> None:
    """Best-effort: attach session_id/user_id/metadata to the currently
    active trace. Same fail-soft principle as observation() — never raises."""
    attrs = {k: v for k, v in kwargs.items() if v is not None}
    if not attrs:
        return
    try:
        get_langfuse_client().update_current_trace(**attrs)
    except Exception:
        pass


def current_trace_id() -> str | None:
    """The active trace's ID, or None (no trace, or Langfuse unavailable)."""
    try:
        return get_langfuse_client().get_current_trace_id()
    except Exception:
        return None


def score(trace_id: str | None, name: str, value, data_type: str | None = None, comment: str | None = None) -> None:
    """Best-effort: record a score on a trace (filterable and chartable in
    Langfuse). By trace ID, so it works from any thread. Never raises."""
    if not trace_id or value is None:
        return
    try:
        get_langfuse_client().create_score(trace_id=trace_id, name=name, value=value, data_type=data_type,
                                           comment=comment)
    except Exception:
        pass

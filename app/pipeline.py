"""Chains the two stages: retrieval (chunks) → summarisation (LLM).
Either stage can be used on its own; this is just the Retrieval tab's
end-to-end path, traced as one Langfuse trace.

Retrieval runs once and returns two sets of chunks: Ⓐ from the primary
embedding and Ⓑ from the alternative one. Every requested model (e.g.
Claude as the gold standard next to a local model) answers from each set,
so answers are comparable across both models and both retrievals.

Scheduling: Bedrock calls run concurrently for the requested chunks."""

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor

import guardrails
from retrieval import hybrid_search, ignore_stage
from summarisation import summarise
from tracing import observation, tag_current_trace

def _summarise_or_error(question: str, chunks: list[dict], provider: str, model: str) -> dict:
    """One failed generation must not take the other retrieval stages with it."""
    try:
        result = summarise(question, chunks, provider, model)
        policy = guardrails.check("output", question, result.get("answer") or "")
        return {**result, "answer": result["answer"] if policy.allowed else policy.message,
            "thinking": None, "error": None, "guardrail": policy.decision}
    except Exception as exc:
        return {
            "answer": None, "thinking": None, "provider": provider, "model": model,
            "input_tokens": None, "output_tokens": None, "duration": None, "error": "Travel answer generation failed.",
        }


def retrieval_view(search: dict) -> dict:
    """One embedding's search result in the shape the UI renders."""
    chunks = search["rankings"]["fused"]
    return {
        "model": search["model"],
        "vector_name": search["vector_name"],
        "chunks": chunks,
        "dense_ranking": search["rankings"]["dense"],
        "sparse_ranking": search["rankings"]["sparse"],
        "sources": sorted({chunk["source_url"] for chunk in chunks}),
        "query_vector": search["dense_vector"],
        "sparse_term_count": search["sparse_term_count"],
        "timings": search["timings"],
        # The search stages themselves, excluding the UI explanation queries.
        "duration": sum(search["timings"].values()),
        "explain": search["explain"],
        "error": None,
    }


def _generate_all(question: str, jobs: list[tuple[list[dict], str, str]]) -> list[dict]:
    """Run generations in order, preserving the parent Langfuse context."""
    with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as remote:
        futures = [
            remote.submit(
                contextvars.copy_context().run, _summarise_or_error, question, chunks, provider, model
            )
            for chunks, provider, model in jobs
        ]
        return [f.result() for f in futures]


def answer_question(
    question: str,
    models: list[tuple[str, str]],
    session_id: str | None = None,
    user_id: str | None = None,
    interaction_id: str | None = None,
    on_stage=ignore_stage,
) -> dict:
    """models = [(provider, model), ...]; empty = retrieval only (no LLM call).
    summarisations (from Ⓐ's chunks) and alt_summarisations (from Ⓑ's; empty
    if Ⓑ didn't run) come back in the same order as models. on_stage hears
    each stage (embedding, retrieval, generation) start and finish."""
    policy = guardrails.check("input", question)
    if not policy.allowed:
        return _blocked_result(policy, models)
    with observation(as_type="span", name="answer_question", input={"question": question}) as root:
        tag_current_trace(
            session_id=session_id,
            user_id=user_id,
            metadata={"interaction_id": interaction_id} if interaction_id else None,
        )
        t_start = time.perf_counter()

        search = hybrid_search(question, on_stage=on_stage)
        retrieval = retrieval_view(search)
        alt = search["alt"]
        retrieval["alt"] = alt if alt["error"] else retrieval_view(alt)

        chunk_sets = [retrieval["chunks"]] + ([] if alt["error"] else [retrieval["alt"]["chunks"]])
        content = "\n".join(chunk["text"] for chunks in chunk_sets for chunk in chunks)
        if content:
            output_policy = guardrails.check("output", question, content)
            if not output_policy.allowed:
                return _blocked_result(output_policy, models)
        jobs = [(chunks, provider, model) for chunks in chunk_sets for provider, model in models]
        if jobs:
            on_stage("generation", "start", models=sorted({model for _, model in models}))
        t_gen = time.perf_counter()
        results = _generate_all(question, jobs)
        t_gen = time.perf_counter() - t_gen
        summaries, alt_summaries = results[:len(models)], results[len(models):]
        if jobs:
            on_stage("generation", "done", seconds=t_gen,
                     errors=sum(1 for r in results if r["error"]),
                     output_tokens=sum(r["output_tokens"] or 0 for r in results))
        else:
            on_stage("generation", "skipped")

        total = time.perf_counter() - t_start
        root.update(
            output={
                "answers": {
                    f"{tag}:{s['provider']}:{s['model']}": s["answer"] or s["error"]
                    for tag, group in (("A", summaries), ("B", alt_summaries)) for s in group
                },
                "sources": retrieval["sources"],
                "timings_seconds": {
                    **retrieval["timings"],
                    **{f"summarise:{tag}:{s['model']}": s["duration"]
                       for tag, group in (("A", summaries), ("B", alt_summaries)) for s in group},
                    "total": total,
                },
            }
        )
        return {"retrieval": retrieval, "summarisations": summaries, "alt_summarisations": alt_summaries,
                "generation_seconds": t_gen if jobs else None, "total": total}


def _blocked_result(policy, models):
    return {"guardrails": {policy.stage: {"decision": policy.decision}},
            "retrieval": {"chunks": [], "sources": [], "query_vector": [], "dense_ranking": [],
                          "sparse_ranking": [], "alt": {"error": "Blocked by travel policy"},
                          "error": None, "timings": {}, "duration": 0},
            "summarisations": [{"answer": policy.message, "thinking": None, "provider": provider, "model": model,
                                "input_tokens": 0, "output_tokens": 0, "duration": 0, "error": None}
                               for provider, model in models],
            "alt_summarisations": [], "generation_seconds": None, "total": 0, "message": policy.message}

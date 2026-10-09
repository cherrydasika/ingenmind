"""Summarisation stage: retrieved chunks in, a short cited LLM answer out.
Retrieval knows nothing about LLMs; see retrieval.py."""

import time

import llm
from tracing import observation

MAX_TOKENS = 512

NO_ANSWER = "I couldn't find anything relevant in the ingested documents to answer that."


def list_models() -> tuple[list[dict], str | None]:
    return ([{"provider": llm.PROVIDER, "model": llm.MODEL}], llm.config_error())


def _build_prompt(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[Source {i + 1}: {chunk['source_url']}]\n{chunk['text']}" for i, chunk in enumerate(chunks)
    )
    return (
        "You are a chat assistant. Answer the user's question using ONLY the context below. "
        "Keep it brief and conversational: 1-3 short sentences, under 60 words. No headers, "
        "no preamble, no bullet lists unless the answer is genuinely a list. If the context "
        "doesn't contain the answer, say so in one sentence instead of guessing. Cite "
        "sources inline using [Source N].\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}"
    )


def _call_llm(prompt: str) -> dict:
    reply = llm.generate(prompt, max_tokens=MAX_TOKENS)
    if not reply.text:
        raise RuntimeError(f"{llm.PROVIDER} returned no answer text (stop reason: {reply.stop_reason})")
    return {
        "answer": reply.text,
        "thinking": None,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
    }


def summarise(question: str, chunks: list[dict], provider: str, model: str) -> dict:
    if not chunks:
        return {
            "answer": NO_ANSWER, "thinking": None, "provider": provider, "model": model,
            "input_tokens": None, "output_tokens": None, "duration": 0.0,
        }

    prompt = _build_prompt(question, chunks)
    with observation(
        as_type="generation",
        name="summarise",
        model=model,
        input=[{"role": "user", "content": prompt}],
        metadata={"provider": provider},
    ) as gen:
        t0 = time.perf_counter()
        if provider != llm.PROVIDER or model != llm.MODEL:
            raise ValueError("unsupported model selection")
        result = _call_llm(prompt)
        duration = time.perf_counter() - t0
        usage = {
            k: v for k, v in
            {"input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]}.items()
            if v is not None
        }
        gen.update(output=result["answer"], usage_details=usage or None)
        return {**result, "provider": provider, "model": model, "duration": duration}

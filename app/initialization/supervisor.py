"""The Initialization Supervisor: leads the setup conversation (epic #19).

One model loop through llm.chat, outside the answer graph. Each user message
sends the saved transcript (conversation.py) with the requirements gathered
so far, so setup resumes after any pause; the model may call tools, then
answers with its next message to the user. Its roles are prompts in this one
loop: this module has the Clarification role; the Domain Analyst, Research
and the rest join it in later issues.

    respond(text)   one user turn → the agent's reply (NEW → DISCOVERING_DOMAIN → CLARIFYING)
    confirm()       the user confirms the requirements → DOMAIN_READY
"""

import json

import knowledge_system
import llm
from tracing import current_trace_id, observation, tag_current_trace

from . import blueprint, blueprint_run, build, content, conversation, evaluation, sources_run, state
from .requirements import SetupRequirements, merge, missing

OPENING = "What would you like your knowledge system to help users with?"
MAX_TOOL_TURNS = 6
TRANSCRIPT_TURNS = 40
CONVERSING = (state.NEW, state.DISCOVERING_DOMAIN, state.CLARIFYING)

CLARIFICATION_PROMPT = f"""You are the Initialization Agent. You help someone with no knowledge of RAG engineering \
set up the knowledge system an assistant answers from, like a knowledgeable consultant. The conversation opened \
with your question: "{OPENING}"

Your job now is to understand what the knowledge system is for, well enough to research the domain next:
- Ask only questions whose answer would change what the knowledge system covers or where its knowledge comes \
from: which countries or regions, who will ask, what kinds of questions, whether live or changing information is \
wanted, organisations that matter, how authoritative the sources must be. Never ask about technical matters \
(chunking, embeddings, databases, models).
- Ask at most two short questions per message. Do not ask what the user already said. When a default is obvious, \
offer it instead of asking openly ("UK, for general passengers — is that right?") and list it in `assumed` until \
the user agrees.
- Do not interpret the request narrowly or broadly on your own: "information about trains" does not say which \
country, or whether it is for passengers or staff.
- Live or changing information (departures, delays, prices that change, availability) is answered by live tools, \
not by the knowledge base: when the user wants it, record it in `live_information` and say so in one sentence.
- After every user message, call record_requirements with what you learnt (only the fields that changed).
- When the purpose, audience, regions and question types are known and nothing material is open, call \
requirements_complete, then end your message with a short bullet summary of the requirements and ask the user to \
press "Looks right" or say what to change.
- Do not research, name sources or promise content yet: the next step researches the domain and proposes them.
- Write plainly and briefly, in the language the user writes in. The knowledge system can be about any domain."""

TOOLS = [
    {"name": "record_requirements",
     "description": "Save what you learnt about the knowledge system. Send only the fields that changed; a field "
                    "you send replaces the saved one (send whole lists).",
     "schema": SetupRequirements.model_json_schema()},
    {"name": "requirements_complete",
     "description": "Say the requirements are complete, once purpose, audience, regions and question types are "
                    "known and nothing material is open. Then summarise them and ask the user to confirm.",
     "schema": {"type": "object", "properties": {}}},
]


class NotConversing(RuntimeError):
    """Setup is past its conversation (or the knowledge system is set up)."""


def ensure_opening() -> None:
    """The first message is always the agent's opening question."""
    if not conversation.turns(last=1):
        conversation.add_turn("agent", OPENING, knowledge_system.status()["state"])


def view() -> dict:
    """What the setup page shows."""
    current = knowledge_system.status()["state"]
    if current in CONVERSING:
        ensure_opening()
    saved = conversation.requirements()
    values = SetupRequirements.model_validate(saved["requirements"] or {})
    blueprint.expire_stale(blueprint_run.STALE_MINUTES)
    latest = blueprint.latest()
    return {"state": current, "step": state.step(current), "steps": [{"key": k, "label": l} for k, l, _ in state.STEPS],
            "turns": conversation.turns(), "requirements": values.model_dump(), "missing": missing(values),
            "complete": saved["complete"], "confirmed_at": saved["confirmed_at"],
            "blueprint": {k: latest[k] for k in ("version", "status", "blueprint", "error", "feedback", "confirmed_at",
                                                  "created_at")}
                         | {"research": {k: (latest["research"] or {}).get(k) for k in ("queries", "pages")}}
                         if latest else None,
            "sources": sources_run.view() if state.STATES.index(current) >= state.STATES.index(state.DISCOVERING_SOURCES)
                       else None,
            "content": content.view() if state.STATES.index(current) >= state.STATES.index(state.ANALYSING_SOURCES)
                       else None,
            "build": build.view() if state.STATES.index(current) >= state.STATES.index(state.INGESTION_APPROVED)
                     else None,
            "evaluation": evaluation.view() if state.STATES.index(current) >= state.STATES.index(state.EVALUATING)
                          else None}


def _system(values: SetupRequirements, complete: bool) -> str:
    gaps = missing(values)
    return (f"{CLARIFICATION_PROMPT}\n\nREQUIREMENTS SO FAR (saved):\n{values.model_dump_json(indent=1)}\n"
            f"Still missing: {'; '.join(gaps) if gaps else 'nothing required'}."
            + ("\nThey are marked complete; if the user changes something, record it and summarise again."
               if complete else ""))


def _messages() -> list[dict]:
    """The transcript in Converse shape: user first, roles alternating."""
    out = []
    for turn in conversation.turns(last=TRANSCRIPT_TURNS):
        role = "user" if turn["role"] == "user" else "assistant"
        if not out and role == "assistant":
            continue   # the opening question is in the system prompt
        if out and out[-1]["role"] == role:
            out[-1]["content"][0]["text"] += "\n\n" + turn["text"]
        else:
            out.append({"role": role, "content": [{"text": turn["text"]}]})
    return out


# The model often writes its message to the user alongside its tool calls,
# then has nothing to add after the results; sometimes it writes nothing at
# all. Text from any turn counts (_run), and every result says what to do.
NEXT = " If you have not written your message to the user yet, write it now; otherwise stop."
FALLBACK = "Could you tell me a little more about what it should help people with?"


def _tool(use: dict, values: SetupRequirements) -> tuple[str, SetupRequirements, bool]:
    """(result text, requirements, error)"""
    name, args = use.get("name"), use.get("input") if isinstance(use.get("input"), dict) else {}
    if name == "record_requirements":
        values = merge(values.model_dump(), args)
        gaps = missing(values)
        return (f"Saved. Still missing: {'; '.join(gaps)}." if gaps else
                "Saved. Nothing required is missing: summarise the requirements and ask the user to confirm, "
                "unless something material is still open.") + NEXT, values, False
    if name == "requirements_complete":
        gaps = missing(values)
        if gaps:
            return f"Not complete: still missing {'; '.join(gaps)}. Ask about it." + NEXT, values, True
        return "Complete. Summarise the requirements and ask the user to confirm." + NEXT, values, False
    return f"Unknown tool {name!r}." + NEXT, values, True


def _run(chat) -> tuple[str, SetupRequirements, bool]:
    """(reply, requirements, complete). Complete means nothing required is
    missing: the user can confirm then, whether or not the model called
    requirements_complete (it does not always)."""
    saved = conversation.requirements()
    values = SetupRequirements.model_validate(saved["requirements"] or {})
    complete = saved["complete"]
    messages = _messages()
    said = []   # text the model wrote, in any turn
    for _ in range(MAX_TOOL_TURNS):
        with observation(as_type="generation", name="setup_supervisor", model=llm.MODEL,
                         input={"system": _system(values, complete), "messages": messages}) as gen:
            turn = chat(_system(values, complete), messages, TOOLS, max_tokens=1200, temperature=0.3)
            gen.update(output=turn.content, usage_details={"input": turn.input_tokens, "output": turn.output_tokens})
        uses = [b["toolUse"] for b in turn.content if "toolUse" in b]
        text = "\n\n".join(b["text"] for b in turn.content if b.get("text")).strip()
        if text:
            said.append(text)
        if not uses:
            return "\n\n".join(said) or FALLBACK, values, complete
        messages = messages + [{"role": "assistant", "content": turn.content}]
        results = []
        for use in uses:
            result, values, error = _tool(use, values)
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "status": "error" if error else "success",
                                           "content": [{"text": result}]}})
        complete = not missing(values)
        conversation.save_requirements(values.model_dump(), complete)
        messages = messages + [{"role": "user", "content": results}]
    return "\n\n".join(said) or FALLBACK, values, complete


def respond(text: str, user_id: str | None = None, chat=None) -> dict:
    """One user turn of the setup conversation; returns view(). chat: a
    stand-in for llm.chat (tests); looked up per call, so it can be patched."""
    chat = chat or llm.chat
    text = (text or "").strip()
    if not text:
        raise ValueError("say something")
    current = knowledge_system.status()["state"]
    if current not in CONVERSING:
        raise NotConversing("setup is not in its conversation now" if current != state.READY else
                            "the knowledge system is set up; reset it to set it up again")
    ensure_opening()
    if current == state.NEW:
        state.transition(state.DISCOVERING_DOMAIN, user_id, expected=state.NEW)
        current = state.DISCOVERING_DOMAIN
    with observation(as_type="span", name="setup_turn", input={"text": text, "state": current}) as span:
        tag_current_trace(name="setup_turn", user_id=user_id, tags=["source:setup", f"state:{current}"])
        trace_id = current_trace_id()
        conversation.add_turn("user", text, current, trace_id)
        reply, values, complete = _run(chat)
        conversation.add_turn("agent", reply, current, trace_id)
        span.update(output={"reply": reply, "complete": complete, "missing": missing(values)})
    if current == state.DISCOVERING_DOMAIN:
        state.transition(state.CLARIFYING, user_id, expected=state.DISCOVERING_DOMAIN)
    return view()


CONFIRMED = ("Thanks. Next I'll research the domain and write a blueprint of what the knowledge system "
             "should cover, for you to check.")


def confirm(user_id: str | None = None) -> dict:
    """The user confirms the requirements: setup moves on to the blueprint (#11)."""
    if knowledge_system.status()["state"] != state.CLARIFYING:
        raise NotConversing("there is nothing to confirm now")
    conversation.confirm(user_id)   # ValueError unless complete
    state.transition(state.DOMAIN_READY, user_id, expected=state.CLARIFYING,
                     requirements=json.loads(json.dumps(conversation.requirements()["requirements"])))
    conversation.add_turn("agent", CONFIRMED, state.DOMAIN_READY)
    try:
        blueprint_run.start()      # the research starts at once; its status shows on the page
    except Exception:
        pass                       # the page offers to start it again
    return view()

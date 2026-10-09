"""Running the Domain Blueprint step of setup (#11): research in the
background, revising from the user's feedback, confirming.

Research takes a minute or two, so it runs in a thread of the web app; the
setup page polls the latest version's status. A version still "researching"
after STALE_MINUTES (the web app restarted mid-run) counts as failed, so the
user can start again. A revision makes no new searches: it fetches the same
pages again (only their URLs are stored) and rewrites the blueprint with the
user's feedback and the previous version.
"""

import threading

import knowledge_system
import research

from . import blueprint, conversation, sources_run, state
from .requirements import SetupRequirements

STALE_MINUTES = 15
CONFIRMED = ("Blueprint confirmed. Next I'll look for sources that cover it, and you'll choose which ones to "
             "trust.")


def _start_thread(work) -> None:
    """Background work (tests replace this to run inline)."""
    threading.Thread(target=work, name="blueprint-research", daemon=True).start()


def _requirements() -> SetupRequirements:
    saved = conversation.requirements()
    if not saved["confirmed_at"]:
        raise ValueError("the requirements are not confirmed yet")
    return SetupRequirements.model_validate(saved["requirements"])


def _in_blueprint_step() -> None:
    if knowledge_system.status()["state"] != state.DOMAIN_READY:
        raise state.TransitionNotAllowed("the blueprint step is not open now")


def _run(version: int, work) -> None:
    try:
        bp, record = work()
        blueprint.finish(version, bp, record)
    except Exception as error:   # the user sees the reason and can start again
        blueprint.fail(version, f"{type(error).__name__}: {error}")


def start(generate=None) -> int:
    """Research a new blueprint from the confirmed requirements; returns its version."""
    _in_blueprint_step()
    requirements = _requirements()
    blueprint.expire_stale(STALE_MINUTES)
    version = blueprint.start_version()
    _start_thread(lambda: _run(version, lambda: (generate or blueprint.generate)(requirements)))
    return version


def revise(feedback: str, writer=None, fetch=None) -> int:
    """A new version from the latest ready one and the user's feedback."""
    feedback = (feedback or "").strip()
    if not feedback:
        raise ValueError("say what to change")
    _in_blueprint_step()
    requirements = _requirements()
    previous = blueprint.latest()
    if not previous or previous["status"] != "ready":
        raise ValueError("there is no blueprint to revise yet")
    blueprint.expire_stale(STALE_MINUTES)
    version = blueprint.start_version(feedback=feedback[:2000])
    write, get = writer or blueprint.write_with_llm, fetch or research.fetch_source

    def work():
        record = previous["research"] or {}
        pages = [p for p in (get(page["url"]) for page in record.get("pages", [])) if not p.get("error")]
        bp, _ = write(requirements, pages, feedback=feedback, previous=previous["blueprint"])
        return blueprint.check(bp, pages), {**record, "revised_from": previous["version"],
                                            "pages": [{"url": p["url"], "title": p.get("title"),
                                                       "chars": p.get("chars")} for p in pages]}
    _start_thread(lambda: _run(version, work))
    return version


def confirm(user_id: str | None = None) -> int:
    """Confirm the latest ready blueprint: setup moves on to finding sources."""
    _in_blueprint_step()
    latest = blueprint.latest()
    if not latest or latest["status"] != "ready":
        raise ValueError("there is no ready blueprint to confirm")
    blueprint.confirm(latest["version"], user_id)
    knowledge_system.set_blueprint_version(latest["version"])
    state.transition(state.DISCOVERING_SOURCES, user_id, expected=state.DOMAIN_READY,
                     blueprint_version=latest["version"])
    conversation.add_turn("agent", CONFIRMED, state.DISCOVERING_SOURCES)
    try:
        sources_run.start()        # source discovery starts at once; its status shows on the page
    except Exception:
        pass                       # the page offers to start it again
    return latest["version"]


def back_to_conversation(user_id: str | None = None) -> None:
    """Change the requirements: back to the conversation (a new blueprint follows)."""
    state.transition(state.CLARIFYING, user_id, expected=state.DOMAIN_READY, reason="change the requirements")

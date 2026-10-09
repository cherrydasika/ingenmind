"""The setup states and the transitions allowed between them (epic #19).

Setup moves forward one state at a time; it may also go back to change the
requirements (CLARIFYING) or the choices (source or content selection),
for example to fix gaps the readiness report shows. Every change is
compare-and-set on the knowledge system record (knowledge_system.py) and
written to its history, so two requests cannot both move the same state.
"""

import knowledge_system

NEW = knowledge_system.NEW
DISCOVERING_DOMAIN = "DISCOVERING_DOMAIN"
CLARIFYING = "CLARIFYING"
DOMAIN_READY = "DOMAIN_READY"
DISCOVERING_SOURCES = "DISCOVERING_SOURCES"
AWAITING_SOURCE_SELECTION = "AWAITING_SOURCE_SELECTION"
ANALYSING_SOURCES = "ANALYSING_SOURCES"
AWAITING_CONTENT_SELECTION = "AWAITING_CONTENT_SELECTION"
INGESTION_APPROVED = "INGESTION_APPROVED"
INGESTING = "INGESTING"
INDEXING = "INDEXING"
EVALUATING = "EVALUATING"
READY = knowledge_system.READY

STATES = (NEW, DISCOVERING_DOMAIN, CLARIFYING, DOMAIN_READY, DISCOVERING_SOURCES, AWAITING_SOURCE_SELECTION,
          ANALYSING_SOURCES, AWAITING_CONTENT_SELECTION, INGESTION_APPROVED, INGESTING, INDEXING, EVALUATING, READY)
FORWARD = dict(zip(STATES, STATES[1:]))

# Going back: change the requirements until ingestion is approved (and after
# an evaluation); change the choices after an analysis, a failed build, an
# evaluation, or once live (only a knowledge system that guided setup built).
BACK = {
    CLARIFYING: {DOMAIN_READY, DISCOVERING_SOURCES, AWAITING_SOURCE_SELECTION, ANALYSING_SOURCES,
                 AWAITING_CONTENT_SELECTION, EVALUATING},
    AWAITING_SOURCE_SELECTION: {ANALYSING_SOURCES, AWAITING_CONTENT_SELECTION, EVALUATING, READY},
    AWAITING_CONTENT_SELECTION: {INGESTION_APPROVED, INGESTING, INDEXING, EVALUATING, READY},
}

# The six steps the user sees, and the states each covers.
STEPS = (
    ("purpose", "Purpose", (NEW, DISCOVERING_DOMAIN, CLARIFYING)),
    ("blueprint", "Blueprint", (DOMAIN_READY,)),
    ("sources", "Sources", (DISCOVERING_SOURCES, AWAITING_SOURCE_SELECTION)),
    ("content", "Content", (ANALYSING_SOURCES, AWAITING_CONTENT_SELECTION)),
    ("build", "Build and evaluate", (INGESTION_APPROVED, INGESTING, INDEXING, EVALUATING)),
    ("live", "Go live", (READY,)),
)


class TransitionNotAllowed(ValueError):
    pass


def allowed(current: str, to: str, origin: str | None = None) -> bool:
    if FORWARD.get(current) == to:
        return True
    if current == READY and origin != "setup":
        return False   # a knowledge system that setup did not build has nothing to go back to
    return current in BACK.get(to, set())


def step(state: str) -> str:
    """The user-facing step a state belongs to."""
    return next(key for key, _, states in STEPS if state in states)


def transition(to: str, user_id: str | None = None, expected: str | None = None, **details) -> str:
    """Move the knowledge system to `to` if the table allows it from its
    current state (and the current state is `expected`, when given); returns
    the state it came from. TransitionNotAllowed otherwise."""
    if to not in STATES:
        raise TransitionNotAllowed(f"unknown state {to!r}")
    status = knowledge_system.status()
    current = status["state"]
    if expected is not None and current != expected:
        raise TransitionNotAllowed(f"setup is at {current}, not {expected}")
    if not allowed(current, to, status["origin"]):
        raise TransitionNotAllowed(f"setup cannot go from {current} to {to}")
    if not knowledge_system.set_state(current, to, user_id, details):
        raise TransitionNotAllowed(f"setup moved on from {current} meanwhile")
    return current

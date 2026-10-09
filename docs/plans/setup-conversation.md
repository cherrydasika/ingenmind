# Plan: setup conversation and its state machine

Status: **Done — pull request open (closes #18); deploy after the merge.** The local install went through setup in the browser and
is at `DOMAIN_READY` with confirmed UK rail requirements (no knowledge yet),
the starting point for #19. The user took every recommendation (2026-10-08):
`llm.chat` with the saved transcript; reset the local install for testing;
stop at `DOMAIN_READY`. Branch `init/setup-conversation`. Update this file
at the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #18, part of epic #27 (RAG Initialization Agent); builds on
#17 (the knowledge system record and reset). Working agreement: phases with
a stop at each; a branch and a pull request; the user merges (AGENTS.md).

## Goal

A guided, conversational setup of the one knowledge system. The first
message is always "What would you like your knowledge system to help users
with?". The agent asks only the questions that materially change the
knowledge system, records what it learns as structured **requirements**,
summarises them for the user to confirm, and stops at `DOMAIN_READY`, where
the Domain Blueprint (#19) takes over. Setup is an explicit, saved state
machine, so the user can leave and come back.

## How the code is today

- **The knowledge system record** (`app/knowledge_system.py`, #17): `state`
  is `NEW` or `READY`; a reset sets `NEW`; admins land on the setup page
  (`web/static/js/knowledge_system.js`, `SetupPage`), now a placeholder.
- **Agent loops**: the answer graph's agents run through the AgentCore
  harness on EC2 (`app/agent.py`, `_Run.invoke`), which keeps each
  conversation server-side per runtime session; locally through the local
  harness (`app/agent_runtime.py`, history in `agent_sessions`).
- **Direct model calls**: `llm.chat(system, messages, tools)` (Converse
  message shape, tool use) and `llm.call_tool` (forced tool) — what the
  evaluators, the source profiler and the judge use, on EC2 (Bedrock) and
  locally (now Anthropic, fixed today).

## Design

### 1. States (`app/initialization/state.py`)

All of epic #27's states, with the transitions allowed between them:

```
NEW → DISCOVERING_DOMAIN → CLARIFYING ⇄ (user turns) → DOMAIN_READY
    → DISCOVERING_SOURCES → AWAITING_SOURCE_SELECTION → ANALYSING_SOURCES
    → AWAITING_CONTENT_SELECTION → INGESTION_APPROVED → INGESTING
    → INDEXING → EVALUATING → READY
```

plus going back: from any later state to `CLARIFYING` (change the
requirements), and from `READY` / `EVALUATING` to
`AWAITING_SOURCE_SELECTION` or `AWAITING_CONTENT_SELECTION` (fix gaps,
#25). `transition(to, user)` checks the table, writes the state on the
knowledge system record and an audit event; anything else raises. This
issue drives `NEW` → `DOMAIN_READY`; later issues drive the rest.

### 2. The conversation (`app/initialization/conversation.py`)

```
app_setup_turns   turn_id, at, role (user | agent), text, state (when it was said),
                  trace_id
app_setup         one row: requirements jsonb (current), updated_at
```

Both cleared by a reset (#17's list gains them).

### 3. The Initialization Supervisor (`app/initialization/supervisor.py`)

One model loop through **`llm.chat`**, not the AgentCore harness: setup
is a separate conversation, outside the answer graph, and sending the saved
transcript each turn makes it resumable after any pause (a harness session
expires; the database does not). Same code on EC2 (Bedrock) and locally.

Per user message: the saved transcript + the new message → the model, with
tools, until it answers with text (its next message to the user):

- `record_requirements` — merges what it learnt into `SetupRequirements`
  (Pydantic): purpose, audience, country and regions, question types,
  topics in and out of scope, organisations named, live information wanted
  (and that it goes to tools, not the knowledge base), authority wanted,
  language; each field with where it came from (the user said / assumed).
- `requirements_complete` — when nothing material is missing: the agent
  shows a summary and asks the user to confirm; state stays `CLARIFYING`.

Roles as prompts in one loop: this issue writes the **Clarification** role
(and the supervisor's own prompt); the Domain Analyst, Research and the
rest join it in #19 onwards. The prompt says: ask one or two questions at a
time, only what would change the knowledge system, offer sensible defaults
("UK, for general passengers?"), never a fixed questionnaire; stop asking
when the requirements are enough.

**Confirming**: the user presses "Looks right" (or says so) →
`DOMAIN_READY`, the requirements frozen for #19. "Change" goes on
clarifying.

Limits: at most 6 tool turns per message; the transcript is cut to its last
40 turns in the prompt (with the requirements always included).

### 4. API (`manage_settings`)

- `GET /api/setup` — state, transcript, requirements, whether they are
  complete.
- `POST /api/setup/message` `{"text"}` — one user turn; the agent's reply
  (JSON; the reply takes a few seconds). Starts setup from `NEW`. Refused
  when the knowledge system is `READY` (reset first) or past `CLARIFYING`.
- `POST /api/setup/confirm` — requirements confirmed → `DOMAIN_READY`.

### 5. UI: the setup page becomes a chat

The page shows the six user-facing steps (Purpose → Blueprint → Sources →
Content → Build and evaluate → Go live) with the current one highlighted
(mapped from the state); the conversation; a **What I've learnt** panel
with the requirements as they are recorded; and, once complete, "Looks
right" / "Change something". Later steps (#19 onwards) replace the chat
with structured screens at their states.

### 6. Tracing

One Langfuse trace per setup turn (`setup_turn`, tagged `source:setup`),
the model calls as generations with their tool calls.

## Files to touch

| File | Change |
|---|---|
| `app/initialization/` (new) | `state.py`, `conversation.py`, `supervisor.py`, `requirements.py` |
| `app/knowledge_system.py` | the full state list; reset also clears the setup tables |
| `app/web_api.py`, `app/auth.py` | `/api/setup` endpoints, rules |
| `web/static/js/knowledge_system.js` (or a new `setup.js`), `api.js` | the chat, steps and requirements panel |
| tests | `test_setup.py` (new; add to AGENTS.md and CI's list), `test_pg_store.py` |
| README, PROGRESS.md, BACKLOG ("Local model calls fail" is fixed) | last phase |

## Phases

- [x] **1. Plan** — this file.
- [x] **2. State machine and conversation storage**, with tests.
  - `app/initialization/state.py`: the 13 states, `FORWARD` (one step at a
    time), `BACK` (to `CLARIFYING` until ingestion is approved and after an
    evaluation; to source or content selection after an analysis, a failed
    build, an evaluation, or once live), the six user-facing `STEPS`,
    `allowed()`, `step()`, `transition(to, user, expected=…)`. Going back
    from `READY` only for a knowledge system setup built (origin `setup`).
  - `knowledge_system.set_state()`: compare-and-set (`WHERE state =
    current`), a `state` event with from/to; reaching `READY` this way sets
    origin `setup`. The reset also empties `app_setup_turns` and
    `app_setup`.
  - `app/initialization/conversation.py`: `app_setup_turns` (role, text,
    state, trace) and `app_setup` (requirements, complete, confirmed by and
    when); saving changed requirements withdraws a confirmation; confirming
    needs them complete.
  - `app/test_setup.py` (own database; added to AGENTS.md and CI): forward
    only one step, the full walk to `READY` with its history, going back,
    no going back for an install setup did not build, compare-and-set,
    steps, turns, requirements and confirmation, a reset clearing setup.
  - Found: the test command now pins `EMBEDDING_PROVIDER=bedrock
    LLM_PROVIDER=bedrock` (CI's defaults), since `.env` sets local
    providers for running the app; without it the store tests meet
    384-dimension vectors. Full suite 296 OK, with and without `data/`.
- [x] **3. Supervisor and Clarification role**, with stubbed-model tests
  and a live check against Claude (Haiku 4.5, Anthropic API) locally.
  - `requirements.py`: `SetupRequirements` (purpose, audience, regions,
    question types, out of scope, organisations, live information,
    authority, language, assumed), `merge()` (sent fields replace, unknown
    fields dropped, a string read as a list), `missing()` over `REQUIRED`
    (purpose, audience, regions, question types).
  - `supervisor.py`: `respond()` (`NEW → DISCOVERING_DOMAIN`, the turn,
    then `CLARIFYING`), `confirm()` (`CLARIFYING → DOMAIN_READY`, the
    requirements in the event), `view()`; the Clarification prompt; tools
    `record_requirements`, `requirements_complete`; at most 6 tool turns;
    the last 40 turns sent, the opening question in the system prompt.
    Traced as `setup_turn` (`source:setup`) with `setup_supervisor`
    generations.
  - Found live, and fixed: (1) Claude writes its question **alongside**
    the tool call and adds nothing after the result, so the first reply
    was the fallback: text from every turn now counts, and tool results
    say "write it now if you have not"; (2) it summarised without calling
    `requirements_complete`, so nothing was confirmable: **complete now
    means nothing required is missing**, the tool only prompts the
    summary.
  - Live run after the fixes: "Information about trains." → which country
    and who uses it; "The UK, ordinary passengers" → which questions (with
    options); the topics plus "live departures would be nice" → a summary
    with live departures marked as answered by live tools; complete;
    confirm → `DOMAIN_READY`.
  - Tests: `Supervisor` in `test_setup.py` (19 in the file); full suite
    307 OK as CI runs it.
- [x] **4. API and UI**, checked in the browser locally.
  - API (`manage_settings`): `GET /api/setup` (the view), `POST
    /api/setup/message` (400 for empty or over 4,000 characters, 409 past
    the conversation or when set up, 502 when the model fails — the
    user's turn is kept, send again), `POST /api/setup/confirm` (409 when
    not clarifying, 400 when incomplete).
  - `web/static/js/setup.js` (the placeholder left `knowledge_system.js`):
    the six steps (done ✓, current highlighted), the conversation (agent
    replies as Markdown, "Thinking…" while waiting, the draft kept on an
    error), **Looks right** once complete, **What I've learnt** (filled
    fields, assumed items, what is still needed, "Confirmed").
  - Shared Markdown renderer (`ui.js`): a block mixing text and list lines
    ("For example:" then "- …" without a blank line, as Claude writes) now
    renders a paragraph then a list; it used to show the dashes. Also
    benefits Retrieval answers.
  - Found: `respond(chat=llm.chat)` bound the function as a default, so
    patching `llm.chat` did nothing (the API test hit Bedrock): it is now
    looked up per call.
  - Browser, locally with Claude: three answers, the panel filling in,
    **Looks right**, `DOMAIN_READY` with the Blueprint step current; a
    reload resumed the conversation; no console errors.
  - Tests: `SetupApi` (`test_setup.py`, 21 in the file), the permission
    rules (`test_auth.py`).
- [x] **5. Docs, PR.** README ("Knowledge system: setup and reset" gains
  guided setup; the Langfuse list gains `setup_turn`; the project layout
  gains `initialization/`); the backlog loses "Local model calls fail"
  (fixed 2026-10-08). Deploy after the merge: EC2 stays `READY`, setup
  only starts after a reset, so it is not exercised there yet.

## Open questions for the user

1. **Model loop**: `llm.chat` with the saved transcript (recommended:
   resumable, same on EC2 and locally), or the AgentCore harness like the
   answer agents?
2. **Local testing needs a `NEW` install**: setup only starts on a knowledge
   system that is not set up. I would reset the local install again when
   testing phases 3–4 (deleting the demo documents just seeded; reseeding
   takes a minute now that local models work). OK?
3. **Where it stops**: this issue ends at `DOMAIN_READY` with confirmed
   requirements. The domain research and blueprint are #19. OK?

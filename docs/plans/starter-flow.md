# Plan: new domains start with one agent; the builder grows the flow

Status: **Done (2026-10-11)**: phases 2–5; merged with the PR that closes #62. Update this file at the end of every step.

GitHub: issue #62. Branch `flows/starter`, a pull request; the user merges
(AGENTS.md). The user chose (2026-10-11): **the flow is the source of
truth**: what the builder shows is exactly what runs, with no code to edit
or generate.

## Goal

A knowledge system set up for a new domain starts with the smallest flow
that answers: **one agent, with one LLM and one tool (the knowledge-base
search)**. The primary user (an admin) grows it in the flow builder for the
others in their organisation: drag in components, connect and configure
them, evaluate, promote. Every component the builder offers either works
with every setting it shows, or says it does not yet.

## How it is today

- **Flows are data** (`app/flows/`: `spec.py`, `registry.py`,
  `compiler.py`, `store.py`). `compiler._build` turns the live flow into a
  LangGraph graph; `agent._runtime()` maps each component's `impl` to its
  node function. Changing and publishing a flow changes behaviour without
  code. The builder (`web/flows-app`, React Flow) has drag and drop from the
  component list, connections with checks, versions, publish, evaluate,
  promote; admins with `manage_agents` build, everyone else asks.
- **Two layers of agents.** The main graph runs on `State`: the supervisor
  delegates tasks (fan-out) to specialists or subflows. The retrieval agent,
  evidence evaluator, query rewriter and research run inside the
  knowledge-base subflow on `KbState` (a delegated task). So no agent can
  answer on its own: an answer always comes from the supervisor.
- **Setup's candidate flow** (`initialization/evaluation.candidate_flow`)
  is a copy of the live flow (else the built-in `default_flow.json`) with
  the blueprint's brief, domain and scope: the full multi-agent flow.
  Templates (`flows/templates.py`): the full flow, RAG + research, RAG only,
  live data only; all have the supervisor.
- **Settings shown but not applied** (the registry's `applied` list):
  memory (`memory`, `scope`, `short_term_days`, `long_term`), vector DB
  (`engine`, `tables`, `embedding`, `dimensions`, `distance`), web search
  (`provider`, `api_key_parameter`), scraper (`formats`, `chunk_size`,
  `chunk_overlap`), ingest (`table`), supervisor (`brief`, `search_country`,
  `max_turns`; `brief` and `search_country` may be read elsewhere: to
  verify). The MCP tool is not runnable ("soon"); the investigation and
  clarification agents are placeholders (#38).
- **Guardrails** run server-side and fail closed whatever the flow
  contains; the input guardrail node configures its scope and messages.

## Design

1. **An `agent` component** (kind flow, on the main `State`): one LLM loop
   with tools from what is plugged into its ports: `retriever` (the
   knowledge-base search, with citations as today), `tools` (API tools),
   `web_search`. It answers the question itself, with the same answer shape
   as the supervisor's (answer, citations, findings for the answer
   evaluator), so the output guardrail, the answer evaluator, Sessions and
   the Retrieval page work unchanged. Settings: instructions, the brief,
   max turns, a word limit. It reuses the knowledge-base agent's search tool
   and the specialist loop; no new model code.
2. **The starter flow**: Start → Input guardrail → Agent [LLM, Retriever]
   → Answer evaluator → End (`flows/starter_flow.json`, and a "Starter" template). Setup's
   candidate flow starts from it for a new domain, with the blueprint's
   brief and scope; the demo and existing installs keep their flows.
3. **The builder tells the truth**: for each setting not applied, either
   apply it (where cheap and safe) or show it read-only with "fixed: <value>"
   and why; a component that is not runnable stays "soon" and a flow with
   one cannot be published (already checked by `runnable_issues`: confirm
   the builder says so before publish).
4. **Growing the flow**: dropping the evidence evaluator, research, a
   supervisor with specialists, guardrails or evaluators connects validly
   (the existing checks); promoting a new live version asks to run the
   flow's evaluation set first and shows the result next to the current
   live version (setup already does this once).

## Phases

- [x] **1. Plan**: this file; the user's answers below.
- [ ] **2. The agent component**: registry, runtime, compiler checks; tests
  (a flow of one agent answers with citations from the knowledge base;
  with API tools; guardrails and the answer evaluator around it). Stop.
  - Done: `registry.AgentConfig` and the `agent` component (brief, domain,
    instructions, word limit; ports llm, memory, retriever, tools).
    `agent.py`: role `agent` (its prompt plus the node's instructions; tools:
    the knowledge-base search as the knowledge-base agent has it, plus only
    the API tools plugged into the agent), `_agent_node` (runs the agent on
    the question and leaves the draft and a finding with its searches, so
    the output guardrail, the answer evaluator, citations and Sessions work
    unchanged), `_agent_tool` (search or API call by name), the flow's
    settings from the agent node, `agent` allowed in main flows; the answer
    evaluator counts the agent's API calls as evidence.
    `app/flows/starter_flow.json`.
  - **Changed from the plan: the output guardrail is in the starter.** The
    compiler refuses a flow whose answers can reach End without one (a fixed
    safety rule), so the starter is Start → Input guardrail → Agent [LLM,
    Retriever] → Output guardrail → Answer evaluator → End.
  - Tests (`test_agent_runtime.AgentComponentTest`, on the local runtime
    with a scripted model): the starter is valid and runnable; one agent
    answers with a citation, and the answer evaluator sees its chunks; API
    tools plugged in are offered (and only then); a failing answer check
    replaces the answer. Full suite: 416 pass; builder tests pass.
  - Found: the registry marks the supervisor's `brief` and `search_country`
    as not applied, but `_flow_settings` applies them (phase 4 corrects it).
- [x] **3. The starter flow and setup**: the template, the candidate flow for
  a new domain; tests; a live check on the local build (set up a small
  domain, evaluate, go live). Stop.
  - Done: `flows.load_starter()`; a **Starter: one agent** template, first
    in the list. `evaluation.base_flow()`: a domain's candidate builds on its
    own latest published version (what the admin grew is kept), else the
    starter; `apply_blueprint` configures the agent (brief, domain, and
    instructions naming the blueprint's knowledge areas,
    `agent_instructions()`), as before the supervisor and the guardrail.
  - Tests: the candidate is the starter with the blueprint's settings; a
    version grown in the builder (API tools added) survives the next
    preparation; the candidate compiles from the starter and from a grown
    flow; the starter template runs. Full suite: 417 pass.
  - Live check, without resetting the local build (it already has its own
    published flow, which is kept on purpose): the starter configured with
    the local blueprint, published as `starter_check` v1, run on the setup
    evaluation set (16 questions): **14/16 met, pass rate 92%, overall 0.94,
    8.5 s per question (p50), 94k tokens**, against the full multi-agent
    flow's 11–12/16, 77%, 0.88–0.93, ~20 s and 320–410k tokens. Missed:
    "How much can I save per year with a Railcard?" (failed the answer
    check) and "What time does the next train depart?" (expected "live: not
    available", the input guardrail blocked it). One answer began "I need to
    search for information…": a preamble the prompt should stop (phase 4).
- [x] **4. The builder tells the truth**: every unapplied setting applied or
  marked fixed; publish blocked with "soon" components; evaluate before
  promote; checked in the browser. Stop.
  - Done: a "soon" component is an error (`remove it to publish`), so a
    flow with one cannot be published; the registry's applied lists are
    right (the supervisor's `brief` and `search_country` were applied but
    marked not) and a test checks every applied setting exists; settings
    not applied are shown **fixed** and read-only (they were "reference" but
    editable). Making a version live needs a finished evaluation run of it
    (`flows.evals.latest_evaluation`), else a 409 and, in the builder, a
    third click "make live without an evaluation"; the built-in version 0
    and setup's Go live (evaluated already) are unaffected. The agent's
    "I need to search…" / "Let me search…" opening is dropped.
  - Not changed (kept fixed): memory, vector DB, web search provider,
    scraper and ingest-table settings: each needs real work (another
    provider, another table or embedding means re-indexing) and is not
    needed to grow a flow.
  - Tests: publishing is blocked by a "soon" component; applied settings
    exist; make live refused without an evaluation, allowed with one or the
    override, 404 for an unpublished version; the preamble cases. 419 pass
    (three full runs in a row; one earlier run failed
    `test_make_live_and_back` once and did not again). Builder tests 4/4.
    Browser: Agent in the component list, MCP tool "soon", memory's
    settings fixed.
- [x] **5. Docs, PR, deploy.** README: the flow builder (start small, grow
  it; fixed settings, soon components, evaluate before live) and setup's
  candidate flow.

## The user's answers (2026-10-11)

1. The starter shows the input guardrail.
2. **The starter includes the answer evaluator** (not the recommendation):
   Start → Input guardrail → Agent [LLM, Retriever] → Answer evaluator → End.
3. Apply the cheap unapplied settings; mark the rest "fixed".
4. The demo and existing installs keep their flows; only new domains start
   from the starter.

## The questions as asked

1. **The starter's shape**: Start → Input guardrail → Agent [LLM,
   Retriever] → End (recommended: the input guardrail is enforced anyway,
   so showing it is honest), or without the guardrail node (it still runs
   server-side, invisibly).
2. **Answer checking in the starter**: no answer evaluator at first
   (recommended: one agent as asked; the evaluation before go-live measures
   quality, and adding the evaluator is one drag), or include it.
3. **Unapplied settings**: apply the cheap ones and mark the rest "fixed"
   (recommended), or hide them until they work.
4. **The demo and existing installs**: keep their current full flow
   (recommended), or move them to the starter too.

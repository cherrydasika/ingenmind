# Plan: the Domain Blueprint

Status: **Done — pull request open (closes #19); deploy after the merge.** The local install went through the Blueprint step in the
browser and is at `DISCOVERING_SOURCES` with blueprint v3 confirmed, the
starting point for #20. The user took every recommendation (2026-10-08) and added
the Tavily key to the local `.env`, so research runs live locally. Branch `init/domain-blueprint`. Update this file at
the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #19, part of epic #27 (RAG Initialization Agent); builds on
#18 (the setup conversation, which ends at `DOMAIN_READY` with confirmed
requirements). Working agreement: phases with a stop at each; a branch and
a pull request; the user merges (AGENTS.md).

## Goal

From the confirmed requirements, the setup agent **researches the domain**
and writes a **Domain Blueprint**: a structured, versioned description of
what the knowledge system covers, grounded in pages it fetched, which the
user checks and confirms. Everything later reads it: source discovery
(#20), the chunk metadata (#23), the flow's brief and scope, which areas go
to live tools instead of the knowledge base, and the evaluation set (#24).
No domain is hard-coded.

## How the code is today

- **Requirements** (`app/initialization/requirements.py`): purpose,
  audience, regions, question types, out of scope, organisations, live
  information, authority, language, assumed — confirmed at `DOMAIN_READY`
  (#18).
- **Web research** (`app/research.py`): `web_search` (Tavily, with a
  search country), `fetch_source` (HTML and PDF text), and the pattern of a
  forced-tool model call whose claims are then **verified against the
  fetched pages** in code (`SourceProfile`, `verify_profile`, issue #10).
- **Flow settings** (`app/flows/registry.py`): a flow's brief, search
  country, domain, supervisor instructions and guardrail scope steer every
  agent; flows have drafts, published versions and a live version
  (`app/flows/store.py`).
- **Tavily key**: EC2 reads it from SSM; the local `.env` has none.

## Design

### 1. `DomainBlueprint` (`app/initialization/blueprint.py`)

```
name, category, regions[], audience[], purpose, language
knowledge_areas[]: key, name, description,
    knowledge_class: STATIC_KNOWLEDGE | DYNAMIC_KNOWLEDGE | STRUCTURED_DATA | EXTERNAL_TOOL_API,
    example_questions[], evidence_urls[]
entities[]: name (e.g. train_operator, station, ticket type), description
organisations[]: name, role (regulator | government | operator | industry body | other),
    website, evidence_urls[]
source_requirements: authoritative_only, government, operators, other (text)
metadata_fields[]: field, description, example values   (for #23's chunk metadata)
flow: brief, search_country, domain, scope, supervisor_instructions
assumptions[], unknowns[]
```

Every knowledge area and organisation carries the URLs of the fetched pages
it came from; a **check in code** (as `verify_profile` does) drops URLs
that were not fetched and lists unsupported items under `unknowns`. The
flow settings fit the existing fields' limits (brief ≤ 1,000 characters,
scope ≤ 4,000).

### 2. Domain research (Domain Analyst + Research roles)

Runs in the background when the blueprint step starts (it takes 30–90
seconds); the page shows progress and picks the result up when it is done.

1. **Queries** from the requirements (one small model call): the domain's
   regulators, passenger or customer rights, the main organisations,
   policies for each question type — at most 6, each naming the region.
2. **Search and fetch**: `research.web_search` (search country from the
   regions) and `research.fetch_source` on the best results, at most 10
   pages, official sites first.
3. **Write the blueprint**: one forced-tool call with the requirements and
   the pages' text (capped per page), then the code check.
4. **Store** it as a new version.

### 3. Storage

```
app_domain_blueprints   version, created_at, status (researching | ready | failed),
                        blueprint jsonb, research jsonb (queries, pages fetched),
                        error, feedback, confirmed_at, confirmed_by
```

The confirmed version is recorded on the knowledge system
(`blueprint_version`, #17). A reset clears the table.

### 4. Review and confirm

The setup page's Blueprint step shows the blueprint as a readable outline:
knowledge areas grouped by class (static ones for the knowledge base,
dynamic ones for live tools), organisations with their websites, entities,
source requirements, and the generated brief and scope. Then:

- **Looks right** → confirmed; state `DOMAIN_READY → DISCOVERING_SOURCES`
  (where #20 starts).
- **Change something** → the user writes what to change; the model revises
  the blueprint from the same research (a new version, no new searches
  unless it asks for them); or back to the conversation (`CLARIFYING`) to
  change the requirements.

### 5. Flow settings

The blueprint's `flow` block is what the knowledge system's flow will use:
brief, search country, domain, scope, supervisor instructions. This issue
generates and stores them (shown for review). Writing them into a flow
version happens when there is something to run: the candidate flow for
evaluation (#24), made live at go-live (#25). No flow changes here, so the
live flow is untouched.

### 6. API and tracing

- `POST /api/setup/blueprint` — start the research (from `DOMAIN_READY`,
  if no version is researching).
- `GET /api/setup` — gains the latest blueprint version and its status.
- `POST /api/setup/blueprint/revise` `{"feedback"}`, `POST
  /api/setup/blueprint/confirm`.
- Langfuse: one trace per research run (`blueprint_research`, tagged
  `source:setup`) with the query, search, fetch and writing steps.

## Files to touch

| File | Change |
|---|---|
| `app/initialization/blueprint.py` (new) | model, research, writing, check, revise, storage |
| `app/initialization/supervisor.py` | the view gains the blueprint; confirm moves to sources |
| `app/knowledge_system.py` | reset clears `app_domain_blueprints` |
| `app/web_api.py`, `app/auth.py` | endpoints |
| `web/static/js/setup.js`, CSS | the Blueprint step |
| `app/test_setup.py` | blueprint tests (stubbed search, fetch and model) |
| `app/test_blueprint_live.py` (new, opt-in) | live: "UK trains" and "flights" |
| README, PROGRESS.md | last phase |

## Tests

- Stubbed: two different domains (UK trains, flights) produce blueprints
  valid against the same schema; URLs not fetched are dropped and listed
  as unknowns; the flow settings fit their limits; research failing marks
  the version failed and setup can retry; revising makes a new version;
  confirming moves to `DISCOVERING_SOURCES` and records the version;
  a reset clears blueprints.
- Live (opt-in, `RUN_LIVE=1`): for UK trains, live departures come out
  dynamic and refund policies static; for flights, flight status dynamic
  and baggage rules static; both name a regulator from fetched pages.

## Phases

- [x] **1. Plan** — this file.
- [x] **2. Model, research and writing, storage**, with stubbed tests and
  a live check.
  - `app/initialization/blueprint.py`: `DomainBlueprint` (knowledge areas
    with class, example questions and evidence; entities; organisations
    with role, website and evidence; source requirements; metadata fields;
    flow settings; assumptions; unknowns) with lenient validators (an
    unknown class reads as static, roles and lists cleaned), `_normalise`
    for nested fields sent as JSON text; `plan_with_llm` (≤ 6 searches and
    the search country), `gather` (official sites first, then rank and
    score; errors and thin pages skipped; ≤ 10 pages), `write_with_llm`
    (also revises from feedback and the previous version), `check`
    (evidence must be fetched; an organisation with neither evidence nor a
    mention on a page is dropped and listed under unknowns; a website the
    pages do not show is cleared; flow settings cut to their limits),
    `generate` (traced as `blueprint_research`); storage
    `app_domain_blueprints` (one research at a time, finish, fail, latest,
    get, confirm only when ready); a reset clears it.
  - Live run 1 (UK rail, local, 64 s): good, but Delay Repay came out
    dynamic and the flow's search country empty. Fixed: the prompt says a
    rule or scheme *about* changing things is static; the plan's search
    country fills a blank one.
  - `app/test_blueprint_live.py` (opt-in, `RUN_LIVE=1`, stores nothing):
    **passed** for UK trains and flights (110 s): live departures / flight
    status dynamic; refunds and delay compensation / baggage static; ORR /
    CAA as regulators from fetched pages; search country united kingdom.
  - Tests: `Blueprint` in `test_setup.py` (two domains through the same
    schema, gather, versions, nested text fields); 25 in the file.
- [x] **3. Revise and confirm, background run, API**, with tests.
  - `app/initialization/blueprint_run.py`: `start()` (research in a
    thread of the web app from the confirmed requirements; one at a time),
    `revise(feedback)` (no new searches: the previous version's pages are
    fetched again — only their URLs are stored — and rewritten with the
    feedback and the previous blueprint), `confirm()` (the latest ready
    version: recorded on the knowledge system as `blueprint_version`,
    `DOMAIN_READY → DISCOVERING_SOURCES`, a closing turn in the
    conversation), `back_to_conversation()` (`→ CLARIFYING`; confirming
    the requirements again starts a new blueprint). A version still
    researching after 15 minutes (the web app restarted) is marked failed
    (`blueprint.expire_stale`) so setup can start again.
  - Confirming the requirements (`supervisor.confirm`) starts the research
    at once; `view()` carries the latest version (status, blueprint,
    queries and pages, error, feedback).
  - API (`manage_settings`): `POST /api/setup/blueprint` (start again),
    `/blueprint/revise` (400 without feedback or over 2,000 characters),
    `/blueprint/confirm`, `/back`; 409 when the step is not open or
    nothing is ready.
  - Live, locally: research in the background, ready in 54 s (seven areas,
    delay compensation static, live departures dynamic); a revision
    ("Also cover Eurostar and cross-border trains…") from the same 10
    pages in 54 s added "Eurostar and Cross-Border Trains" and its scope.
  - Tests: `BlueprintStep` in `test_setup.py` (33 in the file); the rules
    in `test_auth.py`. Full suite 322 OK.
- [x] **4. The Blueprint step in the setup page**, checked in the browser
  locally.
  - `web/static/js/setup.js`: from `DOMAIN_READY` on, the Blueprint card
    replaces the chat (the conversation folds away below it): progress
    while researching or revising (the change quoted), polled every 4 s
    and stopped on leaving the page; a failure with **Try again**; the
    outline — name, purpose, audience, regions, language; knowledge areas
    grouped by what answers them (knowledge base, datasets, live tools)
    with description, example questions and their pages as links;
    organisations with a role badge and website; folded: how the
    assistant will be set up (brief, scope, routing, domain, search
    country, entity types, metadata), assumptions and unknowns, the
    research (searches, pages read); **Looks right**, **Change
    something** (a box, then **Revise**), **Change the requirements**.
  - Browser, locally (live models and search): version 2 shown; "Trainline
    is a ticket retailer, not a train operator." revised it to version 3
    (polled, about a minute; Trainline now `other`); **Looks right** moved
    to the Sources step; no console errors.
- [x] **5. Docs, PR.** README (guided setup gains the Domain Blueprint and
  its live check; Langfuse gains `blueprint_research`; the project layout
  gains `blueprint.py`, `blueprint_run.py`). Deploy after the merge: EC2
  stays `READY`, so setup is not exercised there yet.

## Open questions for the user

1. **Web search locally.** The research needs Tavily, and the local `.env`
   has no key. Recommended: add the key to the local `.env` yourself
   (`TAVILY_API_KEY=`; it is in SSM as `/rag-systems/prod/tavily-api-key`),
   so I can check research live before merging. Alternatively the live
   checks run on EC2 after merging, as for #10.
2. **Flow settings.** Recommended: generate and store them in the blueprint
   now, and write them into a flow version only when there is something to
   run (#24, #25), so the live flow is untouched until go-live.
3. **Research budget.** Recommended: at most 6 searches and 10 pages per
   blueprint (about 2–3 pence of Tavily and model calls with Haiku), and no
   new searches on a revision unless the change needs them.

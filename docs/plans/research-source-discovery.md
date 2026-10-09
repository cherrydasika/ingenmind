# Plan: research agent → source discovery and profiling

Status: **Done (2026-10-08).** Phases 1–4 merged and deployed (`b0b7b3e`);
the live test passes on EC2; phase 5 (docs) on branch `research/docs`.
Follow-ups are in [the backlog](../BACKLOG.md), "Research: data sources". The user
took every recommendation below (2026-10-07) and wants the live test run on
EC2. Update this file
at the end of every step: tick what is done, note what was found, and say
what comes next.

GitHub issue: #10, "Research agent: find and profile data sources (APIs,
feeds, pricing) and recommend how to ingest them". The user's full prompt
(pasted 2026-10-07) is the reference for anything not covered here.

Working agreement: stop at the end of each phase, summarise, and get the
user's confirmation before the next. Work on a branch and open a pull
request; the user merges (see AGENTS.md). Commit and push only when asked.

## Goal

Today a knowledge gap sends the research agent to find 1–3 web pages, which
are validated and ingested into `research_chunks`. That suits static facts
("is there a pantry car?"), not **data that changes** ("where can I get live
train times?"): a stored page about live data is stale at once. For those
gaps the research agent should find **data sources** (APIs, push feeds, bulk
downloads), profile each from pages it fetched, rank them against the
assistant's brief, and recommend how to integrate the best one. Data-source
reports are saved for an admin; nothing is registered, ingested or coded
automatically.

The bar (a test expectation, never hard-coded): for "live UK train
departures" the report includes at least one Darwin source, marks the
National Rail website as not recommended, and recommends an API-based
method (an `api_tools` tool on the LDB web service).

## How the code is today (what shapes the design)

- **The knowledge-base subflow** (`app/flows/default_flow.json`,
  `subflows.knowledge_base`): `retrieval_agent → evidence_evaluator →`
  (`KNOWLEDGE_GAP`) `research_agent → source_validator →` (`accepted`)
  `ingest_sources → retrieval_agent`, or (`none` / `nothing`)
  `research_report → finish_task`. Research runs once per task.
- **Every routed outcome needs exactly one edge** (`flows/compiler.py`,
  validation). Adding an outcome to an existing component (for example a
  `DATA_SOURCE_GAP` from the evidence evaluator) would make **every saved
  flow invalid**, including the live `travel_assistant` v3 on EC2, which
  lives in the EC2 database, not in git.
- **The evidence evaluator** (`app/evidence.py`) already makes one forced
  tool call per attempt (`LlmAssessment`), with `failure_type` and
  `missing_information`. A `KNOWLEDGE_GAP` routes to research.
- **The research agent** (`ROLES["research"]`, `_research_tool`,
  `_research_agent` in `app/agent.py`) has three tools (`web_search`,
  `fetch_source`, `submit_candidates`) and a prompt budget of 3 searches and
  4 fetches. Its turns are capped by the run's limit (`max_turns`, default
  6; also sent to AgentCore as `maxIterations`).
- **Forced-tool pattern**: `research.assess_with_llm` (`SourceAssessment`),
  inside `observation(...)` for Langfuse, normalised by `structured.py`.
- **Events**: `run.emit({"type": "research", "step": …})`; the public view
  (`_public_progress`) maps steps to workflow nodes and keeps only numbers
  and fixed labels (`_safe_details`). The full result's `research` list
  (validation, ingestion, web searches) reaches the flow playground.
- **App tables** (`app_sessions`, `app_agent_memory`, `app_users`…) live in
  the same Postgres database as the chunks (`config.PGDATABASE`), each
  module creating its own tables (`CREATE TABLE IF NOT EXISTS`).
- **Live tools** are `ApiTool`s in `app/api_tools/` (weather, Swiss
  transport, entry requirements), the natural target of a recommendation.

## Design

### 1. Gap type: one field on the evidence evaluator (no extra call)

Add `gap_type: Literal["static_content", "data_source"] = "static_content"`
to `LlmAssessment` and `EvidenceEvaluation`, with a prompt sentence: set
`data_source` when the missing information is live, frequently changing or
structured data (departures, delays, platforms, prices that change,
availability, positions), or the task asks where to get such data. The
evaluator already runs on every task, so classification costs nothing
extra; old assessments default to `static_content`.

### 2. Keep the graph: the data-source path runs inside the research steps

No new outcomes, so every saved flow keeps working and gets the feature:

- `research_agent` reads `evaluation["gap_type"]`. For `static_content` it
  does exactly what it does now. For `data_source` it runs the
  **data-source playbook** (below) and ends with `submit_data_sources`
  instead of `submit_candidates`.
- `source_validator`, on a data-source gap, runs **profile → probe →
  judge** instead of page validation, saves the report, and returns `none`
  (nothing to ingest), so the existing edge leads to `research_report`.
- `research_report` writes the data-source summary for the supervisor (see
  open question 3).

The steps show as `research` events (`classify`, `profile`, `judge`) in the
workflow view. Separate flow-editor nodes are left for later (open
question 1).

### 3. Data-source playbook (research agent)

A second prompt for the research role, chosen per gap type (the static
prompt is untouched). It searches like an engineer: `<subject> <country>
API`, `open data`, `developer portal`, `data feed`, `<provider> pricing`,
`rate limits`, `terms`, and GitHub client libraries; when a consumer website
shows the data, it follows the trail upstream. The brief's country rule
stays. It ends with `submit_data_sources`: 1–4 candidates, each
`{name, provider, urls: [fetched pages that describe it]}`.

Budget, only on this path: searches, fetches and turns come from a new
`ResearchAgentConfig` on the `research_agent` node (saved flows get the
defaults): `data_source_searches` (6), `data_source_fetches` (10),
`data_source_turns` (10, the app's cap on turns, sent as that run's
`maxIterations`),
`max_profiles` (4), `profile_max_age_days` (30). Read through
`_with_settings`, like the other nodes' settings.

### 4. `SourceProfile` (forced tool, `app/research.py`)

```
SourceProfile: name, provider,
  access_method: rest_api | soap_api | push_feed | bulk_download | web_page | scrape_only
  auth: none | api_key | registration | oauth | contract | unknown
  pricing, rate_limits, freshness (real-time | minutes | daily | static | unknown),
  format, coverage, licence_and_terms,
  docs_url, signup_url, spec_url, evidence_urls: list[str],
  confidence: high | medium | low, unknowns: list[str]
```

One call for all candidates (at most 4), each with its fetched pages'
text (capped per page, as `assess_with_llm` does). The prompt says: only
what the pages say; otherwise the field is `unknown` and named in
`unknowns`. **Then a deterministic check**: every URL field and every
`evidence_urls` entry must be a page that was fetched (or appear in a
fetched page's text); otherwise it is cleared and added to `unknowns`, and
confidence drops to `low` if the access method or pricing is unverified.
Facts are short strings; no page text is copied into the profile.

### 5. Light probing

Where a profile has a `spec_url` or `docs_url` that was not fetched yet, the
profiler fetches it with `research.fetch_source` (read-only GET through
`scraping.fetch_bytes`) and confirms endpoint, auth and format before
profiling. No sign-ups, credentials or authenticated calls.

### 6. Judge and recommendation (forced tool, sees only the profiles)

`SourceRecommendation`: `ranking` (name + one-line reason each),
`recommended`, `fallback`, `method` (`api_tool` | `feed_consumer` |
`bulk_ingest` | `page_ingest` | `none`), and a method sketch:

- `api_tool`: proposed `app/api_tools` module name, description, input
  schema and endpoint (an `ApiTool` like `weather.py`); nothing embedded
- `feed_consumer`: a long-running consumer into a store, then an
  `api_tools` tool reading it; AWS shape noted (ECS/Fargate, Kinesis or
  SQS), not built
- `bulk_ingest`: scheduled ingestion through `common.ingest`
- `page_ingest`: today's path
- `scrape_only`: flagged with the terms that forbid or allow it

Criteria against the brief: freshness needed, cost, licence risk,
engineering effort, reliability. **Deterministic rule after the judge**: a
`scrape_only` source, or one whose terms forbid scraping or reuse, is never
`recommended` or `fallback` (the next eligible one moves up, or `none`).

### 7. Persistence and reuse

Two tables in `config.PGDATABASE`, created by a new module
`app/source_profiles.py` (same pattern as `sessions.py`):

```
research_source_profiles  provider, name (unique together), profile jsonb,
                          topic tsvector (name, provider, coverage, gap),
                          verified_at, created_at
research_source_reports   id, created_at, flow_id, flow_version, task, gap,
                          brief, profiles jsonb, recommendation jsonb,
                          trace_id
```

On a data-source gap, before searching: a full-text lookup of known
profiles for the gap (and the brief's country), at most `max_profiles`.
Fresh ones (`verified_at` within `profile_max_age_days`) are handed to the
research agent as known sources, so it searches only for what is missing;
stale ones have their pricing and docs pages re-fetched and are re-profiled.
Profiles are upserted after each report.

### 8. Events, tracing, UI

- Events: `research` steps `classify` (from the evaluation), `profile`,
  `probe`, `judge`, with counts only in public progress (`_safe_details`).
- Langfuse: `observation(as_type="generation", name="source_profiler" |
  "source_judge")` around each call; `gap_type` on the evidence evaluator's
  generation output.
- The full result's `research` record gains `gap_type`, `profiles` and
  `recommendation` (the flow playground shows them).
- A **Data source reports** card on the Ingestion tab
  (`GET /api/source-reports`, `view_knowledge`): newest first, with the
  ranking, recommendation, method and each profile's facts, unknowns and
  evidence links. Delete with `manage_knowledge`, like research pages.
- `web/flows-app`: the research agent node's new settings appear through
  its JSON-schema form; no new node types (see open question 1).

## Files to touch

| File | Change |
|---|---|
| `app/evidence.py` | `gap_type` on `LlmAssessment` / `EvidenceEvaluation`; prompt sentence |
| `app/research.py` | `SourceProfile`, `profile_sources()`, URL verification, `SourceRecommendation`, `judge_sources()`, scrape-only rule |
| `app/source_profiles.py` (new) | tables, upsert, lookup, reports |
| `app/agent.py` | research role prompt per gap type, `submit_data_sources` tool, budgets, data-source branch in `_research_agent` / `_source_validator` / `_research_report`, events, result fields |
| `app/flows/registry.py` | `ResearchAgentConfig` on `research_agent` (applied settings) |
| `app/web_api.py`, `app/auth.py` | `/api/source-reports` (+ delete), permission rules |
| `web/static/js/ingestion.js`, `api.js` | Data source reports card |
| `web/static/js/flow.js` | step labels for classify / profile / judge |
| `app/test_research.py`, `app/test_evidence.py`, `app/test_flows.py`, `app/test_pg_store.py` | tests below |
| README, PROGRESS.md, `docs/BACKLOG.md` | phase 5 |

## Tests

- Stubbed search, fetch and assessors (as `test_research.py` does):
  a live-data gap goes down the data-source path and a static gap down
  today's path (with today's validator and ingestion untouched); a profile
  field without a fetched source moves to `unknowns`; a `scrape_only`
  source with restrictive terms is never recommended; a fresh stored
  profile is reused without re-profiling and a stale one is re-verified;
  saved flows without the new settings still validate and run.
- Opt-in live test (skipped unless `RUN_LIVE=1`, a Tavily key and a
  configured chat model): "live UK train departures" → a Darwin source,
  the National Rail website not recommended, an API-based method.
- The full test command from AGENTS.md before each checkpoint.

## Phases

- [x] **1. Plan** — this file. Answers: graph shape inside the research
  steps; `gap_type` on the evidence evaluator; the user is told live data
  is not available (option a), naming sources is a backlog item; reports
  viewed with `view_knowledge`, deleted with `manage_knowledge`; the live
  test runs on EC2.
- [x] **2. Gap type, playbook, `SourceProfile`, profiling and probing**,
  with tests; no UI. Built:
  - `evidence.py`: `gap_type` on `LlmAssessment` (unknown values →
    `static_content`) and `EvidenceEvaluation` (`None` when no model call
    ran, i.e. no evidence).
  - `research.py`: `classify_gap()` (only for an unjudged gap; a failure
    falls back to today's path), `SourceProfile` (`unknown` added to the
    access method, auth and freshness vocabularies), `profile_with_llm()`,
    `verify_profile()` (links must be fetched pages or appear on one;
    evidence must be fetched pages; unstated facts listed in `unknowns`;
    low confidence without evidence, access method or pricing),
    `profile_sources()` (at most `max_profiles`; probes the spec or docs
    link a profile names only if a fetched page contains it; a second
    profiling call only for probed candidates).
  - `agent.py`: role `research_data` (playbook prompt, `web_search`,
    `fetch_source`, `submit_data_sources`; shares the research role's LLM
    settings); the search and fetch budget is enforced in `_research_tool`
    and its turn limit sent as `maxIterations`; `_research_agent`,
    `_source_validator` and `_research_report` branch on `gap_type`; the
    report tells the supervisor live data is not available and names no
    source. Events: `classify`, `agent`, `candidates`, `probe`, `profile`.
  - `flows/registry.py`: `ResearchAgentConfig` on `research_agent`
    (searches 6, fetches 10, turns 12, profiles 4, profile age 30 days).
  - Tests: `test_research.py` (`SourceProfiling`, `DataSourceFlow`,
    `DataSourceBudget`), `test_evidence.py` (`GapType`); full suite 268 OK.
  - Not yet: the judge, persistence (the report is only in the run's
    result and its Langfuse trace), the UI, the live test (needs the judge;
    phase 3, run on EC2).
- [x] **3. Judge, recommendation, persistence and reuse**, with tests.
  - `research.py`: `SourceRecommendation` (ranking, recommended,
    fallback, method, `Integration` sketch, not_recommended, rationale),
    `judge_with_llm()` (sees only profiles and the brief),
    `judge_sources()`: a scrape-only source or one whose terms forbid
    scraping or automated access (`FORBIDS_SCRAPING`) is never recommended
    or the fallback and is listed under not_recommended; the method follows
    the recommended source's access method; no call without profiles.
  - `source_profiles.py` (new): `research_source_profiles` (key provider +
    name, generated `tsvector` over name, provider, coverage and the gaps
    it was found for) and `research_source_reports`; `upsert`, `find`
    (OR full-text match, `fresh` by `profile_max_age_days`),
    `save_report`, `list_reports`, `delete_reports`.
  - `agent.py`: before searching, known profiles are looked up; fresh ones
    are reused (named to the research agent so it looks for others; enough
    of them skip the search); stale ones are fetched again from their own
    pages and re-profiled with the new candidates. Then judge, upsert the
    newly profiled sources, save the report. A store failure is recorded
    (`store_error`) and the answer still goes out. Events: `known`,
    `judge`.
  - `data_source_turns` capped at 10 (the app's turn cap; AgentCore's own
    limit is not documented here).
  - Tests: `SourceJudging`, more `DataSourceFlow` (reuse, skip, stale,
    store failure) in `test_research.py`; the store against Postgres in
    `test_pg_store.py`; `test_research_live.py` (opt-in, skipped without
    `RUN_LIVE=1`; stores nothing unless `LIVE_SAVE=1`). Full suite 278 OK.
  - Merged early (PR #12, `6a53bad`) and deployed to EC2 for the live
    test. **Live run 1** (2026-10-07): Darwin Data Feeds recommended
    (official, free tier of 5M requests per 4 weeks, OGL), fallback
    Realtime Trains API, method `api_tool` with a tool sketch; Rail Data
    Marketplace and Network Rail open data profiled with low confidence;
    links not on fetched pages were removed. **Failed** the bar: the
    National Rail website was never profiled. Run 2 reused run 1's
    AgentCore session (same session ID) and did not search at all.
  - Fixes (PR "Research: live test fixes"): a new session per live run;
    the playbook always includes the best-known public website or app for
    the data in the brief's country, with its terms page (no country or
    site named in the prompt); `unknowns` no longer repeats a field the
    model already listed. Merged as PR #13 (`7680af5`), deployed.
  - **Live run 3** (2026-10-07, `7680af5`): **passed** in 51 s. Six
    searches (open data portal, National Rail developer API, Realtime
    Trains API and terms, National Rail terms of use). Darwin Data Feeds
    recommended (push feed, free, OGL; high confidence), fallback RTT
    Next-Generation API (free non-commercial, £4/month Hobbyist), Network
    Rail open data (low confidence), and the National Rail Live Trains
    Board not recommended. Method `feed_consumer`: the profiler merged
    Darwin's push feed and its SOAP departure API into one profile, so the
    method followed the push feed; the issue's ideal was an `api_tool` on
    the LDB web service. The website's terms stayed "unknown" although the
    agent searched for them. Both addressed in phase 4.
- [x] **4. Events, tracing, UI** (Ingestion card, workflow steps, flow
  settings).
  - Public progress maps the research steps `classify`, `known`,
    `profile`, `probe`, `judge` to workflow nodes (`r_classify`…), with
    counts and fixed labels only; the workflow view (`flow.js`) shows "Live
    data: looking for data sources", how many were profiled, and the
    recommended method.
  - `GET /api/source-reports` (`view_knowledge`) and
    `POST /api/source-reports/remove` (`manage_knowledge`).
  - Ingestion tab: a **Data source reports** card
    (`web/static/js/source_reports.js`): each report folds open to the
    rationale, recommendation and fallback, the integration sketch, a
    profile table (access, pricing and limits, freshness, terms,
    confidence, links, unknowns) and the sources not to use; remove with an
    inline confirmation. Checked in the browser locally with a fixture
    report (shown, expanded, removed; no console errors).
  - Flow builder: the research agent's settings come from its schema;
    `profile_max_age_days` is now listed as applied.
  - Tracing was already in place: `gap_classifier`, `source_profiler`,
    `source_judge` generations, and `gap_type` in the evidence
    evaluator's output.
  - Playbook, from live run 3: each API or feed of a provider is its own
    candidate; the website's terms page goes in that candidate's urls;
    pages profiled per candidate raised from 3 to 4.
  - Tests: public-progress mapping (`test_guardrails.py`), report
    endpoints (`test_pg_store.py`), permission rules (`test_auth.py`).
  - Merged as PR #14 (`ee472df`), deployed. **Live run 4** (2026-10-08):
    passed in 42 s, but the playbook fixes did not take: Darwin was still
    one push-feed profile (the agent searched for OpenLDBWS but did not
    submit it separately), the websites' terms stayed unknown (one page
    each), and the judge claimed "scraping would violate terms of
    service" for websites whose terms were unknown.
  - Fixes in code (branch `research/split-sources-and-terms`): the
    profiler may give up to 3 profiles per candidate, one per API or feed
    of a provider; a not-recommended reason that claims something about a
    source's terms is rewritten to "terms of use not verified" when its
    profile does not state them (`_grounded_reason`), and the judge's
    prompt says so too. Fetching a website's terms page needs the links
    the text extraction drops: backlog. Merged as PR #15 (`b0b7b3e`).
  - **Live run 5** (2026-10-08, `b0b7b3e`): passed in 43 s. Darwin XML
    Push Feeds and Darwin SOAP APIs profiled separately (both high
    confidence); recommended the push feed (`feed_consumer`), fallback the
    SOAP API; Realtime Trains and Network Rail open data ranked (low
    confidence); National Rail Live Departures not recommended: "Web page
    only; no documented API or automated access method; terms of use not
    verified". Preferring the SOAP API for lookups is a backlog item.
- [x] **5. Docs**: README ("Data-source research" under the multi-agent
  flow, and the new Langfuse generations), PROGRESS.md, AGENTS.md, and
  four backlog items: create a tool stub from a report, prefer an
  on-demand API for lookups, read a website's terms page, tell users the
  recommended source.

## Open questions for the user

1. **Graph shape.** Recommended: keep the data-source path inside the
   existing research steps (above), so the live v3 and every saved flow get
   it without a new version. The alternative, separate `gap_classifier`,
   `source_profiler` and `source_judge` nodes in the flow editor, needs a
   new evidence outcome, which invalidates every saved flow until it is
   migrated (a compiler change for optional outcomes, or a flow migration).
2. **Classification.** Recommended: a `gap_type` field on the evidence
   evaluator (no extra model call). The alternative is a separate small
   classifier call in `research.py` on knowledge gaps only.
3. **What the user is told.** When a question needs live data the
   knowledge base cannot hold, should the answer (a) say only that live
   data is not available here yet and that the gap was recorded (the report
   stays admin-only, as the prompt says), or (b) also name the recommended
   official source with its link? (b) answers "where can I find live train
   times?" directly, but those names do not come from knowledge-base
   chunks, so the answer evaluator would need to accept them as research
   evidence. Recommended: (a) now, (b) as a backlog item.
4. **Who sees reports.** Recommended: view with `view_knowledge`, delete
   with `manage_knowledge`, on the Ingestion tab.
5. **The live test** needs a working chat model locally, and `.env` has no
   `LLM_PROVIDER` (backlog: "Local model calls fail"). Run it on EC2
   instead, or fix the local setup first?

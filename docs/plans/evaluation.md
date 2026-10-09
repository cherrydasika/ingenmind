# Plan: evaluation set from the blueprint, run per knowledge area

Status: **Phase 3 done (the run and per-area metrics) — waiting for the
user's check before phase 4** (the Evaluation part of the setup page).
Branch `init/evaluation`, draft PR #41. Update this file at the end of
every step: tick what is done, note what was found, say what comes next.

GitHub: issue #16, part of epic #19 (RAG Initialization Agent); builds on
#11 (the blueprint) and #14 (the build: setup is then at `EVALUATING`).
Done before #15 at the user's choice (2026-10-09): #16 → #17 is the
shortest path to a setup-built system going live. Working agreement: phases
with a stop at each; a branch and a pull request; the user merges
(AGENTS.md).

## Goal

After the build, setup writes an **evaluation set from the blueprint** —
questions for every knowledge area, each with the answer and the page it
expects, plus questions that must be refused, sent to a live tool, or
answered "not covered" — runs it on the **candidate flow** (the flow the
blueprint describes), and reports the results **per knowledge area**. A
knowledge system is not "ready" because ingestion finished; #17 turns these
results into the readiness report and **Go live**.

## How the code is today

- **Eval sets and runs** (`app/flows/evals.py`, the Evaluations page):
  sets of `{question, expected, expect_blocked}` (at most 50), runs of a
  set × 1–3 flow versions, one question at a time on a background thread;
  per question the answer, `expectation_met` (blocked when expected;
  otherwise not blocked and not failing the answer check), the answer
  evaluator's pass, overall and scores (correctness, faithfulness,
  completeness, citation quality), `answer_type` (`answer`,
  `clarification`, `not_available`, `conversational`), blocks, time,
  tokens; a summary per run. The user edits sets on the Evaluations page.
- **What an answer reports** (`agent.answer`): `on_metrics` gets numbers
  and labels only; the result's `sources` are the **cited** pages; the
  retrieved pages are only inside the run (`internal["searches"]`).
- **The blueprint's flow settings** (#11): brief, domain, scope, supervisor
  instructions, search country — **not yet applied to any flow**. The live
  flow on EC2 is `travel_assistant` v3; locally the built-in.
- **Knowledge areas** have a class: `STATIC_KNOWLEDGE` and
  `STRUCTURED_DATA` come from sources; `DYNAMIC_KNOWLEDGE` and
  `EXTERNAL_TOOL_API` are answered by live tools (#12).
- **The build** (#14): plan pages carry their areas; chunks carry
  `plan_version`, `areas`, `source_url`.

## Design

### 1. The candidate flow

When the evaluation is prepared, setup makes the flow the blueprint
describes: a copy of the live flow (else the built-in) with the
blueprint's brief, domain, supervisor instructions, search country and
scope applied, saved and **published as a version but not made live**
(id `setup_<domain>`; a later preparation publishes a new version). #17's
**Go live** promotes it with the existing flow promotion.

### 2. The dataset builder (Evaluation role)

One evaluation set per install (`setup_evaluation`), regenerated on
request, editable like any set:

| Kind | From | Expectation (in code) |
|---|---|---|
| `answer` | each sourced area with ingested pages: up to 4 of its pages' chunks; one forced-tool call per area writes questions, each with the expected answer, the **expected page** and a short **supporting quote** | not blocked, the answer check passes; also recorded: the expected page **retrieved** and **cited** |
| `not_covered` | each sourced area **without** ingested pages: one question from its description | not blocked, and the answer does not claim facts (`answer_type` `not_available`, or the check finds no unsupported claims) |
| `live` | each live-tool area: one question | a live tool was called, or `not_available` |
| `out_of_scope` | the blueprint's scope: two questions it excludes | blocked by a guardrail |

- **Grounding checked in code**: a generated `answer` question is kept only
  if its expected page is one of the pages given and its supporting quote
  appears in that page's stored text (normalised whitespace and case);
  otherwise it is dropped and listed. No model is trusted to say a question
  is answerable.
- Size: `QUESTIONS_PER_AREA` (3) for `answer`, within the 50-question
  limit (fewer per area when the blueprint has many areas).
- Questions carry `area`, `kind`, `expected_source`; `clean_questions`
  keeps them; the Evaluations page shows and edits them.

### 3. The run

- **Run evaluation** on the setup page queues the set against the
  candidate flow through the existing runner (`flows.evals.start`).
- The runner records per question, besides today's numbers, the pages
  **retrieved** and **cited** (URLs only, never text): `agent.answer`
  passes them to `on_metrics`; two new columns on `agent_eval_results`.
  Whether a live tool was called comes the same way.
- `expectation_met` follows the kind (table above).

### 4. Metrics per knowledge area (saved with the run's summary)

For each area, from its questions — each a defined count or mean, shown
with its definition on the page:

- **retrieval relevance**: answer questions whose expected page was
  retrieved ÷ answer questions
- **answer correctness**: mean of the evaluator's correctness score
- **groundedness**: mean of the evaluator's faithfulness score
- **citation accuracy**: answer questions citing the expected page ÷
  answer questions (and the evaluator's citation-quality mean)
- **coverage**: the area has ingested pages, and its answer questions
  whose expectation was met ÷ its answer questions
- **refusals done right**: `not_covered`, `live` and `out_of_scope`
  questions whose expectation was met ÷ those questions

`summary["areas"]` holds them; the summary of a set without areas is as
today.

### 5. States, page and API

- From `EVALUATING`: **Prepare the evaluation** (the candidate flow and the
  set, in the background — one model call per area), then the set is
  shown (by area and kind, dropped questions listed, a link to edit it on
  the Evaluations page) with **Run evaluation** and the estimated cost
  (questions × a full answer each). While it runs: progress; then the
  results per area and each question's outcome. The state stays
  `EVALUATING`; #17 adds readiness and Go live. Going back to the content
  stays possible.
- API (`manage_settings`): `POST /api/setup/evaluation/prepare`, `GET
  /api/setup/evaluation`, `POST /api/setup/evaluation/run`.
- Langfuse: `evaluation_set` (the builder) with a `question_writer`
  generation per area.

## Phases

- [x] **1. Plan** — this file. Stop for the user's answers.
- [x] **2. The candidate flow and the dataset builder** (kinds, grounding
  check, storage as an eval set), tests (a built knowledge base gets
  questions for every static area plus failure cases — stubbed) and a live
  run on the local build. Stop.
  - `app/initialization/evaluation.py`: `candidate_flow`, `build_set`,
    `prepare` (background, one at a time), `ensure_prepared`, `view`;
    table `app_setup_evaluations` (emptied by a reset). `flows.evals`
    keeps `kind` (one of `KINDS`), `area`, `expected_source` on questions.
    API: `GET /api/setup/evaluation`, `POST /api/setup/evaluation/prepare`
    (`manage_settings`); `GET /api/setup` prepares it by itself once per
    approved plan when setup is at `EVALUATING`; the setup view has
    `evaluation`.
  - Changed from the design while building: `not_covered` and `live`
    questions are the area's first example question (no model call); the
    answer questions per area share the room left under the 50 limit
    (ceil of room ÷ areas left, at most 3); the grounding check compares
    **words only, in order** (case, whitespace and punctuation ignored) —
    the first live run dropped 3 of 13 good questions because the model
    joined a bulleted list's items with ";" and ","; a second question from
    the **same page and quote** is dropped (areas share pages: the live run
    had near-duplicates); dropped entries keep the page and quote; the
    scope writer is told not to use what the scope sends to live tools
    (it wrote a live-departures question, which is not a refusal).
  - Tests: 11 in `test_setup.Evaluation` (questions for every area and the
    failure cases, the candidate flow's settings and that it is not live,
    it compiles, the grounding check, once per plan, not before the build,
    failures listed or failing the preparation, the 50 limit, one question
    per passage, question fields, the API). Full suite: 376 pass.
  - Live on the local build (plan 4, 49 chunks, Claude via Anthropic):
    25 s, 16 questions — 10 answer (4 areas, 2–3 each), 3 not covered
    (accessibility, station facilities, Eurostar), 1 live, 2 out of scope;
    2 dropped as the same passage. Candidate flow
    `setup_uk_train_information` (v1–v4 locally from these runs, none
    live; the live pointer is still the built-in).
- [x] **3. The run and per-area metrics** (retrieved and cited pages from
  the runner, expectations by kind, `summary["areas"]`), tests and a live
  run of the local set. Stop.
  - `agent._drive` adds `retrieved`, `cited` (page URLs only) and
    `live_tools` (tool names) to a run's metrics. `agent_eval_results`
    gains `kind`, `area`, `retrieved`, `cited`, `live_tools`,
    `expected_retrieved` and `expected_cited`, frozen at run time.
    `flows.evals.expectation()` judges by kind, and an `answer` question
    must get an answer: a "not available" with its page at hand is a miss.
    `summarise_areas()` holds the six metrics (definitions in its
    docstring) in `summary["areas"]`. `evaluation.run()` and
    `POST /api/setup/evaluation/run` queue the set against the candidate
    flow, one run at a time. The view shows the latest run and an estimate
    (30 s per question).
  - **Evaluations never ingest** (`agent.READ_ONLY_SOURCES`, source
    `eval`). The live run's research agent added 5 unreviewed pages
    (Eurostar, King's Cross, station lists) to `research_chunks`, and the
    "Eurostar: not covered" question then got a cited answer. The user's
    rule (2026-10-09): nothing goes into the database without their
    approval or rejection, done in the front end. For now research during
    users' questions keeps ingesting as before, and the 5 local pages stay.
  - The candidate flow empties the guardrail's block, clarify and output
    messages, so they name the blueprint's domain. The live run refused
    with the old flow's "UK trains and the weather".
  - Live run on the local build, before these fixes: 16 questions in 7 min
    (p50 20 s), 779k tokens (about 49k per question). The 4 answer areas
    had retrieval relevance 1.0, correctness 0.97–1.0, groundedness
    0.95–1.0 and citation accuracy 0.67–1.0. Refunds' miss was "not
    available" with the expected page retrieved, now counted as a miss.
    The 3 not-covered areas said "not available" or were answered from the
    researched pages, which no longer happens. The **live-departures
    question was blocked**: the blueprint's scope lists live train times as
    out of scope and there is no live UK train tool. That is a real gap,
    for #17's readiness report. Both out-of-scope questions were blocked.
  - Tests: 382 pass (new: expectations by kind, per-area summary, a setup
    run end to end, one run at a time, the agent's page metrics, an
    evaluation run that never ingests).
- [ ] **4. The Evaluation part of the setup page**, and the new fields on
  the Evaluations page, checked in the browser. Stop.
- [ ] **5. Docs, PR, deploy.**

## The user's answers (2026-10-09): the recommendations, all four

1. **Questions per area**: 3 (recommended; a local run of about 25
   questions takes roughly 10–15 minutes and some model spend, each
   question being a full answer). More gives steadier numbers but costs
   more.
2. **When it runs**: prepare the set automatically when the build
   finishes, and run it when you choose **Run evaluation** after reviewing
   it (recommended); or run it straight away too?
3. **The candidate flow**: a copy of the live flow with the blueprint's
   settings, published as a version but not live until Go live
   (recommended); or evaluate the live flow as it is?
4. **Grounding check**: drop generated questions whose supporting quote is
   not found in the expected page (recommended), or keep them marked
   unchecked?

# Plan: evaluation set from the blueprint, run per knowledge area

Status: **Phase 1 — plan written; waiting for the user's answers** (open
questions at the end). Branch `init/evaluation`. Update this file at the
end of every step: tick what is done, note what was found, say what comes
next.

GitHub: issue #24, part of epic #27 (RAG Initialization Agent); builds on
#19 (the blueprint) and #22 (the build: setup is then at `EVALUATING`).
Done before #23 at the user's choice (2026-10-09): #24 → #25 is the
shortest path to a setup-built system going live. Working agreement: phases
with a stop at each; a branch and a pull request; the user merges
(AGENTS.md).

## Goal

After the build, setup writes an **evaluation set from the blueprint** —
questions for every knowledge area, each with the answer and the page it
expects, plus questions that must be refused, sent to a live tool, or
answered "not covered" — runs it on the **candidate flow** (the flow the
blueprint describes), and reports the results **per knowledge area**. A
knowledge system is not "ready" because ingestion finished; #25 turns these
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
- **The blueprint's flow settings** (#19): brief, domain, scope, supervisor
  instructions, search country — **not yet applied to any flow**. The live
  flow on EC2 is `travel_assistant` v3; locally the built-in.
- **Knowledge areas** have a class: `STATIC_KNOWLEDGE` and
  `STRUCTURED_DATA` come from sources; `DYNAMIC_KNOWLEDGE` and
  `EXTERNAL_TOOL_API` are answered by live tools (#20).
- **The build** (#22): plan pages carry their areas; chunks carry
  `plan_version`, `areas`, `source_url`.

## Design

### 1. The candidate flow

When the evaluation is prepared, setup makes the flow the blueprint
describes: a copy of the live flow (else the built-in) with the
blueprint's brief, domain, supervisor instructions, search country and
scope applied, saved and **published as a version but not made live**
(id `setup_<domain>`; a later preparation publishes a new version). #25's
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
  `EVALUATING`; #25 adds readiness and Go live. Going back to the content
  stays possible.
- API (`manage_settings`): `POST /api/setup/evaluation/prepare`, `GET
  /api/setup/evaluation`, `POST /api/setup/evaluation/run`.
- Langfuse: `evaluation_set` (the builder) with a `question_writer`
  generation per area.

## Phases

- [x] **1. Plan** — this file. Stop for the user's answers.
- [ ] **2. The candidate flow and the dataset builder** (kinds, grounding
  check, storage as an eval set), tests (a built knowledge base gets
  questions for every static area plus failure cases — stubbed) and a live
  run on the local build. Stop.
- [ ] **3. The run and per-area metrics** (retrieved and cited pages from
  the runner, expectations by kind, `summary["areas"]`), tests and a live
  run of the local set. Stop.
- [ ] **4. The Evaluation part of the setup page**, and the new fields on
  the Evaluations page, checked in the browser. Stop.
- [ ] **5. Docs, PR, deploy.**

## Open questions for the user

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
